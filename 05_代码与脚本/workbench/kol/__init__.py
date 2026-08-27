"""
KOL 信号监控模块。

子模块：
  - db.py        数据库操作（博主/帖子/信号的增删改查）
  - scraper.py   多平台抓取器（币安广场 / Twitter / Telegram）
  - classifier.py AI 信号分类与结构化提取
  - notifier.py  信号邮件提醒
  - asset_match.py 币种匹配（关联 core.asset）
  - routes.py    Flask Web 路由
  - runner.py    抓取 + 分类 + 提醒 的主流程
"""
