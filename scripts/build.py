#!/usr/bin/env python3
"""
ブログ工場 ビルドスクリプト（Python標準ライブラリのみ・外部依存なし）

  content/<言語>/*.md  ──(templates/*.html に流し込む)──>  public/

- 既定言語(ja)は public/ 直下、それ以外の言語は public/<言語>/ に出力
- slots/*.html の中身（広告・アフィリタグ）を全ページに自動差し込み
- static/ の中身（画像など）は public/ にそのままコピー
- data/affiliates.json（A8.net の提携台帳）から、記事の内容に合う広告を自動で差し込む
- data/images.json（scripts/images.py が用意した著作権フリー写真の台帳）から、アイキャッチ・本文中の写真・
  一覧のサムネイル・出典表記を自動で入れる

■ 記事で使える「見た目」の書き方（すべて任意。書かなくても自動で整う）
  ==大事な一文==                       … 蛍光ペン風マーカー
  **太字**                              … 太字
  > [!POINT] / > [!NOTE] / > [!WARN]   … 「ポイント」「メモ」「注意」のアイコン付きボックス（次の行から中身）
  - ラベル：説明                         … 箇条書きの「：」の前を自動で太字に
  Q. 質問 / A. 答え（2行続けて書く）      … Q&A の見た目になる（「**質問？**」の次の行に答え、でも可）
  「まとめ」の見出しの中身は「ポイント」ボックス、「注意」を含む見出しの中身は「注意」ボックスに自動で入る

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
    text = re.sub(r"==(.+?)==", r"<mark>\1</mark>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def bold_label(text):
    """「ラベル：説明」形式の箇条書きは、ラベルを太字にして拾い読みしやすくする。"""
    m = re.match(r"^([^：:*<>\[\]`]{2,24})：(.+)$", text)
    return f"**{m.group(1)}**：{m.group(2)}" if m else text


CALLOUTS = {  # > [!POINT] 等のボックス: (CSSクラス, 見出し)
    "POINT": ("box-point", "ポイント"), "TIP": ("box-point", "ポイント"),
    "NOTE": ("box-memo", "メモ"), "MEMO": ("box-memo", "メモ"),
    "WARN": ("box-warn", "注意"), "WARNING": ("box-warn", "注意"), "CAUTION": ("box-warn", "注意"),
}


def box(cls, label, inner):
    return f'<div class="box {cls}"><p class="box-label">{label}</p>{inner}</div>'


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
        out.append("<li>" + inline(bold_label(text)))
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
            cm = re.match(r"^\s*\[!(\w+)\]\s*(.*)$", buf[0]) if buf else None
            if cm and cm.group(1).upper() in CALLOUTS:
                cls, label = CALLOUTS[cm.group(1).upper()]
                rest = ([cm.group(2)] if cm.group(2) else []) + buf[1:]
                out.append(box(cls, label, markdown("\n".join(rest))))
            else:
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
        bold_q = re.match(r"^\*\*(.+?[?？])\*\*$", buf[0]) if len(buf) >= 2 else None
        if bold_q or (re.match(r"^Q[.．:：\s]", buf[0]) and len(buf) >= 2 and re.match(r"^A[.．:：\s]", buf[1])):
            q = bold_q.group(1) if bold_q else re.sub(r"^Q[.．:：\s]\s*", "", buf[0])
            a = " ".join(re.sub(r"^A[.．:：\s]\s*", "", b) for b in buf[1:])
            out.append(f'<div class="faq"><p class="faq-q">{inline(q)}</p><p class="faq-a">{inline(a)}</p></div>')
            continue
        para = "<br>\n".join(inline(b) for b in buf)
        # 「ポイントは〜。」の一文は自動でマーカー
        para = re.sub(r"^(ポイントは[^。<]{4,80}。?)", r"<mark>\1</mark>", para)
        out.append("<p>" + para + "</p>")

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


def wrap_sections(body):
    """「まとめ」の中身はポイントボックス、「注意」を含む見出しの中身は注意ボックスで囲む（その見出しから次の h2 まで）。"""
    parts = re.split(r"(?=<h2[ >])", body)
    out = []
    for part in parts:
        m = re.match(r"(<h2[^>]*>(.*?)</h2>)(.*)", part, re.S)
        if not m or 'class="box' in m.group(3)[:200]:
            out.append(part)
            continue
        head, text, inner = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)), m.group(3)
        # 末尾に付いた広告・比較表は箱の外に出す
        split = re.search(r'\n?(<!-- AD_SLOT_MIDDLE -->|<section class="aff-|<aside class="aff-)', inner)
        core, after = (inner[:split.start()], inner[split.start():]) if split else (inner, "")
        if "まとめ" in text and core.strip():
            out.append(head + box("box-summary", "この記事のポイント", core) + after)
        elif "注意" in text and core.strip():
            out.append(head + box("box-warn", "注意", core) + after)
        else:
            out.append(part)
    return "".join(out)


def load_images():
    f = ROOT / "data" / "images.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def img_tag(root, item, alt, w, h, lazy=True):
    attrs = 'loading="lazy" decoding="async"' if lazy else 'fetchpriority="high" decoding="async"'
    return (f'<img src="{root}{html.escape(item["file"], quote=True)}" width="{w}" height="{h}" '
            f'alt="{html.escape(alt, quote=True)}" {attrs}>')


def insert_body_images(body, imgs, root):
    """本文中の写真を、2つ目の h2 と、後半の h2 の見出しのすぐ下に入れる（まとめ・よくある質問には入れない）。"""
    h2s = [(m, re.sub(r"<[^>]+>", "", m.group(1))) for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", body)]
    usable = [i for i, (_, t) in enumerate(h2s) if not any(w in t for w in ("まとめ", "よくある質問", "FAQ"))]
    if len(usable) < 2:
        return body
    targets = {"body-1": usable[1]}
    if len(usable) >= 3:
        targets["body-2"] = usable[max(2, (len(usable) * 2) // 3)]
    if len(set(targets.values())) < len(targets):
        targets.pop("body-2", None)
    for role, idx in sorted(targets.items(), key=lambda x: -x[1]):  # 後ろから入れて位置がずれないように
        item = imgs.get(role)
        if not item:
            continue
        m, text = h2s[idx]
        alt = item.get("alt") or f"{text}のイメージ写真"
        fig = f'\n<figure class="body-img">{img_tag(root, item, alt, 960, 540)}</figure>'
        body = body[:m.end()] + fig + body[m.end():]
    return body


def photo_credit(imgs):
    """写真の出典。CC0/パブリックドメインなので表記の義務はないが、出どころを明記しておく。"""
    items = [imgs[r] for r in ("eyecatch", "body-1", "body-2") if imgs.get(r)]
    if not items:
        return ""
    links = []
    for it in items:
        label = html.escape(it.get("title") or "photo")
        by = f" by {html.escape(it['creator'])}" if it.get("creator") else ""
        src = html.escape((it.get("source") or "").capitalize())
        href = html.escape(it.get("landing_url") or "", quote=True)
        a = f'<a href="{href}" target="_blank" rel="noopener">{label}</a>' if href else label
        links.append(f"{a}{by}（{html.escape(it.get('license', ''))} / {src}）")
    return ('<p class="photo-credit">写真：' + "、".join(links)
            + '　※著作権フリー（CC0・パブリックドメイン）素材を <a href="https://openverse.org/" target="_blank" rel="noopener">Openverse</a> 経由で使用しています。</p>')


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


def verification_tag(config):
    """Google Search Console の所有権確認タグ（site.json の google_site_verification に content の値を書く）。"""
    code = (config.get("google_site_verification") or "").strip()
    return f'<meta name="google-site-verification" content="{html.escape(code, quote=True)}">\n' if code else ""


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
    images = load_images()
    print(f"広告台帳: 提携中 {len(programs)} 件")

    slots = {
        "slot_head": verification_tag(config) + load_slot("head", None),
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
            body, cta_top, pr_notice = affiliate.apply_own(body, meta)  # 自社サービスの紹介記事
            if cta_top:
                aff_tail = ""
            else:
                body, cta_top, aff_tail, pr_notice = affiliate.apply(body, meta, title, programs)
            body = insert_middle_ad(body, slots["slot_ad_middle"])
            body = wrap_sections(body)
            imgs = images.get(slug, {})
            body = insert_body_images(body, imgs, root)
            eyecatch = og_tags = ""
            if imgs.get("eyecatch"):
                ec = imgs["eyecatch"]
                eyecatch = f'<figure class="eyecatch">{img_tag(root, ec, ec.get("alt") or title, 1200, 675, lazy=False)}</figure>'
                if base_url:
                    og_tags = (f'<meta property="og:image" content="{html.escape(base_url + "/" + ec["file"], quote=True)}">\n'
                               '<meta name="twitter:card" content="summary_large_image">')
            slot_affiliate = slots["slot_affiliate"]
            if aff_tail:
                slot_affiliate = f'<div class="affiliate-slot">{aff_tail}</div>' + slot_affiliate
            if pr_notice:
                names = re.findall(r'class="aff-t?name">([^<]+)<', body + aff_tail)
                print(f"  広告 {slug}: {' / '.join(dict.fromkeys(names))}")

            page = render(tpl_article, {
                **L, **slots, "slot_affiliate": slot_affiliate, "pr_notice": pr_notice,
                "lang": lang, "root": root, "lang_path": lang_path, "year": year,
                "title": html.escape(title), "description": html.escape(description),
                "date": html.escape(date), "toc": build_toc(headings), "body": body,
                "eyecatch": eyecatch, "cta_top": cta_top, "og_image": og_tags, "photo_credit": photo_credit(imgs),
            })
            (out_dir / f"{slug}.html").write_text(page, encoding="utf-8")
            plain_body = re.sub(r"<(aside|section|figure)\b.*?</\1>", "", body, flags=re.S)
            posts.append({"slug": slug, "title": title, "date": date, "excerpt": excerpt_of(plain_body),
                          "thumb": f"img/{slug}/thumb.jpg" if imgs.get("eyecatch") else ""})
            sitemap_urls.append((f"{url_prefix}{slug}", meta.get("updated") or date))
            total += 1

        posts.sort(key=lambda p: (p["date"], p["slug"]), reverse=True)
        def card(p, i):
            lazy = 'loading="lazy" ' if i > 1 else ""
            thumb = (f'<img src="{root}{p["thumb"]}" width="480" height="270" alt="" {lazy}decoding="async">'
                     if p["thumb"] else '<span class="thumb-blank" aria-hidden="true"></span>')
            return (f'<li class="post-card"><a href="{p["slug"]}.html">'
                    f'<span class="thumb">{thumb}</span><span class="card-body">'
                    f'<time datetime="{html.escape(p["date"])}">{html.escape(p["date"])}</time>'
                    f'<span class="card-title">{html.escape(p["title"])}</span>'
                    f'<span class="excerpt">{html.escape(p["excerpt"])}</span></span></a></li>')
        items = "\n    ".join(card(p, i) for i, p in enumerate(posts))
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
        sitemap_urls.append((url_prefix, max([p["date"] for p in posts] or [today])))
        print(f"[{lang}] {len(posts)} 記事")

    if base_url:
        xml = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        xml += [f"  <url><loc>{html.escape(base_url)}/{u}</loc><lastmod>{html.escape(d)}</lastmod></url>"
                for u, d in sitemap_urls]
        xml.append("</urlset>")
        (PUBLIC / "sitemap.xml").write_text("\n".join(xml) + "\n", encoding="utf-8")
        (PUBLIC / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {base_url}/sitemap.xml\n", encoding="utf-8")

    print(f"ビルド完了: 合計 {total} 記事 -> {PUBLIC}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
