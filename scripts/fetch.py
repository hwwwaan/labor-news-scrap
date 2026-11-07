#!/usr/bin/env python3
"""고용관계론 기사 스크랩 — 노동 기사 RSS 를 모아 장별로 나누고 site/articles.json 에 쌓는다.

python3 scripts/fetch.py            # 모아서 저장
python3 scripts/fetch.py --dry-run  # 저장하지 않고 몇 건 들어오는지만

- 깃허브 Actions 가 매일 아침 7시(한국)에 돌리고, 바뀐 site/articles.json 을 커밋한다.
  화면(site/index.html)은 그 파일을 깃허브에서 바로 읽으므로 Netlify 는 다시 배포하지 않는다.
- 표준 라이브러리만 쓴다. Actions 에서 아무것도 설치하지 않고 돈다.
- 기사 본문과 RSS 요약은 저장하지 않는다(저작권). 제목·언론사·날짜·링크·장 표시만 남긴다.
  요약은 장을 고를 때만 읽고 버린다.
- 장 핵심어와 구글뉴스 검색어는 chapters.json 에서 고친다. 이미 쌓인 기사는 다시 나누지 않는다.
"""
import html
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "site" / "articles.json"
CHAPTERS = json.loads((ROOT / "chapters.json").read_text(encoding="utf-8"))["chapters"]
KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
KEEP_DAYS = 150          # 이보다 오래된 기사는 목록에서 뺀다(한 학기 + 여유). 늘리면 화면이 그만큼 무거워진다
# 구글뉴스가 한국어로 번역해 끼워 넣는 외국 매체(베트남 노동조합 기사 등). 출처 주소에 이 글자가 있으면 뺀다
BLOCK_SOURCES = (".vn/", ".vn", "vietnam", "laodong", "mezha.net", "indexbox")
MAX_TAGS = 3             # 기사 하나에 붙이는 장 수

# 노동 전문 매체라 기사 전부를 받는다. 장에 안 걸리면 「장 미분류」로 남는다.
FEEDS = [
    ("매일노동뉴스", "https://www.labortoday.co.kr/rss/allArticle.xml"),
    ("참여와혁신", "https://www.laborplus.co.kr/rss/allArticle.xml"),
]


def google_url(q):
    return ("https://news.google.com/rss/search?q=" + quote(f"{q} when:7d")
            + "&hl=ko&gl=KR&ceid=KR:ko")


def get(url):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml"})
    with urlopen(req, timeout=20) as r:
        return r.read()


def text_of(el, tag):
    node = el.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    d = None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try:
                d = datetime.strptime(s, fmt)
                break
            except ValueError:
                pass
        if d is None:
            return None
        if d.tzinfo is None:            # 한국 매체 RSS 는 시간대 없이 한국 시각을 준다
            d = d.replace(tzinfo=KST)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(KST)


def title_key(t):
    return re.sub(r"[\W_]+", "", t).lower()


def classify(title, body, hint=None):
    """장마다 걸린 말을 세어 점수가 높은 장부터 MAX_TAGS 개. 제목에 걸리면 2점, 요약에만 걸리면 1점.
    hint 는 구글뉴스 검색어를 건 장 — 본문에서 걸렸을 수 있어 1점을 더하고, 걸린 말이 없으면 검색어를 대신 적는다."""
    hint = hint or {}
    scored = []
    for ch in CHAPTERS:
        hits = [k for k in ch["keywords"] if k in title or k in body]
        score = sum(2 if k in title else 1 for k in hits)
        if ch["num"] in hint:
            score += 1
            if not hits:
                hits = [hint[ch["num"]].replace('"', "")]
        if score:
            hits.sort(key=lambda k: (k not in title, -len(k)))
            scored.append((score, ch["num"], hits[:3]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"ch": n, "kw": kw} for _, n, kw in scored[:MAX_TAGS]]


def items_from(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return root.iter("item")


def collect():
    got, log = [], []
    for outlet, url in FEEDS:
        try:
            n = 0
            for it in items_from(get(url)):
                title = strip_tags(text_of(it, "title"))
                link = text_of(it, "link")
                if not title or not link:
                    continue
                got.append({
                    "title": title, "url": link, "outlet": outlet, "via": "rss",
                    "date": parse_date(text_of(it, "pubDate")),
                    "tags": classify(title, strip_tags(text_of(it, "description"))),
                })
                n += 1
            log.append(f"{outlet} {n}건")
        except Exception as e:  # 한 곳이 막혀도 나머지는 받는다
            log.append(f"{outlet} 실패({type(e).__name__})")
    for ch in CHAPTERS:
        for q in ch.get("queries", []):
            try:
                n = 0
                for it in items_from(get(google_url(q))):
                    title = strip_tags(text_of(it, "title"))
                    src = it.find("source")
                    src_url = (src.get("url") or "").lower() if src is not None else ""
                    if any(b in src_url for b in BLOCK_SOURCES):
                        continue
                    outlet = (src.text or "").strip() if src is not None and src.text else ""
                    outlet = re.sub(r"^https?://|^www\.|/$", "", outlet).removeprefix("www.").rstrip("/")
                    if outlet and title.endswith(" - " + outlet):
                        title = title[: -len(" - " + outlet)].strip()
                    link = text_of(it, "link")
                    if not title or not link:
                        continue
                    got.append({
                        "title": title, "url": link, "outlet": outlet or "구글뉴스", "via": "google",
                        "date": parse_date(text_of(it, "pubDate")),
                        "tags": classify(title, "", {ch["num"]: q}),
                    })
                    n += 1
                log.append(f"구글 「{q}」 {n}건")
            except Exception as e:
                log.append(f"구글 「{q}」 실패({type(e).__name__})")
            time.sleep(0.6)
    return got, log


def merge_tags(a, b):
    out = {t["ch"]: t for t in a}
    for t in b:
        if t["ch"] in out:
            kw = out[t["ch"]]["kw"] + [k for k in t["kw"] if k not in out[t["ch"]]["kw"]]
            out[t["ch"]] = {"ch": t["ch"], "kw": kw[:3]}
        else:
            out[t["ch"]] = t
    order = [t["ch"] for t in a] + [t["ch"] for t in b if t["ch"] not in {x["ch"] for x in a}]
    return [out[c] for c in order][:MAX_TAGS]


def main():
    dry = "--dry-run" in sys.argv
    now = datetime.now(KST)
    old = json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else {"articles": []}
    by_key = {title_key(a["title"]): a for a in old["articles"]}

    got, log = collect()
    if not got:
        print("받은 기사가 하나도 없어 저장하지 않습니다:", " · ".join(log))
        sys.exit(1)

    added = 0
    for g in got:
        key = title_key(g["title"])
        if len(key) < 6:
            continue
        date = g["date"] or now
        if date > now + timedelta(hours=1):
            date = now
        if key in by_key:
            a = by_key[key]
            a["tags"] = merge_tags(a["tags"], g["tags"])
            if a["via"] == "google" and g["via"] == "rss":   # 언론사 원문 주소가 있으면 그쪽을 쓴다
                a.update(url=g["url"], outlet=g["outlet"], via="rss")
            continue
        by_key[key] = {
            "id": hashlib.sha1(key.encode()).hexdigest()[:10],
            "title": g["title"], "url": g["url"], "outlet": g["outlet"], "via": g["via"],
            "date": date.isoformat(timespec="minutes"),
            "seen": now.date().isoformat(),
            "tags": g["tags"],
        }
        added += 1

    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
    arts = sorted((a for a in by_key.values() if a["date"] >= cutoff), key=lambda a: a["date"], reverse=True)
    print(" · ".join(log))
    print(f"새 기사 {added}건 · 전체 {len(arts)}건")
    if dry:
        return
    out = {
        "updated": now.isoformat(timespec="minutes"),
        "chapters": [{"num": c["num"], "title": c["title"]} for c in CHAPTERS],
        "sources": [f for f, _ in FEEDS] + ["구글 뉴스 검색"],
        "articles": arts,
    }
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(out, ensure_ascii=False, indent=0, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
