#!/usr/bin/env python3
"""
note の有料記事「自動化ランキング100 完全版（◯年◯月版）」を作って売る（毎朝の note-run.py の中で動く）。

  python scripts/note_ranking.py build     … 今の版の原稿を作り直す（data/note/ranking/manuscript.md・投稿はしない）
  python scripts/note_ranking.py status    … 今の状態（版・URL・値段・下書きで止めている理由）
  python scripts/note_ranking.py month [--force]
                                           … 月の見直し（Claude Code の下調べ → 順位の付け直し → 原稿）だけ行う（投稿はしない）

毎朝（note-run.py run から daily() が呼ばれる）:
  1. まだ売っていなければ: 下書きを作る／更新する → 公開設定で「有料」を選ぶ。
     note が本人情報（氏名・住所）の登録を求めたら、何も入力せずに下書きで止め、STATUS.md の「要対応」に出す。
     登録が済んでいれば、300円・有料エリアの線を「11〜100位と、職業別の見方はこの先で。」の直後に引いて公開する。
  2. 売っていれば: 販売履歴（ダッシュボード。保存済みのパスワードで確認画面を通る）で売れた数を数え、
     50件を超えたら 500円に変えて記録する（data/note/ranking/state.json の price_log・logs/note.log）。
  3. 毎月1日: prepare_month()（note-run.py が Chrome を開く前に呼ぶ）で
     Claude Code（scripts/note-ranking-prompt.md）が新しい道具・項目を公式サイトで確かめ、読者のコメントの「面倒」を項目に結びつける
     → 順位を付け直して「◯月版」の原稿を作る → daily() で有料記事のタイトルと本文を差し替え、
     その日の通常投稿の代わりに「◯月版を公開しました」の紹介記事（無料）を出す。
売れた数は公開リポジトリに上げない（state/note-ranking-sales.json）。
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import note_design  # noqa: E402

DIR = ROOT / "data" / "note" / "ranking"           # 版ごとの順位・原稿・状態（GitHub に上がる）
STATE = DIR / "state.json"
MANUSCRIPT = DIR / "manuscript.md"
INTRO = DIR / "intro.md"
SALES = ROOT / "state" / "note-ranking-sales.json"  # 売れた数（公開リポジトリに上げない）
PROMPT = ROOT / "scripts" / "note-ranking-prompt.md"
BASE_RANKING = ROOT / "data" / "jidouka-ranking.json"
RESEARCH_DIR = ROOT / "data" / "jidouka-research"
SITE = json.loads((ROOT / "site.json").read_text(encoding="utf-8"))
BASE_URL = SITE["base_url"].rstrip("/")
ACT_URL = "https://maf-group.jp/act/"
SERIES = "自動化ランキング"
MAGAZINE = "面倒を減らすシリーズ"
FIRST_PRICE, SECOND_PRICE, PRICE_UP_AFTER = 300, 500, 50   # 50件を超えたら（51件目から）500円
PAYWALL_TEXT = "11〜100位と、職業別の見方はこの先で。"       # 有料エリアの線はこの段落の直後
TAGS = ["自動化", "仕事効率化", "業務効率化", "時短家事", "暮らし"]

# 職業別のおすすめ（見出し → ランキングの「分野」・ひとこと）
JOBS = [
    ("会社員", ["会社員"], "メール・資料・会議まわりの、毎日くり返す作業が中心です。会社のルールで使える道具が決まっていることも多いので、まずは今ある道具の中で試せるものから選んでください。"),
    ("経理", ["経理・事務"], "毎月同じ時期に同じ手順でくり返す作業が多く、自動化の効果が分かりやすい分野です。まずは銀行・カードの明細の取り込みから始める人が多いです。"),
    ("個人事業主", ["個人事業主", "SNS・発信"], "ひとりで営業・事務・発信まで抱えると、作業の時間が本業を圧迫しがちです。お金の記録と、お客さんとのやりとりから1つずつ任せていくのがおすすめです。"),
    ("飲食店", ["飲食店"], "営業中は手が離せないので、電話・締め作業・シフトのように「営業の外」で積み上がる作業から減らすのが近道です。"),
    ("美容室・サロン", ["美容室・サロン"], "予約・連絡・記録の3つが中心です。施術中に手が止まる電話や、予約台帳の二重管理から見直すと、ほかの作業もつながって楽になります。"),
    ("介護", ["介護"], "記録と書類の作業が、利用者さんと向き合う時間を削りがちです。手書き→転記の二度手間をなくすところから始めると、変化が分かりやすいです。"),
    ("先生", ["教育"], "授業の準備のほかに、連絡・採点・書類の作業が積み重なります。保護者への連絡や採点のように、毎回同じ形の作業から任せるのがおすすめです。"),
    ("小売・ネットショップ", ["小売・ネットショップ"], "在庫・受注・発送の数字を何度も写すところに手間が集まります。数字を1か所にまとめる道具から考えると選びやすくなります。"),
    ("不動産・建設・リフォーム", ["不動産・管理", "建設・リフォーム"], "見積・点検・写真・連絡のように、現場と事務所の間で同じ情報を何度も書き写す作業が多い分野です。"),
    ("医療・クリニック", ["医療・クリニック"], "受付と予約の電話、問診の書き写しが大きな手間です。患者さんが自分で入力できる形にすると、受付の負担が軽くなります。"),
    ("家のこと（家事・子育て・お金・健康）", ["家事", "子育て・家族", "お金", "健康・学び"], "家の面倒は、毎日・毎週の「小さなくり返し」です。道具を1つ入れるだけで始められるものが多いので、気になる順に1つずつ試してみてください。"),
]
EASE = {5: "★☆☆（やさしい：設定すれば始められる）", 4: "★☆☆（やさしい）", 3: "★★☆（ふつう：最初に少し準備が要る）",
        2: "★★★（少しむずかしい：やり方の見直しが要る）", 1: "★★★（むずかしい：仕組みづくりが要る）"}


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    p = ROOT / "logs" / "note.log"
    p.parent.mkdir(exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def jload(p, dflt):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return dflt


def jsave(p, data):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def load_state():
    return jload(STATE, {})


def save_state(st):
    jsave(STATE, st)


def today():
    return datetime.date.fromisoformat(__import__("os").environ.get("BLOG_TODAY") or datetime.date.today().isoformat())


def edition_label(ym):
    y, m = ym.split("-")
    return f"{int(y)}年{int(m)}月版"


def title_of(ym):
    return f"自動化ランキング100 完全版（{edition_label(ym)}）"


def edition_path(ym):
    return DIR / f"{ym}.json"


# ---------------------------------------------------------------------------
# 道具の名前（公式サイトで確かめたものだけ）とブログ記事
# ---------------------------------------------------------------------------
def clean_tool(name):
    name = re.sub(r"【[^】]*】", "", name)
    name = re.sub(r"（[^）]*提携[^）]*）", "", name)
    return name.strip()


def research_tools():
    """data/jidouka-research/<回>.md（公式サイトで確かめた道具）→ {困りごと: [(名前, 確認日)]}。"""
    out = {}
    for f in sorted(RESEARCH_DIR.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        m = re.search(r"確認日:\s*(\d{4}-\d{2}-\d{2})", text)
        day = m.group(1) if m else ""
        cur = None
        for line in text.splitlines():
            h2 = re.match(r"^## \d+\.\s*(.+)$", line)
            if h2:
                cur = h2.group(1).strip()
                continue
            if line.startswith("## "):
                cur = None
            h3 = re.match(r"^### (.+)$", line)
            if h3 and cur:
                name = clean_tool(h3.group(1))
                for k in {cur, re.sub(r"（[^）]*）$", "", cur).strip()}:   # 「予約電話の対応（飲食店）」→「予約電話の対応」でも引ける
                    if name and name not in [n for n, _ in out.get(k, [])]:
                        out.setdefault(k, []).append((name, day))
    return out


def blog_links():
    """記事の回 → ブログ記事の URL（自動化記事を書いた回だけ）。"""
    done = jload(ROOT / "data" / "jidouka-progress.json", {}).get("done", {})
    return {str(k): f"{BASE_URL}/{v['slug']}?utm_source=note-rank" for k, v in done.items() if v.get("slug")}


# ---------------------------------------------------------------------------
# 版（順位）をつくる
# ---------------------------------------------------------------------------
def base_score(it):
    return it["困っている人の多さ"] * 2 + it["自動化のしやすさ"] + it["案内できる広告・サービス"]


def first_edition(ym):
    """最初の版: data/jidouka-ranking.json の順位をそのまま使う。"""
    items = json.loads(BASE_RANKING.read_text(encoding="utf-8"))
    tools = research_tools()
    out = []
    for it in sorted(items, key=lambda x: x["順位"]):
        out.append({k: it[k] for k in ("困りごと", "分野", "今のやり方", "自動化の方法", "道具の例",
                                       "困っている人の多さ", "自動化のしやすさ", "案内できる広告・サービス", "根拠", "記事の回")}
                   | {"順位": it["順位"], "点": base_score(it), "追加": "",
                      "確かめた道具": [{"name": n, "checked": d} for n, d in tools.get(it["困りごと"], [])[:3]]})
    return {"edition": ym, "made_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "items": out,
            "new": [{"困りごと": it["困りごと"], "name": t["name"], "checked": t["checked"]}
                    for it in out for t in it["確かめた道具"] if t["checked"][:7] == ym],
            "notes": "初版"}


def month_views(ym_prev):
    """前の月のブログ記事の閲覧数（state/report の取り込み）→ {slug: 数}。"""
    views = {}
    for f in sorted((ROOT / "state" / "report").glob(f"{ym_prev}-*.json")):
        v = jload(f, {}).get("views") or {}
        for path, x in (v.get("pages") or {}).items():
            slug = path.strip("/").split("/")[-1].split("?")[0]
            views[slug] = views.get(slug, 0) + (x.get("views", 0) if isinstance(x, dict) else 0)
        for k, n in (v.get("utm") or {}).items():   # このランキングからブログへのクリック（?utm_source=note-rank）は倍で数える
            parts = k.split(":")
            if len(parts) >= 3 and parts[2] == "note-rank" and isinstance(n, int):
                slug = parts[1].strip("/").split("/")[-1]
                views[slug] = views.get(slug, 0) + n * 2
    return views


def next_edition(prev, ym, research):
    """前の版 + 下調べ（新しい項目・道具・コメントの「面倒」）+ よく読まれた記事 → 新しい版。"""
    items = [dict(it) for it in prev["items"]]
    names = {it["困りごと"] for it in items}
    new_items = []
    for n in (research.get("new_items") or [])[:5]:
        try:
            if n["困りごと"] in names or not n.get("公式サイト"):
                continue
            it = {"困りごと": n["困りごと"], "分野": n["分野"], "今のやり方": n["今のやり方"], "自動化の方法": n["自動化の方法"],
                  "道具の例": n["道具の例"], "困っている人の多さ": int(n["困っている人の多さ"]), "自動化のしやすさ": int(n["自動化のしやすさ"]),
                  "案内できる広告・サービス": int(n.get("すぐ使える道具", 3)), "根拠": "調査あり（" + n["公式サイト"] + "）",
                  "記事の回": None, "追加": ym, "確かめた道具": [{"name": t["name"], "checked": t.get("checked", "")} for t in n.get("道具", [])][:3]}
            items.append(it)
            names.add(it["困りごと"])
            new_items.append(it["困りごと"])
        except (KeyError, ValueError, TypeError):
            continue
    new_tools = []
    by_name = {it["困りごと"]: it for it in items}
    for t in research.get("tools") or []:
        it = by_name.get(t.get("困りごと"))
        if it and t.get("name") and t.get("url") and t["name"] not in [x["name"] for x in it.get("確かめた道具", [])]:
            it.setdefault("確かめた道具", []).append({"name": clean_tool(t["name"]), "checked": t.get("checked", "")})
            it["確かめた道具"] = it["確かめた道具"][-3:]
            new_tools.append({"困りごと": it["困りごと"], "name": clean_tool(t["name"]), "checked": t.get("checked", "")})
    voice = {k: int(v) for k, v in (research.get("comment_hits") or {}).items() if k in by_name}
    prev_month = (datetime.date.fromisoformat(ym + "-01") - datetime.timedelta(days=1)).strftime("%Y-%m")
    views = month_views(prev_month)
    links = jload(ROOT / "data" / "jidouka-progress.json", {}).get("done", {})
    art_views = {k: views.get(v.get("slug"), 0) for k, v in links.items()}
    ranked = sorted((n for n in art_views.values() if n > 0), reverse=True)
    for it in items:
        bonus_voice = min(2, voice.get(it["困りごと"], 0))
        n = art_views.get(str(it.get("記事の回")), 0)
        bonus_read = 2 if ranked and n == ranked[0] and n > 0 else 1 if ranked and n >= ranked[len(ranked) // 2] and n > 0 else 0
        it["点"] = base_score(it) + bonus_voice + bonus_read
        it["前の順位"] = it.get("順位")
        it["読者の声"], it["よく読まれた"] = bonus_voice, bonus_read
    items.sort(key=lambda x: (-x["点"], x.get("前の順位") or 999))
    items = items[:100]
    for i, it in enumerate(items, 1):
        it["順位"] = i
    return {"edition": ym, "made_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "items": items,
            "new": [{"困りごと": n, "name": "", "checked": ""} for n in new_items if n in {x["困りごと"] for x in items}] + new_tools,
            "voice": voice, "notes": research.get("notes", "")}


# ---------------------------------------------------------------------------
# 原稿
# ---------------------------------------------------------------------------
def tools_line(it):
    checked = it.get("確かめた道具") or []
    if "（自社）" in it["道具の例"]:   # 自社サービスは名前で売り込まない（最後の一言で案内する）
        return "名入れギフトのデザインエディタ" if "オリシャン" in it["道具の例"] else it["道具の例"].replace("（自社）", "")
    if not checked:
        return it["道具の例"]
    day = max(t["checked"] for t in checked if t.get("checked")) if any(t.get("checked") for t in checked) else ""
    names = "、".join(t["name"] for t in checked)
    return f"{it['道具の例']}（例：{names}。公式サイトで確認{'：' + day if day else ''}）"


def item_block(it, links, paid):
    lines = [f"### {it['順位']}位　{it['困りごと']}", f"- 分野：{it['分野']}"]
    if paid:
        lines.append(f"- 今のやり方：{it['今のやり方']}")
    lines.append(f"- 自動化の方法：{it['自動化の方法']}")
    lines.append(f"- 道具の例：{tools_line(it)}")
    if paid:
        lines.append(f"- むずかしさ：{EASE.get(it['自動化のしやすさ'], '★★☆（ふつう）')}")
    link = links.get(str(it.get("記事の回")))
    if link:
        lines.append(f"- 詳しく：ブログの記事で、道具の選び方までまとめています {link}")
    return "\n".join(lines)


def counts(ed):
    c = {"調査": 0, "自社": 0, "推定": 0}
    for it in ed["items"]:
        g = it.get("根拠", "")
        c["調査" if g.startswith("調査") else "自社" if "自社" in g else "推定"] += 1
    return c


def manuscript(ed):
    """(フロントマター付きの原稿, 無料部分, 有料部分)。"""
    ym = ed["edition"]
    links = blog_links()
    items = ed["items"]
    c = counts(ed)
    restaurant = next((it for it in items if it["分野"] == "飲食店" and ("締め" in it["困りごと"] or "日計" in it["困りごと"]
                                                                   or "集計" in it["困りごと"] or "日報" in it["困りごと"])), None)
    free = [
        "毎月の請求書、毎日の献立、閉店後の締め作業。一回ごとは小さくても、くり返すうちに時間と気力を持っていかれる作業があります。",
        "この記事は、そうした「毎回の面倒」を100個集めて、自動化しやすい順に並べたランキングです。毎月見直して、その月の版に書き換えています。",
        "1〜10位はここで全部読めます。",
        "",
        "## このランキングの作り方",
        "",
        "順位は、困りごと1つずつに3つの点をつけて、合計の高い順に並べています。",
        "",
        "- 困っている人の多さ（5点。いちばん大事なので2倍で数えます）",
        "- 自動化のしやすさ（5点。道具を入れるだけで済むか、やり方から見直しが要るか）",
        "- すぐ使える道具・サービスがそろっているか（5点）",
        "",
        f"もとにしたのは、ネットで公開されている調査や各道具の公式サイト（{c['調査']}項目）と、"
        f"実際に仕組みを作ってきた経験（{c['自社']}項目）です。残りの{c['推定']}項目は、調べた範囲からの推定です。",
        "",
        ("飲食店をやっていた頃、閉店後のレジ締めと日計表の集計を手でやっていました。"
         + (f"この作業も{restaurant['順位']}位に入っています。" if restaurant else "")
         + "一つひとつは慣れた作業でも、毎日となると重い。そういう作業ほど、仕組みに任せたときの変化が大きいと感じています。"),
        "",
        "毎月1日に、次の3つを反映して順位を見直します。",
        "",
        "- ネットの調査：新しく出た自動化の道具を調べて、公式サイトで確かめたものを追加",
        "- 読者の声：コメントで教えてもらった「面倒」を、近い項目の点に足す",
        "- よく読まれた項目：ブログやこの記事でよく読まれた・クリックされた項目の点に足す",
        "",
        "道具の料金は変わるので書いていません。道具の名前を挙げているものは、公式サイトで確かめたものだけです。使う前に、公式サイトで今の内容を確かめてください。",
        "",
        "## 1〜10位",
        "",
    ]
    for it in items[:10]:
        free += [item_block(it, links, paid=False), ""]
    free += [
        "## 11〜100位と、職業別の見方",
        "",
        "この先では、次の内容を読めます。",
        "",
        "- 11〜100位（困りごと、今のやり方、自動化の方法、道具の例、むずかしさ）",
        "- 職業別のおすすめ（会社員、経理、個人事業主、飲食店、美容室、介護、先生など）",
        "- 今月の新着",
        "",
        "毎月の見直しも、この記事を書き換える形で続けます。",
        "",
        PAYWALL_TEXT,
    ]
    paid = ["## 11〜100位", ""]
    for it in items[10:]:
        paid += [item_block(it, links, paid=True), ""]
    paid += ["## 職業別のおすすめ", "",
             "ランキングを、仕事ごとに分けて並べ直しました。数字はランキングの順位です。まずは「最初の1つ」から試してみてください。", ""]
    for job, fields, lead in JOBS:
        mine = [it for it in items if it["分野"] in fields]
        if not mine:
            continue
        paid += [f"### {job}", lead, "", f"- 最初の1つ：{mine[0]['順位']}位 {mine[0]['困りごと']}（{mine[0]['自動化の方法']}）"]
        if len(mine) > 1:
            paid.append("- 次に：" + "、".join(f"{it['順位']}位 {it['困りごと']}" for it in mine[1:3]))
        if len(mine) > 3:
            paid.append("- ほかに：" + "、".join(f"{it['順位']}位 {it['困りごと']}" for it in mine[3:]))
        paid.append("")
    paid += ["## 今月の新着", ""]
    new_items = [n for n in ed.get("new", []) if not n.get("name")]
    new_tools = [n for n in ed.get("new", []) if n.get("name")]
    by = {it["困りごと"]: it for it in items}
    if ed.get("notes") == "初版":
        paid += ["今月は初版です。100項目すべてを、今回はじめてまとめました。来月からは、新しく加わった項目と道具をここに書きます。", ""]
    if new_items:
        paid += ["今月、新しく加わった項目です。", ""] + [f"- {by[n['困りごと']]['順位']}位 {n['困りごと']}" for n in new_items if n["困りごと"] in by] + [""]
    if new_tools:
        paid += ["今月、公式サイトで確かめて「道具の例」に加えたものです。", ""]
        group = {}
        for t in new_tools:
            if t["困りごと"] in by and t["name"] not in group.setdefault(t["困りごと"], []):
                group[t["困りごと"]].append(t["name"])
        paid += [f"- {by[k]['順位']}位 {k}：{'、'.join(v)}" for k, v in sorted(group.items(), key=lambda x: by[x[0]]["順位"])] + [""]
    moved = [it for it in items if it.get("前の順位") and it["前の順位"] - it["順位"] >= 3]
    if moved:
        paid += ["順位が大きく上がった項目です（読者の声・よく読まれた項目を反映）。", ""]
        paid += [f"- {it['困りごと']}：{it['前の順位']}位 → {it['順位']}位" for it in moved[:5]] + [""]
    if not (new_items or new_tools or moved or ed.get("notes") == "初版"):
        paid += ["今月は、順位の入れかわりと新しい項目はありませんでした。", ""]
    paid += ["## 最後に", "",
             "ここに挙げた道具は、どれも既製品です。仕事のやり方によっては、ぴったり合うものが見つからない作業もあると思います。",
             "",
             "既製品で合わない作業は、仕組みごと作ることもできます。見積・点検・受付のような、仕事に合わせた仕組みづくりについては、こちらで紹介しています。",
             ACT_URL,
             "",
             "あなたの家や職場の一番の面倒は何ですか？コメントで教えてください。来月の見直しで、近い項目の順位に反映します。"]
    free_md, paid_md = "\n".join(free).strip(), "\n".join(paid).strip()
    front = "\n".join(["---", f"title: {title_of(ym)}", f"tags: {'|'.join(TAGS)}", f"edition: {ym}", "---", ""])
    return front + free_md + "\n\n<!-- ここから有料 -->\n\n" + paid_md + "\n", free_md, paid_md


def plain_len(md):
    return len(re.sub(r"\s", "", re.sub(r"^#+ |^- ", "", md, flags=re.M)))


def thumb(ym, out):
    spec = {"badge": f"{SERIES}｜{edition_label(ym)}", "title": "暮らしと仕事の／[自動化]／ランキング100",
            "sub": "毎回の面倒100個と、自動化の方法・道具の例", "motif": "laptop"}
    note_design.make_thumb(spec, out, palette_index=1)
    return out


def build(ym=None):
    st = load_state()
    ym = ym or st.get("edition") or today().strftime("%Y-%m")
    if not edition_path(ym).exists():
        jsave(edition_path(ym), first_edition(ym))
    ed = jload(edition_path(ym), {})
    text, free, paid = manuscript(ed)
    MANUSCRIPT.write_text(text, encoding="utf-8", newline="\n")
    st.update(edition=ym, title=title_of(ym), free_chars=plain_len(free), paid_chars=plain_len(paid))
    st.setdefault("price", FIRST_PRICE)
    st.setdefault("price_log", [])
    save_state(st)
    return ed, free, paid


# ---------------------------------------------------------------------------
# 毎月の見直し（Claude Code の下調べ）
# ---------------------------------------------------------------------------
def find_claude():
    import shutil
    for c in (shutil.which("claude"), Path.home() / ".local" / "bin" / "claude.exe"):
        if c and Path(c).exists():
            return str(c)
    return None


def research(ym, prev):
    out = DIR / f"research-{ym}.json"
    if out.exists():
        return jload(out, {})
    comments = jload(ROOT / "data" / "note-comments.json", {}).get("comments", {})
    texts = [c.get("text", "")[:300] for c in comments.values() if c.get("action") != "skipped"]
    since = (datetime.date.fromisoformat(ym + "-01") - datetime.timedelta(days=40)).isoformat()
    texts = [c.get("text", "")[:300] for c in comments.values()
             if c.get("action") != "skipped" and c.get("created_at", "")[:10] >= since] or texts[-30:]
    claude = find_claude()
    if not claude:
        log("note ランキング: claude コマンドが見つからないため、下調べなしで見直します")
        return {}
    from claude_env import claude_env
    prompt = (PROMPT.read_text(encoding="utf-8")
              .replace("{EDITION}", edition_label(ym))
              .replace("{TODAY}", today().isoformat())
              .replace("{OUTPUT}", out.relative_to(ROOT).as_posix())
              .replace("{ITEMS}", "\n".join(f"- {it['順位']}位 {it['困りごと']}（{it['分野']}・道具の例: {it['道具の例']}）" for it in prev["items"]))
              .replace("{COMMENTS}", "\n".join(f"- {t}" for t in texts) or "（今月はコメントがありません）"))
    try:
        subprocess.run([claude, "-p", prompt, "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Write", "WebSearch", "WebFetch"],
                       cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=30 * 60, env=claude_env())
    except subprocess.TimeoutExpired:
        log("note ランキング: 下調べが30分で終わらなかったため、ここまでの分で見直します")
    data = jload(out, None)
    if data is None:
        log("note ランキング: 下調べの結果が保存されませんでした（新しい項目・道具なしで見直します）")
        data = {}
        jsave(out, data)
    return data


def month_due(st):
    """今月の版にまだなっていない（毎月1日の朝。1日に動けなかったときは、その月の最初の実行）。"""
    ym = today().strftime("%Y-%m")
    return bool(st.get("edition")) and st["edition"] < ym


def prepare_month(force=False):
    """毎月の見直し（Chrome を開く前に）。新しい版の原稿を作ったら True。"""
    st = load_state()
    if not (force or month_due(st)):
        return False
    ym = today().strftime("%Y-%m")
    prev = jload(edition_path(st["edition"]), None) or first_edition(st["edition"])
    log(f"note ランキング: {edition_label(ym)} への見直しを始めます（下調べ → 順位の付け直し）")
    data = research(ym, prev)
    ed = next_edition(prev, ym, data)
    jsave(edition_path(ym), ed)
    st["pending_edition"] = ym
    save_state(st)
    text, free, paid = manuscript(ed)
    MANUSCRIPT.write_text(text, encoding="utf-8", newline="\n")
    log(f"note ランキング: {edition_label(ym)} の原稿を作りました（新しい項目・道具 {len(ed['new'])} 件）")
    return True


def highlight(ed):
    """今月の注目: 新しく入った項目の一番上 → 一番上がった項目 → 1位。(順位, 困りごと, 理由)。"""
    by = {it["困りごと"]: it for it in ed["items"]}
    new = [by[n["困りごと"]] for n in ed.get("new", []) if not n.get("name") and n["困りごと"] in by]
    if new:
        it = min(new, key=lambda x: x["順位"])
        return it, "今月、新しく加わった項目です"
    up = [it for it in ed["items"] if it.get("前の順位") and it["前の順位"] > it["順位"]]
    if up:
        it = max(up, key=lambda x: (x["前の順位"] - x["順位"], -x["順位"]))
        return it, f"先月の{it['前の順位']}位から上がりました"
    return ed["items"][0], "今月も1位です"


def intro_md(ed, url):
    ym = ed["edition"]
    it, why = highlight(ed)
    title = f"自動化ランキング100、{int(ym[5:])}月版を公開しました"
    body = [
        f"「自動化ランキング100 完全版」を、{edition_label(ym)}に更新しました。毎回の面倒を100個集めて、自動化しやすい順に並べたランキングです。",
        "",
        f"## 今月の注目は{it['順位']}位の{it['困りごと']}",
        "",
        f"{why}。",
        "",
        f"- 今のやり方：{it['今のやり方']}",
        f"- 自動化の方法：{it['自動化の方法']}",
        f"- 道具の例：{tools_line(it)}",
        "",
        "## 今月の見直しで反映したこと",
        "",
        "- 新しく出た自動化の道具（公式サイトで確かめたものだけ）",
        "- コメントで教えてもらった「面倒」",
        "- よく読まれた・クリックされた項目",
        "",
        "1〜10位は無料で読めます。11〜100位と職業別のおすすめは、ランキングの記事にまとめています。",
        url,
        "",
        "あなたの家や職場の一番の面倒は何ですか？コメントで教えてください。来月の見直しで反映します。",
    ]
    tags = ["自動化", "仕事効率化", "時短家事", "暮らし"]
    return title, "\n".join(body), tags


# ---------------------------------------------------------------------------
# note の画面操作
# ---------------------------------------------------------------------------
def note_run():
    """scripts/note-run.py の部品（投稿・画像・マガジン）を使う。"""
    if "note_run" in sys.modules:
        return sys.modules["note_run"]
    spec = importlib.util.spec_from_file_location("note_run", ROOT / "scripts" / "note-run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["note_run"] = mod
    spec.loader.exec_module(mod)
    return mod


def note_api(page, path):
    return page.evaluate("""async (p) => { const r = await fetch('https://note.com' + p, {credentials: 'include'});
        return {status: r.status, body: r.ok ? await r.json() : null}; }""", path)


def note_info(page, key):
    """記事の状態（status・price・name）。取れなければ {}。"""
    r = note_api(page, f"/api/v3/notes/{key}")
    return ((r or {}).get("body") or {}).get("data") or {}


def open_editor(page, key):
    nr = note_run()
    if key:
        page.goto(f"https://editor.note.com/notes/{key}/edit/", wait_until="domcontentloaded")
    else:
        page.goto("https://note.com/notes/new", wait_until="domcontentloaded")
        page.wait_for_url(re.compile(r"editor\.note\.com/notes/\w+/edit"), timeout=60000)
    page.locator('textarea[placeholder="記事タイトル"]').wait_for(timeout=30000)
    page.wait_for_timeout(3000)
    return key or re.search(r"/notes/(\w+)/", page.url).group(1), nr


def has_header(page):
    ty = page.locator('textarea[placeholder="記事タイトル"]').bounding_box()["y"]
    return page.evaluate("(ty) => [...document.querySelectorAll('img')].some(i => { const r = i.getBoundingClientRect();"
                         " return r.width > 300 && r.bottom <= ty + 5; })", ty)


def fill_editor(page, title, body_html, image):
    """タイトル・見出し画像・本文を入れ直す（本文はいったん全部消してから貼る）。"""
    nr = note_run()
    box = page.locator('textarea[placeholder="記事タイトル"]')
    box.fill(title)
    if image and not has_header(page):
        nr.set_header_image(page, image)
    body = page.locator(".ProseMirror").first
    body.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.wait_for_timeout(800)
    page.evaluate(nr.PASTE_JS, body_html)
    page.wait_for_timeout(3000)
    if len(body.inner_text().strip()) < 3000:
        raise RuntimeError("本文を入力できませんでした（note のエディタの仕様が変わった可能性）")


def save_draft(page):
    """「下書き保存」を押す（ボタンがなければ自動保存を待つ）。"""
    btn = page.get_by_role("button", name=re.compile("下書き保存"))
    if btn.count():
        btn.first.click()
    page.wait_for_timeout(4000)


def choose_paid(page):
    """公開設定の画面で「有料」を選ぶ。本人情報の登録を求められたら、何も入力せずに閉じて False。"""
    page.get_by_text("有料", exact=True).first.click()
    page.wait_for_timeout(2500)
    text = page.evaluate("() => document.body.innerText")
    if "本人情報の登録" in text or "本人情報の入力" in text:
        note_run().shot(page, "ranking-identity")
        try:   # 何も入力せずに閉じる（画面左上の「キャンセル」と別の、ダイアログの中のほう＝最後のもの）
            page.get_by_role("button", name="キャンセル", exact=True).last.click(timeout=5000)
        except Exception:
            page.keyboard.press("Escape")
        page.wait_for_timeout(1500)
        return False
    return True


def set_price(page, price):
    box = page.locator('input[name*="price" i], input[placeholder*="価格"], input[aria-label*="価格"], input[type="number"]').first
    box.wait_for(timeout=15000)
    box.fill(str(price))
    box.press("Tab")
    page.wait_for_timeout(1000)


def set_paywall(page):
    """「有料エリア設定」→ 線を PAYWALL_TEXT の段落の直後へ。"""
    btn = page.get_by_role("button", name=re.compile("有料エリア設定"))
    if btn.count():
        btn.first.click()
        page.wait_for_timeout(3000)
    marker = page.get_by_text(PAYWALL_TEXT, exact=False).first
    marker.wait_for(timeout=30000)
    y = marker.bounding_box()["y"]
    moves = page.get_by_role("button", name=re.compile("ラインをこの場所に変更"))
    best = None
    for i in range(moves.count()):
        bb = moves.nth(i).bounding_box()
        if bb and bb["y"] > y and (best is None or bb["y"] < best[1]):
            best = (i, bb["y"])
    if best is None:
        raise RuntimeError("有料エリアの線を引く場所が見つかりません（note の画面の仕様が変わった可能性）")
    moves.nth(best[0]).click()
    page.wait_for_timeout(1500)
    note_run().shot(page, "ranking-paywall")


def to_publish(page):
    page.get_by_role("button", name="公開に進む").click()
    page.wait_for_url(re.compile(r"/publish"), timeout=60000)
    page.wait_for_timeout(2500)


def add_tags(page, tags):
    box = page.locator('input[placeholder*="ハッシュタグ"]').first
    have = page.evaluate("() => document.body.innerText")
    for t in tags:
        if f"#{t}\n" in have:
            continue
        box.click()
        box.fill(t)
        box.press("Enter")
        page.wait_for_timeout(500)


def submit(page, update=False):
    page.get_by_role("button", name=re.compile("^更新する" if update else "^投稿する")).last.click()
    page.wait_for_timeout(6000)


def body_html():
    nr = note_run()
    _, body = nr.read_md(MANUSCRIPT)
    return nr.to_html(body.replace("<!-- ここから有料 -->", ""))


def ensure_draft(page, st):
    """原稿を下書きに入れる（下書きがあれば入れ直す）。"""
    ym = st.get("pending_edition") or st["edition"]
    img = thumb(ym, ROOT / "state" / "note-img" / f"ranking-{ym}.png")
    key, _ = open_editor(page, st.get("draft_key"))
    fill_editor(page, title_of(ym), body_html(), img)
    save_draft(page)
    note_run().shot(page, "ranking-draft")
    st["draft_key"] = key
    st["draft_url"] = f"https://editor.note.com/notes/{key}/edit/"
    st["draft_saved_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    st["draft_edition"] = ym
    save_state(st)
    return key


def publish_paid(page, user, st):
    """下書きを有料（FIRST_PRICE 円）で公開する。本人情報が要るなら下書きで止める。"""
    key = st["draft_key"]
    open_editor(page, key)
    to_publish(page)
    add_tags(page, TAGS)
    if not choose_paid(page):
        st.update(status="draft", reason="note で有料記事を売るには、最初に本人情報（氏名・住所）の登録が必要です"
                                         "（公開設定で「有料」を選ぶと「本人情報の入力」が出ます）。勝手に入力しないため、下書きで止めています",
                  checked_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        st.pop("last_error", None)
        save_state(st)
        log(f"note ランキング: 本人情報の登録が必要なため、下書きのままにしました（{st['draft_url']}）")
        return None
    set_price(page, st.get("price") or FIRST_PRICE)
    try:
        note_run().add_to_magazine(page, MAGAZINE)
    except Exception as e:
        log(f"note ランキング: マガジンに入れられませんでした（{type(e).__name__}）。あとで入れ直します")
    set_paywall(page)
    note_run().shot(page, "ranking-before-submit")
    submit(page)
    url = note_run().wait_published(user, key)
    info = note_info(page, key)
    if not url or not info.get("price"):
        note_run().shot(page, "error-ranking-publish")
        raise RuntimeError(f"有料で公開できたことを確かめられませんでした（URL {url}・値段 {info.get('price')}）")
    st.update(status="published", reason="", key=key, url=url, price=int(info["price"]),
              published_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    st["price_log"].append({"at": st["published_at"], "price": st["price"], "why": "公開"})
    save_state(st)
    log(f"note ランキング: {st['price']}円で公開しました → {url}")
    return url


def verify_password(page):
    """売上まわりの画面の「パスワードの確認」を、保存済みのパスワードで通る。"""
    from note_browser import CREDS
    page.goto("https://note.com/dashboard/salesmanage", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    pw_box = page.locator('input[type="password"]')
    if pw_box.count():
        pw = json.loads(CREDS.read_text(encoding="utf-8-sig")).get("password", "")
        pw_box.fill(pw)
        page.get_by_role("button", name="確認して続ける").click()
        page.wait_for_timeout(5000)


def sold_count(page, st):
    """公開した月から今月までの販売履歴で、この記事が売れた数。取れなければ None。"""
    verify_password(page)
    key, title = st["key"], re.sub(r"（.*?版）", "", st.get("title", ""))
    start = datetime.date.fromisoformat(st["published_at"][:10]).replace(day=1)
    n, d = 0, start
    while d <= today():
        page_no = 1
        while True:
            r = note_api(page, f"/api/v1/stats/purchasers?datespan={d:%Y%m}&month=true&sort=date_desc&filter=&page={page_no}")
            if not r or r.get("status") != 200:
                return None
            data = (r.get("body") or {}).get("data") or {}
            for p in data.get("purchasers") or []:
                s = json.dumps(p, ensure_ascii=False)
                if key in s or (title and title in s):
                    n += max(1, int(p.get("count") or 1)) if isinstance(p.get("count"), int) else 1
            if data.get("last_page", True):
                break
            page_no += 1
        d = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return n


def change_price(page, st, price, why):
    open_editor(page, st["key"])
    to_publish(page)
    choose_paid(page)
    set_price(page, price)
    set_paywall(page)
    submit(page, update=True)
    info = note_info(page, st["key"])
    if int(info.get("price") or 0) != price:
        note_run().shot(page, "error-ranking-price")
        raise RuntimeError(f"値段を {price}円に変えられませんでした（今の値段 {info.get('price')}）")
    st["price"] = price
    st["price_log"].append({"at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "price": price, "why": why})
    save_state(st)
    log(f"note ランキング: 値段を {price}円に変えました（{why}）")


def update_edition(page, user, st):
    """有料記事のタイトル・本文・見出し画像を新しい版に差し替えて「更新する」。"""
    ym = st["pending_edition"]
    img = thumb(ym, ROOT / "state" / "note-img" / f"ranking-{ym}.png")
    nr = note_run()
    open_editor(page, st["key"])
    fill_editor(page, title_of(ym), body_html(), None)
    try:
        if img:
            page.locator('textarea[placeholder="記事タイトル"]').locator("xpath=preceding::button[1]").click()
            with page.expect_file_chooser(timeout=15000) as fc:
                page.get_by_role("button", name=re.compile("画像を(アップロード|変更)")).click()
            fc.value.set_files(str(img))
            page.wait_for_timeout(2500)
            page.get_by_role("button", name="保存", exact=True).last.click()
            page.wait_for_timeout(4000)
    except Exception as e:
        log(f"note ランキング: 見出し画像は前の版のままにしました（{type(e).__name__}）")
    to_publish(page)
    choose_paid(page)
    set_price(page, st["price"])
    set_paywall(page)
    submit(page, update=True)
    info = note_info(page, st["key"])
    if info.get("name") != title_of(ym):
        nr.shot(page, "error-ranking-update")
        raise RuntimeError(f"新しい版に差し替えられませんでした（今のタイトル {info.get('name')}）")
    st.update(edition=ym, title=title_of(ym), pending_edition=None, updated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    st.setdefault("monthly_log", []).append({"edition": ym, "at": st["updated_at"]})
    save_state(st)
    log(f"note ランキング: {edition_label(ym)} に更新しました → {st['url']}")


def post_intro(page, user, st):
    """その月の紹介記事（無料）を出す。data/note.json に記録して、その日の通常投稿の1本に数える。"""
    nr = note_run()
    ed = jload(edition_path(st["edition"]), {})
    title, body, tags = intro_md(ed, st["url"])
    slug = f"ranking-intro-{st['edition']}"
    done = nr.load_log()
    if slug in done:
        return False
    INTRO.write_text(f"---\ntitle: {title}\ntags: {'|'.join(tags)}\n---\n\n{body}\n", encoding="utf-8", newline="\n")
    same = nr.find_same_title(user, title)
    url = same
    if not same:
        img = thumb(st["edition"], ROOT / "state" / "note-img" / f"{slug}.png")
        url, _ = nr.post_to_note(page, user, title, nr.to_html(body), tags, image=img, publish=not nr.REHEARSAL, magazine=MAGAZINE)
        if nr.REHEARSAL:
            log(f"note ランキング:（通し確認）紹介記事を公開設定の画面まで入力して止めました")
            return True
    done[slug] = {"url": url, "title": title, "header": True, "series": SERIES, "magazine": MAGAZINE,
                  "posted_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    nr.save_log(done)
    log(f"note ランキング: 紹介記事を出しました → {url}")
    return True


def daily(page, user):
    """毎朝（note-run.py run から・ログイン済みの page で）。紹介記事を出したら True（その日の通常投稿は出さない）。"""
    st = load_state()
    if not st:
        build()
        st = load_state()
    nr = note_run()
    try:
        if st.get("status") != "published":
            if st.get("pending_edition"):   # 売り出す前に月が変わったら、新しい版で売り出す
                st.update(edition=st.pop("pending_edition"), title=None)
                save_state(st)
                build(st["edition"])
                st = load_state()
            build(st["edition"])   # 毎朝作り直す（ブログの自動化記事が増えると「詳しく」のリンクが増える）
            st = load_state()
            digest = hashlib.sha1(MANUSCRIPT.read_bytes()).hexdigest()
            if st.get("draft_edition") != st["edition"] or not st.get("draft_key") or st.get("draft_digest") != digest:
                ensure_draft(page, st)
                st["draft_digest"] = digest
                save_state(st)
            if nr.REHEARSAL:
                log("note ランキング:（通し確認）下書きの更新まで（公開設定には進みません）")
                return False
            publish_paid(page, user, st)
            return False
        sold = sold_count(page, st)
        if sold is not None:
            jsave(SALES, {"sold": sold, "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "price": st.get("price")})
            if sold > PRICE_UP_AFTER and int(st.get("price") or 0) < SECOND_PRICE and not nr.REHEARSAL:
                change_price(page, st, SECOND_PRICE, f"売れた数が{PRICE_UP_AFTER}件を超えたため")
        else:
            log("note ランキング: 販売履歴を取れませんでした（値段の判定は次回）")
        if st.get("pending_edition") and not nr.REHEARSAL:
            update_edition(page, user, st)
            return post_intro(page, user, load_state())
    except Exception as e:
        nr.shot(page, "error-ranking")
        st = load_state()
        st["last_error"] = f"{datetime.datetime.now():%Y-%m-%d %H:%M} {type(e).__name__}: {str(e)[:200]}"
        save_state(st)
        log(f"note ランキング: 失敗しました（{type(e).__name__}: {str(e)[:300]}）。次回また試します")
    return False


def status_line():
    """STATUS.md 用の一行と、要対応の文（なければ空）。"""
    st = load_state()
    if not st:
        return "", ""
    sales = jload(SALES, {})
    if st.get("status") == "published":
        line = f"{edition_label(st['edition'])}・{st.get('price')}円・[記事]({st.get('url')})" + (f"・売れた数 {sales['sold']}" if "sold" in sales else "")
        return line, ""
    line = f"{edition_label(st['edition'])}・**下書きで止めています**"
    return line, (f"- note の有料記事「{st.get('title')}」: {st.get('reason')}。"
                  "note の公開設定で「有料」を選んだときに出る「本人情報の入力」（氏名・住所。他の人には公開されません）を一度だけ登録してください。"
                  "登録が済めば、次の朝の実行で 300円・有料エリアの線つきで自動で公開します。" if st.get("reason") else "")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "status", "month"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.cmd == "build":
        ed, free, paid = build()
        st = load_state()
        print(json.dumps({k: st.get(k) for k in ("edition", "title", "free_chars", "paid_chars")}, ensure_ascii=False))
    elif args.cmd == "month":
        print(prepare_month(args.force))
    else:
        print(json.dumps(load_state(), ensure_ascii=False, indent=1))
        print(status_line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
