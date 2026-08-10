# -*- coding: utf-8 -*-
"""备用数据源抓取 v4（数据源灾备，见 operations-governance.md §5.2）。

v4 变更（2026-08-08，据用户第三轮实测修复）：
  1. A股加第二数据源：东财失败自动切新浪财经（不同服务器，互为备份）。
     注意：新浪 ETF 日线为未复权价——与主源 Yahoo 的 A股原始层同口径，
     切换使用时走主管线同一套份额折算清洗，见 update_cn_data.py。
  2. 节奏放缓（每票间隔 2 秒、每路由只试一次），避免触发东财 IP 限流
     ——前几轮的密集重试疑似已触发临时限流（RST 挂断），通常数小时自动解除。
  3. 新增 --test 快速探针：只抓一只票探路，通了再全量。

用法：
  python backup_data_fetch.py cn --test     # 30秒探针：判断东财/新浪哪个通
  python backup_data_fetch.py cn            # A股全量
  python backup_data_fetch.py us --token X  # 美股全量（Tiingo，token 见下）
  python backup_data_fetch.py compare us|cn # 与主源重叠期比对（切换前必做）

Tiingo token（美股用，免费）：tiingo.com 注册 → Account → API Token 复制；
可 setx TIINGO_TOKEN <token> 永久保存（重开窗口生效）。
"""
import sys
import io
import os
import time
import urllib.request

import pandas as pd

US_TICKERS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "SHY", "BIL"]
CN_CODES = ["510300", "510500", "512100", "510880", "512010", "159928",
            "512800", "512880", "512660", "512480", "512690", "512400",
            "518880", "511010"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_ROUTE = "direct"


def set_route(mode: str):
    global _ROUTE
    _ROUTE = mode
    if mode == "direct":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()   # 跟随系统代理
    urllib.request.install_opener(opener)


def _patch_requests():
    try:
        import requests
        if getattr(requests, "_route_patched", False):
            return
        _orig_get = requests.get
        def _get(url, **kw):
            if _ROUTE == "direct":
                kw.setdefault("proxies", {"http": None, "https": None})
            return _orig_get(url, **kw)
        requests.get = _get
        requests._route_patched = True
    except ImportError:
        pass


# ---------------- A股：东财 → 新浪 双源 ----------------

def _cn_eastmoney(code: str) -> pd.Series:
    import akshare as ak
    df = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="qfq")
    if len(df) < 100:
        raise ValueError(f"仅 {len(df)} 行")
    df["日期"] = pd.to_datetime(df["日期"])
    return df.set_index("日期")["收盘"]


def _cn_sina(code: str) -> pd.Series:
    import akshare as ak
    sym = ("sh" if code.startswith(("5", "6")) else "sz") + code
    df = ak.fund_etf_hist_sina(symbol=sym)
    if len(df) < 100:
        raise ValueError(f"仅 {len(df)} 行")
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["close"]


CN_SOURCES = [("eastmoney", _cn_eastmoney), ("sina", _cn_sina)]


def _cn_fetch_one(code: str):
    """单票：源×路由逐一尝试，返回 (Series, 描述) 或 (None, None)。"""
    for src_name, fn in CN_SOURCES:
        for route in ("direct", "proxy"):
            set_route(route)
            try:
                s = fn(code)
                return s, f"{src_name}[{route}]"
            except Exception as e:
                print(f"  {code} {src_name}[{route}] 失败: {type(e).__name__}: {e}")
    return None, None


def fetch_cn(test_only: bool = False):
    _patch_requests()
    codes = CN_CODES[:1] if test_only else CN_CODES
    out, failed = {}, []
    for c in codes:
        s, via = _cn_fetch_one(c)
        if s is not None:
            out[c] = s
            print(f"OK {c} via {via}: {len(s)} 行, 截至 {s.index[-1].date()}")
        else:
            failed.append(c)
        time.sleep(2.0)
    if test_only:
        print("✅ 探针通过，可跑全量: python backup_data_fetch.py cn" if out
              else "❌ 探针失败：两个源均不通。若浏览器能开 eastmoney.com，"
                   "多半是脚本 IP 被临时限流，隔天再试。")
        return
    if out:
        pd.DataFrame(out).sort_index().to_csv("cn_etf_daily_backup.csv")
        print(f"已写出 cn_etf_daily_backup.csv（{len(out)}/{len(CN_CODES)} 只）")
        print("注意：若含新浪来源，价格为未复权口径，切换使用时按主管线做份额折算清洗。")
    print(f"❌ 失败品种: {failed}" if failed else "✅ A股备源全部成功")


# ---------------- 美股：Tiingo ----------------

def fetch_us(token: str, test_only: bool = False):
    if not token:
        print("缺少 Tiingo token（tiingo.com 免费注册 → Account → API Token）。")
        print("用法: python backup_data_fetch.py us --token 你的token")
        print("     或 setx TIINGO_TOKEN 你的token（重开窗口后免带参数）")
        sys.exit(1)
    tickers = US_TICKERS[:1] if test_only else US_TICKERS
    out, failed = {}, []
    for t in tickers:
        url = (f"https://api.tiingo.com/tiingo/daily/{t}/prices"
               f"?startDate=1990-01-01&format=csv&token={token}")
        ok = False
        for route in ("direct", "proxy"):
            set_route(route)
            try:
                req = urllib.request.Request(url, headers=UA)
                raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
                if not raw.strip().lower().startswith("date"):
                    print(f"  {t} [{route}] 响应异常，前100字符: {raw[:100]!r}")
                    continue
                df = pd.read_csv(io.StringIO(raw), parse_dates=["date"], index_col="date")
                if len(df) < 500 or "adjClose" not in df.columns:
                    print(f"  {t} [{route}] 行数/列异常，换路由")
                    continue
                out[t] = df["adjClose"]
                print(f"OK {t} tiingo[{route}]: {len(df)} 行, 截至 {df.index[-1].date()}")
                ok = True
                break
            except Exception as e:
                print(f"  {t} [{route}] 失败: {type(e).__name__}: {e}")
        if not ok:
            failed.append(t)
        time.sleep(1.0)
    if test_only:
        print("✅ 探针通过，可跑全量" if out else "❌ 探针失败")
        return
    if out:
        pd.DataFrame(out).sort_index().to_csv("us_etf_daily_backup.csv")
        print(f"已写出 us_etf_daily_backup.csv（{len(out)}/{len(US_TICKERS)} 只）")
    print(f"❌ 失败品种: {failed}" if failed else "✅ 美股备源全部成功")


# ---------------- 比对 ----------------

def compare(market: str):
    """通过线：日收益率相关 >0.999、年化漂移 <0.5%（新浪源因复权口径差异
    允许分红日附近偏差，整体趋势一致即可用于灾难模式）。"""
    main_file = {"us": "us_etf_daily.csv", "cn": "cn_etf_daily.csv"}[market]
    bkp_file = {"us": "us_etf_daily_backup.csv", "cn": "cn_etf_daily_backup.csv"}[market]
    a = pd.read_csv(main_file, index_col=0, parse_dates=True)
    b = pd.read_csv(bkp_file, index_col=0, parse_dates=True)
    common = [c for c in a.columns if c in b.columns]
    idx = a.index.intersection(b.index)[-756:]
    print(f"重叠期 {idx[0].date()} ~ {idx[-1].date()}，{len(idx)} 天，{len(common)} 个品种")
    for c in common:
        ra = a.loc[idx, c].pct_change().dropna()
        rb = b.loc[idx, c].pct_change().dropna()
        j = ra.index.intersection(rb.index)
        corr = ra[j].corr(rb[j])
        drift = ((1 + rb[j]).prod() / (1 + ra[j]).prod()) ** (252 / len(j)) - 1
        flag = "OK " if (corr > 0.999 and abs(drift) < 0.005) else "⚠️ "
        print(f"{flag}{c}: 日收益相关 {corr:.4f}, 年化漂移 {drift:+.2%}")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "cn"
    test = "--test" in args
    token = args[args.index("--token") + 1] if "--token" in args else os.environ.get("TIINGO_TOKEN", "")
    if cmd == "cn":
        fetch_cn(test_only=test)
    elif cmd == "us":
        fetch_us(token, test_only=test)
    elif cmd == "compare":
        compare(args[1])
    else:
        raise SystemExit("用法: python backup_data_fetch.py cn|us [--test] [--token X] | compare us|cn")
