# attr_gold 待人工复核清单（P3-B 半自动定标残项）

> 共 3 条。这些行**不参与评测**（`label=null`），
> 需人工确认后填 `label`（`[]`/`[asset_id...]`/`["__non_crypto__"]`）并把 `needs_review` 置 false。

| cid | bucket | pairs | 判读备注 | title |
|---|---|---|---|---|
| 16322 | crypto_gap | OPENAIUSDT,OUSDT,AEON | 人工判读存疑: 正文缺失（title=null），无法判读 | null |
| 29337 | unknown_mixed | XDP | 人工判读存疑: Bithumb 上线 XDP 韩元交易对；资产存在（id=26496 Doppler Finance）但 market_cap_rank=NULL，严格解析失败 | BlockBeats 消息，10 月 9 日，据官方公告，Bithumb 将上线 XDP 韩元交易对。 |
| 30301 | crypto_gap | CUSDT,GSBUSDT,JPMBUSDT | 人工判读存疑: 正文缺失（'null 概述'），无法判读 | null 概述 |
