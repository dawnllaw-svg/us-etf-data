# -*- coding: utf-8 -*-
"""在 GitHub Actions 中运行：下载 8 只美股 ETF 的复权日线，覆盖写入 us_etf_daily.csv。
任何异常都以非零退出码结束，让 Action 显示失败。"""
import sys
import time

import pandas as pd
import yfinance as yf

TICKERS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "SHY"]

last_err = None
for attempt in range(3):
    try:
        df = yf.download(TICKERS, start="1999-01-01", auto_adjust=True, progress=False)["Close"]
        df = df[TICKERS].dropna(how="all")
        if len(df) < 5000:
            raise ValueError(f"rows too few: {len(df)}")
        age_days = (pd.Timestamp.utcnow().tz_localize(None) - df.index[-1]).days
        if age_days > 6:
            raise ValueError(f"data stale: last date {df.index[-1].date()}")
        df.round(4).to_csv("us_etf_daily.csv")
        print(f"OK: {len(df)} rows, {df.index[0].date()} ~ {df.index[-1].date()}")
        sys.exit(0)
    except Exception as e:
        last_err = e
        print(f"attempt {attempt+1} failed: {e}", file=sys.stderr)
        time.sleep(60)

print(f"all attempts failed: {last_err}", file=sys.stderr)
sys.exit(1)
