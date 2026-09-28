# -*- coding: utf-8 -*-
"""Phase 6 月度数据刷新（GitHub Actions；BaoStock 历史股票池 + AkShare 财务）。

每月刷新三件事，产物发布为 Release：
  1. 按实际历史月末交易日补充股票池，禁止把实时快照回填过去
  2. 财务季表增量（近 3 个季度，覆盖重述）
  3. 现金流量表及资产负债率增量（同上）；保留已有行业映射

用法：python -u update_cn_stock_monthly.py
"""
import time
import json
import socket
from pathlib import Path

import pandas as pd

OUT = Path("out"); OUT.mkdir(exist_ok=True)
U = OUT / "universe_monthly.csv.gz"
F = OUT / "fundamentals_quarterly.csv.gz"
C = OUT / "cashflow_quarterly.csv.gz"


def retry(fn, *a, **k):
    for i in range(1, 4):
        try:
            return fn(*a, **k)
        except Exception as e:
            if i == 3:
                raise RuntimeError(f"{fn.__name__} failed after 3 attempts") from e
            time.sleep(5 * i)


def refresh_universe(ak, month_end):
    """Query the actual historical trading date; never backdate live quotes."""
    import baostock as bs
    old = pd.read_csv(U, dtype=str)
    start = pd.Timestamp(old['month_end'].max()) + pd.offsets.MonthEnd(1)
    months = pd.date_range(start, month_end, freq='ME')
    if not len(months):
        return
    login = bs.login()
    if login.error_code != '0':
        raise RuntimeError(f'BaoStock login failed: {login.error_msg}')
    frames = [old]
    try:
        for end in months:
            rs = bs.query_trade_dates(start_date=end.replace(day=1).strftime('%Y-%m-%d'),
                                     end_date=end.strftime('%Y-%m-%d'))
            calendar = bs_frame(rs)
            dates = calendar.loc[calendar.is_trading_day == '1', 'calendar_date']
            if dates.empty:
                raise RuntimeError(f'No trading date for {end:%Y-%m}')
            query_date = dates.max()
            frame = bs_frame(bs.query_all_stock(day=query_date))
            frame = frame[frame.code.str.match(r'^(sh\.6|sz\.[03])')].copy()
            if len(frame) < 3000:
                raise RuntimeError(f'Incomplete historical universe: {len(frame)}')
            frame['month_end'] = end.strftime('%Y-%m-%d')
            frame['source_trade_date'] = query_date
            frames.append(frame)
            print(f'universe {end:%Y-%m}: {len(frame)} rows at {query_date}', flush=True)
    finally:
        bs.logout()
    pd.concat(frames, ignore_index=True).drop_duplicates(
        ['month_end', 'code'], keep='last').to_csv(U, index=False)


def bs_frame(result):
    if result.error_code != '0':
        raise RuntimeError(f'BaoStock: {result.error_msg}')
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    if result.error_code != '0':
        raise RuntimeError(f'BaoStock stream: {result.error_msg}')
    return pd.DataFrame(rows, columns=result.fields)


def merge_preserving(old, new, keys):
    """A newly missing field must not erase an existing balance-sheet value."""
    for frame in (old, new):
        for key in keys:
            frame[key] = frame[key].astype(str)
        if 'code6' in keys:
            frame['code6'] = frame['code6'].str.zfill(6)
    return (new.drop_duplicates(keys, keep='last').set_index(keys)
            .combine_first(old.drop_duplicates(keys, keep='last').set_index(keys))
            .reset_index())


def refresh_quarters(ak, n_recent=3):
    qs = recent_quarters()[-n_recent:]
    for dest, fn, ren in [
        (F, ak.stock_yjbb_em, {"股票代码": "code6", "股票简称": "name", "每股收益": "eps",
                               "营业总收入-营业总收入": "revenue", "营业总收入-同比增长": "rev_yoy",
                               "营业收入-营业收入": "revenue",
                               "营业收入-同比增长": "rev_yoy", "净利润-净利润": "net_profit",
                               "净利润-同比增长": "np_yoy", "净资产收益率": "roe_weighted",
                               "销售毛利率": "gross_margin", "最新公告日期": "pub_date"}),
        (C, ak.stock_xjll_em, {"股票代码": "code6", "股票简称": "name",
                               "公告日期": "pub_date_cf",
                               "经营性现金流-现金流量净额": "cfo", "净现金流-净现金流": "net_cash",
                               "投资性现金流-现金流量净额": "cfi", "融资性现金流-现金流量净额": "cff"}),
    ]:
        frames = []
        for q in qs:
            df = retry(fn, date=q)
            if df is not None and len(df) >= 1000:
                df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
                required = (['code6', 'revenue', 'roe_weighted', 'pub_date'] if dest == F
                            else ['code6', 'cfo', 'pub_date_cf'])
                missing = [c for c in required if c not in df.columns]
                if missing or df.columns.duplicated().any():
                    raise RuntimeError(f'{fn.__name__}: unexpected schema; missing={missing}')
                if any(df[c].notna().mean() < 0.5 for c in required):
                    raise RuntimeError(f'{fn.__name__}: required fields mostly empty')
                df["report_date"] = q
                frames.append(df)
                print(f"  {dest.name} {q}: {len(df)} 行")
            else:
                raise RuntimeError(f'{fn.__name__} {q}: empty or incomplete data')
            time.sleep(1.5)
        if not frames:
            continue
        new = pd.concat(frames, ignore_index=True)
        keep = list(dict.fromkeys(c for c in list(ren.values()) + ["report_date", "资产负债率", "debt_ratio"] if c in new.columns))
        new = new[keep].rename(columns={"资产负债率": "debt_ratio"})
        new["code6"] = new.code6.astype(str).str.zfill(6)
        if dest.exists():
            old = pd.read_csv(dest, dtype={"code6": str, "report_date": str})
            new = merge_preserving(old, new, ['code6', 'report_date'])
        new.to_csv(dest, index=False)
        print(f"{dest.name}: {len(new)} 行 / {new.report_date.nunique()} 季")


def refresh_balance(ak):
    frames = []
    for q in recent_quarters():
        frame = retry(ak.stock_zcfz_em, date=q)
        if frame is None or len(frame) < 1000:
            raise RuntimeError(f'Incomplete balance sheet for {q}')
        frame = frame.rename(columns={'股票代码': 'code6', '资产负债率': 'debt_ratio',
                                      '公告日期': 'pub_date_bs'})
        frame['report_date'] = q
        frames.append(frame[['code6', 'report_date', 'debt_ratio', 'pub_date_bs']])
    old = pd.read_csv(F, dtype={'code6': str, 'report_date': str})
    merge_preserving(old, pd.concat(frames, ignore_index=True),
                     ['code6', 'report_date']).to_csv(F, index=False)


def recent_quarters(today=None):
    # Only quarters past the full-market reporting window are required.
    today = pd.Timestamp(today or pd.Timestamp.now(tz='Asia/Shanghai').date())
    quarters = pd.date_range('2010-03-31', today, freq='QE')
    due = []
    for q in quarters:
        deadline = (q + pd.DateOffset(months={3: 1, 6: 2, 9: 1, 12: 4}[q.month])
                    + pd.offsets.MonthEnd(0))
        if deadline.normalize() < today.normalize():
            due.append(q.strftime('%Y%m%d'))
    return due[-3:]


def main():
    import akshare as ak
    socket.setdefaulttimeout(45)
    now = pd.Timestamp.now(tz='Asia/Shanghai')
    month_end = (now.normalize().replace(day=1) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    required = [U, F, C, OUT / 'industry_map.csv.gz']
    for path in required:
        if not path.exists() or not path.stat().st_size:
            raise RuntimeError(f'Missing baseline: {path}')
        pd.read_csv(path, nrows=1)  # Reject corrupt downloads before network requests.
    refresh_universe(ak, month_end)
    refresh_quarters(ak)
    refresh_balance(ak)
    manifest = {'generated_utc': pd.Timestamp.now(tz='UTC').isoformat(),
                'universe_month_end': month_end, 'financial_quarters': recent_quarters(),
                'industry_status': 'existing baseline retained; no backdated live industry map',
                'financial_vintages': 'latest revisions; historical publication versions not reconstructed'}
    (OUT / 'cn_stock_quality.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
