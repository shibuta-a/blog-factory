#!/usr/bin/env python3
"""
楽天市場の商品リンクを、記事の内容に合わせて自動で用意する（楽天ウェブサービス「楽天市場商品検索API」・正式な窓口）。

  python scripts/rakuten.py                  … 商品がまだない（または30日より古い）記事すべてに用意する
  python scripts/rakuten.py <slug> ...       … 指定した記事だけ（--redo で取り直す）
  python scripts/rakuten.py --test "検索語"   … キーの確認。検索して上位の商品を表示するだけ（台帳は変えない）

■ どの記事に入るか
  記事のフロントマターに検索語を書いた記事だけ（| 区切りで最大3つ。1つの検索語から1商品ずつ選ぶ）:
    rakuten: トレカ スリーブ | カードファイル 収納 | トレカ ローダー
  rakuten: none で入れない。注意喚起ジャンル（genres.json の ads: none）には入れない。

■ 台帳: data/rakuten.json（GitHub に上げる。Cloudflare のビルドはここから表示するだけで、API は呼ばない）
    { slug: { "queries": [...], "fetched_at": "YYYY-MM-DD", "items": [{name, url, image, shop, query}] } }
  build.py が本文の <!-- RAKUTEN --> の位置（なければ「よくある質問」か「まとめ」の直前）に商品の枠を入れる。
  価格・レビューは変わるので表示しない（「楽天市場で価格を見る」ボタンにする）。

■ 商品の選び方: 検索結果（在庫あり・画像あり）から、genres.json の rakuten_ng と下の NG 語を含む商品を除き、
  検索語を全部含む商品を、楽天の検索順（関連の強い順）で。レビューが3件未満の商品は後回し。同じショップ・同じ商品は重ねない。

■ キー: .secrets/rakuten.json（GitHub に上げない）
    {"application_id": "...", "access_key": "...", "affiliate_id": "...", "origin": "https://blog-factory-cf7.pages.dev"}
  キーがなければ何もせずに終わる（公開は止めない）。
"""
import argparse
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
LEDGER = ROOT / "data" / "rakuten.json"
KEYS = ROOT / ".secrets" / "rakuten.json"
API = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
MAX_QUERIES = 3
REFRESH_DAYS = 30
WAIT = 1.2  # 1秒に1回まで（楽天の上限を守る）
NG = ("中古", "訳あり", "ジャンク", "福袋", "まとめ売り", "転売", "せどり", "情報商材", "アダルト", "18禁")


def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def front_matter(path):
    text = path.read_text(encoding="utf-8").lstrip("﻿")
    meta = {}
    if text.startswith("---"):
        rest = text.split("\n", 1)[1]
        end = re.search(r"^---\s*$", rest, re.M)
        for line in rest[: end.start() if end else 0].splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


def slug_of(path, meta):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", meta.get("slug") or path.stem).strip("-") or "post"


def genres():
    g = load_json(ROOT / "data" / "genres.json", {})
    return {k: v for k, v in g.items() if not k.startswith("_") and isinstance(v, dict)}


def queries_of(meta):
    q = (meta.get("rakuten") or "").strip()
    if not q or q.lower() in ("none", "no", "off", "なし"):
        return []
    if (genres().get(meta.get("genre", "")) or {}).get("ads") == "none":
        return []
    return [x.strip() for x in q.split("|") if x.strip()][:MAX_QUERIES]


def search(keys, keyword, hits=20):
    params = {"applicationId": keys["application_id"], "accessKey": keys["access_key"],
              "affiliateId": keys["affiliate_id"], "keyword": keyword, "hits": hits, "imageFlag": 1,
              "availability": 1, "sort": "standard", "format": "json", "formatVersion": 2}
    origin = keys.get("origin", "https://blog-factory-cf7.pages.dev")
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params), headers={
        "Origin": origin, "Referer": origin + "/", "accessKey": keys["access_key"], "User-Agent": "blog-factory/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"楽天API エラー {e.code}: {body}") from None
    items = []
    for it in data.get("Items", []):
        it = it.get("Item", it)  # formatVersion 1 / 2 のどちらでも読む
        imgs = it.get("mediumImageUrls") or []
        img = imgs[0] if imgs else ""
        img = img.get("imageUrl", "") if isinstance(img, dict) else img
        items.append({"name": it.get("itemName", ""), "url": it.get("affiliateUrl") or it.get("itemUrl", ""),
                      "image": re.sub(r"\?_ex=\d+x\d+", "?_ex=300x300", img), "shop": it.get("shopName", ""),
                      "code": it.get("itemCode", ""), "reviews": int(it.get("reviewCount") or 0)})
    return items


def clean_name(name):
    """楽天の商品名は宣伝文句が長いので、【】や★などを外して短くする。"""
    name = re.sub(r"[【\[［(（<＜《][^】\]］)）>＞》]{0,40}[】\]］)）>＞》]", " ", name)
    name = re.sub(r"(楽天)?ランキング\s*\d+\s*冠?位?(獲得|受賞)?|送料無料|ポイント\s*\d+\s*倍|\d+%\s*OFF|クーポン(配布中|あり)?|あす楽|期間限定|SALE|セール", " ", name, flags=re.I)
    name = re.sub(r"[★☆◆◇■□●○♪！!※＼／]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return (name[:48] + "…") if len(name) > 48 else name


def choose(cands, ng, used_codes, used_shops, query=""):
    ok = [c for c in cands if c["url"] and c["code"] not in used_codes
          and not any(w in c["name"] for w in NG + tuple(ng))]
    words = query.split()
    # 検索語を全部含む商品を先に。その中は楽天の検索順（＝関連の強い順）のまま。レビューがほとんどない商品は後回し
    ok = [dict(c, rank=i) for i, c in enumerate(ok)]
    ok.sort(key=lambda c: (-sum(w.lower() in c["name"].lower() for w in words), c["reviews"] < 3, c["rank"]))
    for c in ok:
        if c["shop"] not in used_shops:
            return c
    return ok[0] if ok else None


def prepare(md, keys, redo=False):
    meta = front_matter(md)
    slug = slug_of(md, meta)
    qs = queries_of(meta)
    ledger = load_json(LEDGER, {})
    if not qs:
        if slug in ledger:  # 検索語を消した記事は商品も外す
            ledger.pop(slug)
            LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return False
    entry = ledger.get(slug, {})
    fresh = entry.get("fetched_at", "") >= (datetime.date.today() - datetime.timedelta(days=REFRESH_DAYS)).isoformat()
    if entry.get("queries") == qs and fresh and not redo:
        return False
    ng = (genres().get(meta.get("genre", "")) or {}).get("rakuten_ng", [])
    items, codes, shops = [], set(), set()
    for q in qs:
        try:
            c = choose(search(keys, q), ng, codes, shops, q)
        except RuntimeError as e:
            log(f"  {slug}: 「{q}」{e}")
            return False
        time.sleep(WAIT)
        if not c:
            log(f"  {slug}: 「{q}」で合う商品が見つからず")
            continue
        codes.add(c["code"])
        shops.add(c["shop"])
        items.append({"name": clean_name(c["name"]), "url": c["url"], "image": c["image"], "shop": c["shop"], "query": q})
        log(f"  {slug}: 「{q}」→ {clean_name(c['name'])}（{c['shop']}）")
    ledger[slug] = {"queries": qs, "fetched_at": datetime.date.today().isoformat(), "items": items}
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return True


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--test")
    a = ap.parse_args()
    keys = load_json(KEYS, {})
    if not all(keys.get(k) for k in ("application_id", "access_key", "affiliate_id")):
        log("楽天のキー（.secrets/rakuten.json）がないため、楽天の商品リンクはスキップしました。")
        return 0
    if a.test:
        for c in search(keys, a.test, 10)[:5]:
            log(f"- {clean_name(c['name'])}（{c['shop']} / レビュー {c['reviews']}件）\n  {c['url'][:90]}")
        return 0
    # 記事を消したら台帳からも外す
    ledger = load_json(LEDGER, {})
    alive = {slug_of(md, front_matter(md)) for md in CONTENT.rglob("*.md")}
    if any(s not in alive for s in ledger):
        LEDGER.write_text(json.dumps({s: v for s, v in ledger.items() if s in alive}, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")
    for md in sorted(CONTENT.rglob("*.md")):
        meta = front_matter(md)
        if meta.get("draft", "").lower() in ("true", "yes", "1"):
            continue
        if a.slugs and slug_of(md, meta) not in a.slugs:
            continue
        prepare(md, keys, redo=a.redo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
