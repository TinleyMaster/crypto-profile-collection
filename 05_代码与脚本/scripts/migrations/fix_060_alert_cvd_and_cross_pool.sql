-- fix_060: 盘面异动告警邮件审计（2026-09-21）剩余项的 schema 支撑
--
-- 对应审计 P1-3 / P1-5：
--   1) `cvd_usd` / `cvd_ratio`（P1-3）：主池 `_compute_l2` 原实现只把 CVD 窗口净额的
--      **符号**写进 `cvd_dir`，金额从未入库 ⇒ 渲染层不可能显示幅度，也无法区分
--      「杠杆推涨（OI↑ + 价↑ + 现货主动卖）」。生产者现按同窗口（OI_RISE_BARS 个
--      5m 桶）落净额与占成交额比。
--   2) `alert_suppressed_at` / `alert_suppressed_reason`（P1-5）：同币在 main 与
--      squeeze 两个通道各发一封、口径相反（实测 XMR 39min / 龙虾 41min）。
--      互斥窗口（60min）内只发先到的那封，后者标此列**留痕**：
--      `alerted_at` 必须保持 NULL（未发信），但不能只「跳过」—— 主池候选集按
--      `alerted_at IS NULL` 取，不留标记会在 20 分钟窗口内反复重试，随后又被
--      「丢信号检测」误计为异常（该检测已同步加 `alert_suppressed_at IS NULL`）。
--
-- 幂等：全部 ADD COLUMN IF NOT EXISTS，无数据回填、无破坏性操作。

ALTER TABLE biz.scan_signal
    ADD COLUMN IF NOT EXISTS cvd_usd                numeric(20, 2),
    ADD COLUMN IF NOT EXISTS cvd_ratio              numeric(12, 6),
    ADD COLUMN IF NOT EXISTS alert_suppressed_at    timestamptz,
    ADD COLUMN IF NOT EXISTS alert_suppressed_reason text;

COMMENT ON COLUMN biz.scan_signal.cvd_usd IS
    'CVD 窗口净额（USD，正=主动买占优）；与 cvd_dir 同源，仅供渲染幅度（审计 P1-3）';
COMMENT ON COLUMN biz.scan_signal.cvd_ratio IS
    'CVD 净额 / 同窗口成交额（约 -1~1）；NULL = 该窗口无成交额数据';
COMMENT ON COLUMN biz.scan_signal.alert_suppressed_at IS
    '跨池互斥抑制时刻（审计 P1-5）；非空 ⇒ 刻意不发信，且不计入「丢信号」检测';
COMMENT ON COLUMN biz.scan_signal.alert_suppressed_reason IS
    '跨池互斥抑制原因（如「跨池互斥：squeeze 池已在 60 分钟内告警」）';