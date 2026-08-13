# -*- coding: utf-8 -*-
"""Phase 6 月度数据刷新（GitHub Actions 运行，不依赖 baostock）。

每月刷新三件事，产物发布为 Release：
  1. 宇宙月末快照追加一行（东财实时行情取全市场名称 → ST 标记；不再用 baostock）
  2. 财务季表增量（近 3 个季度，覆盖重述）
  3. 现金流量表增量（同上）

用法：python -u update_cn_stock_monthly.py
"""
import time
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
                print(f"::warning::{fn.__name__} 失败: {type(e).__name__}: {e}")
                return None
            time.sleep(5 * i)


def refresh_universe(ak, month_end):
    """用东财实时行情重建当月末在市名单（代码/名称/ST），停牌由成交量为0判定。"""
    spot = retry(ak.stock_zh_a_spot_em)
    if spot is None or spot.empty:
        print("::warning::实时行情为空，宇宙未更新"); return
    df = spot.rename(columns={"代码": "code6", "名称": "code_name", "成交量": "vol"})
    df = df[df.code6.astype(str).str.match(r"^(6|0|3)")]          # 剔除北交所/退市整理
    df["code"] = np.where(df.code6.str.startswith("6"), "sh." + df.code6, "sz." + df.code6)
    df["tradeStatus"] = (pd.to_numeric(df.get("vol"), errors="coerce").fillna(0) > 0).astype(int)
    df["month_end"] = month_end
    new = df[["code", "tradeStatus", "code_name", "month_end"]]
    old = pd.read_csv(U) if U.exists() else None
    if old is not None:
        new = (pd.concat([old, new], ignore_index=True)
               .drop_duplicates(subset=["month_end", "code"], keep="last"))
    new.to_csv(U, index=False)
    print(f"universe: +{month_end}，累计 {len(new)} 行 / {new.month_end.nunique()} 期")


def refresh_quarters(ak, n_recent=3):
    qs = [d.strftime("%Y%m%d") for d in pd.date_range("2010-03-31", pd.Timestamp.today(), freq="QE")][-n_recent:]
    for dest, fn, ren in [
        (F, ak.stock_yjbb_em, {"股票代码": "code6", "股票简称": "name", "每股收益": "eps",
                               "营业收入-同比增长": "rev_yoy", "净利润-净利润": "net_profit",
                               "净利润-同比增长": "np_yoy", "净资产收益率": "roe_weighted",
                               "销售毛利率": "gross_margin", "最新公告日期": "pub_date"}),
        (C, ak.stock_xjll_em, {"股票代码": "code6", "股票简称": "name",
                               "经营性现金流-现金流量净额": "cfo", "净现金流-净现金流": "net_cash",
                               "投资性现金流-现金流量净额": "cfi", "融资性现金流-现金流量净额": "cff"}),
    ]:
        frames = []
        for q in qs:
            df = retry(fn, date=q)
            if df is not None and len(df):
                df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
                df["report_date"] = q
                frames.append(df)
                print(f"  {dest.name} {q}: {len(df)} 行")
            time.sleep(1.5)
        if not frames:
            continue
        new = pd.concat(frames, ignore_index=True)
        keep = [c for c in list(ren.values()) + ["report_date", "资产负债率", "debt_ratio"] if c in new.columns]
        new = new[keep].rename(columns={"资产负债率": "debt_ratio"})
        new["code6"] = new.code6.astype(str).str.zfill(6)
        if dest.exists():
            old = pd.read_csv(dest, dtype={"code6": str, "report_date": str})
            new = (pd.concat([old, new], ignore_index=True)
                   .drop_duplicates(subset=["code6", "report_date"], keep="last"))
        new.to_csv(dest, index=False)
        print(f"{dest.name}: {len(new)} 行 / {new.report_date.nunique()} 季")


def refresh_industry(ak, month_end):
    """月度追加一期行业快照；随时间积累即成为真正的 PIT 行业历史。"""
    I = OUT / "industry_map.csv.gz"
    try:
        info = retry(ak.sw_index_first_info)
        if info is None or info.empty:
            print("::warning::申万行业信息为空，跳过行业快照"); return
        ncol = "行业名称" if "行业名称" in info.columns else info.columns[1]
        ccol = "行业代码
