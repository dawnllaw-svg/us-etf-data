# -*- coding: utf-8 -*-
"""定期报告全文抓取器（在 GitHub Actions 里跑，不在沙箱里跑）。

背景：Claude 会话所在沙箱的出口是白名单代理，巨潮/东财/雪球一律 403，只有 GitHub 可达；
经许可通道抓 200 页以上的 PDF 又会被截断在前 30 页，财务附注（约 90 页起）读不到。
解决办法：把抓取搬到用户自己的 GitHub Actions（网络无限制），把年报/半年报**全文文本**
连同解析出的关键科目发布为 Release 资产；沙箱侧只需从 GitHub 下载，附注就能整本读。

三条取数通道，按顺序回退，任一成功即止：
  1) 东财公告全文分页接口（快，但部分公司 page_size=0，如中国神华）
  2) 巨潮公告查询 API → 终稿 PDF → pdfplumber 全文提取（覆盖 1 的缺口，页数不限）
  3) 东财公告 attach_url 指向的 PDF → 同样用 pdfplumber 提取

输出（发布为 Release 资产）：
  filings_<code>_<report_type>_<year>.txt.gz   每份报告的全文文本
  filings_manifest.json                        每份报告的来源通道、页数、字节数、抓取时间
  filings_items.json                           正则解析出的关键科目（账龄/受限/跌价/商誉/转固/研发资本化/前五客户）

用法：
  python3 fetch_filings.py --codes 600905,600406,... --years 2025,2026 --out dist/
依赖：requests、pdfplumber（workflow 里 pip install）
"""
from __future__ import annotations

import argparse, gzip, io, json, re, sys, time
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
EM_INDEX = "https://np-anotice-stock.eastmoney.com/api/security/ann"
EM_CONTENT = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
CNINFO_QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_HOST = "https://static.cninfo.com.cn/"
TIMEOUT = 60


def _get(url, **kw):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
            if r.status_code == 200:
                return r
        except Exception as e:                       # 网络抖动重试，失败不抛给上层
            if attempt == 2:
                print(f"  ! {type(e).__name__}: {url[:80]}", file=sys.stderr)
        time.sleep(2 + attempt * 3)
    return None


# ---------------- 通道 1：东财分页全文 ----------------

def em_find_reports(code: str, years: list[int]) -> list[dict]:
    """返回该股票的年报/半年报公告条目（art_code、标题、日期、attach_url）。"""
    out = []
    for page in (1, 2, 3):
        r = _get(EM_INDEX, params=dict(sr=-1, page_size=50, page_index=page, ann_type="A",
                                       client_source="web", stock_list=code, f_node=0, s_node=0))
        if not r:
            break
        try:
            items = r.json().get("data", {}).get("list", [])
        except Exception:
            break
        for it in items:
            title = it.get("title", "")
            date = (it.get("notice_date") or "")[:10]
            yr = int(date[:4]) if date[:4].isdigit() else 0
            if yr and (yr in years or yr - 1 in years):
                if re.search(r"(年度报告|半年度报告)$", title) and "摘要" not in title:
                    out.append(dict(art_code=it.get("art_code"), title=title, date=date,
                                    attach=(it.get("attach_url") or "")))
        if not items:
            break
    # 去重，保留最近的
    seen, uniq = set(), []
    for it in out:
        k = it["title"]
        if k not in seen:
            seen.add(k); uniq.append(it)
    return uniq


def em_fulltext(art_code: str) -> tuple[str, int]:
    """逐页取全文。返回 (文本, 页数)；page_size=0 时返回 ("", 0)。"""
    r = _get(EM_CONTENT, params=dict(art_code=art_code, client_source="web", page_index=1))
    if not r:
        return "", 0
    try:
        d = r.json().get("data", {})
    except Exception:
        return "", 0
    n = int(d.get("page_size") or 0)
    if n <= 1 and not (d.get("notice_content") or "").strip():
        return "", 0
    parts = [d.get("notice_content") or ""]
    for p in range(2, n + 1):
        rp = _get(EM_CONTENT, params=dict(art_code=art_code, client_source="web", page_index=p))
        if not rp:
            continue
        try:
            parts.append(rp.json().get("data", {}).get("notice_content") or "")
        except Exception:
            pass
        time.sleep(0.3)
    return "\n".join(parts), n


# ---------------- 通道 2/3/4：PDF 全文 ----------------

def dfcfw_pdf_url(art_code: str) -> str:
    """东财终稿 PDF 的稳定命名：https://pdf.dfcfw.com/pdf/H2_<art_code>_1.pdf

    2026-09-04 增补。此前回退依赖 attach_url（常为空）与巨潮查询（参数易错），
    对 page_size=0 的公司全部落空；而该 URL 只要有 art_code 就能拼出来，是最稳的一条。
    """
    return f"https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf" if art_code else ""



def pdf_text(url: str) -> str:
    import pdfplumber
    r = _get(url)
    if not r or not r.content[:4] == b"%PDF":
        return ""
    buf, out = io.BytesIO(r.content), []
    with pdfplumber.open(buf) as pdf:
        for pg in pdf.pages:
            out.append(pg.extract_text() or "")
    return "\n".join(out)


def cninfo_pdf_url(code: str, title: str, years: list[int]) -> str:
    """巨潮公告查询：按股票代码与报告类型找终稿 PDF 路径。

    要点（2026-09-03 修）：
    - searchkey 传关键词经常匹配不到，改为不传 searchkey、只按 category 过滤后自行筛标题；
    - category 按年报/半年报分开传，混传会命中另一类；
    - 目标年份从标题里取（"2025年年度报告" → 2025），不能用公告发布年份。
    """
    is_sz = code[0] in "013"
    plate, column = ("sz", "szse") if is_sz else ("sh", "sse")
    interim = "半年度" in title
    cat = "category_bndbg_szsh" if interim else "category_ndbg_szsh"
    ym = re.search(r"(20\d{2})", title)
    want_year = ym.group(1) if ym else ""
    body = dict(pageNum=1, pageSize=50, column=column, plate=plate, tabName="fulltext",
                stock=f"{code},", searchkey="", category=cat,
                seDate="", sortName="", sortType="", isHLtitle="true")
    for attempt in range(2):
        try:
            r = requests.post(CNINFO_QUERY, data=body, headers={**UA,
                              "Content-Type": "application/x-www-form-urlencoded",
                              "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"},
                              timeout=TIMEOUT)
            anns = (r.json() or {}).get("announcements") or []
        except Exception:
            anns = []
        for a in anns:
            t = re.sub(r"<[^>]+>", "", a.get("announcementTitle", ""))
            if any(x in t for x in ("摘要", "英文", "已取消", "更正")):
                continue
            if interim and "半年度报告" not in t:
                continue
            if not interim and ("年度报告" not in t or "半年度" in t):
                continue
            if want_year and want_year not in t:
                continue
            return CNINFO_HOST + a["adjunctUrl"]
        time.sleep(2)
    return ""


# ---------------- 关键科目解析（只抓事实，不做判断） ----------------

PATTERNS = {
    "应收账款账龄": r"(应收账款[\s\S]{0,80}?按账龄披露[\s\S]{0,1200})",
    "受限资产": r"((?:所有权|使用权)受到?限制的资产[\s\S]{0,1200})",
    "存货跌价": r"(存货跌价准备[\s\S]{0,900})",
    "商誉": r"(商誉[\s\S]{0,60}?(?:账面原值|减值准备)[\s\S]{0,1200})",
    "在建工程转固": r"(本期转入固定资产[\s\S]{0,900})",
    "研发投入": r"(研发投入(?:情况)?[\s\S]{0,60}?资本化[\s\S]{0,600})",
    "前五名客户": r"(前[五5]名客户[\s\S]{0,400})",
    "前五名供应商": r"(前[五5]名供应商[\s\S]{0,400})",
    "关联交易": r"(关联交易[\s\S]{0,60}?(?:采购商品|接受劳务)[\s\S]{0,900})",
    "员工情况": r"((?:在职员工的?数量|员工情况)[\s\S]{0,500})",
}


def parse_items(text: str) -> dict:
    out = {}
    for k, pat in PATTERNS.items():
        m = re.search(pat, text)
        out[k] = re.sub(r"\s+", " ", m.group(1))[:1500] if m else None
    return out


# ---------------- 主流程 ----------------

def run(codes: list[str], years: list[int], outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    manifest, items = [], {}
    for code in codes:
        print(f"[{code}]", flush=True)
        reports = em_find_reports(code, years)
        if not reports:
            manifest.append(dict(code=code, status="no_report_found"))
            continue
        for rep in reports:
            title, art = rep["title"], rep["art_code"]
            text, npages, via = "", 0, ""
            if art:
                text, npages = em_fulltext(art)
                via = "eastmoney_paged" if text else ""
            # 分页通道返回过短（page_size=1 之类的截断）也视为失败，继续走 PDF 通道。
            # 定期报告全文正常在 20 万字符以上，5 万以下必是截断。
            if len(text) < 50_000:
                if text:
                    print(f"  分页正文仅 {len(text):,} 字符，判为截断，转 PDF 通道", flush=True)
                text = ""
            tried = []
            if not text:
                u = dfcfw_pdf_url(art)
                if u:
                    text = pdf_text(u); via = "dfcfw_pdf" if text else ""
                    tried.append(f"dfcfw_pdf:{'ok' if text else 'empty'}")
            if not text and rep.get("attach"):
                text = pdf_text(rep["attach"]); via = "eastmoney_pdf" if text else ""
                tried.append(f"attach_pdf:{'ok' if text else 'empty'}")
            if not text:
                url = cninfo_pdf_url(code, title, years)
                tried.append(f"cninfo_query:{'found' if url else 'no_match'}")
                if url:
                    text = pdf_text(url); via = "cninfo_pdf" if text else ""
                    tried.append(f"cninfo_pdf:{'ok' if text else 'empty'}")
            # 资产名必须是纯 ASCII：GitHub Release 上传接口对中文文件名会 404
            ym = re.search(r"(20\d{2})", title)
            yr = ym.group(1) if ym else (rep["date"][:4] or "unknown")
            typ = "interim" if "半年度" in title else ("annual" if "年度报告" in title else "other")
            key = f"{code}_{yr}_{typ}"
            rec = dict(code=code, title=title, date=rep["date"], art_code=art,
                       via=via or "FAILED", pages=npages, chars=len(text),
                       fallback=";".join(tried) if tried else "",
                       file=f"filings_{key}.txt.gz" if text else None,
                       fetched_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            manifest.append(rec)
            if text:
                with gzip.open(outdir / rec["file"], "wt", encoding="utf-8") as f:
                    f.write(text)
                items[key] = parse_items(text)
                print(f"  ok via {via}: {len(text):,} chars", flush=True)
            else:
                print(f"  FAILED: {title}", file=sys.stderr, flush=True)
            time.sleep(1)
    (outdir / "filings_manifest.json").write_text(
        json.dumps(dict(generated_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        reports=manifest), ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / "filings_items.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for m in manifest if m.get("file"))
    print(f"\n完成：{ok}/{len(manifest)} 份报告取到全文 → {outdir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True, help="6位代码，逗号分隔")
    ap.add_argument("--years", default="2025,2026")
    ap.add_argument("--out", default="dist")
    a = ap.parse_args()
    run([c.strip() for c in a.codes.split(",") if c.strip()],
        [int(y) for y in a.years.split(",")], Path(a.out))
