"""只读核验：C 桶内高频未知地址到底是「合约」还是「疑似漏标交易所热钱包」。

背景：onchain_verify.py ⑤b 频率启发式(窗口内 txn>=阈值)列出 transfer_log 两端都不命中
地址库的未知高频地址。其中大部分形态(前导 0x00…)是合约(路由/金库/token)，并非交易所。
本脚本对每个候选做两层识别：
  1. 本地 DB：是否已在 onchain_address_label（任意 label_type）/ core.asset（token 合约）
  2. Etherscan 全家桶 getsourcecode：有 ContractName => 已验证合约（非交易所热钱包）
       无 ContractName => EOA（可能是交易所热钱包，真候选，需人工核实）

仅 SELECT + GET 公共 API，不写库。

用法：
  DATABASE_URL=$DATABASE_URL ETHERSCAN_API_KEY=$ETHERSCAN_API_KEY \
    python 05_代码与脚本/scripts/bin/verify_unknown_exchange_candidates.py [--days 7] [--min-txn 20] [--limit 40]
"""
import argparse
import os
import sys
import time
import json

import psycopg2
import psycopg2.extras
import urllib.request
import urllib.parse

URL = os.environ.get("DATABASE_URL")
if not URL:
    raise SystemExit("DATABASE_URL 未设置")

ETH_KEY = os.environ.get("ETHERSCAN_API_KEY") or os.environ.get("BSCSCAN_API_KEY") or ""

# Etherscan 全家桶：同一 API key 通用
EXPLORER = {
    "eth": "https://api.etherscan.io",
    "bsc": "https://api.bscscan.com",
    "base": "https://api.basescan.org",
    "optimism": "https://api-optimistic.etherscan.io",
    "arbitrum": "https://api.arbiscan.io",
    "polygon": "https://api.polygonscan.com",
    "avalanche": "https://api.snowtrace.io",
}
NON_EVM = {"tron", "solana", "bitcoin", "ripple", "polkadot", "ton", "sui", "aptos"}


def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "onchain-verify/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_json(host, params):
    api = f"{host}/api?" + urllib.parse.urlencode(params)
    if ETH_KEY:
        api += f"&apikey={ETH_KEY}"
    return http_get_json(api)


def is_contract(chain, addr):
    """用 eth_getCode 确定性区分 EOA(返回 '0x') 与合约(返回字节码)。
    Etherscan 全家桶通用；比 getsourcecode 的 ContractName 更可靠（覆盖未验证合约）。"""
    host = EXPLORER.get(chain)
    if not host or not ETH_KEY:
        return None
    try:
        data = _get_json(host, {
            "module": "proxy", "action": "eth_getCode",
            "address": addr,
        })
        code = (data.get("result") or "").strip()
        if code and code != "0x":
            return True
        if code == "0x":
            return False
    except Exception as e:
        return f"<api_err:{e}>"
    return None


def get_contract_name(chain, addr):
    """仅对「已验证合约」返回 ContractName，用于给合约命名（非 EOA 判定依据）。"""
    host = EXPLORER.get(chain)
    if not host or not ETH_KEY:
        return None
    try:
        data = _get_json(host, {
            "module": "contract", "action": "getsourcecode",
            "address": addr,
        })
        res = (data.get("result") or [{}])
        if isinstance(res, list) and res:
            name = (res[0].get("ContractName") or "").strip()
            return name or None
    except Exception as e:
        return f"<api_err:{e}>"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-txn", type=int, default=20, dest="min_txn")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()
    DAYS = args.days

    with psycopg2.connect(URL, connect_timeout=30) as conn:
        conn.readonly = True
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # ① 取 C 桶高频未知地址候选
            cur.execute(f"""
                WITH exch AS (
                    SELECT lower(address) AS address, chain FROM biz.onchain_exchange_wallet
                    WHERE confidence='high'
                    UNION
                    SELECT lower(address), chain FROM biz.onchain_address_label
                    WHERE label_type='exchange' AND confidence IN ('high','medium')
                ),
                c AS (
                    SELECT tl.from_address AS addr, tl.chain, tl.value_usd
                    FROM biz.onchain_transfer_log tl
                    LEFT JOIN exch f ON f.address=lower(tl.from_address) AND f.chain=tl.chain
                    LEFT JOIN exch t ON t.address=lower(tl.to_address)   AND t.chain=tl.chain
                    WHERE tl.block_timestamp >= now() - interval '{DAYS} days'
                      AND tl.tx_hash NOT LIKE '0xtest%'
                      AND (tl.is_suspect IS NOT TRUE OR tl.is_suspect IS NULL)
                      AND f.address IS NULL AND t.address IS NULL
                    UNION ALL
                    SELECT tl.to_address, tl.chain, tl.value_usd
                    FROM biz.onchain_transfer_log tl
                    LEFT JOIN exch f ON f.address=lower(tl.from_address) AND f.chain=tl.chain
                    LEFT JOIN exch t ON t.address=lower(tl.to_address)   AND t.chain=tl.chain
                    WHERE tl.block_timestamp >= now() - interval '{DAYS} days'
                      AND tl.tx_hash NOT LIKE '0xtest%'
                      AND (tl.is_suspect IS NOT TRUE OR tl.is_suspect IS NULL)
                      AND f.address IS NULL AND t.address IS NULL
                ),
                freq AS (
                    SELECT addr, chain, count(*) AS txn, sum(value_usd) AS vol
                    FROM c WHERE addr IS NOT NULL
                    GROUP BY addr, chain
                )
                SELECT addr, chain, txn, vol
                FROM freq WHERE txn >= {args.min_txn}
                ORDER BY txn DESC, vol DESC
                LIMIT {args.limit}
            """)
            cands = cur.fetchall()

            print(f"=== C 桶高频未知地址核验（窗口 {DAYS}d, txn>={args.min_txn}, 取 {args.limit}）===")
            print(f"候选数: {len(cands)}  ETH_KEY={'有' if ETH_KEY else '无(仅本地DB)'}")

            contract_n = 0
            eoa_cand_n = 0
            already_labeled_n = 0
            unverifiable_n = 0
            api_err_n = 0

            for r in cands:
                addr = r["addr"]
                chain = r["chain"]
                # ② 本地 DB 是否已有标签（任意 label_type）
                cur.execute("""
                    SELECT label_type, label_name, confidence
                    FROM biz.onchain_address_label
                    WHERE lower(address)=lower(%s) AND chain=%s
                    LIMIT 1
                """, (addr, chain))
                loc = cur.fetchone()

                cname = None
                is_c = None
                if chain in EXPLORER:
                    is_c = is_contract(chain, addr)   # 确定性 EOA/合约
                    time.sleep(0.3)                   # 限频
                    if is_c is True:
                        cname = get_contract_name(chain, addr)  # 仅命名
                        time.sleep(0.3)
                elif chain in NON_EVM:
                    is_c = "non-evm"

                if loc:
                    tag = f"已标注[{loc['label_type']}/{loc['label_name']}/{loc['confidence']}]"
                    already_labeled_n += 1
                elif is_c is True:
                    tag = f"合约:{cname or '未验证合约'}"
                    contract_n += 1
                elif is_c is False:
                    # EOA 且无本地标签 = 真候选（交易所热钱包是 EOA）
                    tag = "⚠️ EOA-疑似交易所/做市商热钱包(真候选)"
                    eoa_cand_n += 1
                elif is_c == "non-evm":
                    tag = "非EVM-待人工"
                    unverifiable_n += 1
                else:  # api_err / None
                    tag = f"API异常:{is_c}" if str(is_c).startswith("<api_err") else "API未核验"
                    api_err_n += 1

                print(f"  {chain:<10} {str(addr)[:42]:<44} txn={r['txn']:>4} "
                      f"vol=${r['vol']:,.0f}  -> {tag}")

            print(f"\n=== 小结 ===")
            print(f"  合约(非交易所): {contract_n}")
            print(f"  已本地标注: {already_labeled_n}")
            print(f"  非EVM/待人工: {unverifiable_n}")
            print(f"  API异常未核验: {api_err_n}")
            print(f"  ⚠️ EOA 真候选(疑似漏标交易所/做市商): {eoa_cand_n}")
            if eoa_cand_n:
                print("  → 真候选为 EOA（交易所热钱包是 EOA），需人工/Arkham 核实后补库；"
                      "切勿无脑标 exchange（做市商/大户也是 EOA）")
            else:
                print("  → 无 EOA 真候选，C 桶主要为合约/已标注流量")

    print("\nCANDIDATE_VERIFY_DONE (只读)")


if __name__ == "__main__":
    main()
