#!/usr/bin/env python3
"""
ブログ工場 ビルドスクリプト（Python標準ライブラリのみ・外部依存なし）

  content/<言語>/*.md  ──(templates/*.html に流し込む)──>  public/

- 既定言語(ja)は public/ 直下、それ以外の言語は public/<言語>/ に出力
- slots/*.html の中身（広告・アフィリタグ）を全ページに自動差し込み
- static/ の中身（画像など）は public/ にそのままコピー
- data/affiliates.json（A8.net の提携台帳）から、記事の内容に合う広告を自動で差し込む

使い方:  python scripts/build.py
"""
import datetime
import html
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import affiliate  # noqa: E402  記事の内容に合わせた広告の自動差し込み

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
TEMPLATES = ROOT / "templates"
SLOTS = ROOT / "slots"
STATIC = ROOT / "static"
PUBLIC = ROOT / "public"


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------
def parse_front_matter(text):
    """先頭の --- で囲まれた key: value を読み取る。"""
    text = text.lstrip("﻿")
    meta = {}
    if text.startswith("---"):
        parts = text.split("\n", 1)
        rest = parts[1] if len(parts) > 1 else ""
        end = re.search(r"^---\s*$", rest, re.M)
        if end:
            for line in rest[: end.start()].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"').strip("'")
            text = rest[end.end():].lstrip("\n")
    return meta, text


# ---------------------------------------------------------------------------
# Minimal Markdown -> HTML
# ---------------------------------------------------------------------------
def inline(text):
    codes = []

    def keep_code(m):
        codes.append("<code>" + html.escape(m.group(1)) + "</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", keep_code, text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)",
                  lambda m: f'<img src="{m.group(2)}" alt="{html.escape(m.group(1))}" loading="lazy">', text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)",
                  lambda m: f'<a href="{m.group(2)}"'
                  + (' target="_blank" rel="noopener"' if m.group(2).startswith("http") else "")
                  + f">{m.group(1)}</a>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
HTML_BLOCK_RE = re.compile(r"^\s*<(/?(div|p|table|iframe|script|ins|section|figure|aside|ul|ol|blockquote|details|style)\b|!--)", re.I)


def render_list(items):
    """items: [(indent, ordered, text)] -> ネスト対応のリストHTML"""
    out = []
    stack = []  # (indent, tag)
    for indent, ordered, text in items:
        tag = "ol" if ordered else "ul"
        if not stack or indent > stack[-1][0]:
            out.append(f"<{tag}>")
            stack.append((indent, tag))
        else:
            while len(stack) > 1 and indent < stack[-1][0]:
                out.append(f"</li></{stack.pop()[1]}>")
            out.append("</li>")
        out.append("<li>" + inline(text))
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def markdown(text, headings=None):
    """Markdown を HTML に変換。headings を渡すと (level, id, text) を集める。"""
    lines = text.replace("\r\n", "\n").split("\n")
    out = []
    i = 0
    n = len(lines)

    def is_block_start(l):
        return (not l.strip() or l.startswith("#") or l.startswith("```") or l.startswith(">")
                or LIST_RE.match(l) or HTML_BLOCK_RE.match(l)
                or re.match(r"^\s*(\*\*\*+|---+|___+)\s*$", l))

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # fenced code
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(buf))}</code></pre>")
            continue

        # heading
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            level = len(m.group(1))
            content = inline(m.group(2))
            if headings is not None and level in (2, 3):
                hid = f"h-{len(headings) + 1}"
                headings.append((level, hid, re.sub(r"<[^>]+>", "", content)))
                out.append(f'<h{level} id="{hid}">{content}</h{level}>')
            else:
                out.append(f"<h{level}>{content}</h{level}>")
            i += 1
            continue

        # horizontal rule
        if re.match(r"^\s*(\*\*\*+|---+|___+)\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # blockquote
        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + markdown("\n".join(buf)) + "</blockquote>")
            continue

        # table
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$", lines[i + 1]):
            head = split_row(line)
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(split_row(lines[i]))
                i += 1
            t = ["<table><thead><tr>"] + [f"<th>{inline(c)}</th>" for c in head] + ["</tr></thead><tbody>"]
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue

        # list
        if LIST_RE.match(line):
            items = []
            while i < n:
                lm = LIST_RE.match(lines[i])
                if lm:
                    items.append([len(lm.group(1).expandtabs(4)), lm.group(2)[0].isdigit(), lm.group(3)])
                    i += 1
                elif lines[i].strip() and lines[i].startswith((" ", "\t")) and items:
                    items[-1][2] += " " + lines[i].strip()  # 継続行
                    i += 1
                else:
                    break
            out.append(render_list(items))
            continue

        # raw HTML block (広告タグ等をそのまま書ける)
        if HTML_BLOCK_RE.match(line):
            buf = []
            while i < n and lines[i].strip():
                buf.append(lines[i])
                i += 1
            out.append("\n".join(buf))
            continue

        # paragraph
        buf = [stripped]
        i += 1
        while i < n and not is_block_start(lines[i]):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>" + "<br>\n".join(inline(b) for b in buf) + "</p>")

    return "\n".join(out)


def build_toc(headings):
    if not headings:
        return ""
    out = ["<ol>"]
    in_sub = False
    for idx, (level, hid, text) in enumerate(headings):
        link = f'<a href="#{hid}">{text}</a>'
        if level == 3 and idx > 0:
            if not in_sub:
                out.append("<ol>")
                in_sub = True
            out.append(f"<li>{link}</li>")
        else:
            if in_sub:
                out.append("</ol>")
                in_sub = False
            if idx > 0:
                out.append("</li>")
            out.append(f"<li>{link}")
    if in_sub:
        out.append("</ol>")
    out.append("</li></ol>")
    return "".join(out)


def insert_middle_ad(body, ad_html):
    """本文のちょうど真ん中あたりの <h2> の直前に AD_SLOT_MIDDLE を差し込む。"""
    positions = [m.start() for m in re.finditer(r"<h2[ >]", body)]
    marker = "<!-- AD_SLOT_MIDDLE -->\n" + ad_html + "\n"
    if len(positions) >= 2:
        p = positions[len(positions) // 2]
        return body[:p] + marker + body[p:]
    paras = [m.end() for m in re.finditer(r"</p>", body)]
    if len(paras) >= 4:
        p = paras[len(paras) // 2 - 1]
        return body[:p] + "\n" + marker + body[p:]
    return body + "\n" + marker


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(template, values):
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), "")), template)


def load_slot(name, css_class):
    f = SLOTS / f"{name}.html"
    if not f.exists():
        return ""
    raw = f.read_text(encoding="utf-8")
    # slot ファイル内の説明コメントは除いて、中身が空なら何も出さない
    content = re.sub(r"<!--.*?-->", "", raw, flags=re.S).strip()
    if not content:
        return ""
    return f'<div class="{css_class}">{content}</div>' if css_class else content


def excerpt_of(body_html, length=90):
    text = re.sub(r"<[^>]+>", "", body_html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:length] + ("…" if len(text) > length else "")


def main():
    config = json.loads((ROOT / "site.json").read_text(encoding="utf-8"))
    default_lang = config.get("default_lang", "ja")
    base_url = config.get("base_url", "").rstrip("/")
    langs = config["languages"]

    tpl_article = (TEMPLATES / "article.html").read_text(encoding="utf-8")
    tpl_index = (TEMPLATES / "index.html").read_text(encoding="utf-8")

    programs = affiliate.load_ledger()
    print(f"広告台帳: 提携中 {len(programs)} 件")

    slots = {
        "slot_head": load_slot("head", None),
        "slot_ad_top": load_slot("ad_top", "ad-slot"),
        "slot_ad_middle": load_slot("ad_middle", "ad-slot"),
        "slot_affiliate": load_slot("affiliate", "affiliate-slot"),
    }

    # public は毎回作り直す（中身は全部自動生成物）
    if PUBLIC.exists():
        shutil.rmtree(PUBLIC)
    PUBLIC.mkdir(parents=True)
    (PUBLIC / "assets").mkdir()
    shutil.copy(TEMPLATES / "style.css", PUBLIC / "assets" / "style.css")
    if STATIC.exists():
        shutil.copytree(STATIC, PUBLIC, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".gitkeep"))

    # 記事がある言語だけ出力する
    active = [l for l in langs if (CONTENT / l).is_dir() and any((CONTENT / l).glob("*.md"))]
    if default_lang not in active:
        active.insert(0, default_lang)

    today = datetime.date.today().isoformat()
    year = datetime.date.today().year
    sitemap_urls = []
    total = 0

    for lang in active:
        L = langs[lang]
        is_default = lang == default_lang
        out_dir = PUBLIC if is_default else PUBLIC / lang
        out_dir.mkdir(parents=True, exist_ok=True)
        root = "" if is_default else "../"
        lang_path = "index.html" if is_default else f"{lang}/index.html"
        url_prefix = "" if is_default else f"{lang}/"

        posts = []
        for md in sorted((CONTENT / lang).glob("*.md")) if (CONTENT / lang).is_dir() else []:
            meta, text = parse_front_matter(md.read_text(encoding="utf-8"))
            if meta.get("draft", "").lower() in ("true", "yes", "1"):
                continue
            slug = re.sub(r"[^A-Za-z0-9_-]+", "-", meta.get("slug") or md.stem).strip("-") or "post"
            date = meta.get("date") or today
            if date > today:
                continue  # 予約投稿：日付が未来の記事はまだ出さない
            headings = []
            body = markdown(text, headings)
            title = meta.get("title") or (headings[0][2] if headings else md.stem)
            description = meta.get("description") or excerpt_of(body, 120)
            body, aff_tail, pr_notice = affiliate.apply(body, meta, title, programs)
            body = insert_middle_ad(body, slots["slot_ad_middle"])
            slot_affiliate = slots["slot_affiliate"]
            if aff_tail:
                slot_affiliate = f'<div class="affiliate-slot">{aff_tail}</div>' + slot_affiliate
            if pr_notice:
                names = re.findall(r'class="aff-name">([^<]+)<', body + aff_tail)
                print(f"  広告 {slug}: {' / '.join(dict.fromkeys(names))}")

            page = render(tpl_article, {
                **L, **slots, "slot_affiliate": slot_affiliate, "pr_notice": pr_notice,
                "lang": lang, "root": root, "lang_path": lang_path, "year": year,
                "title": html.escape(title), "description": html.escape(description),
                "date": html.escape(date), "toc": build_toc(headings), "body": body,
            })
            (out_dir / f"{slug}.html").write_text(page, encoding="utf-8")
            posts.append({"slug": slug, "title": title, "date": date, "excerpt": excerpt_of(body)})
            sitemap_urls.append(f"{url_prefix}{slug}")
            total += 1

        posts.sort(key=lambda p: (p["date"], p["slug"]), reverse=True)
        items = "\n    ".join(
            f'<li><time datetime="{html.escape(p["date"])}">{html.escape(p["date"])}</time>'
            f'<a href="{p["slug"]}.html">{html.escape(p["title"])}</a>'
            f'<p class="excerpt">{html.escape(p["excerpt"])}</p></li>'
            for p in posts
        )
        switch = ""
        if len(active) > 1:
            links = []
            for other in active:
                if other == lang:
                    continue
                href = root + ("" if other == default_lang else f"{other}/") + "index.html"
                links.append(f'<a href="{href}" hreflang="{other}">{langs[other]["switch_label"]}</a>')
            switch = '<nav class="lang-switch">' + "".join(links) + "</nav>"

        index = render(tpl_index, {
            **L, **slots,
            "lang": lang, "root": root, "lang_path": lang_path, "year": year,
            "posts": items, "lang_switch": switch,
        })
        (out_dir / "index.html").write_text(index, encoding="utf-8")
        sitemap_urls.append(url_prefix)
        print(f"[{lang}] {len(posts)} 記事")

    if base_url:
        xml = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        xml += [f"  <url><loc>{html.escape(base_url)}/{u}</loc></url>" for u in sitemap_urls]
        xml.append("</urlset>")
        (PUBLIC / "sitemap.xml").write_text("\n".join(xml) + "\n", encoding="utf-8")
        (PUBLIC / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {base_url}/sitemap.xml\n", encoding="utf-8")

    print(f"ビルド完了: 合計 {total} 記事 -> {PUBLIC}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
