#!/usr/bin/env python3
"""
記事の写真（アイキャッチ＋本文中2枚）を、著作権フリーの素材から自動で用意する。

  python scripts/images.py                 … 写真がまだない記事すべてに用意する
  python scripts/images.py <slug> ...      … 指定した記事だけ
  python scripts/images.py <slug> --redo   … 選び直す（今の写真を捨てて次の候補へ）
  python scripts/images.py --sheet <slug> <role> [--query 英語]  … 候補の一覧画像を作る（目で選ぶとき用）
  python scripts/images.py --pick <slug> <role> <番号> … 一覧画像の番号で差し替える

■ 取得元と規約
  Openverse（https://openverse.org / WordPress財団運営）の API を使う。APIキー不要。
  ライセンスは CC0（著作権放棄）と Public Domain Mark だけに絞る
  → 商用利用可・改変可・クレジット表記不要。念のため記事末尾に出典を自動で表記する。
  画像はダウンロードして自サイト（static/img/）に置く（ホットリンクはしない）。

■ どんな写真を探すか（英語で検索する）
  記事のフロントマターに書けば優先:
    image_query: futon bedding           … アイキャッチ
    image_queries: laundry | closet      … 本文中の写真（| 区切りで最大2つ）
  書いていなければ、タイトルの言葉から下の QUERY_MAP で自動で決める。

保存先: static/img/<slug>/{eyecatch,thumb,body-1,body-2}.jpg と data/images.json（出典の台帳）
Pillow が必要（ローカルの publish 時だけ使う。Cloudflare のビルドでは使わない）。
"""
import argparse
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
STATIC_IMG = ROOT / "static" / "img"
MANIFEST = ROOT / "data" / "images.json"
API = "https://api.openverse.org/v1/images/"
UA = "blog-factory/1.0 (+https://blog-factory-cf7.pages.dev)"

# 日本語のテーマ → 英語の検索語（アイキャッチ, 本文1, 本文2）。上から順に、最初に当たったものを使う
QUERY_MAP = [
    (("布団", "ふとん"), ("bedroom bed duvet", "white pillows bed", "bedroom morning light")),
    (("家事代行", "家事"), ("clean kitchen home", "mop floor cleaning", "cooking vegetables kitchen")),
    (("宅配クリーニング", "クリーニング", "衣類"), ("clothes rack shirts", "folded sweaters", "wardrobe closet clothes")),
    (("コーヒー",), ("coffee cup", "barista coffee", "coffee beans")),
]
DEFAULT_QUERIES = ("desk notebook laptop", "workspace", "books")

ROLES = {  # 役割: (幅, 高さ)
    "eyecatch": (1200, 675),
    "body-1": (960, 540),
    "body-2": (960, 540),
}
THUMB = (480, 270)
# 自動で選ぶのは、人の手で選別されたストック写真サイトだけ（Flickr 等の投稿写真は、
# mature=false でも不適切な写真が混じることがあるため自動では使わない。--sheet では全ソースから選べる）
AUTO_SOURCES = "stocksnap,rawpixel"
SKIP_WORDS = ("sexy", "amateur", "lingerie", "bikini", "underwear", "nsfw", "erotic", "boudoir", "drawing", "illustration", "print", "engraving", "vintage", "pattern", "background",
              "logo", "poster", "map", "diagram", "text", "advert", "nude", "museum", "rp-p")


def log(msg):
    print(msg, flush=True)


def load_manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}


def save_manifest(m):
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def front_matter(path):
    text = path.read_text(encoding="utf-8").lstrip("﻿")
    meta = {}
    if text.startswith("---"):
        body = text.split("\n", 1)[1]
        end = re.search(r"^---\s*$", body, re.M)
        for line in body[: end.start() if end else 0].splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


def slug_of(path, meta):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", meta.get("slug") or path.stem).strip("-") or "post"


def queries_for(meta):
    title = meta.get("title", "")
    base = next((q for words, q in QUERY_MAP if any(w in title for w in words)), DEFAULT_QUERIES)
    q = list(base)
    if meta.get("image_query"):
        q[0] = meta["image_query"]
    extra = [s.strip() for s in (meta.get("image_queries") or "").split("|") if s.strip()]
    for i, s in enumerate(extra[:2]):
        q[i + 1] = s
    return dict(zip(ROLES, q))


def search(query, page_size=20, sources=AUTO_SOURCES):
    params = {"q": query, "license": "cc0,pdm", "category": "photograph", "aspect_ratio": "wide",
              "mature": "false", "page_size": page_size}
    if sources:
        params["source"] = sources
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        results = json.loads(r.read().decode("utf-8")).get("results", [])
    time.sleep(1.5)  # 無料APIなので間隔をあける
    out = []
    for x in results:
        t = (x.get("title") or "").lower()
        if any(w in t for w in SKIP_WORDS):
            continue
        if (x.get("width") or 0) < 900:
            continue
        out.append(x)
    return out


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return Image.open(io.BytesIO(r.read())).convert("RGB")


def save_cropped(img, size, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    ImageOps.fit(img, size, Image.LANCZOS).save(path, "JPEG", quality=78, optimize=True, progressive=True)


def credit_of(x):
    return {
        "title": x.get("title") or "",
        "creator": x.get("creator") or "",
        "license": (x.get("license") or "").upper(),
        "source": x.get("source") or x.get("provider") or "",
        "landing_url": x.get("foreign_landing_url") or "",
        "openverse_id": x.get("id"),
    }


def fetch_role(slug, role, query, used_ids, skip_ids=()):
    cands = search(query)
    if not cands and " " in query:  # ストック写真のタイトルは短いので、当たらなければ最初の1語で探し直す
        cands = search(query.split()[0])
    for x in cands:
        if x["id"] in used_ids or x["id"] in skip_ids:
            continue
        try:
            img = download(x["url"])
        except Exception as e:
            log(f"    取得失敗（次の候補へ）: {e.__class__.__name__}")
            continue
        save_cropped(img, ROLES[role], STATIC_IMG / slug / f"{role}.jpg")
        if role == "eyecatch":
            save_cropped(img, THUMB, STATIC_IMG / slug / "thumb.jpg")
        used_ids.add(x["id"])
        return {"file": f"img/{slug}/{role}.jpg", "query": query, **credit_of(x)}
    return None


def no_images(meta):
    """genres.json で images: none のジャンル（ポケカなど、カード・キャラクター画像が混じりやすいもの）は写真を付けない。"""
    f = ROOT / "data" / "genres.json"
    genres = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    return (genres.get(meta.get("genre", "")) or {}).get("images") == "none" if isinstance(genres, dict) else False


def prepare(md, redo=False):
    meta = front_matter(md)
    slug = slug_of(md, meta)
    if no_images(meta):
        return False
    manifest = load_manifest()
    entry = manifest.get(slug, {})
    if entry and all(r in entry for r in ROLES) and not redo:
        return False
    log(f"写真を用意: {slug}")
    used = {v.get("openverse_id") for k, v in entry.items() if isinstance(v, dict) and not redo}
    skipped = set(entry.get("_skipped", []))
    if redo:
        skipped |= {v.get("openverse_id") for v in entry.values() if isinstance(v, dict)}
        entry = {"_skipped": sorted(skipped)}
    for role, query in queries_for(meta).items():
        if role in entry:
            continue
        got = fetch_role(slug, role, query, used, skipped)
        if got:
            entry[role] = got
            log(f"  {role}: 「{query}」→ {got['title'][:50]}（{got['license']} / {got['source']}）")
        else:
            log(f"  {role}: 「{query}」で使える写真が見つからず")
    manifest[slug] = entry
    save_manifest(manifest)
    return True


def contact_sheet(slug, role, query=None):
    """候補を番号付きで1枚に並べる（目で選ぶとき用）。"""
    md = next(p for p in CONTENT.rglob("*.md") if slug_of(p, front_matter(p)) == slug)
    query = query or queries_for(front_matter(md))[role]
    cands = search(query, 20)  # まずは選別済みのストック写真サイトから
    if len(cands) < 4:
        cands += search(query, 20, sources=None)
    cands = cands[:12]
    sheet = Image.new("RGB", (4 * 320, 3 * 200), "white")
    for i, x in enumerate(cands):
        try:
            img = ImageOps.fit(download(x["thumbnail"] or x["url"]), (316, 178))
        except Exception:
            continue
        sheet.paste(img, ((i % 4) * 320 + 2, (i // 4) * 200 + 2))
        ImageDraw.Draw(sheet).text(((i % 4) * 320 + 6, (i // 4) * 200 + 182), f"{i}: {x['title'][:38]}", fill="black")
    out = ROOT / ".secrets" / "shots" / f"sheet-{slug}-{role}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    (out.with_suffix(".json")).write_text(json.dumps(cands, ensure_ascii=False), encoding="utf-8")
    log(f"候補一覧（検索「{query}」）: {out}")


def pick(slug, role, index):
    cands = json.loads((ROOT / ".secrets" / "shots" / f"sheet-{slug}-{role}.json").read_text(encoding="utf-8"))
    x = cands[index]
    img = download(x["url"])
    save_cropped(img, ROLES[role], STATIC_IMG / slug / f"{role}.jpg")
    if role == "eyecatch":
        save_cropped(img, THUMB, STATIC_IMG / slug / "thumb.jpg")
    manifest = load_manifest()
    old = manifest.setdefault(slug, {}).get(role, {})
    manifest[slug][role] = {"file": f"img/{slug}/{role}.jpg", "query": old.get("query", ""), **credit_of(x)}
    save_manifest(manifest)
    log(f"{slug} {role} → {x['title']}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--sheet", nargs=2, metavar=("SLUG", "ROLE"))
    ap.add_argument("--query", help="--sheet で使う検索語（省略時は記事から自動）")
    ap.add_argument("--pick", nargs=3, metavar=("SLUG", "ROLE", "N"))
    a = ap.parse_args()
    if a.sheet:
        return contact_sheet(*a.sheet, query=a.query)
    if a.pick:
        return pick(a.pick[0], a.pick[1], int(a.pick[2]))
    for md in sorted(CONTENT.rglob("*.md")):
        meta = front_matter(md)
        if a.slugs and slug_of(md, meta) not in a.slugs:
            continue
        try:
            prepare(md, a.redo)
        except Exception as e:  # 写真が取れなくても記事の公開は止めない
            log(f"  写真の用意に失敗（写真なしで続行）: {md.name}: {e}")


if __name__ == "__main__":
    main()
