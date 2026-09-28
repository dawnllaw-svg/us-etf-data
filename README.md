# us-etf-data

美股 ETF（SPY/QQQ/IWM/EFA/EEM/TLT/GLD/SHY）复权日线数据，由 GitHub Actions 每个交易日
美股收盘后（UTC 22:30）自动更新，数据源 Yahoo Finance（auto_adjust，含分红复权）。

- `us_etf_daily.csv` — 数据文件，1999 年至今
- `download.py` — 下载脚本
- `.github/workflows/main.yml` — ETF定时任务配置

本仓库仅存放公开市场数据，供量化策略研究使用。

## 数据管道维护（2026-09）

| 数据 | 工作流 | 北京时间计划 |
|---|---|---|
| 美股及A股ETF | main.yml | 周二至周六06:30 |
| 可转债观察快照 | cn-convertible-data.yml | 周一至周五16:45 |
| A股历史月末名单、财务及现金流 | cn-stock-data.yml | 每月1–3日08:30 |
| 定期报告全文 | filings.yml | 5月2日、9月2日04:00 |

GitHub可能延迟启动。支持手工触发，互斥任务不会取消正在采集的数据。

发布采用带run ID的唯一版本、先上传草稿再公开，不删除旧版本。下载历史基线使用完整分页，验证大小和gzip完整性；失败即停止。A股基线路径仍为四个原始gzip文件，并附cn_stock_quality.json。

A股月末名单使用BaoStock对应历史交易日数据，禁止将今天实时行情标为上月月末。财务更新保留旧字段，补资产负债表、营收和公告日期。仅强制更新已过完整披露窗口的季度。财务接口返回最新重述版本，不等同历史每一时点的原始披露版本；使用者仍需PIT核验。行业基线暂保留，并在质量清单明确说明未刷新，不将当前行业回填成过去行业。

ETF生成逐品种quality.json，包含缺失、历史保留和最后有效日期；7个自然日是故障检查阈值，不是交易日历。可转债snap_date是采集观察日期，不能据此断定行情源也是当日成交价。公告不完整或可转债快照明显残缺时停止发布。

本仓库不含策略主程序。用户本地D:/quant/topdown_v3/quant.py是A股多因子核心；ETF多腿是附加层，完整配置与实现尚待定位，不能仅凭数据品种推断权重。

离线检查：`python -m unittest discover -s tests -v`。
