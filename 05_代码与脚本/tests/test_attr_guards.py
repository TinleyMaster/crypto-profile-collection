#!/usr/bin/env python3
"""P3-ATTR-001 · linker 归属护栏注入测试（回归用）。

对 `catalyst.pipeline._resolve_asset_ids` 注入**合成用例**，逐条验证 P3-C 引入的
护栏（错连 > NULL 铁律的机器化守卫）：

| 组 | 验证点 |
|---|---|
| A 商品同名 | `XAUUSDT` 行情稿必须**不**连；含加密语境的同符号稿必须连 |
| B 美股撞名 | 裸/单字符 ticker（SHY/V/C/M）在金融稿中必须**不**连 |
| C 强信号召回 | 有 quote 后缀 / 标题含符号者必须连 |
| D 门禁不破 | 交易所名、信源名、"代币化" 不得点亮歧义 gate |
| E 项目名通道 | 「标题项目方名 → 代币」是**弱信号**：泛词/地理词/名称不对应必须**不**连；
真项目名（完全同名 或 `X + 组织词`）必须连 |

用法：
    python tests/test_attr_guards.py          # 需可连 prod（只读）
退出码 0 = 全绿；1 = 有失败用例。

已知残余（P3-C-B3 记录，未纳入断言，避免用"未知"污染回归基线）：
  · `Cafe de Coral Group` → `CORAL`（"Coral" 是港式快餐品牌词，非项目名）
  · `Alibaba Group Holding` → `BABA`（rank=NULL 资产，经 `map_pairs_to_asset_ids`
    的 canonical_symbol 兜底路径进入 —— 该路径**不能**加 rank 约束，
    否则新上市无排名的真币（如 MARSCOIN）会全军覆没）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "workbench"))
sys.path.insert(0, str(ROOT / "scripts" / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from catalyst.db import get_conn  # noqa: E402
from catalyst.pipeline import _resolve_asset_ids  # noqa: E402
import catalyst.linker as linker  # noqa: E402

EMPTY = "empty"

# (pairs, title, body, expect)  expect=EMPTY 或 期望连到的 symbol 集合（子集判定）
CASES: list[tuple[list[str], str, str, object]] = [
    # ── A 商品同名（gate 必须挡住商品行情稿）──
    (["XAUUSDT"], "现货黄金刷新日高，突破 4,170 美元/盎司，日内涨幅达 1.6%", "", EMPTY),
    (["XAUUSDT"], "现货黄金在亚盘走高上涨 1%，现报 4146 美元/盎司", "", EMPTY),
    (["XAUUSDT", "CLUSDT", "BZUSDT"],
     "美国通胀数据意外走高，亚太市场下挫，韩国交易所被迫启动熔断机制", "", EMPTY),
    (["XAUUSDT", "COPPERUSDT", "BZUSDT"],
     "Oil prices fell in a volatile session, while London copper edged higher. Spot gold also declined.",
     "", EMPTY),
    (["XPDUSDT"], "现货钯金突破 1,300 美元/盎司，日内涨幅达 1.85%", "", EMPTY),
    (["XAUUSDT"], "XAU 代币已上线去中心化交易所，链上交易量激增", "", {"XAU"}),
    # ── B 美股撞名（裸/单字符 ticker 需标题佐证）──
    (["SHY"], "美国 2 年期国债收益率回落，最新报 4.818%", "", EMPTY),
    (["TLT", "SHY", "IEF"], "美国国债收益率上升后其投资吸引力增强", "", EMPTY),
    # ⚠️ 边界面：若标题**真的写了** `GM` 独立 token，本规则会放行（漏连 vs 错连的
    #    取舍代价）。prod 实测 cid 13967 的标题不含独立 `GM`，故被正确拦下。
    (["HYUNDAIUSDT", "F", "GM"], "Cox Automotive 预测 Hyundai 销量下滑，通用汽车亦承压", "", EMPTY),
    (["HTZ"], "Creditors to Hertz Global Holdings Inc. are organizing across two groups", "", EMPTY),
    (["ACN"], "Anthropic 已选定埃森哲作为首家“嵌入式评估方”", "", EMPTY),
    (["VUSDT"], "Visa 公布的新数据显示，该公司稳定币关联卡交易量上升", "", EMPTY),
    (["CUSDT"], "花旗代币服务正在拓展至日本和阿联酋", "", EMPTY),
    (["MUSDT"], "Macy's reported second-quarter net sales of $4.87 billion", "", EMPTY),
    (["RTX"], "RTX has teamed up with fusion-energy startup Xcimer", "", EMPTY),
    (["B3"], "巴西证券交易所 B3 预计推出代币化资产平台", "", EMPTY),
    (["ZCSHUSDT", "SNPSUSDT"],
     "Bybit 将于 10 月 13 日新增新思科技（SNPS USDT）、Zcash ETF（ZCSH USDT）股票永续合约",
     "", EMPTY),
    # ── C 强信号召回（不能因护栏误杀真币）──
    (["CTUSDT"], "币安将于 2026 年 10 月 01 日上线 CTUSDT 永续合约", "", {"CT"}),
    (["CT"], "币安将上线 CTUSDT 永续合约", "", {"CT"}),
    ([], "火星财经消息，OKX 将于 9 月 30 日上线 CT/USDT 现货交易。CT 存币将于同日开放提币。", "", {"CT"}),
    (["CT"], "Concrete 发推表示，已向符合条件的用户开放 CT 代币申领", "", {"CT"}),
    (["CASHCAT"], "UPBIT 将于 9 月 28 日 17:00 上线 Cash Cat (CASHCAT)", "", {"CASHCAT"}),
    (["PUFFER"], "据 Gate 行情数据显示，或受 Upbit 上线 PUFFER 韩元交易影响", "", {"PUFFER"}),
    (["AEON"], "AI 经济结算协议 AEON 宣布新增智利本地银行转账支付", "", {"AEON"}),
    (["NEAR"], "Foresight News 消息，Bitwise 宣布美国首只现货 NEAR ETP 上线", "", {"NEAR"}),
    (["WUSDT"], "BlockBeats 消息，据 HTX 行情数据，W 现报 0.0181 美元，24 小时跌幅 30.45%", "", {"W"}),
    (["WUSDT"], "Wormhole (W) is trading at $0.02842, up 61.39% over the past 24 hours", "", {"W"}),
    (["GUSDT"], "火星财经消息，Gravity 代币 G 突破 0.0085 美元，现报 0.00856 美元", "", {"G"}),
    (["QUSDT"], "Quack AI (Q) fell to an intraday low of 0.02 USDT", "", {"Q"}),
    (["OUSDT"], "PANews 消息，Binance Wallet 宣布上线 o1.exchange (O) Alpha 交易竞赛", "", {"O"}),
    (["SOLUSDT"], "Forward Industries increased its SOL holdings by nearly $100M", "", {"SOL"}),
    # ── D 门禁不破（交易所名/信源名/代币化 不得点亮 gate）──
    (["SNPSUSDT"], "Bitget 已上线 SNPS（Synopsys）股票永续合约", "", EMPTY),
    (["SNPSUSDT"], "Foresight News 消息，据 Cointelegraph 报道，Synopsys 发布财报", "", EMPTY),
    (["SNPSUSDT"], "SNPS 代币已上线链上交易所并开放质押", "", {"SNPS"}),
    # ── E 项目名通道（弱信号：泛词/地理词/名称不对应 必须拦）──
    # 泛词（地理/国别）—— 实测 cid 149/870
    ([], "Millennium Management is suing a Dymon Asia Capital employee in Hong Kong court", "", EMPTY),
    ([], "Japan Finance Minister Satsuki Katayama said the government has maintained the stance", "", EMPTY),
    # 泛词（产品/描述）—— 实测 cid 58/81/218
    ([], "Binance Liquid Swap Upgrades Pool Prices WebSocket", "", EMPTY),
    ([], "SimpleSwap said its fixed-rate exchanges have run inside the Exodus wallet for over a year", "", EMPTY),
    # 符号对应过松（symbol 命中但资产名不以该词开头）—— 实测 cid 1111/1396
    ([], "SlowMist said it detected two attackers on the BSC chain copying the method used", "", EMPTY),
    ([], "Ant Group's Afu app has reached 150 million users, with daily consultations near", "", EMPTY),
    # 名称完全不对应（既非同名、也非「词+组织词」）—— 实测 cid 490/1608
    ([], "UBS Group AG completed its debut Chinese domestic bond sale this week", "", EMPTY),
    ([], "According to Jin10, the OpenAI Foundation invested $60 million in artificial intelligence", "", EMPTY),
    # 真项目名必须放行：完全同名 / 候选词+组织词
    ([], "RWA tokenization issuance and management platform Realio Network was attacked on September 20", "", {"RIO"}),
    ([], "TRON network Super Representatives have launched a vote on mainnet Committee Proposal", "", {"TRX"}),
    ([], "Liqi Digital Assets and XDC Network have renewed their partnership with a target of", "", {"XDC"}),
    ([], "Spark said it will close its SparkLend lending protocol on Gnosis Chain on September 30", "", {"GNO"}),
]


class _It:
    __slots__ = ("title", "body_text", "related_pairs")

    def __init__(self, t, b, p):
        self.title, self.body_text, self.related_pairs = t, b, p


def main() -> int:
    passed = failed = 0
    fails: list[str] = []
    with get_conn() as conn:
        cur = conn.cursor()
        for i, (pairs, title, body, expect) in enumerate(CASES, 1):
            # 清缓存，避免跨用例污染（模块级缓存）
            linker._symbol_asset_cache.clear()
            linker._empty_source_hints.clear()
            aid = list(_resolve_asset_ids(_It(title, body, list(pairs)), conn, "trading_pairs"))
            got: set[str] = set()
            if aid:
                cur.execute("select canonical_symbol from core.asset where asset_id = any(%s)", (aid,))
                got = {r["canonical_symbol"] for r in cur.fetchall()}
            if expect == EMPTY:
                ok = not got
                want = "∅"
            else:
                ok = bool(set(expect) & got) if got else False
                want = "/".join(sorted(expect))  # type: ignore[arg-type]
            if ok:
                passed += 1
                print(f"  ✅ #{i:<2} pairs={','.join(pairs) or '-':<22} 期望={want:<12} 实得={sorted(got) or '∅'}")
            else:
                failed += 1
                msg = f"#{i} pairs={pairs} 期望={want} 实得={sorted(got)} | {title[:60]}"
                fails.append(msg)
                print(f"  ❌ {msg}")
    print("=" * 88)
    print(f"注入测试：{passed} 通过 / {failed} 失败 / 共 {len(CASES)}")
    if fails:
        print("失败明细：")
        for m in fails:
            print("   -", m)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
