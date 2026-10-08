#!/usr/bin/env python3
"""
【将来用の器】ネタ → Claude API で記事を自動生成 → publish まで全自動で回す。

今はまだ動かさない前提の雛形。動かすには:
  1. Claude API キーを取得（https://console.anthropic.com/ → API Keys）
  2. このフォルダの1つ上（blog-factory直下）に .env ファイルを作り、1行だけ書く:
         ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
     ★ここにAPIキーを入れればAPIで全自動量産できる★
     （.env は .gitignore 済みなので GitHub には上がらない）
  3. topics.txt に記事ネタを1行1本で書く（先頭の未処理ネタから順に使われる）
  4. python scripts/auto-generate.py         … 1本生成して公開
     python scripts/auto-generate.py -n 3    … 3本生成して公開

外部ライブラリ不要（標準ライブラリの urllib で API を呼ぶ）。
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOPICS = ROOT / "topics.txt"
DONE = ROOT / "topics_done.txt"

MODEL = os.environ.get("BLOG_MODEL", "claude-sonnet-5-5")  # 使うモデル（環境変数で変更可）

PROMPT = """あなたはプロのブログライターです。次のテーマで、読者の役に立つ日本語のブログ記事を書いてください。

テーマ: {topic}

出力ルール:
- Markdown のみを出力（前置き・後書きの説明は不要）
- 先頭に次の形式のフロントマターを付ける:
---
title: （32文字前後の魅力的なタイトル）
date: {date}
description: （120文字以内の要約）
slug: （英小文字とハイフンだけのURL用の名前）
---
- 本文は ## 見出しを4〜6個、合計2500〜4000字
- 事実が不確かなことは断定しない
"""


def load_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                key = line.split("=", 1)[1].strip()
    return key


def generate(topic, key):
    body = {
        "model": MODEL,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": PROMPT.format(topic=topic, date=datetime.date.today().isoformat())}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in data.get("content", []))
    return re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", text.strip())


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1, help="生成する記事数")
    args = ap.parse_args()

    key = load_api_key()
    if not key:
        sys.exit("APIキー未設定です。blog-factory/.env に ANTHROPIC_API_KEY=... を書いてください（このファイル冒頭の説明参照）。")
    if not TOPICS.exists():
        sys.exit("topics.txt がありません。記事ネタを1行1本で書いてください。")

    topics = [t.strip() for t in TOPICS.read_text(encoding="utf-8").splitlines() if t.strip() and not t.startswith("#")]
    done = set(DONE.read_text(encoding="utf-8").splitlines()) if DONE.exists() else set()
    todo = [t for t in topics if t not in done][: args.n]
    if not todo:
        sys.exit("未処理のネタがありません。topics.txt に追加してください。")

    for topic in todo:
        print(f"生成中: {topic}")
        md = generate(topic, key)
        m = re.search(r"^slug:\s*([a-z0-9-]+)", md, re.M)
        slug = m.group(1) if m else datetime.datetime.now().strftime("post-%Y%m%d-%H%M%S")
        out = ROOT / "content" / "ja" / f"{slug}.md"
        out.write_text(md + "\n", encoding="utf-8")
        with DONE.open("a", encoding="utf-8") as f:
            f.write(topic + "\n")
        print(f"保存: {out.relative_to(ROOT)}")

    subprocess.run([sys.executable, str(ROOT / "scripts" / "publish.py"), "-m", f"auto: {len(todo)} 記事"], check=True)


if __name__ == "__main__":
    main()
