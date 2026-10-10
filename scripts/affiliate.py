"""
記事の内容に合わせて、台帳（data/affiliates.json）から広告を自動で選んで差し込む部品。
build.py から呼ばれる（Python標準ライブラリのみ）。

■ 選び方（単純なキーワード一致）
  - 台帳の各案件の keywords が、記事のタイトル・本文に何回出てくるかで点数をつける（タイトルは3倍）
  - 案件の genre_keywords（例：クリーニング、布団）が1つも出てこない記事には入れない
    → コーヒー記事などクリーニングと無関係な記事には広告が出ない
  - 点数の高い順に最大3件

■ 記事側でできる指定（フロントマター）
  affiliates: none                 … この記事には広告を入れない
  affiliates: リナビス, リネット     … 入れる案件を名前で指定（自動選択より優先）

■ 置き場所（「売れる形」の3か所。しつこくならないよう各1回まで）
  1. 目次の下 … 一番合う1件の申込みボックス（cta_top）
  2. 比較表とその直後の申込みボックス … 2件以上選ばれた「比較・おすすめ」系の記事だけ。
     本文に <!-- AFFILIATE_COMPARE --> があればそこ、なければ「よくある質問」か「まとめ」の見出しの直前
  3. まとめの後（記事末尾） … 申込みボックス
  本文に <!-- AFFILIATE_CTA --> を書くと、3. のボックスは記事末尾ではなくその位置に入る。

■ 比較表の中身は台帳の table（price / storage / days / feature）だけを使う。
  空欄は「公式サイトで確認」と表示し、事実でない数字は出さない（景品表示法）。
  どの列も全社空欄なら、その列ごと出さない。

■ ボタンの文言は台帳の cta で個別に変えられる。ボックスの一言は cta_note（なければ catch）。

■ 自社サービスの紹介記事（A8 ではない）: 本文に次の形の行を書く（続けて書けば1つの箱にボタンが並ぶ）
    <!-- CTA: 無料でLINE相談・資料請求する → https://lin.ee/xxxx -->
    <!-- CTA: 公式サイトで詳しく見る → https://example.com/ -->
  → その位置（ふつうは「まとめ」の後）・目次の下・「よくある質問」の直前の3か所に申込みボックスが入る。
    A8 の広告は自動では入らない。PR表記は「運営元による自社サービスの紹介です」になる
    （フロントマター pr_notice: で文言を変えられる。cta_note: でボックスの一言、cta_name: で見出しを変えられる）。
"""
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "affiliates.json"

MAX_PICK = 3
TITLE_WEIGHT = 3
COMPARE_WORDS = ("比較", "おすすめ", "ランキング", "選び方", "どこがいい")
TABLE_BEFORE = ("よくある質問", "まとめ")  # 比較表は、この見出しの直前に自動で入れる
# ボタンの文言（行動を促す＋ハードルを下げる一言。事実と言い切れない「最短○分」等は使わない）
BTN_TOP = "今すぐ料金をチェックする"
BTN_AFTER_TABLE = "公式サイトで今すぐ詳細を見る"
BTN_END = "料金を見てみる（見るだけ無料）"
BTN_ROW = "料金を見る"
UNKNOWN = "公式サイトで確認"
PR_NOTICE = '<p class="pr-notice">※本記事にはプロモーション（広告）が含まれています。</p>'


def load_ledger(path=LEDGER):
    """台帳を読む。提携中（url があるもの）だけ返す。"""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    progs = []
    for name, p in data.items():
        if name.startswith("_") or not isinstance(p, dict):
            continue
        if not p.get("url") or p.get("enabled") is False:
            continue
        progs.append({"name": name, **p})
    return progs


def _count(text, word):
    return text.count(word) if word else 0


def score(program, title, text):
    genre = program.get("genre_keywords") or []
    if genre and not any(g in title or g in text for g in genre):
        return 0
    s = 0
    for kw in program.get("keywords") or []:
        s += _count(title, kw) * TITLE_WEIGHT + min(_count(text, kw), 10)
    return s


def pick(programs, meta, title, plain_text):
    """記事に合う案件を最大3件、合う順に返す。"""
    want = (meta.get("affiliates") or "").strip()
    if want.lower() in ("none", "no", "false", "off", "なし"):
        return []
    if want:
        names = [w.strip() for w in re.split(r"[,、]", want) if w.strip()]
        by_name = {p["name"]: p for p in programs}
        return [by_name[n] for n in names if n in by_name][:MAX_PICK]
    scored = [(score(p, title, plain_text), -p.get("priority", 0), p["name"], p) for p in programs]
    scored = [x for x in scored if x[0] > 0]
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [x[3] for x in scored[:MAX_PICK]]


# ---------------------------------------------------------------------------
# HTML（見た目は style.css の .aff-* ）
# ---------------------------------------------------------------------------
ICON_HEART = ('<svg class="aff-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 21s-7.5-4.6-10-9.3C.3 8.4 2.2 4.5 6 4.5c2.2 0 3.6 1.2 4.4 2.4h3.2'
              'c.8-1.2 2.2-2.4 4.4-2.4 3.8 0 5.7 3.9 4 7.2C19.5 16.4 12 21 12 21z" transform="scale(.92) translate(1 1)"/></svg>')
ICON_CHECK = ('<svg class="aff-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="11" fill="currentColor"/>'
              '<path d="M7 12.5l3.2 3.2L17.5 8.5" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _link(p, text, cls):
    """A8 の広告リンク。rel=sponsored（広告であることを検索エンジンに伝える）"""
    return (f'<a class="{cls}" href="{html.escape(p["url"], quote=True)}" '
            f'target="_blank" rel="nofollow sponsored noopener">{html.escape(text)}</a>')


def _pixel(p):
    """A8 の成果計測用の1px画像（report_tag）。A8 が発行する広告コードどおり、リンクとセットで置く。"""
    tag = p.get("report_tag") or ""
    m = re.search(r'src="([^"]+)"', tag) if "<img" in tag else None
    src = m.group(1) if m else tag
    if not src.startswith("http"):
        return ""
    return f'<img class="aff-px" src="{html.escape(src, quote=True)}" width="1" height="1" alt="" loading="lazy">'


def _name(p):
    return p.get("label") or p["name"]


def _points(p):
    pts = p.get("points") or []
    if not pts:
        return ""
    return '<ul class="aff-points">' + "".join(f"<li>{html.escape(x)}</li>" for x in pts[:3]) + "</ul>"


def render_cta(p, button=BTN_END, kicker="この記事のイチオシ", icon=ICON_CHECK, variant=""):
    """申込みボックス：アイコン＋ひとこと＋サービス名＋ボタン を目立つ箱で囲む。"""
    note = p.get("cta_note") or p.get("catch") or ""
    return (
        f'<aside class="aff-box aff-cta{(" " + variant) if variant else ""}" aria-label="おすすめサービス">'
        f'<p class="aff-kicker">{icon}<span>{html.escape(kicker)}</span></p>'
        f'<p class="aff-name">{html.escape(_name(p))}</p>'
        + (f'<p class="aff-catch">{html.escape(note)}</p>' if note else "")
        + _points(p)
        + _link(p, p.get("cta") or button, "aff-btn")
        + '<p class="aff-micro">※公式サイトに移動します</p>'
        + _pixel(p)
        + "</aside>"
    )


TABLE_COLS = [("price", "料金の目安"), ("storage", "保管"), ("days", "仕上がり日数")]  # 既定（cleaning）


def table_cols(genre):
    """比較表の列名。data/genres.json の table_columns でジャンルごとに変えられる。"""
    try:
        conf = json.loads((ROOT / "data" / "genres.json").read_text(encoding="utf-8")).get(genre or "", {})
    except (OSError, ValueError):
        conf = {}
    names = conf.get("table_columns") or {}
    return [(k, names.get(k, label)) for k, label in TABLE_COLS]


def render_table(progs):
    """比較表：各行の右端に申込みボタン。1位に「イチオシ」バッジ。スマホでは縦積みのカードになる。"""
    tables = [p.get("table") or {} for p in progs]
    cols = [(k, label) for k, label in table_cols(progs[0].get("genre")) if any(t.get(k) for t in tables)]
    head = "<th scope=\"col\">サービス</th>" + "".join(f'<th scope="col">{l}</th>' for _, l in cols) \
        + '<th scope="col">特徴</th><th scope="col"><span class="sr-only">申込み</span></th>'
    rows = []
    for i, (p, t) in enumerate(zip(progs, tables)):
        badge = '<span class="aff-badge">イチオシ</span>' if i == 0 else f'<span class="aff-no">{i + 1}</span>'
        unknown_cls = ' class="is-unknown"'
        cells = "".join(
            f'<td data-label="{l}"{"" if t.get(k) else unknown_cls}>{html.escape(t.get(k) or UNKNOWN)}</td>'
            for k, l in cols)
        tr_cls = ' class="is-top"' if i == 0 else ""
        feature = t.get("feature") or p.get("catch") or UNKNOWN
        rows.append(
            f'<tr{tr_cls}>'
            f'<th scope="row">{badge}<span class="aff-tname">{html.escape(_name(p))}</span></th>'
            f'{cells}<td data-label="特徴">{html.escape(feature)}</td>'
            f'<td class="aff-td-btn">{_link(p, BTN_ROW, "aff-btn aff-btn-sm")}{_pixel(p)}</td></tr>')
    dates = sorted(p.get("fetched_at", "") for p in progs if p.get("fetched_at"))
    when = ""
    if dates:
        y, m = dates[-1][:4], dates[-1][5:7].lstrip("0")
        when = f"（{y}年{m}月時点）"
    return (
        '<section class="aff-compare" aria-label="サービス比較表">'
        '<p class="aff-compare-title">おすすめサービス比較表</p>'
        '<div class="aff-table-wrap"><table class="aff-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        f'<p class="aff-note">※料金・内容は各社の公式情報・広告素材の記載をもとにした目安です{when}。'
        "最新の料金・条件は必ず公式サイトでご確認ください。</p>"
        "</section>"
    )


def _insert_before_heading(body_html, block):
    """「よくある質問」→「まとめ」の順に探して、その h2 の直前に入れる。なければ末尾。"""
    for word in TABLE_BEFORE:
        m = re.search(r"<h2[^>]*>[^<]*" + re.escape(word), body_html)
        if m:
            return body_html[:m.start()] + block + "\n" + body_html[m.start():]
    return body_html + "\n" + block


def apply(body_html, meta, title, programs):
    """
    本文HTMLに広告を差し込む。
    戻り値: (本文HTML, 目次下のボックス, 記事末尾のボックス, PR表記HTML)
    合う案件がなければ本文はそのまま・ほかは空。
    """
    plain = re.sub(r"<[^>]+>", " ", body_html)
    chosen = pick(programs, meta, title, plain)
    if not chosen:
        body_html = body_html.replace("<!-- AFFILIATE_COMPARE -->", "").replace("<!-- AFFILIATE_CTA -->", "")
        return body_html, "", "", ""
    top = chosen[0]

    # 2. 比較表＋直後の申込みボックス
    if len(chosen) > 1 and ("<!-- AFFILIATE_COMPARE -->" in body_html or any(w in title for w in COMPARE_WORDS)):
        block = render_table(chosen) + render_cta(top, BTN_AFTER_TABLE, "比較して選ぶなら", ICON_CHECK, "aff-cta-sub")
        if "<!-- AFFILIATE_COMPARE -->" in body_html:
            body_html = body_html.replace("<!-- AFFILIATE_COMPARE -->", block, 1)
        else:
            body_html = _insert_before_heading(body_html, block)
    body_html = body_html.replace("<!-- AFFILIATE_COMPARE -->", "")

    # 1. 目次の下（冒頭）は控えめなボックス、3. まとめの後はしっかりしたボックス
    cta_top = render_cta(top, BTN_TOP, "忙しい人にまずおすすめ", ICON_HEART, "aff-cta-top")
    cta_end = render_cta(top, BTN_END, "迷ったらここから")
    if "<!-- AFFILIATE_CTA -->" in body_html:  # 差し込み口があれば、末尾のボックスはそこに置く（二重にしない）
        body_html = body_html.replace("<!-- AFFILIATE_CTA -->", cta_end, 1).replace("<!-- AFFILIATE_CTA -->", "")
        cta_end = ""
    return body_html, cta_top, cta_end, PR_NOTICE


# ---------------------------------------------------------------------------
# 楽天市場の商品リンク（scripts/rakuten.py が data/rakuten.json に用意したもの）
# ---------------------------------------------------------------------------
def render_rakuten(items, show_images=True):
    """楽天の商品枠。価格・レビューは変わるので出さず、楽天市場のページで確かめてもらう。"""
    cards = []
    for it in items:
        url = html.escape(it["url"], quote=True)
        img = (f'<img src="{html.escape(it["image"], quote=True)}" width="150" height="150" alt="" loading="lazy" decoding="async">'
               if show_images and it.get("image") else "")
        cards.append(
            f'<li class="rk-item"><a href="{url}" target="_blank" rel="nofollow sponsored noopener">'
            + (f'<span class="rk-img">{img}</span>' if img else "")
            + f'<span class="rk-body"><span class="rk-name">{html.escape(it["name"])}</span>'
            f'<span class="rk-shop">{html.escape(it.get("shop", ""))}</span>'
            '<span class="rk-btn">楽天市場で価格を見る</span></span></a></li>')
    return ('<section class="rk-box" aria-label="楽天市場の関連商品">'
            '<p class="rk-title">この記事に関連する商品（楽天市場）</p>'
            f'<ul class="rk-list{"" if show_images else " rk-noimg"}">{"".join(cards)}</ul>'
            '<p class="aff-note">※価格・在庫・送料は変わることがあります。購入前に楽天市場の商品ページでご確認ください。</p>'
            "</section>")


def apply_rakuten(body_html, items, show_images=True):
    """<!-- RAKUTEN --> の位置、なければ「よくある質問」か「まとめ」の直前に商品枠を入れる。"""
    if not items:
        return body_html.replace("<!-- RAKUTEN -->", ""), False
    block = render_rakuten(items, show_images)
    if "<!-- RAKUTEN -->" in body_html:
        return body_html.replace("<!-- RAKUTEN -->", block, 1).replace("<!-- RAKUTEN -->", ""), True
    return _insert_before_heading(body_html, block), True


# ---------------------------------------------------------------------------
# 自社サービス（A8 以外）の申込みボックス
# ---------------------------------------------------------------------------
OWN_CTA_RE = re.compile(r"<!--\s*CTA:\s*(.+?)\s*(?:→|->)\s*(https?://\S+?)\s*-->")
OWN_PR_NOTICE = "※本記事は運営元による自社サービスの紹介です。"


def render_own_cta(buttons, kicker, name, note, variant="", icon=ICON_CHECK):
    btns = "".join(
        f'<a class="aff-btn{" aff-btn-alt" if i else ""}" href="{html.escape(url, quote=True)}" '
        f'target="_blank" rel="noopener">{html.escape(text)}</a>'
        for i, (text, url) in enumerate(buttons))
    return (
        f'<aside class="aff-box aff-cta aff-own{(" " + variant) if variant else ""}" aria-label="お問い合わせ">'
        f'<p class="aff-kicker">{icon}<span>{html.escape(kicker)}</span></p>'
        + (f'<p class="aff-name">{html.escape(name)}</p>' if name else "")
        + (f'<p class="aff-catch">{html.escape(note)}</p>' if note else "")
        + f'<div class="aff-btns">{btns}</div>'
        + '<p class="aff-micro">※相談・資料請求は無料です</p>'
        + "</aside>"
    )


def apply_own(body_html, meta):
    """
    <!-- CTA: 文言 → URL --> を自社サービスの申込みボックスに変える。
    戻り値: (本文HTML, 目次下のボックス, PR表記HTML)。CTA 行がなければ (そのまま, "", "")
    """
    groups = list(re.finditer(r"(?:<!--\s*CTA:.*?-->\s*)+", body_html))
    if not groups:
        return body_html, "", ""
    buttons = OWN_CTA_RE.findall(groups[0].group(0))
    name = meta.get("cta_name", "")
    note = meta.get("cta_note", "")
    end_box = render_own_cta(buttons, "まずは気軽に聞いてみる", name, note)
    for g in reversed(groups):
        body_html = body_html[:g.start()] + end_box + "\n" + body_html[g.end():]
    mid_box = render_own_cta(buttons, "気になったら", name, note, "aff-cta-sub")
    m = re.search(r"<h2[^>]*>[^<]*よくある質問", body_html)
    if m:
        body_html = body_html[:m.start()] + mid_box + "\n" + body_html[m.start():]
    top_box = render_own_cta(buttons, "無料で相談できます", name, note, "aff-cta-top", ICON_HEART)
    notice = f'<p class="pr-notice">{html.escape(meta.get("pr_notice") or OWN_PR_NOTICE)}</p>'
    return body_html, top_box, notice
