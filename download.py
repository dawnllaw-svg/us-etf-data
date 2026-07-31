# -*- coding: utf-8 -*-
"""在 GitHub Actions 中运行：下载美股 + A股 ETF 复权日线。
v3（2026-07-31）：逐品种校验 + 与仓库现有数据合并——任何品种在本次下载中
缺失或异常时，保留该品种的旧列而不是提交空列（修复 QQQ 空列事故）。"""
import os
import sys
import time

import pandas as pd
import yfinance as yf

US_TICKERS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "SHY", "BIL"]
CN_TICKERS = {
    "510300": "510300.SS", "510500": "510500.SS", "512100": "512100.SS",
    "510880": "510880.SS", "512010": "512010.SS", "159928": "159928.SZ",
    "512800": "512800.SS", "512880": "512880.SS", "512660": "512660.SS",
    "512480": "512480.SS", "512690": "512690.SS", "512400": "512400.SS",
    "518880": "518880.SS", "511010": "511010.SS",
}


def fetch(tickers):
    last_err = None
    for attempt in range(3):
        try:
            df = yf.download(tickers, start="1999-01-01", auto_adjust=True, progress=False)["Close"]
            return df.dropna(how="all")
        except Exception as e:
            last_err = e
            print(f"attempt {attempt+1} failed: {e}", file=sys.stderr)
            time.sleep(60)
    raise SystemExit(f"download failed: {last_err}")


def merge_and_save(new, filename, min_rows=1000):
    """逐品种校验；异常品种保留仓库现有旧列。全列均无有效数据才失败。"""
    old = pd.read_csv(filename, index_col=0, parse_dates=True) if os.path.exists(filename) else None
    kept, degraded = {}, []
    for t in new.columns:
        n_new = new[t].dropna().shape[0]
        n_old = old[t].dropna().shape[0] if old is not None and t in old.columns else 0
        if n_new >= max(min_rows, int(n_old * 0.9)):
            kept[t] = new[t]
        elif n_old > 0:
            kept[t] = old[t]
            degraded.append(f"{t}(new={n_new}, kept old={n_old})")
        else:
            degraded.append(f"{t}(new={n_new}, NO fallback)")
    if not kept:
        raise SystemExit(f"{filename}: no valid columns at all")
    out = pd.DataFrame(kept).sort_index().dropna(how="all")
    out.round(4).to_csv(filename)
    status = f"{filename}: {len(out)} rows, {len(kept)} tickers, ~{out.dropna(how='all').index[-1].date()}"
    if degraded:
        status += f" | DEGRADED: {', '.join(degraded)}"
        print(f"::warning::{filename} degraded tickers: {', '.join(degraded)}")
    print(status)


us = fetch(US_TICKERS)
merge_and_save(us[[c for c in US_TICKERS if c in us.columns]], "us_etf_daily.csv", 1000)

cn_raw = fetch(list(CN_TICKERS.values()))
cn = cn_raw.rename(columns={v: k for k, v in CN_TICKERS.items()})
merge_and_save(cn[[c for c in CN_TICKERS if c in cn.columns]], "cn_etf_daily.csv", 800)
