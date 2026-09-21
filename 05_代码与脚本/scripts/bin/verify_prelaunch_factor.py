"""验证 prelaunch 因子：催化剂发布前 24h 价格变化 vs 72h 超额收益。

假设：发布前已启动的催化剂（prelaunch_ret 高）→ 信息已被定价，追高风险大；
     发布前平稳的催化剂 → 信息未被定价，发布后才有反应空间。

但 deep_dive 发现 STRONG 组中位量比 0.87（发布前已启动）——需用数据验证方向。
"""
import sys
import math
from pathlib import Path
from collections import defaultdict

# kol / catalyst 包所在目录，兼容两种部署结构：
#   本地开发：<project>/workbench/  容器部署：/app/（Dockerfile 扁平拷贝）
# 容器内原写法 parent.parent.parent/"workbench" 指向不存在的 /app/workbench。
_SCRIPT_PATH = Path(__file__).resolve()
_WB_CANDIDATE = _SCRIPT_PATH.parent.parent.parent / "workbench"
WORKBENCH_DIR = (
    _WB_CANDIDATE
    if (_WB_CANDIDATE / "catalyst" / "__init__.py").exists()
    else _SCRIPT_PATH.parent.parent.parent
)
sys.path.insert(0, str(WORKBENCH_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from kol.db import get_conn


def main():
    with get_conn() as conn:
        # 1. 取 L1 精确 outcome（有 klines 覆盖的）
        rows = conn.execute("""
            SELECT o.catalyst_id, o.asset_id, o.excess_72h, o.base_time,
                   a.canonical_symbol AS symbol
            FROM biz.catalyst_outcome o
            JOIN biz.asset_catalyst ac ON ac.catalyst_id = o.catalyst_id
            JOIN core.asset a ON a.asset_id = o.asset_id
            WHERE o.ret_source = 'klines+market_daily' AND o.excess_72h IS NOT NULL
        """).fetchall()
        print(f"L1 精确 outcome 样本: {len(rows)}")

        # 2. 加载 klines 索引（symbol, ts -> close）
        # 用每个资产的全部 1h kline 一次性加载，bisect 定位
        symbols = sorted({r["symbol"] for r in rows})
        kline_cache = {}
        print(f"涉及资产: {len(symbols)} 个，加载 klines...")
        for sym in symbols:
            # asset_klines.symbol 为交易对格式（canonical_symbol + 'USDT'），
            # 与 collect_catalyst_outcome.load_asset_symbols 保持一致
            pair = sym + "USDT"
            ks = conn.execute("""
                SELECT open_time, close_px FROM biz.asset_klines
                WHERE symbol = %s AND interval = '1h' ORDER BY open_time
            """, (pair,)).fetchall()
            kline_cache[sym] = [(k["open_time"].timestamp(), float(k["close_px"])) for k in ks]
        print("klines 加载完成")

        # 3. 计算每个 outcome 的 prelaunch_ret_24h（发布前 24h 价格变化）
        import bisect
        results = []
        missing = 0
        for r in rows:
            ks = kline_cache.get(r["symbol"], [])
            if len(ks) < 2:
                missing += 1
                continue
            t0 = float(r["base_time"].timestamp())
            closes = [c for _, c in ks]
            ts_list = [t for t, _ in ks]
            # 找到 t0 及 t0-24h 的位置
            i0 = bisect.bisect_right(ts_list, t0) - 1
            i_24 = bisect.bisect_right(ts_list, t0 - 86400) - 1
            if i0 < 0 or i_24 < 0 or i0 >= len(closes) or i_24 >= len(closes):
                missing += 1
                continue
            p0 = closes[i0]
            p24 = closes[i_24]
            if p24 <= 0 or p0 <= 0:
                missing += 1
                continue
            prelaunch_ret = (p0 - p24) / p24 * 100.0
            # excess_72h 已是百分数（如 6.54 = +6.54%），无需再 *100
            ret72 = float(r["excess_72h"])
            results.append((prelaunch_ret, ret72))
        print(f"成功计算: {len(results)}，缺失/不足: {missing}")

        # 4. winsorize：72h 超额收益按 1%/99% 截尾，抑制极端值污染
        ret_vals = sorted(y for _, y in results)
        n = len(ret_vals)
        lo_cap = ret_vals[n // 100]
        hi_cap = ret_vals[(99 * n) // 100]
        wins = [min(max(y, lo_cap), hi_cap) for _, y in results]
        clipped = sum(1 for _, y in results if y < lo_cap or y > hi_cap)

        # 5. 分箱统计（用 winsorize 后的收益）
        bins = [(-1e9, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 20), (20, 1e9)]
        labels = ["<-10%", "-10~-5%", "-5~0%", "0~5%", "5~10%", "10~20%", ">20%"]
        bucket = defaultdict(list)
        for (pre, _), w in zip(results, wins):
            for (lo, hi), lb in zip(bins, labels):
                if lo <= pre < hi:
                    bucket[lb].append(w)
                    break

        print(f"\n=== prelaunch_ret_24h 分箱 vs 72h 超额收益（winsorize 1%/99%，截断 {clipped} 个） ===")
        print(f"{'发布前24h涨跌':<12}{'样本':>5}{'均值':>8}{'中位':>8}{'p25':>8}{'p75':>8}{'≥5%占比':>10}")
        for lb in labels:
            v = bucket[lb]
            if not v:
                continue
            nv = len(v)
            sv = sorted(v)
            avg = sum(v) / nv
            med = sv[nv // 2]
            p25 = sv[nv // 4]
            p75 = sv[(3 * nv) // 4]
            p_strong = sum(1 for x in v if x >= 5) / nv * 100
            print(f"{lb:<12}{nv:>5}{avg:>8.2f}{med:>8.2f}{p25:>8.2f}{p75:>8.2f}{p_strong:>9.1f}%")

        # 6. 相关性（winsorize 后）
        if len(results) > 10:
            import statistics
            pre_l = [x for x, _ in results]
            r72_l = list(wins)
            # Spearman 近似（rank 相关性）
            def _rank(vals):
                order = sorted(range(len(vals)), key=lambda i: vals[i])
                ranks = [0] * len(vals)
                for rank, idx in enumerate(order):
                    ranks[idx] = rank
                return ranks
            rp, rq = _rank(pre_l), _rank(r72_l)
            n = len(rp)
            mp = sum(rp) / n
            mq = sum(rq) / n
            num = sum((a - mp) * (b - mq) for a, b in zip(rp, rq))
            den = math.sqrt(sum((a - mp) ** 2 for a in rp) * sum((b - mq) ** 2 for b in rq))
            spearman = num / den if den else 0
            print(f"\nSpearman 相关 (prelaunch_ret vs ret72): {spearman:.4f}")


if __name__ == "__main__":
    main()
