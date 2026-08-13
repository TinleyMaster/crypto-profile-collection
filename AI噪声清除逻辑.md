当前 AI 噪声清理（B4 任务）的完整逻辑如下。核心脚本是 phase_b2_ai_noise_clean_by_asset.py ，AI 判断在 llm_client.py 。

## 一、目标与数据范围
清理 biz.doc_source_entry 表中， discovered_from LIKE 'deep_crawl:%' 且 entity_type='asset' 的链接。核心字段是 ai_noise_checked_at （ NULL =未检查，非空=已检查）。

总原则：宁可保留，不可误删 。判定为噪声是 硬删除 （ DELETE ），相关则是 标记已检查 （ UPDATE ai_noise_checked_at = NOW() ）。

## 二、执行流程（分三阶段 + AI）
整个 main() 按顺序执行（单资产 --asset-id 与批量模式逻辑一致）：

### 阶段 1：规则直删（不经过 AI）
run_rule_delete 对 RULE_NOISE_DOMAINS 里已知的噪声域名直接 DELETE ：

- 非加密学术论文： arxiv 、 springer 、 neurips 、 researchgate 、 iacr 等
- 通用编程/技术文档： rubydoc 、 rubygems 、 nuget 、 packagist 、 docs.rs 、 postgresql 、 laravel 等
- 电商/企业/工具站： amazon 、 dropbox 、 digitalocean 、 manageengine 等
注意 ：审计公司 GitHub 仓库、社交平台（twitter/x.com、linkedin、t.me、reddit、medium 等） 已从规则直删列表移除 ，不再进入规则直删。

### 阶段 2：误判纠正（重置，重新评估）
两个重置动作，把 ai_noise_checked_at 重新置回 NULL ：

1. reset_ai_false_positives ：对「关联 >50 个资产」的域名（排除审计平台、github/gitlab/bitbucket），重置为未检查，让 AI 在资产上下文中重新评估——防止之前全局误判。
2. reset_dense_domains ：对「单资产下链接数 >100 且占比 >90%」的域名重置——这类通常是 应用类网站被误爬 （会计平台、无分页 Web 应用）。

### 阶段 3：按资产分组 + 分级 AI 判断（核心）
1. 取待处理资产 ： get_asset_domain_groups 找出有未检查链接的资产（含 symbol/name/description_short 基础资料），按未检查数降序取前 20 个，并把每个资产的未检查链接 按域名聚合 （domain / 条数 / entry_ids / 前 3 个样本 URL）。
2. 识别跨资产审计聚合域名 ： get_cross_asset_domains 找出 deep_crawl 中「关联 >50 个资产」的域名（排除社交平台与 github/gitlab/bitbucket），用于判定审计聚合链接。

对每个资产，按以下三级依次处理：

#### 3.1 社交平台域名：直接判过（不送 AI）
twitter/x.com、linkedin、t.me/telegram.me、reddit、medium、discord、facebook、instagram、youtube 等 SOCIAL_DOMAINS 直接 mark_checked 保留，不进入 AI 判断，避免误删团队/社区/公告类投研资料。

#### 3.2 审计聚合链接：按条交给 AI（发送代币基础资料）
filter_audit_links_with_ai 对「关联 >50 资产」的域名（或已知审计平台域名）下的每一条链接，调用 llm.judge_audit_links ，把代币基础资料（符号、名称、简介 description_short）与链接 URL 一并发给 AI：

- keep=true：仅保留 当前代币 自己的审计资料，mark_checked
- keep=false：其他项目/无关审计内容，直接 DELETE

#### 3.3 域名级 AI 判断：noise / relevant / uncertain
batch_check_asset_noise 把剩余（非社交、非审计聚合）域名一并发给 AI，AI 能看到该资产的 全部域名 一起判断，返回三态：

- decision=noise → delete_noise_ids 硬删除该域名下所有 entry_ids
- decision=relevant → mark_checked 标记已检查保留
- decision=uncertain → 进入 3.4 二次判断

#### 3.4 不确定链接：页面解析后二次判断
recheck_uncertain_links 对 AI 首次判断为 uncertain 的域名，逐条抓取页面正文（requests + BeautifulSoup 提取标题与正文），再调用 llm.judge_links_with_content 结合正文判断：

- noise=false → 保留（mark_checked）
- noise=true → 删除
- 页面抓取失败 → 保守保留（mark_checked）

## 三、AI 判断的具体标准（prompt）
### batch_check_asset_noise（域名级三态）
相关（decision=relevant，保留）：

1. 项目官方文档（白皮书、代币经济学、治理、路线图）
2. 审计报告和安全评估（即使来自第三方审计平台）
3. 社交平台链接（Twitter/X、Telegram、Discord、LinkedIn、Reddit、Medium）
4. 合作伙伴/生态页面
5. 加密行业通用平台（CoinGecko、CMC、DeFiLlama、Dune）
6. GitHub 仓库（含第三方审计仓库）

噪声（decision=noise，删除）：

1. 非加密学术论文（arxiv、springer、neurips…）
2. 通用编程/技术文档（npm、pip、nuget、postgresql、docker…）
3. 与该资产完全无关的其他项目文档
4. 电商/企业网站
5. 通用工具/聚合网站

不确定（decision=uncertain）：仅凭域名和样本 URL 无法判断时标记，程序抓取页面正文后再二次判断。

附加规则：

- ⚠️ 原则：宁可保留，不可误删；无法确定优先判 relevant，只有认为必须读正文才能确认时才判 uncertain。
- ⚠️ 密度预警：单资产下域名链接数 >100 且占比 >90%，极可能是应用误爬，判噪声。

### judge_audit_links（审计链接按条）
发送代币基础资料（符号/名称/简介），要求 AI 只保留「当前代币」的审计资料，其他项目一律删除；URL 中不含该代币标识且无法确认归属时判删除。

### judge_links_with_content（正文二次判断）
结合页面正文判断是否噪声，宁可保留不可误删，无法确定判 noise=false（保留）。

参数： temperature=0.1 ， max_tokens=4096 ，DeepSeek 禁用思考模式（噪声判断不需要深度推理）。

## 四、结果处理与容错
- decision=noise 的域名 → delete_noise_ids 硬删除。
- decision=relevant 的域名 → mark_checked 标记 ai_noise_checked_at = NOW() 。
- 审计 AI 返回 keep=false → 删除；keep=true → 保留。
- 二次判断 noise=true → 删除；noise=false → 保留。

容错机制（都偏向「保留」）：

- AI 调用异常 → 默认保留（相关/keep=true/noise=false）
- AI 返回 JSON 解析失败 → 默认保留
- 页面抓取失败 → 默认保留
- 只有 AI 明确返回删除信号才删除

## 五、自动循环触发
phase_b2_ai_noise_clean_by_asset_auto.py 通过 subprocess 反复调核心脚本 --execute --limit 20 ，最多 100 轮，当输出「处理资产: 0 个」时停止。

一句话总结 ：先按规则直删已知噪声域名 → 重置两类可能被误判的域名 → 对每个资产，社交平台直接判过、审计聚合链接按条发给 AI（带代币基础资料）保留本项目审计、其余域名按三态 AI 判断（noise 删 / relevant 留 / uncertain 抓页面正文二次判断），全程「宁可保留不可误删」。
