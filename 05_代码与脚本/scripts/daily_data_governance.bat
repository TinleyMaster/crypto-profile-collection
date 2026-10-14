@echo off
rem ============================================================
rem 每日数据治理：清理 doc_source_entry 噪音 + 构建基本面因子
rem 由 Windows 计划任务 CryptoDailyGovernance 在工作日 10:00 调用
rem 日志：data\research\daily_governance.log
rem ============================================================
chcp 65001 >nul
cd /d "E:\瞎搞乱搞\web3\加密货币研究报告\05_代码与脚本\scripts"

echo [%date% %time%] ===== 每日数据治理开始 ===== >> "data\research\daily_governance.log"
echo --- clean_doc_source_entry --- >> "data\research\daily_governance.log"
C:\python\python311\python.exe bin\clean_doc_source_entry.py --apply --skip-backup >> "data\research\daily_governance.log" 2>&1
echo --- build_factors --- >> "data\research\daily_governance.log"
C:\python\python311\python.exe bin\build_factors.py --all --limit 500 >> "data\research\daily_governance.log" 2>&1
echo [%date% %time%] ===== 完成 ===== >> "data\research\daily_governance.log"
