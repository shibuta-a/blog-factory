#!/usr/bin/env python3
"""
note の「顔」（プロフィールのアイコン・ヘッダー）と、記事ごとのサムネイル（見出し画像）を作る。
X（濃紺・システム会社）とは別の、暮らし向けのさわやかな絵柄。フォントは Zen Maru Gothic（assets/fonts・OFL）。
HTML を Playwright（このPCの Chrome）で撮って PNG にする。

  python scripts/note_design.py profile            … data/note-profile/icon.png（400×400）・header.png（1280×670）
  python scripts/note_design.py thumb <slug> [--no N] [--out PATH]
                                                   … data/note/<slug>.md のフロントマターからサムネイルを作る

サムネイルの文字は note 用原稿（data/note/<slug>.md）のフロントマターで決める（note-prompt.md で書かせる）:
  thumb_topic: 献立                       … 左上のラベルの末尾（シリーズ名・番号のあと）
  thumb_title: 毎日の献立、／[AIに]考えて／もらう。 … 大きな文字。「／」で改行（2〜3行）、[ ] はマーカー
  thumb_sub:   冷蔵庫の中身を伝えるだけで、1週間分と買い物リスト … 下の一行（読むと何が分かるか）
  thumb_motif: bowl                       … 右の絵（MOTIFS のキー）
文字がはみ出したら、大きな文字を小さくして撮り直す。最小の大きさでもはみ出すなら FitError（言い換えが必要）。
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "assets" / "fonts"
PROFILE_DIR = ROOT / "data" / "note-profile"
THUMB_DIR = ROOT / "data" / "note-thumbs"
W, H = 1280, 670

SERIES = {"orishan": "小さな商売のつくり方"}   # ジャンル → シリーズ名
SERIES_DEFAULT = "面倒を減らす"

F = "'ZenMaru','Zen Maru Gothic',sans-serif"
INK, SUB, BLUE, MINT = "#24384A", "#4F6B80", "#3BA4E8", "#5CCFB0"

# ---------------------------------------------------------------------------
# 絵（白を基調にしたシンプルな図形。背景の色の上に置く）
# ---------------------------------------------------------------------------
BOWL = ('<svg viewBox="0 0 120 120"><ellipse cx="60" cy="58" rx="40" ry="10" fill="#fff"/><path d="M20 58 Q60 110 100 58Z" fill="#fff"/>'
        '<circle cx="45" cy="50" r="7" fill="#FFB347"/><circle cx="62" cy="47" r="8" fill="#7ED6A6"/><circle cx="76" cy="52" r="6" fill="#FF8A80"/></svg>')
SHIRT = ('<svg viewBox="0 0 120 120"><rect x="28" y="40" width="64" height="16" rx="6" fill="#fff"/><rect x="24" y="58" width="72" height="16" rx="6" fill="#DFF3FF"/>'
         '<rect x="30" y="76" width="60" height="16" rx="6" fill="#fff"/></svg>')
CALC = ('<svg viewBox="0 0 120 120"><rect x="30" y="22" width="60" height="78" rx="10" fill="#fff"/><rect x="38" y="30" width="44" height="16" rx="4" fill="#BFE3FA"/>'
        + "".join(f'<rect x="{38 + c * 16}" y="{54 + r * 14}" width="12" height="10" rx="3" fill="#9CCFF2"/>' for r in range(3) for c in range(3)) + "</svg>")
MOTIFS = {
    "bowl": BOWL,          # 献立・料理
    "towel": SHIRT,        # 洗濯・掃除・布団
    "calc": CALC,          # 家計簿・お金
    "invoice": ('<svg viewBox="0 0 120 120"><path d="M32 18 H76 L90 32 V102 H32 Z" fill="#fff"/><path d="M76 18 V32 H90" fill="#E8EEF3"/>'
                '<rect x="42" y="44" width="38" height="6" rx="3" fill="#C9D6E0"/><rect x="42" y="58" width="30" height="6" rx="3" fill="#C9D6E0"/>'
                '<rect x="42" y="72" width="38" height="6" rx="3" fill="#C9D6E0"/><circle cx="72" cy="88" r="9" fill="#FF8A80"/></svg>'),  # 請求書・書類
    "clock": ('<svg viewBox="0 0 120 120"><circle cx="60" cy="62" r="40" fill="#fff"/><circle cx="60" cy="62" r="4" fill="#24384A"/>'
              '<path d="M60 62 V36 M60 62 L78 72" stroke="#24384A" stroke-width="6" stroke-linecap="round"/><circle cx="60" cy="16" r="6" fill="#fff"/></svg>'),  # 時間・段取り
    "laptop": ('<svg viewBox="0 0 120 120"><rect x="26" y="30" width="68" height="46" rx="6" fill="#fff"/><rect x="33" y="37" width="54" height="32" rx="3" fill="#CFE8FA"/>'
               '<path d="M16 82 H104 L98 92 H22 Z" fill="#fff"/><path d="M46 53 L56 62 L74 45" fill="none" stroke="#5CCFB0" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/></svg>'),  # 仕事・AI・パソコン
    "book": ('<svg viewBox="0 0 120 120"><path d="M60 32 Q40 22 18 28 V92 Q40 86 60 96 Z" fill="#fff"/><path d="M60 32 Q80 22 102 28 V92 Q80 86 60 96 Z" fill="#F2F7FB"/>'
             '<rect x="28" y="44" width="22" height="5" rx="2.5" fill="#C9D6E0"/><rect x="28" y="56" width="18" height="5" rx="2.5" fill="#C9D6E0"/>'
             '<rect x="70" y="44" width="22" height="5" rx="2.5" fill="#C9D6E0"/><rect x="70" y="56" width="18" height="5" rx="2.5" fill="#C9D6E0"/></svg>'),  # 勉強・資格
    "house": ('<svg viewBox="0 0 120 120"><path d="M22 58 L60 26 L98 58 V96 Q98 100 94 100 H26 Q22 100 22 96 Z" fill="#fff"/>'
              '<rect x="50" y="70" width="20" height="30" rx="4" fill="#FFD9A8"/><rect x="32" y="62" width="12" height="12" rx="2" fill="#CFE8FA"/><rect x="76" y="62" width="12" height="12" rx="2" fill="#CFE8FA"/></svg>'),  # 家事全般・家事代行
    "cart": ('<svg viewBox="0 0 120 120"><path d="M16 30 H30 L40 76 H92 L100 44 H34" fill="none" stroke="#fff" stroke-width="8" stroke-linejoin="round" stroke-linecap="round"/>'
             '<rect x="40" y="48" width="54" height="22" rx="4" fill="#fff"/><circle cx="46" cy="90" r="7" fill="#fff"/><circle cx="86" cy="90" r="7" fill="#fff"/></svg>'),  # 買い物・日用品
    "bottle": ('<svg viewBox="0 0 120 120"><path d="M52 14 H68 V36 Q82 46 82 62 V100 Q82 106 76 106 H44 Q38 106 38 100 V62 Q38 46 52 36 Z" fill="#fff"/>'
               '<rect x="52" y="14" width="16" height="10" rx="2" fill="#E9C46A"/><rect x="44" y="64" width="32" height="26" rx="4" fill="#FFE08A"/>'
               '<path d="M60 70 l3 6 6 1 -4.5 4 1 6 -5.5-3 -5.5 3 1-6 -4.5-4 6-1z" fill="#fff"/></svg>'),  # お祝いのお酒（オリシャン）
    "sprout": ('<svg viewBox="0 0 120 120"><path d="M34 74 H86 L80 102 H40 Z" fill="#fff"/><path d="M60 74 V48" stroke="#fff" stroke-width="6" stroke-linecap="round"/>'
               '<path d="M60 52 Q40 52 36 32 Q56 30 60 52Z" fill="#7ED6A6"/><path d="M60 46 Q76 44 84 26 Q64 24 60 46Z" fill="#A6E8C4"/></svg>'),  # 小さく始める・育てる・副業
}
# 背景の色（記事ごとに順番に変える）: (全体の地, 右上の円, カードのグラデ, 影の色)
PALETTES = [
    ("#FFF3DE", "#FFE2B8", "#FFE7C2,#FFC98F", "200,120,40"),    # オレンジ
    ("#E6F4FF", "#CFE9FF", "#CDEBFF,#9ED3FF", "40,110,190"),    # 空色
    ("#E8F8F0", "#CDEFDF", "#D3F4E6,#9EE0C4", "40,150,110"),    # ミント
    ("#FFEEF0", "#FFD8DE", "#FFE0E5,#FFB7C2", "200,80,100"),    # 桜
    ("#F1EEFF", "#DFD8FF", "#E6E0FF,#C3B6FF", "110,90,200"),    # ラベンダー
    ("#FFF8D9", "#FFEDA8", "#FFF1BF,#FFDB70", "190,150,30"),    # レモン
]
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳" + "".join(chr(c) for c in range(0x3251, 0x325F + 1)) + "".join(chr(c) for c in range(0x32B1, 0x32BF + 1))


class FitError(RuntimeError):
    """最小の文字の大きさでも枠に収まらない（言い換えが必要）"""


def font_face():
    if not (FONTS / "ZenMaruGothic-Bold.ttf").exists():
        raise FileNotFoundError(f"フォントがありません: {FONTS}")
    url = lambda n: (FONTS / n).resolve().as_uri()  # noqa: E731
    return (f"@font-face{{font-family:'ZenMaru';font-weight:500;src:url('{url('ZenMaruGothic-Medium.ttf')}')}}"
            f"@font-face{{font-family:'ZenMaru';font-weight:700;src:url('{url('ZenMaruGothic-Bold.ttf')}')}}")


def page_html(body_style, inner):
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{font_face()}</style></head>"
            f"<body style=\"margin:0;{body_style}\">{inner}</body></html>")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# 撮影
# ---------------------------------------------------------------------------
_SHARED_PW = None


def use_playwright(pw):
    """呼び出し側（note-run.py）がすでに sync_playwright() の中にいるときは、それを使う（入れ子にできないため）。"""
    global _SHARED_PW
    _SHARED_PW = pw


class Shooter:
    """Chrome を1回だけ開いて、何枚でも撮る。"""

    def __enter__(self):
        self._own = _SHARED_PW is None
        if self._own:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
        else:
            self._pw = _SHARED_PW
        try:
            self.browser = self._pw.chromium.launch(channel="chrome")
        except Exception:
            self.browser = self._pw.chromium.launch()
        return self

    def __exit__(self, *exc):
        self.browser.close()
        if self._own:
            self._pw.stop()

    def open(self, html, w, h):
        tmp = ROOT / "state" / "note-design.html"
        tmp.parent.mkdir(exist_ok=True)
        tmp.write_text(html, encoding="utf-8")
        pg = self.browser.new_page(viewport={"width": w, "height": h})
        pg.goto(tmp.resolve().as_uri())
        pg.evaluate("() => Promise.all(['500','700'].map(w => document.fonts.load(w + ' 40px ZenMaru', 'あ'))).then(() => document.fonts.ready)")
        pg.wait_for_timeout(300)
        bad = pg.evaluate("() => ['500','700'].filter(w => !document.fonts.check(w + ' 40px ZenMaru', 'あ'))")
        if bad:
            pg.close()
            raise RuntimeError(f"Zen Maru Gothic を読み込めませんでした（太さ {bad}）")
        return pg


# ---------------------------------------------------------------------------
# プロフィール（アイコン・ヘッダー）
# ---------------------------------------------------------------------------
def card(label, svg, bg, x, y, rot):
    return (f'<div style="position:absolute;left:{x}px;top:{y}px;width:250px;transform:rotate({rot}deg);background:#fff;border-radius:26px;padding:12px;box-shadow:0 14px 30px rgba(40,100,150,.16)">'
            f'<div style="height:200px;border-radius:18px;background:{bg};display:flex;align-items:center;justify-content:center"><div style="width:130px;height:130px">{svg}</div></div>'
            f'<div style="display:flex;align-items:center;justify-content:center;gap:10px;height:56px;font-family:{F};font-weight:700;font-size:28px;color:{INK}">'
            f'<span style="width:30px;height:30px;border-radius:50%;background:{MINT};color:#fff;font-size:20px;display:flex;align-items:center;justify-content:center">✓</span>{label}</div></div>')


def profile_header_html():
    inner = (f'<div style="position:absolute;width:520px;height:520px;border-radius:50%;background:#DDF5EC;right:-120px;top:-160px"></div>'
             f'<div style="position:absolute;width:260px;height:260px;border-radius:50%;background:#FFF1C9;left:-80px;bottom:-110px"></div>'
             f'<div style="position:absolute;left:80px;top:150px;font-size:26px;font-weight:700;color:{BLUE};letter-spacing:.06em">家事・お金・仕事の</div>'
             f'<h1 style="position:absolute;left:76px;top:195px;margin:0;font-size:60px;line-height:1.4;font-weight:700">毎回の<span style="background:linear-gradient(transparent 60%,#FFE08A 60%)">面倒</span>を、<br>ひとつずつ減らす。</h1>'
             f'<div style="position:absolute;left:80px;top:420px;font-size:28px;font-weight:500;color:{SUB}">今日から試せる、小さな仕組みのメモ</div>'
             + card("献立", BOWL, "linear-gradient(135deg,#FFE7C2,#FFD1A8)", 640, 40, -5)
             + card("洗濯", SHIRT, "linear-gradient(135deg,#CDEBFF,#A9DBFF)", 930, 70, 4)
             + card("家計簿", CALC, "linear-gradient(135deg,#D3F4E6,#B3EAD3)", 770, 370, -2))
    return page_html(f"width:{W}px;height:{H}px;background:linear-gradient(160deg,#E6F4FF 0%,#F7FCFF 55%,#FFFFFF 100%);"
                     f"font-family:{F};color:{INK};position:relative;overflow:hidden", inner)


def profile_icon_html():
    inner = ('<svg viewBox="0 0 200 200" width="270" height="270"><path d="M38 94 L100 42 L162 94 L162 160 Q162 170 152 170 L48 170 Q38 170 38 160 Z" fill="#fff" stroke="#3BA4E8" stroke-width="9" stroke-linejoin="round"/>'
             '<path d="M70 114 L93 137 L134 94" fill="none" stroke="#5CCFB0" stroke-width="14" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    return page_html("width:400px;height:400px;background:linear-gradient(160deg,#BFE6FF,#E6F4FF);display:flex;align-items:center;justify-content:center", inner)


def make_profile(out_dir=PROFILE_DIR):
    out_dir.mkdir(parents=True, exist_ok=True)
    with Shooter() as s:
        for html, w, h, name in [(profile_header_html(), W, H, "header.png"), (profile_icon_html(), 400, 400, "icon.png")]:
            pg = s.open(html, w, h)
            pg.screenshot(path=str(out_dir / name))
            pg.close()
    return out_dir / "icon.png", out_dir / "header.png"


# ---------------------------------------------------------------------------
# 記事サムネイル
# ---------------------------------------------------------------------------
TEXT_LEFT, TEXT_RIGHT = 80, 760          # 文字を置いてよい横の範囲（右は絵のカード）
TITLE_TOP, TITLE_BOTTOM = 190, 540       # 大きな文字の縦の範囲
SIZES = [84, 80, 76, 72, 68, 64, 60]     # 大きな文字の大きさ（はみ出したら順に小さく）


def series_name(genre):
    return SERIES.get(genre, SERIES_DEFAULT)


def badge(series, no, topic):
    num = CIRCLED[no - 1] if 1 <= no <= len(CIRCLED) else f"No.{no}"
    return " ".join(x for x in (series, num, topic) if x)


def title_lines(text):
    """「／」区切り・[ ] マーカー → HTML の行のリスト"""
    out = []
    for line in [x.strip() for x in re.split(r"[／/]|\\n", text) if x.strip()]:
        h = esc(line)
        h = re.sub(r"\[(.+?)\]", r'<span style="background:linear-gradient(transparent 60%,#FFE08A 60%)">\1</span>', h)
        out.append(h.replace("[", "").replace("]", ""))
    return out


def thumb_html(spec, size, palette):
    bg, circle, grad, shadow = palette
    motif = MOTIFS.get(spec.get("motif"), MOTIFS["house"])
    lines = "<br>".join(title_lines(spec["title"]))
    inner = (f'<div style="position:absolute;width:560px;height:560px;border-radius:50%;background:{circle};right:-110px;top:-120px"></div>'
             f'<div style="position:absolute;right:110px;top:120px;width:380px;height:380px;border-radius:40px;background:linear-gradient(135deg,{grad});transform:rotate(4deg);'
             f'box-shadow:0 18px 40px rgba({shadow},.22);display:flex;align-items:center;justify-content:center"><div style="width:250px;height:250px">{motif}</div></div>'
             f'<div id="badge" style="position:absolute;left:{TEXT_LEFT}px;top:90px;display:inline-block;white-space:nowrap;background:{BLUE};color:#fff;font-weight:700;font-size:30px;padding:10px 26px;border-radius:40px">{esc(spec["badge"])}</div>'
             f'<div id="title" style="position:absolute;left:{TEXT_LEFT}px;top:{TITLE_TOP}px;white-space:nowrap;font-size:{size}px;font-weight:700;line-height:1.3">{lines}</div>'
             f'<div id="sub" style="position:absolute;left:84px;top:560px;white-space:nowrap;font-size:30px;color:{SUB};font-weight:500">{esc(spec["sub"])}</div>')
    return page_html(f"width:{W}px;height:{H}px;background:linear-gradient(150deg,{bg} 0%,#FFFFFF 60%,#E6F4FF 100%);"
                     f"font-family:{F};color:{INK};position:relative;overflow:hidden", inner)


MEASURE_JS = """() => Object.fromEntries(['badge','title','sub'].map(id => {
  const r = document.getElementById(id).getBoundingClientRect();
  return [id, {left: r.left, right: r.right, top: r.top, bottom: r.bottom}];
}))"""


def overflow(box, title_lines_n):
    """枠からはみ出している部分の説明（なければ空）"""
    bad = []
    if box["badge"]["right"] > TEXT_RIGHT:
        bad.append(f"左上のラベルが {box['badge']['right'] - TEXT_RIGHT:.0f}px はみ出し")
    if box["title"]["right"] > TEXT_RIGHT:
        bad.append(f"大きな文字が右に {box['title']['right'] - TEXT_RIGHT:.0f}px はみ出し")
    if box["title"]["bottom"] > TITLE_BOTTOM:
        bad.append(f"大きな文字が下に {box['title']['bottom'] - TITLE_BOTTOM:.0f}px はみ出し")
    if title_lines_n > 3:
        bad.append(f"大きな文字が {title_lines_n} 行（3行まで）")
    if box["sub"]["right"] > W - 60:
        bad.append(f"下の一行が {box['sub']['right'] - (W - 60):.0f}px はみ出し")
    return bad


def make_thumb(spec, out, palette_index=0, shooter=None):
    """spec: {badge, title, sub, motif}。はみ出したら大きな文字を小さくする。収まった大きさを返す。
    下の一行・ラベル・行数は小さくしても直らないので、すぐ FitError（言い換え）。"""
    palette = PALETTES[palette_index % len(PALETTES)]
    n = len(title_lines(spec["title"]))
    if not (spec.get("title") and spec.get("sub")) or n < 1:
        raise FitError("サムネイルの文字（thumb_title・thumb_sub）がありません")
    own = shooter is None
    s = Shooter().__enter__() if own else shooter
    try:
        last = []
        for size in SIZES:
            pg = s.open(thumb_html(spec, size, palette), W, H)
            box = pg.evaluate(MEASURE_JS)
            last = overflow(box, n)
            if not last:
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                pg.screenshot(path=str(out))
                pg.close()
                return size
            pg.close()
            if any("大きな文字" not in b for b in last):
                break
        raise FitError("・".join(last))
    finally:
        if own:
            s.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# note 用原稿 → サムネイルの指定
# ---------------------------------------------------------------------------
def read_front(path):
    text = Path(path).read_text(encoding="utf-8").lstrip("﻿").replace("\r\n", "\n")
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    meta = {}
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


def thumb_spec(meta, genre, no):
    """note 用原稿のフロントマター → make_thumb に渡す指定。thumb_* が足りなければ None。"""
    if not (meta.get("thumb_title") and meta.get("thumb_sub")):
        return None
    return {"badge": badge(series_name(genre), no, meta.get("thumb_topic", "")),
            "title": meta["thumb_title"], "sub": meta["thumb_sub"], "motif": meta.get("thumb_motif", "")}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["profile", "thumb"])
    ap.add_argument("slug", nargs="?")
    ap.add_argument("--no", type=int, default=1, help="シリーズの番号")
    ap.add_argument("--genre", default="")
    ap.add_argument("--palette", type=int, default=0)
    ap.add_argument("--out")
    args = ap.parse_args()
    if args.cmd == "profile":
        for p in make_profile():
            print(p)
        return 0
    meta = read_front(ROOT / "data" / "note" / f"{args.slug}.md")
    spec = thumb_spec(meta, args.genre, args.no)
    if not spec:
        sys.exit("thumb_title・thumb_sub がフロントマターにありません")
    out = Path(args.out) if args.out else THUMB_DIR / f"{args.slug}.png"
    size = make_thumb(spec, out, args.palette)
    print(json.dumps({"out": str(out), "title_size": size}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
