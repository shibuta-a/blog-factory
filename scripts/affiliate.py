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

■ 本文中の差し込み口（Markdownにこの1行を書くと、その位置に入る）
  <!-- AFFILIATE_COMPARE -->   … 比較ボックス（選ばれた案件をカードで並べる）
  <!-- AFFILIATE_CTA -->       … 一番合う1件の申込みボタン
  どちらも書いていない記事は、記事末尾（AFFILIATE_SLOT）に自動で入る。
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


def _points(p):
    pts = p.get("points") or []
    if not pts:
        return ""
    return '<ul class="aff-points">' + "".join(f"<li>{html.escape(x)}</li>" for x in pts[:3]) + "</ul>"


def _cta_text(p):
    return p.get("cta") or f"{p.get('label') or p['name']}の公式サイトを見る"


def render_cta(p):
    """1件だけを大きなボタンで紹介するボックス。"""
    return (
        '<aside class="aff-box aff-cta" aria-label="おすすめサービス">'
        '<p class="aff-kicker">この記事で紹介したサービス</p>'
        f'<p class="aff-name">{html.escape(p.get("label") or p["name"])}</p>'
        + (f'<p class="aff-catch">{html.escape(p["catch"])}</p>' if p.get("catch") else "")
        + _points(p)
        + _link(p, _cta_text(p), "aff-btn")
        + _pixel(p)
        + "</aside>"
    )


def render_compare(progs):
    """2〜3件をカードで並べる比較ボックス。"""
    if len(progs) == 1:
        return render_cta(progs[0])
    cards = []
    for i, p in enumerate(progs, 1):
        cards.append(
            '<div class="aff-card">'
            f'<p class="aff-rank">{i}</p>'
            f'<p class="aff-name">{html.escape(p.get("label") or p["name"])}</p>'
            + (f'<p class="aff-catch">{html.escape(p["catch"])}</p>' if p.get("catch") else "")
            + _points(p)
            + _link(p, "公式サイトを見る", "aff-btn")
            + _pixel(p)
            + "</div>"
        )
    return (
        '<aside class="aff-box aff-compare" aria-label="サービス比較">'
        '<p class="aff-kicker">おすすめサービスを比較</p>'
        '<div class="aff-cards">' + "".join(cards) + "</div></aside>"
    )


def apply(body_html, meta, title, programs):
    """
    本文HTMLに広告を差し込む。戻り値: (本文HTML, 記事末尾用HTML, PR表記HTML)
    合う案件がなければ本文はそのまま・末尾とPR表記は空。
    """
    plain = re.sub(r"<[^>]+>", " ", body_html)
    chosen = pick(programs, meta, title, plain)
    if not chosen:
        body_html = body_html.replace("<!-- AFFILIATE_COMPARE -->", "").replace("<!-- AFFILIATE_CTA -->", "")
        return body_html, "", ""

    used_inline = False
    if "<!-- AFFILIATE_COMPARE -->" in body_html:
        body_html = body_html.replace("<!-- AFFILIATE_COMPARE -->", render_compare(chosen), 1)
        body_html = body_html.replace("<!-- AFFILIATE_COMPARE -->", "")
        used_inline = True
    if "<!-- AFFILIATE_CTA -->" in body_html:
        body_html = body_html.replace("<!-- AFFILIATE_CTA -->", render_cta(chosen[0]), 1)
        body_html = body_html.replace("<!-- AFFILIATE_CTA -->", "")
        used_inline = True

    if used_inline:
        tail = ""
    elif len(chosen) > 1 and any(w in title for w in COMPARE_WORDS):
        tail = render_compare(chosen)
    else:
        tail = render_cta(chosen[0])
    return body_html, tail, PR_NOTICE
