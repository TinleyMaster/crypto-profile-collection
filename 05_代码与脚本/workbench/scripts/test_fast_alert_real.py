"""
催化剂快通道邮件真实数据测试。

功能：
1. 从生产库拉取最近 24h 内最近的一条 A 级 open 信号
2. 自动翻译催化剂标题/摘要为中文（写入 title_cn / ai_summary）
3. 调用 AI 做 A 级深度评审（写入 ai_deep_review）
4. 生成带 AI 深度评审的完整邮件预览 HTML
5. 打开浏览器查看效果

用法：
    cd workbench
    python scripts/test_fast_alert_real.py

可选参数：
    --signal-id <id>   指定信号 ID（不指定则取最新 A 级）
    --no-ai            跳过 AI 翻译和深度评审（仅看邮件模板效果）
    --open-browser     生成后自动打开浏览器
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser

# 确保 workbench 在路径
HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.path.dirname(HERE)
sys.path.insert(0, WB)

# crypto_research 包在 scripts/src 目录下，确保也加入路径
CR_SRC = os.path.normpath(os.path.join(WB, "..", "scripts", "src"))
if os.path.isdir(CR_SRC) and CR_SRC not in sys.path:
    sys.path.insert(0, CR_SRC)

from catalyst.notifier import (
    _build_fast_alert_html,
    _fetch_signal_row,
)


def get_conn():
    """获取数据库连接（用 catalyst 模块自带的连接管理器）。"""
    from catalyst.db import get_conn as catalyst_get_conn
    return catalyst_get_conn()  # 返回 context manager


def _run_migration(conn):
    """自动执行迁移（添加 title_cn 和 ai_deep_review 字段）。"""
    conn.execute("""
        ALTER TABLE biz.asset_catalyst
        ADD COLUMN IF NOT EXISTS title_cn VARCHAR(512);
    """)
    conn.execute("""
        ALTER TABLE biz.catalyst_signal
        ADD COLUMN IF NOT EXISTS ai_deep_review JSONB;
    """)


def find_latest_a_signal(conn) -> int | None:
    """查找最近一条 A 级 open 信号。"""
    row = conn.execute("""
        SELECT s.signal_id
        FROM biz.catalyst_signal s
        WHERE s.tier = 'A'
          AND s.status = 'open'
        ORDER BY s.created_at DESC
        LIMIT 1
    """).fetchone()
    return row["signal_id"] if row else None


def translate_and_review(conn, signal_id: int) -> dict:
    """翻译 + 深度评审，返回最新的信号行数据。"""
    row = _fetch_signal_row(conn, signal_id)
    if not row:
        print(f"[错误] 找不到信号 {signal_id}")
        sys.exit(1)

    print(f"信号: {row.get('symbol')} / {row.get('canonical_name')}")
    print(f"  原标题: {row.get('catalyst_title')[:80]}")
    print(f"  当前评分: {row.get('composite_score')}")

    # 翻译
    if not row.get("title_cn"):
        try:
            from catalyst.ai_enhance import CatalystTranslator
            translator = CatalystTranslator.from_settings()
            if translator:
                print("[翻译] 正在翻译催化剂标题和摘要...")
                result = translator.translate(
                    row.get("catalyst_title") or "",
                    row.get("catalyst_summary") or "",
                )
                if result and result.get("title_cn"):
                    conn.execute(
                        "UPDATE biz.asset_catalyst "
                        "SET title_cn = %s, ai_summary = COALESCE(%s, ai_summary), "
                        "    ai_processed = true, ai_processed_at = NOW() "
                        "WHERE catalyst_id = %s",
                        (
                            result["title_cn"],
                            result.get("summary_cn"),
                            row["catalyst_id"],
                        ),
                    )
                    conn.commit()
                    print(f"[翻译] 完成: {result['title_cn'][:60]}")
                else:
                    print("[翻译] AI 返回为空，跳过")
            else:
                print("[翻译] LLM 不可用，跳过翻译")
        except Exception as e:
            print(f"[翻译] 失败: {e}")
    else:
        print(f"[翻译] 已有中文标题: {row['title_cn'][:60]}")

    # 深度评审
    if not row.get("ai_deep_review"):
        try:
            from catalyst.ai_enhance import AISignalDeepReviewer
            reviewer = AISignalDeepReviewer.from_settings()
            if reviewer:
                # 重新取最新数据
                latest_row = _fetch_signal_row(conn, signal_id)
                print("[深度评审] 正在进行 AI 深度评审（约 30-60 秒）...")
                from catalyst.notifier import _signal_row_to_deep_review_input
                review_data = _signal_row_to_deep_review_input(latest_row)
                deep = reviewer.review(review_data)
                if deep:
                    import json
                    conn.execute(
                        "UPDATE biz.catalyst_signal "
                        "SET ai_deep_review = %s::jsonb, ai_reason = %s "
                        "WHERE signal_id = %s",
                        (json.dumps(deep, ensure_ascii=False),
                         deep.get("overall_review")[:500],
                         signal_id),
                    )
                    conn.commit()
                    print(f"[深度评审] 完成: {deep.get('verdict')} (信心度: {deep.get('confidence_level')})")
                else:
                    print("[深度评审] AI 返回为空，跳过")
            else:
                print("[深度评审] LLM 不可用，跳过深度评审")
        except Exception as e:
            print(f"[深度评审] 失败: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("[深度评审] 已有深度评审结果")

    # 返回最新行
    return _fetch_signal_row(conn, signal_id)


def main():
    parser = argparse.ArgumentParser(description="催化剂快通道邮件真实数据测试")
    parser.add_argument("--signal-id", type=int, help="指定信号 ID")
    parser.add_argument("--no-ai", action="store_true", help="跳过 AI 翻译和深度评审")
    parser.add_argument("--open-browser", action="store_true", help="生成后自动打开浏览器")
    args = parser.parse_args()

    print("=" * 60)
    print("催化剂快通道邮件测试")
    print("=" * 60)

    # 数据库连接（上下文管理器自动提交/关闭）
    print("\n[1/4] 连接数据库...")
    try:
        cm = get_conn()
    except Exception as e:
        print(f"[错误] 数据库连接失败: {e}")
        sys.exit(1)
    print("  ✓ 连接成功")

    with cm as conn:
        # 先执行迁移（如果字段不存在则加上）
        try:
            conn.execute("SELECT title_cn FROM biz.asset_catalyst LIMIT 0")
        except Exception:
            print("[迁移] 缺少 title_cn / ai_deep_review 字段，正在执行迁移...")
            _run_migration(conn)
            print("[迁移] 完成")

        # 找信号
        print("\n[2/4] 查找 A 级信号...")
        signal_id = args.signal_id
        if not signal_id:
            signal_id = find_latest_a_signal(conn)
            if not signal_id:
                print("[错误] 没有找到 A 级 open 信号")
                sys.exit(1)
        print(f"  信号 ID: {signal_id}")

        # AI 处理
        if args.no_ai:
            print("\n[3/4] 跳过 AI 处理（--no-ai）")
            row = _fetch_signal_row(conn, signal_id)
        else:
            print("\n[3/4] AI 翻译 + 深度评审...")
            row = translate_and_review(conn, signal_id)

        # 生成预览
        print("\n[4/4] 生成邮件预览...")
        html = _build_fast_alert_html(row)

        out_dir = os.path.join(WB, "scripts", "output")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"fast_alert_real_{signal_id}.html")

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)

        size = os.path.getsize(out_path)
        print(f"  ✓ 生成成功: {out_path}")
        print(f"  文件大小: {size:,} bytes")

        # 统计中英文
        import re
        text = re.sub(r'<[^>]+>', ' ', html)
        zh = len(re.findall(r'[\u4e00-\u9fff]', text))
        en = len(re.findall(r'[a-zA-Z]{3,}', text))
        total = zh + en
        ratio = zh / total * 100 if total > 0 else 0
        print(f"  中文字符: {zh}  |  英文单词: {en}  |  中文占比: {ratio:.1f}%")

    print("\n" + "=" * 60)
    print(f"预览文件: {out_path}")
    print("=" * 60)

    if args.open_browser:
        webbrowser.open(f"file:///{out_path}")
        print("已打开浏览器")


if __name__ == "__main__":
    main()
