#!/usr/bin/env python3
"""
自動化いきもの診断（/shindan/）の生き物データを用意する道具（PC で使う。Cloudflare のビルドでは使わない）。

  python scripts/shindan_tools.py wiki <ja|en> <記事名>        … Wikipedia の本文を取ってきて手元に保存（出典の確かめ用）。URL と保存先を表示
  python scripts/shindan_tools.py find <id> <学名> [ja記事名] [en記事名]
                                                              … 使える写真の候補（Wikimedia Commons・ライセンスが条件に合うものだけ）を表示
  python scripts/shindan_tools.py photo <id> "File:xxx.jpg"   … 写真を取ってきて縮小し static/shindan/img/<id>.jpg（と小さい版）に置く。
                                                                 撮影者・ライセンス・元の URL は state/shindan-photos/<id>.json に記録
  python scripts/shindan_tools.py check [data/shindan-species.json]
                                                              … データの点検（特徴の引用が出典の本文にあるか・写真のライセンス・ファイル）

■ 写真のライセンス: パブリックドメイン・CC0・CC BY・CC BY-SA だけ。NC（非営利のみ）・ND（改変禁止）・GFDL だけのものは使わない。
■ 特徴は、出典の本文にある文（quote）を必ず添える。check で本文に本当にあるかを確かめる。
"""
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "state" / "shindan-cache"
CREDITS = ROOT / "state" / "shindan-photos"
IMG = ROOT / "static" / "shindan" / "img"
UA = "blog-factory-shindan/1.0 (https://blog-factory-cf7.pages.dev/shindan/)"
ALLOWED = re.compile(r"^(cc0( 1\.0)?( universal)?|public domain|pd([- ].*)?|cc[- ]by(-sa)?[- ][0-9.]+([- ][a-z]{2,}(-[a-z]+)?)?)$", re.I)


def get(url, binary=False, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            return data if binary else json.loads(data.decode("utf-8"))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 + i * 3)


def api(host, **params):
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    return get(f"https://{host}/w/api.php?" + urllib.parse.urlencode(params))


def norm(s):
    s = html.unescape(s)
    s = re.sub(r"\s+", "", s)
    return s.replace("（", "(").replace("）", ")").replace("，", "、").replace(",", "、").lower()


def cache_path(url):
    return CACHE / (re.sub(r"[^\w.-]+", "_", urllib.parse.unquote(url).split("//", 1)[-1])[:150] + ".txt")


def wiki(lang, title):
    d = api(f"{lang}.wikipedia.org", action="query", prop="extracts", explaintext=1, redirects=1, titles=title)
    page = d["query"]["pages"][0]
    if page.get("missing"):
        raise SystemExit(f"見つかりません: {lang}:{title}")
    url = f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(page["title"].replace(" ", "_"))
    CACHE.mkdir(parents=True, exist_ok=True)
    p = cache_path(url)
    p.write_text(page["extract"], encoding="utf-8")
    return url, p


def license_ok(name):
    n = (name or "").strip()
    return bool(n) and not re.search(r"\b(nc|nd)\b|non-?commercial|no ?deriv|gfdl", n, re.I) and bool(ALLOWED.match(n))


def file_info(titles):
    d = api("commons.wikimedia.org", action="query", prop="imageinfo", iiprop="url|size|extmetadata|mime",
            iiurlwidth=1200, titles="|".join(titles))
    out = []
    for p in d["query"]["pages"]:
        ii = (p.get("imageinfo") or [{}])[0]
        md = ii.get("extmetadata") or {}
        val = lambda k: re.sub(r"<[^>]+>", "", html.unescape((md.get(k) or {}).get("value", ""))).strip()
        out.append({"file": p["title"], "page": ii.get("descriptionurl", ""), "thumb": ii.get("thumburl", ""),
                    "width": ii.get("width", 0), "height": ii.get("height", 0), "mime": ii.get("mime", ""),
                    "license": val("LicenseShortName"), "license_url": val("LicenseUrl"),
                    "author": re.sub(r"\s+", " ", val("Artist"))[:120], "desc": val("ImageDescription")[:160]})
    return out


def find(sid, sci, ja=None, en=None):
    titles = []
    for lang, t in (("ja", ja), ("en", en or sci)):
        if not t:
            continue
        try:
            d = api(f"{lang}.wikipedia.org", action="query", prop="pageimages|images", piprop="name", redirects=1,
                    imlimit=30, titles=t)
            pg = d["query"]["pages"][0]
            if pg.get("pageimage"):
                titles.append("File:" + pg["pageimage"])
            titles += [i["title"] for i in pg.get("images", []) if re.search(r"\.(jpe?g|png)$", i["title"], re.I)]
        except Exception as e:
            print(f"  ({lang} の記事から取れず: {e})")
    d = api("commons.wikimedia.org", action="query", list="search", srnamespace=6, srlimit=20,
            srsearch=f'"{sci}" filetype:bitmap')
    titles += [x["title"] for x in d["query"]["search"]]
    titles = list(dict.fromkeys(titles))[:45]
    rows = []
    for i in range(0, len(titles), 40):
        rows += file_info(titles[i:i + 40])
    ok = [r for r in rows if license_ok(r["license"]) and r["width"] >= 800 and "jpeg" in r["mime"]]
    print(f"{sid}（{sci}）: 条件に合う写真 {len(ok)} / 候補 {len(rows)}")
    for r in ok[:15]:
        print(f"  {r['file']}  [{r['license']}] {r['width']}x{r['height']}  by {r['author'][:40]}  {r['desc'][:70]}")
    return ok


def photo(sid, title):
    from io import BytesIO
    from PIL import Image
    if not title.startswith("File:"):
        title = "File:" + title
    info = file_info([title])[0]
    if not license_ok(info["license"]):
        raise SystemExit(f"使えないライセンスです: {info['license']}（{title}）")
    raw = get(info["thumb"], binary=True)
    im = Image.open(BytesIO(raw)).convert("RGB")
    IMG.mkdir(parents=True, exist_ok=True)
    for name, w in ((f"{sid}.jpg", 800), (f"{sid}-s.jpg", 320)):
        x = im.copy()
        x.thumbnail((w, w * 2))
        x.save(IMG / name, "JPEG", quality=82, optimize=True, progressive=True)
    CREDITS.mkdir(parents=True, exist_ok=True)
    rec = {"file": f"shindan/img/{sid}.jpg", "commons": title, "page": info["page"], "author": info["author"] or "不明",
           "license": info["license"], "license_url": info["license_url"], "width": im.width, "height": im.height}
    (CREDITS / f"{sid}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rec, ensure_ascii=False))
    return rec


def check(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    species = data["species"] if isinstance(data, dict) else data
    bad, seen = [], set()
    for s in species:
        sid = s.get("id", "?")
        if sid in seen:
            bad.append(f"{sid}: id が重複")
        seen.add(sid)
        if not s.get("photo") and (CREDITS / f"{sid}.json").exists():
            s["photo"] = json.loads((CREDITS / f"{sid}.json").read_text(encoding="utf-8"))
        for k in ("id", "name", "sci", "type", "facts", "likeness", "aruaru", "photo"):
            if not s.get(k):
                bad.append(f"{sid}: {k} がない")
        if not 2 <= len(s.get("facts", [])) <= 3:
            bad.append(f"{sid}: 特徴は2〜3個")
        if len(s.get("aruaru", [])) != 3:
            bad.append(f"{sid}: あるあるは3つ")
        for f in s.get("facts", []):
            src = f.get("source", "")
            cp = cache_path(src)
            if not cp.exists():
                bad.append(f"{sid}: 出典の本文が手元にない（wiki で取る）: {src}")
                continue
            if norm(f.get("quote", "")) not in norm(cp.read_text(encoding="utf-8")) or len(norm(f.get("quote", ""))) < 6:
                bad.append(f"{sid}: 引用が出典の本文に見つからない: {f.get('quote', '')[:40]}")
        ph = s.get("photo") or {}
        if not license_ok(ph.get("license", "")):
            bad.append(f"{sid}: 写真のライセンスが条件外: {ph.get('license')}")
        if not (ROOT / "static" / (ph.get("file") or "x")).exists():
            bad.append(f"{sid}: 写真のファイルがない")
    cells = {}
    for s in species:
        c = s.get("cell") or {}
        key = "rare" if s.get("rare") else f"{c.get('lazy')}-{c.get('scene')}-{c.get('style')}"
        cells.setdefault(s.get("type"), []).append(key)
    want = {f"{l}-{w}-{t}" for l in ("low", "mid", "high") for w in ("work", "life") for t in ("solo", "team")}
    for t, keys in cells.items():
        if set(keys) - {"rare"} != want or len(keys) != len(set(keys)):
            bad.append(f"型 {t}: 12のマス（lazy×scene×style）に1種ずつになっていない: {sorted(keys)}")
    print(f"{len(species)} 種を点検: 問題 {len(bad)} 件")
    for b in bad:
        print("  " + b)
    return 1 if bad else 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 0
    if a[0] == "wiki":
        url, p = wiki(a[1], a[2])
        print(url)
        print(p)
    elif a[0] == "find":
        find(a[1], a[2], *(a[3:5]))
    elif a[0] == "photo":
        photo(a[1], a[2])
    elif a[0] == "check":
        return check(a[1] if len(a) > 1 else ROOT / "data" / "shindan-species.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
