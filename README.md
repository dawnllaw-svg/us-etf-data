# us-etf-data

美股 ETF（SPY/QQQ/IWM/EFA/EEM/TLT/GLD/SHY）复权日线数据，由 GitHub Actions 每个交易日
美股收盘后（UTC 22:30）自动更新，数据源 Yahoo Finance（auto_adjust，含分红复权）。

- `us_etf_daily.csv` — 数据文件，1999 年至今
- `download.py` — 下载脚本
- `.github/workflows/update.yml` — 定时任务配置

本仓库仅存放公开市场数据，供量化策略研究使用。
