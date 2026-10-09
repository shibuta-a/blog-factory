#!/usr/bin/env python3
"""
ネタ → Claude API で記事を自動生成 → 広告を自動挿入 → publish まで全自動で回す。

★ 渋田さんが「自動投稿オンにして」と言うまでは動かさない（毎日の自動実行は README の手順で登録する）★

動かすのに必要なもの:
  1. Claude API キー（https://console.anthropic.com/ → API Keys）を、次のどちらかに1行で書く:
         C:\\blog-factory\\.secrets\\anthropic.env   … おすすめ（.secrets/ ごと GitHub 非公開）
         C:\\blog-factory\\.env                      … 従来の場所（こちらも .gitignore 済み）
     中身:  ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
  2. topics.txt に記事ネタを1行1本で書く（上から順に、未処理のものを使う）

使い方:
  python scripts/auto-generate.py              … 1本生成して公開
  python scripts/auto-generate.py -n 3         … 3本生成して公開
  python scripts/auto-generate.py --dry-run    … 生成して content/ja/ に保存するだけ（公開しない）

記事の型:
  ネタにクリーニング・布団・家事代行などの言葉が入っていれば「クリーニング比較記事の型」
  （templates/kata/cleaning-compare.md）で書かせ、比較カードと申込みボタンの差し込み口を必ず入れる。
  広告そのもの（どの案件を出すか）は、ビルド時に data/affiliates.json から記事の内容で自動選択される。

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
KATA_CLEANING = ROOT / "templates" / "kata" / "cleaning-compare.md"

MODEL = os.environ.get("BLOG_MODEL", "claude-sonnet-5-5")  # 使うモデル（環境変数で変更可）

# この言葉がネタに入っていたら「クリーニング比較記事の型」を使う
CLEANING_WORDS = ("クリーニング", "布団", "ふとん", "家事代行", "宅配", "衣類", "保管", "コート", "ダウン", "シミ抜き")

FRONT_MATTER_RULE = """- 先頭に次の形式のフロントマターを付ける:
---
title: （32文字前後の魅力的なタイトル）
date: {date}
description: （120文字以内の要約）
slug: （英小文字とハイフンだけのURL用の名前）
image_query: （記事に合う写真を探すための英語2語。例: bedroom bed / clean kitchen / coffee cup）
---"""

# 「売れる見た目」にするための書き方（build.py がボックス・マーカー等に変換する）
STYLE_RULE = """- 見た目のルール（読みやすさのため。やりすぎない）:
  - 各見出しの中で一番大事な一文を、0〜1か所だけ ==このように== 囲む（蛍光ペン風になる）
  - 箇条書きは「- ラベル：説明」の形を使う（ラベルが自動で太字になる）
  - 補足のコツは「> [!POINT]」、注意点は「> [!WARN]」で始まる引用ブロックにする（記事全体で1〜2回まで）
  - よくある質問は、見出しにせず「Q. 質問」「A. 答え」を2行続けて書き、質問ごとに空行を入れる
  - 1段落は3文程度までに区切る"""

PROMPT_GENERAL = """あなたはプロのブログライターです。次のテーマで、読者の役に立つ日本語のブログ記事を書いてください。

テーマ: {topic}

出力ルール:
- Markdown のみを出力（前置き・後書きの説明は不要）
""" + FRONT_MATTER_RULE + """
- 本文は ## 見出しを4〜6個、合計2500〜4000字
- 事実が不確かなことは断定しない
""" + STYLE_RULE + chr(10)

PROMPT_CLEANING = """あなたはプロのブログライターです。次のテーマで、日本語の比較記事を書いてください。

テーマ: {topic}

書き手の設定: 飲食店を経営してきた運営者。忙しくて家事が回らない読者の気持ちが実感として分かる、やさしい語り口。

必ず次の「型」の見出し構成に沿って書くこと（【】や（例）の部分を中身に置き換える。説明用の <!-- --> コメントは出力しない）:

=== 型ここから ===
{kata}
=== 型ここまで ===

出力ルール:
- Markdown のみを出力（前置き・後書きの説明は不要）
""" + FRONT_MATTER_RULE + """
- 「<!-- AFFILIATE_COMPARE -->」の行は「おすすめ」の見出しの中に、「<!-- AFFILIATE_CTA -->」の行は「まとめ」の最後に、
  それぞれ1回だけ、前後に空行を入れてそのまま書く（ここに広告が自動で入る）
- 特定の会社・サービスの料金・日数・割引などの具体的な数字は書かない（不正確になるため）。一般的な相場観は「目安」として書いてよい
- 「絶対」「必ず」「業界No.1」などの断定・誇大表現は使わない
- 本文は合計3000〜4500字
""" + STYLE_RULE + chr(10)


def load_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    for env in (ROOT / ".secrets" / "anthropic.env", ROOT / ".env"):
        if key:
            break
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    return key


def is_cleaning(topic):
    return any(w in topic for w in CLEANING_WORDS)


def build_prompt(topic):
    date = datetime.date.today().isoformat()
    if is_cleaning(topic) and KATA_CLEANING.exists():
        kata = KATA_CLEANING.read_text(encoding="utf-8")
        kata = re.sub(r"\A---.*?\n---\n", "", kata, flags=re.S)                 # 型のフロントマターは除く
        kata = re.sub(r"<!--(?!\s*AFFILIATE_).*?-->\n?", "", kata, flags=re.S)  # 説明コメントは除く
        return PROMPT_CLEANING.format(topic=topic, date=date, kata=kata.strip())
    return PROMPT_GENERAL.format(topic=topic, date=date)


def ensure_affiliate_markers(md):
    """クリーニング記事で差し込み口を書き忘れていたら補う。"""
    if "<!-- AFFILIATE_COMPARE -->" not in md:
        m = re.search(r"^## .*(おすすめ|比較).*$", md, re.M)
        if m:
            md = md[: m.end()] + "\n\n<!-- AFFILIATE_COMPARE -->\n" + md[m.end():]
    if "<!-- AFFILIATE_CTA -->" not in md:
        md = md.rstrip() + "\n\n<!-- AFFILIATE_CTA -->\n"
    return md


def generate(topic, key):
    body = {
        "model": MODEL,
        "max_tokens": 10000,
        "messages": [{"role": "user", "content": build_prompt(topic)}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in data.get("content", []))
    md = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", text.strip())
    if is_cleaning(topic):
        md = ensure_affiliate_markers(md)
    return md


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1, help="生成する記事数")
    ap.add_argument("--dry-run", action="store_true", help="生成して保存するだけ（公開しない）")
    ap.add_argument("--show-prompt", metavar="TOPIC", help="APIを呼ばずに、そのネタで使うプロンプトを表示するだけ")
    args = ap.parse_args()

    if args.show_prompt:
        print(build_prompt(args.show_prompt))
        return

    key = load_api_key()
    if not key:
        sys.exit("APIキー未設定です。.secrets/anthropic.env に ANTHROPIC_API_KEY=... を書いてください（このファイル冒頭の説明参照）。")
    if not TOPICS.exists():
        sys.exit("topics.txt がありません。記事ネタを1行1本で書いてください。")

    topics = [t.strip() for t in TOPICS.read_text(encoding="utf-8").splitlines() if t.strip() and not t.startswith("#")]
    done = set(DONE.read_text(encoding="utf-8").splitlines()) if DONE.exists() else set()
    todo = [t for t in topics if t not in done][: args.n]
    if not todo:
        sys.exit("未処理のネタがありません。topics.txt に追加してください。")

    for topic in todo:
        print(f"生成中: {topic}（型: {'クリーニング比較' if is_cleaning(topic) else '通常'}）")
        md = generate(topic, key)
        m = re.search(r"^slug:\s*([a-z0-9-]+)", md, re.M)
        slug = m.group(1) if m else datetime.datetime.now().strftime("post-%Y%m%d-%H%M%S")
        out = ROOT / "content" / "ja" / f"{slug}.md"
        if out.exists():
            out = out.with_name(f"{slug}-{datetime.datetime.now():%Y%m%d%H%M}.md")
        out.write_text(md + "\n", encoding="utf-8")
        if not args.dry_run:
            with DONE.open("a", encoding="utf-8") as f:
                f.write(topic + "\n")
        print(f"保存: {out.relative_to(ROOT)}")

    if args.dry_run:
        print("--dry-run のため公開していません。")
        return
    subprocess.run([sys.executable, str(ROOT / "scripts" / "publish.py"), "-m", f"auto: {len(todo)} 記事"], check=True)


if __name__ == "__main__":
    main()
