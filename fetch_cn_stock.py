# -*- coding: utf-8 -*-
"""Phase 6 财务数据管道：A股全市场日线+估值+季频基本面（PIT），在 GitHub Actions 运行。

设计要点（与 phase6-value-design.md 对应）：
- 日频骨干 = baostock：未复权收盘价 + 复权因子、估值（peTTM/pbMRQ/psTTM/pcfNcfTTM）、
  成交额/换手率（用于流通市值代理 = amount/(turn/100)，全程 PIT 且覆盖已退市股票）、
  tradestatus/isST（可交易性与 ST 过滤，回测与实盘同一套规则）。
- PIT 股票宇宙 = baostock query_all_stock(每个月末)：当日在市名单，天然含退市消失，
  避免幸存者偏差。北交所(bj.)与指数默认剔除。
- 季频质量 = akshare 东财批量表：业绩报告 stock_yjbb_em（ROE加权/毛利率/营收净利同比/
  最新公告日期→PIT 生效日）+ 资产负债表 stock_zcfz_em（资产负债率）。每季 2 次调用。
- 断点续跑：out/checkpoint.txt 记录已完成股票，重跑自动跳过（配合 actions/cache）。

用法：
  python fetch_cn_stock.py probe                # 连通性探针（30 秒）
  python fetch_cn_stock.py bulk                 # 历史全量（约 1.5~2.5 小时）
  python fetch_cn_stock.py incremental          # 增量：当年日线 + 近两季基本面
  python fetch_cn_stock.py bulk --start 2010-01-01 --test   # 试跑：仅 5 只股票、4 个季度

输出（out/ 目录，由 workflow 上传为 GitHub Release 资产）：
  universe_monthly.csv.gz   月末 PIT 在市名单
  daily_YYYY.parquet        分年日线（未复权价+复权因子+估值+状态）
  fundamentals_quarterly.csv.gz  季频基本面（含公告日期）
  manifest.json             数据清单（行数/覆盖区间/生成时间）
"""
import argparse
import gzip
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

OUT = Path("out")
OUT.mkdir(exist_ok=True)
CHECKPOINT = OUT / "checkpoint.txt"

DAILY_FIELDS = "date,code,close,volume,amount,turn,tradestatus,isST,peTTM,pbMRQ,psTTM,pcfNcfTTM"


# ---------------- baostock 基础 ----------------

def bs_login():
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise SystemExit(f"baostock login failed: {lg.error_msg}")
    return bs


def rs_to_df(rs) -> pd.DataFrame:
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)


def month_ends(start: str, end: str) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(start, end, freq="ME")]


def quarter_ends(start: str, end: str) -> list[str]:
    return [d.strftime("%Y%m%d") for d in pd.date_range(start, end, freq="QE")]


# ---------------- 1) PIT 宇宙 ----------------

def fetch_universe(bs, start: str, end: str, save: bool = True) -> pd.DataFrame:
    frames = []
    for d in month_ends(start, end):
        df = rs_to_df(bs.query_all_stock(day=d))
        if df.empty:            # 节假日月末：往前找最近交易日（最多回退 10 天）
            for k in range(1, 11):
                d2 = (pd.Timestamp(d) - pd.Timedelta(days=k)).strftime("%Y-%m-%d")
                df = rs_to_df(bs.query_all_stock(day=d2))
                if not df.empty:
                    break
        if df.empty:
            print(f"::warning::universe {d} empty, skipped")
            continue
        df = df[df["code"].str.match(r"(sh\.6|sz\.0|sz\.3)")]   # 剔除指数/基金/北交所
        df["month_end"] = d
        frames.append(df)
        time.sleep(0.1)
    if not frames:   # 区间内无月末（如月中跑增量）：空结果是合法情形，不崩溃
        print("::warning::no month-end snapshots in range, universe unchanged")
        return pd.DataFrame(columns=["code", "tradeStatus", "code_name", "month_end"])
    out = pd.concat(frames, ignore_index=True)
    if save:
        out.to_csv(OUT / "universe_monthly.csv.gz", index=False)
    print(f"universe: {len(out)} rows, {out['month_end'].nunique()} month-ends, "
          f"{out['code'].nunique()} distinct codes")
    return out


# ---------------- 2) 日线 + 复权因子 ----------------

def fetch_daily(bs, codes: list[str], start: str, end: str):
    done = set()
    if CHECKPOINT.exists():
        done = set(CHECKPOINT.read_text().split())
        print(f"checkpoint: {len(done)} codes already done")
    buf, part = [], 0
    t0 = time.time()
    for i, code in enumerate(codes):
        if code in done:
            continue
        try:
            df = rs_to_df(bs.query_history_k_data_plus(
                code, DAILY_FIELDS, start_date=start, end_date=end,
                frequency="d", adjustflag="3"))                    # 未复权
            fac = rs_to_df(bs.query_adjust_factor(code, start, end))  # 复权因子
            if not df.empty:
                if not fac.empty:
                    fac = fac[["dividOperateDate", "backAdjustFactor"]].rename(
                        columns={"dividOperateDate": "date"})
                    df = df.merge(fac, on="date", how="left")
                else:
                    df["backAdjustFactor"] = None
                buf.append(df)
        except Exception as e:
            print(f"::warning::{code} daily failed: {type(e).__name__}: {e}")
        with open(CHECKPOINT, "a") as f:
            f.write(code + "\n")
        if len(buf) >= 400:
            part += 1
            pd.concat(buf, ignore_index=True).to_parquet(OUT / f"daily_part{part:03d}.parquet")
            buf = []
        if (i + 1) % 200 == 0:
            rate = (i + 1 - len(done)) / max(time.time() - t0, 1)
            print(f"  daily {i+1}/{len(codes)} ({rate:.1f} stk/s)")
    if buf:
        part += 1
        pd.concat(buf, ignore_index=True).to_parquet(OUT / f"daily_part{part:03d}.parquet")


def consolidate_daily():
    """part 文件合并为分年 parquet（幂等：增量模式与既有年文件去重合并）。"""
    parts = sorted(OUT.glob("daily_part*.parquet"))
    if not parts:
        print("no daily parts to consolidate")
        return
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    for col in ["close", "volume", "amount", "turn", "peTTM", "pbMRQ", "psTTM",
                "pcfNcfTTM", "backAdjustFactor"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["backAdjustFactor"] = df.groupby("code")["backAdjustFactor"].ffill().fillna(1.0)
    df["date"] = pd.to_datetime(df["date"])
    for y, g in df.groupby(df["date"].dt.year):
        fp = OUT / f"daily_{y}.parquet"
        if fp.exists():
            old = pd.read_parquet(fp)
            g = (pd.concat([old, g], ignore_index=True)
                 .drop_duplicates(subset=["date", "code"], keep="last"))
        g.sort_values(["code", "date"]).to_parquet(fp)
        print(f"daily_{y}.parquet: {len(g)} rows")
    for p in parts:
        p.unlink()


# ---------------- 3) 季频基本面（东财批量） ----------------

def fetch_fundamentals(start: str, end: str, test: bool = False):
    import akshare as ak
    qs = quarter_ends(start, end)
    if test:
        qs = qs[-4:]
    yjbb, zcfz = [], []
    for q in qs:
        for name, fn, sink in [("yjbb", ak.stock_yjbb_em, yjbb),
                               ("zcfz", ak.stock_zcfz_em, zcfz)]:
            for attempt in range(3):
                try:
                    df = fn(date=q)
                    if df is not None and len(df):
                        df["report_date"] = q
                        sink.append(df)
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"::warning::{name} {q} failed: {type(e).__name__}: {e}")
                    time.sleep(5 * (attempt + 1))
            time.sleep(1.0)
        print(f"  fundamentals {q}: yjbb={sum(len(x) for x in yjbb)} zcfz={sum(len(x) for x in zcfz)}")
    if not yjbb:
        print("::warning::no yjbb data at all")
        return
    a = pd.concat(yjbb, ignore_index=True)
    a = a.rename(columns={"股票代码": "code6", "股票简称": "name",
                          "每股收益": "eps", "营业收入-营业收入": "revenue",
                          "营业收入-同比增长": "rev_yoy", "净利润-净利润": "net_profit",
                          "净利润-同比增长": "np_yoy", "净资产收益率": "roe_weighted",
                          "销售毛利率": "gross_margin", "最新公告日期": "pub_date"})
    keep_a = [c for c in ["code6", "name", "eps", "revenue", "rev_yoy", "net_profit",
                          "np_yoy", "roe_weighted", "gross_margin", "pub_date",
                          "report_date"] if c in a.columns]
    a = a[keep_a]
    if zcfz:
        b = pd.concat(zcfz, ignore_index=True)
        b = b.rename(columns={"股票代码": "code6", "资产负债率": "debt_ratio",
                              "公告日期": "pub_date_bs"})
        keep_b = [c for c in ["code6", "debt_ratio", "pub_date_bs", "report_date"] if c in b.columns]
        merged = a.merge(b[keep_b], on=["code6", "report_date"], how="left")
    else:
        merged = a
    fp = OUT / "fundamentals_quarterly.csv.gz"
    if fp.exists():
        prev = pd.read_csv(fp, dtype={"code6": str, "report_date": str})
        merged["code6"] = merged["code6"].astype(str)
        merged["report_date"] = merged["report_date"].astype(str)
        merged = (pd.concat([prev, merged], ignore_index=True)
                  .drop_duplicates(subset=["code6", "report_date"], keep="last"))
    merged.to_csv(fp, index=False)
    print(f"fundamentals: {len(merged)} rows, {merged['report_date'].nunique()} quarters")


# ---------------- manifest ----------------

def write_manifest(mode: str):
    files = {}
    for p in sorted(OUT.glob("*")):
        if p.name in ("checkpoint.txt", "manifest.json"):
            continue
        files[p.name] = p.stat().st_size
    manifest = {"mode": mode, "generated_utc": pd.Timestamp.now(tz='UTC').isoformat(),
                "files": files}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(json.dumps(manifest, indent=1))


# ---------------- 入口 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["probe", "bulk", "incremental"])
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    end = pd.Timestamp.now(tz='UTC').strftime("%Y-%m-%d")

    if args.mode == "probe":
        bs = bs_login()
        df = rs_to_df(bs.query_history_k_data_plus(
            "sh.600000", DAILY_FIELDS, start_date="2024-01-01", end_date="2024-02-01",
            frequency="d", adjustflag="3"))
        print(f"baostock: {len(df)} rows for sh.600000 ✔" if len(df) else "baostock ✘")
        bs.logout()
        import akshare as ak
        y = ak.stock_yjbb_em(date="20240331")
        print(f"akshare/eastmoney: {len(y)} rows for 2024Q1 业绩报告 ✔" if len(y) else "akshare ✘")
        return

    if args.mode == "bulk":
        bs = bs_login()
        uni = fetch_universe(bs, args.start, end)
        codes = sorted(uni["code"].unique())
        if args.test:
            codes = codes[:5]
        fetch_daily(bs, codes, args.start, end)
        bs.logout()
        consolidate_daily()
        fetch_fundamentals(args.start, end, test=args.test)
        write_manifest("bulk")
        return

    if args.mode == "incremental":
        # 前提：workflow 已把上一版 Release 的 out/ 下载到位
        old_u = OUT / "universe_monthly.csv.gz"
        if not old_u.exists():
            raise SystemExit("增量模式找不到历史数据（universe_monthly.csv.gz 缺失）——"
                             "请先手动运行一次 mode=bulk 生成首版数据包")
        year_start = f"{pd.Timestamp.now(tz='UTC').year}-01-01"
        recent = (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        bs = bs_login()
        # 先读旧、后抓新（save=False 防止覆盖历史宇宙文件），再合并落盘
        old = pd.read_csv(old_u)
        uni_start = (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
        uni_new = fetch_universe(bs, uni_start, end, save=False)
        if len(uni_new):
            merged = (pd.concat([old, uni_new], ignore_index=True)
                      .drop_duplicates(subset=["month_end", "code"], keep="last"))
            merged.to_csv(old_u, index=False)
        codes = sorted(pd.read_csv(old_u)["code"].unique())
        if CHECKPOINT.exists():
            CHECKPOINT.unlink()      # 增量模式每次全走一遍（短区间，快）
        fetch_daily(bs, codes, year_start, end)
        bs.logout()
        consolidate_daily()
        fetch_fundamentals(recent, end)   # 近两个财报季刷新（公告日期会更新）
        write_manifest("incremental")


if __name__ == "__main__":
    main()
