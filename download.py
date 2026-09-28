# -*- coding: utf-8 -*-
"""在 GitHub Actions 中运行：下载美股 + A股 ETF 复权日线。
v5（2026-08-16）：新增货币ETF 511990/511880/511660，供两条 A股腿的防守端兜底规则使用。
v4（2026-07-31）：A股宇宙扩充至 ~40 只（随机池对照实验用）。
保留 v3 的逐品种校验 + 与仓库现有数据列级合并（异常品种保留旧列）。
新增品种若 Yahoo 无数据会在日志中警告并跳过，属预期行为。"""
import os
import sys
import time
import json

import pandas as pd

US_TICKERS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "SHY", "BIL"]

CN_TICKERS = {
    # ---- 现有生产池（勿删） ----
    "510300": "510300.SS", "510500": "510500.SS", "512100": "512100.SS",
    "510880": "510880.SS", "512010": "512010.SS", "159928": "159928.SZ",
    "512800": "512800.SS", "512880": "512880.SS", "512660": "512660.SS",
    "512480": "512480.SS", "512690": "512690.SS", "512400": "512400.SS",
    "518880": "518880.SS", "511010": "511010.SS",
    # ---- 防守端兜底：货币ETF（2026-08-16 新增，系统层批判 P1） ----
    # 用途：511010 自身 252 日动量 ≤0 时，两条 A股腿的防守仓位改持货币ETF。
    # 与美股腿 SHY→BIL 同一条已在役规则。三只按 config.CN_CASH / CN_CASH_ALT 顺序取用。
    # 注意：货币ETF 净值近似恒定、日收益量级 0.005%，若 Yahoo 返回平直序列，
    # 下游按"0% carry 现金"处理——这是已验证过的保守下界，规则依然生效。
    "511990": "511990.SS",  # 华宝添益（主）
    "511880": "511880.SS",  # 银华日利（备1）
    "511660": "511660.SS",  # 建信添益（备2）
    # ---- 宽基/风格扩充 ----
    "510050": "510050.SS",  # 上证50
    "510180": "510180.SS",  # 上证180
    "159901": "159901.SZ",  # 深证100
    "159915": "159915.SZ",  # 创业板
    "159949": "159949.SZ",  # 创业板50
    "588000": "588000.SS",  # 科创50
    "159905": "159905.SZ",  # 深红利
    # ---- 行业/主题扩充 ----
    "512170": "512170.SS",  # 医疗
    "159938": "159938.SZ",  # 医药卫生(广发)
    "159996": "159996.SZ",  # 家电
    "512000": "512000.SS",  # 券商
    "510230": "510230.SS",  # 金融
    "512710": "512710.SS",  # 军工龙头
    "159995": "159995.SZ",  # 芯片
    "515050": "515050.SS",  # 5G通信
    "159939": "159939.SZ",  # 信息技术
    "512720": "512720.SS",  # 计算机
    "515000": "515000.SS",  # 科技龙头
    "515030": "515030.SS",  # 新能源车
    "515790": "515790.SS",  # 光伏
    "516160": "516160.SS",  # 新能源
    "515220": "515220.SS",  # 煤炭
    "159930": "159930.SZ",  # 能源
    "512200": "512200.SS",  # 房地产
    "512980": "512980.SS",  # 传媒
    "159825": "159825.SZ",  # 农业
    "510410": "510410.SS",  # 资源
    "512580": "512580.SS",  # 环保
}


def fetch(tickers):
    import yfinance as yf
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


def merge_and_save(new, filename, min_rows=1000, expected=None, today=None):
    """逐品种校验；异常品种保留仓库现有旧列；全新品种不足 min_rows 则跳过并警告。"""
    old = pd.read_csv(filename, index_col=0, parse_dates=True) if os.path.exists(filename) else None
    kept, degraded = {}, []
    expected = list(expected if expected is not None else new.columns)
    tickers = list(dict.fromkeys(expected + (list(old.columns) if old is not None else [])))
    for t in tickers:
        series = new[t] if t in new.columns else pd.Series(dtype=float)
        n_new = series.dropna().shape[0]
        n_old = old[t].dropna().shape[0] if old is not None and t in old.columns else 0
        if n_new >= max(min_rows, int(n_old * 0.9)):
            kept[t] = series
        elif n_old > 0:
            kept[t] = old[t]
            degraded.append(f"{t}(new={n_new}, kept old={n_old})")
        else:
            degraded.append(f"{t}(new={n_new}, skipped)")
    if not kept:
        raise SystemExit(f"{filename}: no valid columns at all")
    out = pd.DataFrame(kept).sort_index().dropna(how="all")
    if out.index.has_duplicates:
        raise ValueError(f'{filename}: duplicate dates')
    today = pd.Timestamp(today or pd.Timestamp.now(tz='UTC').date())
    columns = {}
    for ticker in expected:
        values = out[ticker].dropna() if ticker in out else pd.Series(dtype=float)
        latest = values.index.max() if len(values) else None
        lag = (today.normalize() - pd.Timestamp(latest).normalize()).days if latest is not None else None
        columns[ticker] = {'latest_date': str(pd.Timestamp(latest).date()) if latest is not None else None,
                           'age_calendar_days': lag, 'rows': len(values)}
        if lag is None or lag > 7:
            degraded.append(f'{ticker}(missing or older than 7 calendar days)')
    quality = {'checked_date': str(today.date()), 'columns': columns, 'warnings': degraded}
    with open(filename.replace('.csv', '_quality.json'), 'w', encoding='utf-8') as f:
        json.dump(quality, f, ensure_ascii=False, indent=2)
    temporary = filename + '.tmp'
    out.round(4).to_csv(temporary)
    os.replace(temporary, filename)
    status = f"{filename}: {len(out)} rows, {len(kept)} tickers, ~{out.dropna(how='all').index[-1].date()}"
    if degraded:
        status += f" | DEGRADED: {', '.join(degraded)}"
        print(f"::warning::{filename} degraded tickers: {', '.join(degraded)}")
    print(status)
    return quality


def main():
    us = fetch(US_TICKERS)
    uq = merge_and_save(us, 'us_etf_daily.csv', 1000, expected=US_TICKERS)
    cn_raw = fetch(list(CN_TICKERS.values()))
    cn = cn_raw.rename(columns={v: k for k, v in CN_TICKERS.items()})
    cq = merge_and_save(cn, 'cn_etf_daily.csv', 800, expected=list(CN_TICKERS))
    # Optional ETF gaps are explicit warnings; complete market failure is fatal.
    for quality in (uq, cq):
        if not any(c['age_calendar_days'] is not None and 0 <= c['age_calendar_days'] <= 7
                   for c in quality['columns'].values()):
            raise SystemExit('No recent market data; refusing to publish stale data as current')


if __name__ == '__main__':
    main()
