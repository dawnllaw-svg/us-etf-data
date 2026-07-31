# -*- coding: utf-8 -*-
"""在 GitHub Actions 中运行：下载美股 ETF + A股 ETF 复权日线。
输出 us_etf_daily.csv 与 cn_etf_daily.csv。任何关键失败以非零退出码结束。"""
import sys
import time

import pandas as pd
import yfinance as yf

US_TICKERS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "SHY", "BIL"]

# A股 ETF：列名用 6 位代码，Yahoo 代码带交易所后缀
CN_TICKERS = {
    "510300": "510300.SS",  # 沪深300
    "510500": "510500.SS",  # 中证500
    "512100": "512100.SS",  # 中证1000
    "510880": "510880.SS",  # 红利
    "512010": "512010.SS",  # 医药卫生
    "159928": "159928.SZ",  # 消费
    "512800": "512800.SS",  # 银行
    "512880": "512880.SS",  # 证券公司
    "512660": "512660.SS",  # 军工
    "512480": "512480.SS",  # 半导体
    "512690": "512690.SS",  # 酒
    "512400": "512400.SS",  # 有色金属
    "518880": "518880.SS",  # 黄金
    "511010": "511010.SS",  # 国债
}


def fetch_with_retry(tickers, min_rows, label):
    last_err = None
    for attempt in range(3):
        try:
            df = yf.download(tickers, start="1999-01-01", auto_adjust=True, progress=False)["Close"]
            df = df.dropna(how="all")
            if len(df) < min_rows:
                raise ValueError(f"{label}: rows too few ({len(df)})")
            age = (pd.Timestamp.now("UTC").tz_localize(None) - df.index[-1]).days
            if age > 6:
                raise ValueError(f"{label}: data stale (last {df.index[-1].date()})")
            return df
        except Exception as e:
            last_err = e
            print(f"{label} attempt {attempt+1} failed: {e}", file=sys.stderr)
            time.sleep(60)
    raise SystemExit(f"{label}: all attempts failed: {last_err}")


us = fetch_with_retry(US_TICKERS, 5000, "US")[US_TICKERS]
us.round(4).to_csv("us_etf_daily.csv")
print(f"US OK: {len(us)} rows, ~{us.index[-1].date()}")

cn_raw = fetch_with_retry(list(CN_TICKERS.values()), 2000, "CN")
cn = cn_raw.rename(columns={v: k for k, v in CN_TICKERS.items()})
cn = cn[[c for c in CN_TICKERS if c in cn.columns]]
missing = [c for c in CN_TICKERS if c not in cn.columns or cn[c].dropna().empty]
if "510300" in missing:
    raise SystemExit("CN: core ticker 510300 missing")
if missing:
    print(f"CN warning: missing {missing}", file=sys.stderr)
cn.round(4).to_csv("cn_etf_daily.csv")
print(f"CN OK: {len(cn)} rows, {len(cn.columns)} tickers, ~{cn.index[-1].date()}")
