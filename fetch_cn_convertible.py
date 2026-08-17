# -*- coding: utf-8 -*-
"""想法卡 #2026-04 数据管道：A股可转债日线 + 每日比价表快照（PIT 积累），在 GitHub Actions 运行。

设计要点（与 research-agenda.md #2026-04 检验计划对应）：
- 每日快照 = akshare bond_cov_comparison（东财转债比价表）：价格、转股价、转股价值、
  转股溢价率等。**从部署日起逐日积累，构成无重构误差的真实 PIT 面板**——这是本管线
  最重要的资产：2027 年站3 时既有若干月真实快照，可用于交叉验证历史重构数据的误差。
- 静态信息 = akshare bond_zh_cov（发行列表）：上市日、发行规模、评级（如有）、正股代码。
- 历史日线 = akshare bond_zh_hs_cov_daily（新浪，逐券 OHLC）：bulk 模式抓全量，
  供 2027 年重构历史双低分时使用（配合正股价与转股价调整史）。
- 历史转股溢价率**不在本管线内重构**——那是 2027 站3 的独立工序，本管线只负责喂原料。
- 断点续跑：out/checkpoint_cb.txt，配合 workflow 重试幂等。

用法：
  python fetch_cn_convertible.py probe        # 连通性探针（30 秒，首次部署先跑这个）
  python fetch_cn_convertible.py snapshot     # 每日：比价表快照追加 + 发行列表刷新
  python fetch_cn_convertible.py bulk         # 一次性：全量历史日线（约 30-60 分钟）
  python fetch_cn_convertible.py bulk --test  # 试跑：仅 5 只

输出（out/ 目录，由 workflow 上传为 GitHub Release 资产）：
  cb_snapshots_YYYY.csv.gz   每日比价表快照（date + 全列，PIT）
  cb_issuance.csv.gz         发行列表（静态，含上市日/规模/评级）
  cb_daily_YYYY.parquet      分年日线（全部转债 OHLCV）
  cb_manifest.json           数据清单

纪律注记：本管线属数据基建，不做任何信号计算；站3 之前不得在此文件里加因子逻辑。
"""
import argparse
import json
import time
from pathlib import Path

import pandas as pd

OUT = Path("out")
OUT.mkdir(exist_ok=True)
CHECKPOINT = OUT / "checkpoint_cb.txt"


def _ak():
    import socket
    socket.setdefaulttimeout(60)   # 东财/新浪接口无内建超时，防挂死（同 baostock 教训）
    import akshare as ak
    return ak


def _sina_symbol(code: str) -> str:
    """6位债券代码 → 新浪符号。11xxxx=沪(sh)，12xxxx=深(sz)。"""
    code = str(code).strip()
    return ("sh" if code.startswith("11") else "sz") + code


def _retry(fn, name: str, tries: int = 3, sleep: float = 5.0):
    for attempt in range(tries):
        try:
            df = fn()
            if df is not None and len(df):
                return df
            print(f"::warning::{name} returned empty (attempt {attempt+1})")
        except Exception as e:
            print(f"::warning::{name} failed (attempt {attempt+1}): "
                  f"{type(e).__name__}: {str(e)[:160]}")
        time.sleep(sleep * (attempt + 1))
    return None


# ---------------- 1) 每日快照（PIT 核心资产） ----------------

def _code_col(df: pd.DataFrame) -> str:
    return next((c for c in df.columns if "债券代码" in str(c)),
                next((c for c in df.columns if "代码" in str(c)), df.columns[0]))


def fetch_snapshot(ak) -> bool:
    today = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d")
    # 降级链（2026-08-17 实测：GitHub runner 上比价表接口被东财掐断，发行列表接口畅通，
    # 且 bond_zh_cov 本身含 债现价/转股价/转股价值/转股溢价率 列，可独立充当快照源）
    df, src = _retry(lambda: ak.bond_cov_comparison(), "bond_cov_comparison", tries=2), "cov_comparison"
    if df is None:
        df, src = _retry(lambda: ak.bond_zh_cov(), "bond_zh_cov(快照降级)"), "zh_cov_fallback"
        if df is not None and not any("溢价率" in str(c) for c in df.columns):
            print("::warning::降级快照缺少转股溢价率列——东财改版？请人工核对列结构")
    if df is None:
        print("::error::两个快照源均失败——今日快照缺失，明日运行自动续上")
        return False
    df.insert(0, "snap_date", today)
    df.insert(1, "snap_src", src)
    # 全列保留：接口列名可能随东财改版漂移，落盘原始列 + 快照日期，清洗留给消费端
    year = today[:4]
    fp = OUT / f"cb_snapshots_{year}.csv.gz"
    df = df.astype(str)
    if fp.exists():
        old = pd.read_csv(fp, dtype=str)
        code = _code_col(df)
        merged = pd.concat([old, df], ignore_index=True)
        if code in merged.columns:
            merged = merged.drop_duplicates(subset=["snap_date", code], keep="last")
    else:
        merged = df
    merged.to_csv(fp, index=False)
    n_today = (merged["snap_date"] == today).sum()
    print(f"snapshot {today} [{src}]: {n_today} 行入档（{fp.name} 共 {len(merged)} 行、"
          f"{merged['snap_date'].nunique()} 个交易日）")
    if n_today < 300:   # 2026-06 存量约 400+ 只；低于 300 视为疑似残缺
        print(f"::warning::今日快照仅 {n_today} 行，疑似接口残缺，请人工核对")
    return True


def fetch_issuance(ak):
    df = _retry(lambda: ak.bond_zh_cov(), "bond_zh_cov")
    if df is None:
        print("::warning::发行列表刷新失败，沿用上一版")
        return
    df.astype(str).to_csv(OUT / "cb_issuance.csv.gz", index=False)
    print(f"issuance: {len(df)} 只（含未上市/已退市）")


# ---------------- 2) 历史日线（bulk，一次性） ----------------

def fetch_daily_bulk(ak, test: bool = False):
    iss = OUT / "cb_issuance.csv.gz"
    if not iss.exists():
        fetch_issuance(ak)
    codes = []
    if iss.exists():
        d = pd.read_csv(iss, dtype=str)
        code_col = next((c for c in d.columns if "债券代码" in c or c == "code"), d.columns[0])
        codes = sorted(d[code_col].dropna().unique())
    if not codes:
        raise SystemExit("发行列表为空，bulk 中止")
    if test:
        codes = codes[:5]
    done = set(CHECKPOINT.read_text().split()) if CHECKPOINT.exists() else set()
    print(f"daily bulk: {len(codes)} codes, {len(done)} done")
    buf, t0, n = [], time.time(), 0
    for code in codes:
        if code in done:
            continue
        sym = _sina_symbol(code)
        try:
            df = ak.bond_zh_hs_cov_daily(symbol=sym)
            if df is not None and len(df):
                df["code"] = code
                buf.append(df)
        except Exception as e:
            print(f"::warning::{sym} daily failed: {type(e).__name__}: {str(e)[:120]}")
        with open(CHECKPOINT, "a") as f:
            f.write(code + "\n")
        n += 1
        if n % 25 == 0:
            rate = n / max(time.time() - t0, 1)
            print(f"  daily {n}/{len(codes)-len(done)} ({rate:.2f}/s)", flush=True)
        time.sleep(0.5)   # 新浪限频保守值
    if buf:
        alld = pd.concat(buf, ignore_index=True)
        alld["date"] = pd.to_datetime(alld["date"])
        for y, g in alld.groupby(alld["date"].dt.year):
            fp = OUT / f"cb_daily_{y}.parquet"
            if fp.exists():
                old = pd.read_parquet(fp)
                g = (pd.concat([old, g], ignore_index=True)
                     .drop_duplicates(subset=["date", "code"], keep="last"))
            g.sort_values(["code", "date"]).to_parquet(fp)
            print(f"cb_daily_{y}.parquet: {len(g)} rows")


# ---------------- manifest ----------------

def write_manifest(mode: str):
    files = {p.name: p.stat().st_size for p in sorted(OUT.glob("*"))
             if p.name not in ("checkpoint_cb.txt", "cb_manifest.json")}
    m = {"mode": mode, "generated_utc": pd.Timestamp.now(tz="UTC").isoformat(),
         "files": files}
    (OUT / "cb_manifest.json").write_text(json.dumps(m, indent=1, ensure_ascii=False))
    print(json.dumps(m, indent=1, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["probe", "snapshot", "bulk"])
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    ak = _ak()

    if args.mode == "probe":
        for name, fn in [("bond_cov_comparison(比价表)", lambda: ak.bond_cov_comparison()),
                         ("bond_zh_cov(发行列表)", lambda: ak.bond_zh_cov())]:
            df = _retry(fn, name, tries=1)
            print(f"{name}: {'✔ ' + str(len(df)) + ' rows, 列: ' + '|'.join(map(str, df.columns[:12])) if df is not None else '✘'}")
        try:
            d = ak.bond_zh_hs_cov_daily(symbol="sh113050")   # 南银转债，2021 上市
            print(f"bond_zh_hs_cov_daily(日线): ✔ {len(d)} rows" if d is not None and len(d) else "日线 ✘")
        except Exception as e:
            print(f"bond_zh_hs_cov_daily(日线): ✘ {type(e).__name__}: {str(e)[:120]}")
        return

    if args.mode == "snapshot":
        ok = fetch_snapshot(ak)
        fetch_issuance(ak)
        write_manifest("snapshot")
        if not ok:
            raise SystemExit(2)   # 非零退出让 workflow 标红，幂等重试窗口补抓
        return

    if args.mode == "bulk":
        fetch_daily_bulk(ak, test=args.test)
        write_manifest("bulk")


if __name__ == "__main__":
    main()
