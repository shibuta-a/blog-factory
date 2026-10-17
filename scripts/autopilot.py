#!/usr/bin/env python3
"""
全自動運転の「司令塔」。記事を書くのは Claude Code 自身（scripts/daily-prompt.md の手順で動く）。
このスクリプトは、その Claude Code が使う道具をまとめたもの（Python標準ライブラリ＋Playwright）。

  python scripts/autopilot.py next             … 次に書くネタ・ジャンル・型・既存記事・広告の状況を表示（1日の上限も判定）
  python scripts/autopilot.py done "<ネタ>" <slug>  … topics.txt のネタに使用済みの印を付ける
  python scripts/autopilot.py ads <genre>      … エンジン②：そのジャンルの広告が足りなければ A8 で検索→提携申請→リンク取得
  python scripts/autopilot.py sync             … 審査待ちが承認されていたら広告リンクを台帳に取り込む
  python scripts/autopilot.py a8-check         … A8 にログインした Chrome が使えるか確認（なければ起動を試す）
  python scripts/autopilot.py topics-check     … 残りネタが少ないジャンルを表示（自動運転の Claude が補充する）
  python scripts/autopilot.py weekly-due       … 週1本のジャンル（genres.json の weekly_day。オリシャン記事）を今日書く日なら 0、違えば 1
  python scripts/autopilot.py jidouka-done <回> <slug>  … 自動化記事（毎日1本）のその回を書いた印を data/jidouka-progress.json に付ける
  python scripts/autopilot.py jidouka-add <file.json>   … 自動化ランキングに困りごとを追加する（残りが10個を切ったとき。形は next が表示）
  python scripts/autopilot.py jidouka-sync     … 提携できた広告に合わせて、ランキングの「広告の状況」を「提携済み」に直す（sync のたびにも行う）
  python scripts/autopilot.py jidouka-research-next  … 次に書く回のうち、下調べ（道具の公式サイト確認）がまだの回を表示（なければ終了コード 2）
  python scripts/autopilot.py review           … 台帳で needs_review（label/catch/table が未整備）の案件を表示
  python scripts/autopilot.py status           … STATUS.md を更新
  python scripts/autopilot.py refresh-next     … 次に見直す公開済み記事を表示（記事が20本未満・今週分済みなら「見直さない」）
  python scripts/autopilot.py refresh-done <slug> "<改善内容>"  … 中身を良くした。本文が実質的に変わっていれば更新日を今日にする
  python scripts/autopilot.py refresh-skip <slug> "<理由>"      … 見直したが直す所がなかった（更新日は変えない）
  python scripts/autopilot.py refresh-mode auto|on|off          … 見直しを 20本たまったら自動開始 / 今すぐ開始 / 停止

スパム対策: 自動生成の記事（フロントマター generated_by: autopilot）は 1日 MAX_PER_DAY 本まで。
毎日1本のジャンル（daily: true。自動化記事 jidouka）: その日まだ書いていなければ next が最優先で出す。ネタは topics.txt ではなく
  data/jidouka-ranking.json の「記事の回」の順（1回で3つの困りごと）。進み具合は data/jidouka-progress.json。
週1本のジャンル（weekly_day）: その曜日以降でその週にまだ書いていなければ、毎日1本の次に出す。それ以外の日は通常の順番に入らない。
1日の本数は「自動化記事＋通常ジャンル1本」、週1本の日は「自動化記事＋オリシャン記事」の2本（boot-run.py が n=2 で呼ぶ）。
"""
import datetime
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TOPICS = ROOT / "topics.txt"
LEDGER = DATA / "affiliates.json"
CANDIDATES = DATA / "a8-candidates.json"
STATE = DATA / "autopilot-state.json"
STATUS = ROOT / "STATUS.md"
LOG = ROOT / "logs" / "autopilot.log"
TASK_NAME = "BlogFactory-Boot"

MAX_PER_DAY = 2          # 自動生成の記事は1日この本数まで（Google のスパム判定を避ける）
LOW_TOPICS = 5           # 残りネタがこれ未満のジャンルは補充する

sys.path.insert(0, str(Path(__file__).resolve().parent))


def log(msg):
    print(msg, flush=True)


def today():
    # 通し確認（boot-run.py --rehearsal）は、明日の日付として動かす（BLOG_TODAY）。ふだんは今日
    return __import__("os").environ.get("BLOG_TODAY") or datetime.date.today().isoformat()


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


GENRES = {k: v for k, v in load_json(DATA / "genres.json", {}).items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# 記事・ネタ
# ---------------------------------------------------------------------------
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
        text = body[end.end():] if end else body
    return meta, text


def articles():
    out = []
    for md in sorted((ROOT / "content").rglob("*.md")):
        meta, text = front_matter(md)
        if meta.get("draft", "").lower() in ("true", "yes", "1"):
            continue
        meta["_slug"] = meta.get("slug") or md.stem
        meta["_path"] = md
        meta["_genre"] = article_genre(meta, text)
        out.append(meta)
    return out


def article_genre(meta, text):
    if meta.get("genre") in GENRES:
        return meta["genre"]
    if "<!-- CTA:" in text:
        return "自社サービス"
    title = meta.get("title", "")
    for g in sorted(GENRES, key=lambda g: g != "kaji"):
        if any(w in title for w in GENRES[g]["genre_keywords"]):
            return g
    return "その他"


def read_topics():
    """[(行番号, genre, 使用済み?, キーワード)]"""
    out, genre = [], ""
    for i, line in enumerate(TOPICS.read_text(encoding="utf-8").splitlines()):
        m = re.match(r"^##\s*genre:\s*(\w+)", line)
        if m:
            genre = m.group(1)
            continue
        m = re.match(r"^-\s*\[( |x)\]\s*(.+?)\s*(?:→.*)?$", line)
        if m and genre:
            out.append((i, genre, m.group(1) == "x", m.group(2).strip()))
    return out


def auto_today():
    # 予約公開の記事は date が公開日になるので、書いた日（created）で数える
    arts = [a for a in articles() if a.get("generated_by") == "autopilot"]
    if __import__("os").environ.get("BLOG_REHEARSAL"):
        # 通し確認（明日の日付で動かす）: 今回書いた記事＝まだ GitHub に送っていない記事を「今日の分」として数える
        r = subprocess.run(["git", "ls-files", "content"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        tracked = set(r.stdout.split())
        return [a for a in arts if a["_path"].relative_to(ROOT).as_posix() not in tracked]
    return [a for a in arts if (a.get("created") or a.get("date")) == today()]


def weekly_genres():
    return [g for g, c in GENRES.items() if isinstance(c.get("weekly_day"), int)]


def daily_genres():
    return [g for g, c in GENRES.items() if c.get("daily")]


# ---------------------------------------------------------------------------
# 自動化記事（毎日1本・data/jidouka-ranking.json の「記事の回」の順に、1記事で3つの困りごと）
# ---------------------------------------------------------------------------
RANKING = DATA / "jidouka-ranking.json"
PROGRESS = DATA / "jidouka-progress.json"
JIDOUKA_ADS = DATA / "jidouka-ads.json"
ACT_URL = "https://maf-group.jp/act/"
ORISHAN_URL = "https://maf-group.jp/orishanlab.html"
JIDOUKA_LOW = 10         # 残りの困りごとがこれ未満になったら、調べてランキングに足す
RESEARCH_DIR = DATA / "jidouka-research"   # 前の日の実行の最後に、次の回の道具を公式サイトで確かめた結果（<回>.md）
RESEARCH_FRESH_DAYS = 30
RANK_KEYS = ["困りごと", "分野", "今のやり方", "自動化の方法", "道具の例", "困っている人の多さ", "自動化のしやすさ",
             "案内できる広告・サービス", "広告のジャンル", "広告の状況", "根拠"]


def ranking():
    return load_json(RANKING, [])


def progress():
    p = load_json(PROGRESS, {})
    p.setdefault("done", {})
    p.setdefault("added", [])
    return p


def rounds_left():
    """まだ書いていない回 {回: [困りごと, ...]}（回の小さい順）。"""
    done = progress()["done"]
    out = {}
    for it in sorted(ranking(), key=lambda x: x["順位"]):
        if str(it["記事の回"]) not in done:
            out.setdefault(it["記事の回"], []).append(it)
    return dict(sorted(out.items()))


def jidouka_due():
    """今日まだ自動化記事を書いていなくて、書く回が残っていれば、そのジャンル id。"""
    for g in daily_genres():
        if any(a["_genre"] == g for a in auto_today()):
            continue
        if rounds_left():
            return g
    return None


def jidouka_programs(item, ledger, any_status=False):
    """その困りごとに使える提携中の広告（台帳の名前）。「提携済み」の困りごとだけ（any_status なら状況を見ない）。"""
    if not any_status and item.get("広告の状況") != "提携済み":
        return []
    conf = load_json(JIDOUKA_ADS, {})
    ad = item.get("広告のジャンル", "")
    genres, words = conf.get("genre_map", {}).get(ad, []), conf.get("words", {}).get(ad, [])
    out = []
    for k, v in ledger.items():
        if not (isinstance(v, dict) and v.get("url") and v.get("enabled") is not False):
            continue
        if v.get("genre") in genres or (v.get("genre") in daily_genres() and any(w in k + v.get("label", "") for w in words)):
            out.append(k)
    return out


def jidouka_sync():
    """「申請候補」「要確認」の困りごとに使える広告が台帳に入っていたら「提携済み」にする。直した数を返す。"""
    items = ranking()
    if not items:
        return 0
    ledger = load_json(LEDGER, {})
    n = 0
    for it in items:
        if it.get("広告の状況") in ("申請候補", "要確認") and jidouka_programs(it, ledger, any_status=True):
            it["広告の状況"] = "提携済み"
            n += 1
    if n:
        save_json(RANKING, items)
    return n


def cmd_jidouka_sync():
    n = jidouka_sync()
    log(f"自動化ランキング: 広告の状況を「提携済み」にした困りごと {n} 件")
    return 0


def show_jidouka(genre, done_today):
    left = rounds_left()
    rnd, items = next(iter(left.items()))
    remaining = sum(len(v) for v in left.values())
    conf = GENRES[genre]
    ledger = load_json(LEDGER, {})
    log(f"■ 本日の自動記事: {len(done_today)}/{MAX_PER_DAY} 本")
    log(f"■ 今日は毎日1本の自動化記事です（ジャンル: {genre}・{conf['label']}）。フロントマターの genre は {genre}")
    log(f"■ 自動化ランキングの第 {rnd} 回（順位 {items[0]['順位']}〜{items[-1]['順位']}）。この{len(items)}つの困りごとを1記事で紹介する")
    log(f"■ 型: {conf['kata']}")
    log(f"■ このジャンルのルール（必ず守る）: {conf['note']}")
    rf = research_file(rnd)
    if rf:
        log(f"■ 下調べ（前の日に公式サイトで確かめた結果）: {rf.relative_to(ROOT).as_posix()} を最初に読み、そこにある事実を使う。"
            "下調べにない道具・数字を書くときだけ、公式サイトで確かめてから書く")
    else:
        log("■ 下調べ: この回はまだありません。書く前に、道具の名前・料金・機能を公式サイトで確かめる")
    rk = False
    for i, it in enumerate(items, 1):
        log(f"\n### {i}. {it['困りごと']}（順位 {it['順位']}・分野: {it['分野']}・根拠: {it['根拠']}）")
        log(f"  今のやり方: {it['今のやり方']}")
        log(f"  自動化の方法: {it['自動化の方法']}")
        log(f"  道具の例: {it['道具の例']}")
        st = it["広告の状況"]
        progs = jidouka_programs(it, ledger)
        if progs:
            log(f"  広告: 提携中の {' / '.join(progs)} から、この困りごとに一番合う1件を選び、この困りごとの節の最後に "
                f"`<!-- AFFILIATE: 案件名 -->` を1行書く（案件名は台帳の名前をそのまま）")
        elif st == "楽天で可":
            rk = True
            log(f"  広告: 楽天の商品リンクを使う（フロントマターの rakuten: に、この困りごとの道具の検索語を1つ入れる。例: {it['道具の例'].split('、')[0]}）")
        elif st == "自社":
            log(f"  広告: なし。この節の最後に自社サービスの案内を1行だけ書く: 「名入れの記念品づくりを仕組みにする例として、オリシャン開業の詳細は公式ページへ。」→ {ORISHAN_URL}")
        else:
            why = "提携済みの広告がまだない" if st in ("提携済み", "申請候補", "要確認") else "広告なしの困りごと"
            log(f"  広告: なし（{why}）。広告は入れない")
    if any(it["広告の状況"] == "—" for it in items):
        log(f"\n■ 「—（広告なし）」の困りごとがあるので、記事の最後（まとめの後）に次の一文とリンクを1回だけ書く:\n"
            f"  既製品で合わない作業は、仕組みそのものを作ることもできます。\n  [システム開発の相談はこちら]({ACT_URL})")
    if rk:
        log("■ 楽天: フロントマターに `rakuten: 検索語 | 検索語` を書く（上の「楽天の商品リンクを使う」困りごとの道具だけ。商品名・価格は本文に書かない）")
    else:
        log("■ 楽天: この回は使わない（rakuten: は書かない）")
    log(f"■ 書き終えたら `python scripts/autopilot.py jidouka-done {rnd} <slug>` を実行する（topics.txt の done は使わない）")
    if remaining - len(items) < JIDOUKA_LOW:
        log(f"\n■ ランキングの残りが {remaining - len(items)} 個になります（{JIDOUKA_LOW} 個未満）。記事を書いたあと、WebSearch で今の「困りごと」と"
            "新しい自動化（旬のものを優先）を調べ、既存と重ならない困りごとを9個以上 logs/jidouka-new.json に JSON の配列で書き、"
            "`python scripts/autopilot.py jidouka-add logs/jidouka-new.json` を実行する。1件の形（キーと値の種類）:")
        log("  " + json.dumps(dict(zip(RANK_KEYS, ["困りごと", "分野", "今のやり方", "自動化の方法", "道具の例", 4, 4, 3,
                                              "広告のジャンル（data/jidouka-ads.json にある名前。なければ なし）",
                                              "提携済み|楽天で可|申請候補|—", "推定"])), ensure_ascii=False))
        log("  点数は1〜5。「根拠」は調べた出典の名前（なければ「推定」）。「自社の実例」は使わない（実際に作ったものだけの印のため）")
    log("■ 公開済みの記事（切り口・タイトルが重ならないようにする）:")
    for a in articles():
        log(f"  - [{a['_genre']}] {a.get('title', a['_slug'])}  ({a['_slug']})")
    return 0


def research_file(rnd):
    """その回の下調べのファイル（RESEARCH_FRESH_DAYS 日以内に作ったものだけ）。なければ None。"""
    f = RESEARCH_DIR / f"{rnd}.md"
    if not f.exists():
        return None
    age = (datetime.date.fromisoformat(today()) - datetime.date.fromtimestamp(f.stat().st_mtime)).days
    return f if age <= RESEARCH_FRESH_DAYS else None


def cmd_jidouka_research_next():
    """まだ書いていない回を前から2つ見て、下調べのない最初の回を表示する（翌日分の下調べ用）。"""
    ledger = load_json(LEDGER, {})
    for rnd, items in list(rounds_left().items())[:2]:
        if research_file(rnd):
            continue
        log(f"■ 下調べする回: 第 {rnd} 回 → 保存先 {(RESEARCH_DIR / f'{rnd}.md').relative_to(ROOT).as_posix()}")
        for i, it in enumerate(items, 1):
            log(f"\n### {i}. {it['困りごと']}（分野: {it['分野']}）")
            log(f"  自動化の方法: {it['自動化の方法']}")
            log(f"  道具の例: {it['道具の例']}")
            progs = jidouka_programs(it, ledger)
            if progs:
                log(f"  提携中の広告（この中の公式サイトも確かめる）: {' / '.join(progs)}")
        return 0
    log("下調べが必要な回はありません（次の2回分は下調べ済み）")
    return 2


def cmd_jidouka_done(rnd, slug):
    if not any(str(it["記事の回"]) == str(rnd) for it in ranking()):
        log(f"自動化ランキングに第 {rnd} 回がありません")
        return 1
    p = progress()
    p["done"][str(rnd)] = {"slug": slug, "date": today()}
    save_json(PROGRESS, p)
    log(f"自動化記事: 第 {rnd} 回 → {slug} を記録しました（残り {sum(len(v) for v in rounds_left().values())} 個）")
    return 0


def cmd_jidouka_add(path):
    try:
        new = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        log(f"{path} を読めません（{e.__class__.__name__}）")
        return 1
    items = ranking()
    have = {it["困りごと"] for it in items}
    left = rounds_left()
    # 書いていない最後の回が3つに満たなければ、そこから埋める
    last = max((it["記事の回"] for it in items), default=0)
    if last in left and len(left[last]) < 3:
        fill, count_in = last, len(left[last])
    else:
        fill, count_in = last + 1, 0
    added = []
    for d in new if isinstance(new, list) else []:
        if not isinstance(d, dict) or any(k not in d for k in RANK_KEYS) or d["困りごと"] in have:
            continue
        if d.get("根拠") == "自社の実例" or d.get("広告の状況") not in ("提携済み", "楽天で可", "申請候補", "—"):
            continue
        it = {k: d[k] for k in RANK_KEYS}
        try:
            it["合計点"] = int(it["困っている人の多さ"]) * 2 + int(it["自動化のしやすさ"]) + int(it["案内できる広告・サービス"])
        except (TypeError, ValueError):
            continue
        if count_in >= 3:
            fill, count_in = fill + 1, 0
        it["順位"] = len(items) + 1
        it["記事の回"] = fill
        count_in += 1
        items.append(it)
        have.add(it["困りごと"])
        added.append(it["困りごと"])
    if not added:
        log("追加できる困りごとがありませんでした（形が違う・既存と同じ）")
        return 1
    save_json(RANKING, items)
    p = progress()
    p["added"].append({"date": today(), "count": len(added), "items": added})
    save_json(PROGRESS, p)
    log(f"自動化ランキングに {len(added)} 個を追加しました: {' / '.join(added)}")
    return 0


def weekly_due(topics=None):
    """今日書くべき週1本のジャンル（weekly_day の曜日以降で、今週まだ書いていない・未使用ネタがある）。なければ None。"""
    d = datetime.date.fromisoformat(today())
    monday = (d - datetime.timedelta(days=d.weekday())).isoformat()
    topics = topics if topics is not None else [t for t in read_topics() if not t[2]]
    arts = articles()
    for g in weekly_genres():
        if d.weekday() < GENRES[g]["weekly_day"]:
            continue
        if any(a["_genre"] == g and a.get("generated_by") == "autopilot"
               and monday <= (a.get("created") or a.get("date") or "") for a in arts):
            continue
        if any(t[1] == g for t in topics):
            return g
    return None


def cmd_weekly_due():
    g = weekly_due()
    if g:
        log(f"今日は週1本の {g}（{GENRES[g]['label']}）を書く日です")
        return 0
    log("今日は週1本のジャンルを書く日ではありません")
    return 1


def cmd_next():
    done_today = auto_today()
    if len(done_today) >= MAX_PER_DAY:
        log(f"本日の上限（{MAX_PER_DAY}本）に達しています。今日はこれ以上書かないでください。")
        return 2
    daily = jidouka_due()
    if daily:  # 毎日1本の自動化記事を最優先
        return show_jidouka(daily, done_today)
    topics = [t for t in read_topics() if not t[2] and t[1] not in daily_genres()]
    due = weekly_due(topics)
    # 週1本のジャンルは、書く日だけ最優先。それ以外の日は通常の順番に入れない
    if not due:
        topics = [t for t in topics if t[1] not in weekly_genres()]
    if not topics:
        log("未使用のネタがありません。topics-check の手順でネタを補充してください。")
        return 3
    # ジャンルを順番に回す（直近に書いたジャンルの次から）。同じジャンルばかり続けない
    order = [g for g in GENRES if g not in weekly_genres() and g not in daily_genres()]
    state = load_json(STATE, {})
    last = state.get("last_genre")
    start = (order.index(last) + 1) % len(order) if last in order else 0
    rotated = ([due] if due else []) + order[start:] + order[:start]
    topic = next((t for g in rotated for t in topics if t[1] == g), topics[0])
    _, genre, _, keyword = topic
    conf = GENRES[genre]
    ledger = load_json(LEDGER, {})
    ads = [k for k, v in ledger.items() if isinstance(v, dict) and v.get("url") and v.get("genre") in ad_genres(genre)
           and v.get("enabled") is not False]
    arts = articles()
    log(f"■ 本日の自動記事: {len(done_today)}/{MAX_PER_DAY} 本")
    log(f"■ ネタ: {keyword}")
    log(f"■ ジャンル: {genre}（{conf['label']}）")
    log(f"■ 型: {conf['kata']}")
    if conf.get("note"):
        log(f"■ このジャンルのルール（必ず守る）: {conf['note']}")
    if due == genre:
        log("■ 今日は週1本の自社サービス記事の日です（この1本を書いたら、残りは通常のジャンルに戻る）")
    if conf.get("facts"):
        log(f"■ 使ってよい事実: {conf['facts']}（書く前に必ず読む。数字・実績はここにあるものだけ）")
    if conf.get("ads") == "none":
        log("■ 広告: このジャンルはアフィリ広告を入れない注意喚起ジャンルです（注意喚起の表記・相談窓口は自動で入る）")
    elif conf.get("ads") == "own":
        log("■ 広告: 自社サービスの記事です。A8・楽天の広告は入りません（AFFILIATE の差し込み口と rakuten: は書かない）。"
            "PR表記・収益保証なしの注記は自動で入ります。末尾に型どおり CTA 2行を書く")
    else:
        log(f"■ このジャンルの提携中広告: {len(ads)} 件 {('（' + ' / '.join(ads) + '）') if ads else ''}")
    if conf.get("rakuten"):
        log("■ 楽天の商品リンク: このジャンルは使える。本文で道具・本・収納用品など「物」を紹介する記事なら、"
            "フロントマターに `rakuten: 検索語 | 検索語` を書く（読者が実際に買いそうな物を2〜3個。"
            f"例: {conf['rakuten']}）。物を紹介しない記事には書かない")
    if conf.get("images") == "none":
        log("■ 写真: このジャンルは写真を自動で付けません（image_query / image_queries は書かなくてよい）")
    if conf.get("ads") not in ("none", "own") and len(ads) < conf.get("min_ads", 2):
        log(f"  → 広告が足りません。先に `python scripts/autopilot.py ads {genre}` を実行してください。")
    log("■ 公開済みの記事（切り口・タイトルが重ならないようにする）:")
    for a in arts:
        log(f"  - [{a['_genre']}] {a.get('title', a['_slug'])}  ({a['_slug']})")
    return 0


def ad_genres(genre):
    """そのジャンルの記事に使える広告のジャンル（genres.json の ad_genres。省略時は自分だけ）。"""
    return GENRES.get(genre, {}).get("ad_genres") or [genre]


def cmd_done(keyword, slug):
    lines = TOPICS.read_text(encoding="utf-8").splitlines()
    for i, genre, used, kw in read_topics():
        if kw == keyword and not used:
            lines[i] = f"- [x] {kw} → {slug}（{today()}）"
            TOPICS.write_text("\n".join(lines) + "\n", encoding="utf-8")
            if genre not in weekly_genres():  # 週1本のジャンルは通常の順番を動かさない
                state = load_json(STATE, {})
                state["last_genre"] = genre
                save_json(STATE, state)
            log(f"使用済みにしました: {kw} → {slug}")
            return 0
    log(f"topics.txt に未使用のネタ「{keyword}」が見つかりません（印は付けていません）")
    return 1


def cmd_topics_check():
    rem = {g: 0 for g in GENRES if g not in daily_genres()}
    for _, g, used, _ in read_topics():
        if not used and g in rem:
            rem[g] += 1
    low = [g for g, n in rem.items() if n < LOW_TOPICS]
    for g, n in rem.items():
        log(f"{g}（{GENRES[g]['label']}）: 残り {n} 件{'  ← 補充が必要' if g in low else ''}")
    if low:
        log("補充の仕方: topics.txt の該当ジャンルの見出しの下に「- [ ] キーワード」を10行足す。"
            "既存のネタ（使用済み含む）と同じ・似た切り口は避ける。")
    return 0


# ---------------------------------------------------------------------------
# A8（エンジン②）
# ---------------------------------------------------------------------------
def set_state(**kw):
    state = load_json(STATE, {})
    state.update(kw)
    save_json(STATE, state)


def a8_logged_in():
    """ログイン済みの Chrome（ポート9333）があるか。なければ起動して、保存済みのログインで入れるか試す。"""
    from a8_browser import CDP_PORT, CDP_URL, CHROME_PATHS, MEDIA_CONSOLE, PROFILE, logged_in_url

    def tabs():
        try:
            with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=3) as r:
                return [t["url"] for t in json.loads(r.read().decode("utf-8")) if t.get("type") == "page"]
        except Exception:
            return None

    if tabs() is None:
        chrome = next((p for p in CHROME_PATHS if Path(p).exists()), None)
        if not chrome:
            return False
        subprocess.Popen([chrome, f"--user-data-dir={PROFILE}", f"--remote-debugging-port={CDP_PORT}",
                          "--remote-debugging-address=127.0.0.1", "--no-first-run", "--window-size=1280,900", MEDIA_CONSOLE],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        for _ in range(20):
            time.sleep(1)
            if tabs() is not None:
                break
    from a8_browser import auto_login

    def usable(page):
        """メディア管理画面を開いて、ログイン画面に飛ばされないか。
        トップは開けても、提携の画面だけ再認証を求められることがある。取り込みで使う画面まで確かめる"""
        for url in (MEDIA_CONSOLE, MEDIA_CONSOLE.rstrip("/") + "/program/list/partnered?pageNo=1&pageSize=100"):
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            if not logged_in_url(page.url):
                return False
        return True

    fail = ""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL, timeout=60000)
            page = browser.contexts[0].new_page()
            ok = usable(page)
            if not ok:  # 切れていたら、保存済みの ID・パスワードで自動でログインし直す
                ok, fail = auto_login(page)
                ok = ok and usable(page)
                if ok:
                    log("[A8] ログインが切れていたため、自動でログインし直しました。")
                else:
                    fail = fail or "自動ログイン後も提携の画面を開けませんでした"
                    log(f"[A8] 自動ログインできませんでした: {fail}")
                    try:
                        SHOTS_DIR = ROOT / ".secrets" / "shots"
                        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(SHOTS_DIR / "a8-login-error.png"))
                    except Exception:
                        pass
            page.close()  # 人にログインさせる作りにしない（ログイン画面を開いたまま残さない）
    except Exception as e:
        log(f"[A8] 確認中にエラー: {e.__class__.__name__}")
        ok = False
        fail = f"確認中にエラー（{e.__class__.__name__}）"
    set_state(a8_login_ok=ok, a8_checked_at=datetime.datetime.now().isoformat(timespec="minutes"),
              a8_login_fail="" if ok else fail)
    return ok


def fetch(*args):
    # 子プロセスのエラー出力も UTF-8 にそろえる（cp932 のままだと読み取りで落ちて、A8 側の本当のエラーが見えない）
    env = {**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch-affiliates.py"), *args],
                       cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, env=env)
    print(r.stdout.strip())
    if r.returncode != 0:
        print(r.stderr.strip()[-800:])
    # A8 を実際に使った結果で、ログイン状態の記録を更新する（STATUS.md が古い「OK」のまま残らないように）
    now = datetime.datetime.now().isoformat(timespec="minutes")
    if "ログインが切れています" in r.stdout + r.stderr:
        set_state(a8_login_ok=False, a8_checked_at=now)
    elif r.returncode == 0:
        set_state(a8_login_ok=True, a8_checked_at=now)
    return r.returncode


def a8_status():
    """(ログインが今も生きていると言えるか, STATUS.md に出す文)。
    A8 はブラウザを閉じるとログインが消える。PC の再起動・シャットダウンをまたいだ「OK」や、6時間より古い「OK」は信用しない。"""
    state = load_json(STATE, {})
    at = state.get("a8_checked_at", "")
    if state.get("a8_login_ok") is None or not at:
        return False, "未確認"
    if state.get("a8_login_ok") is False:
        return False, f"**切れている**（{at.replace('T', ' ')} 確認）"
    checked = datetime.datetime.fromisoformat(at)
    try:
        import ctypes
        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        boot = datetime.datetime.now() - datetime.timedelta(milliseconds=ctypes.windll.kernel32.GetTickCount64())
    except (AttributeError, OSError):
        boot = datetime.datetime.min
    if checked < boot:
        return False, f"**切れている見込み**（{at.replace('T', ' ')} にはOK。その後 PC が再起動したためログインは消えています）"
    if datetime.datetime.now() - checked > datetime.timedelta(hours=6):
        return False, f"不明（{at.replace('T', ' ')} にはOK。6時間以上たっているので、今も使えるかは次の確認まで分かりません）"
    return True, f"OK（{at.replace('T', ' ')} 確認）"


def cmd_a8_check():
    ok = a8_logged_in()
    log("[A8] ログイン済み。自動で提携作業ができます。" if ok else
        "[A8] ログインできませんでした（自動ログインも不可。理由は STATUS.md）。A8 の作業はとばして、記事の作成は続ける。")
    return 0 if ok else 1


def cmd_ads(genre):
    if genre not in GENRES:
        log(f"ジャンル {genre} は data/genres.json にありません")
        return 1
    conf = GENRES[genre]
    if conf.get("ads") == "none" or not conf.get("a8_search"):
        log(f"[{genre}] このジャンルは A8 の提携を使いません。")
        return 0
    ledger = load_json(LEDGER, {})
    have = [k for k, v in ledger.items() if isinstance(v, dict) and v.get("url") and v.get("genre") in ad_genres(genre)]
    if len(have) >= conf.get("min_ads", 2):
        log(f"[{genre}] 提携中の広告が {len(have)} 件あります。新しい申請は不要です。")
        return 0
    if not a8_logged_in():
        log("[A8] ログインが切れているため、提携作業はスキップしました（記事は広告なしで公開され、提携後に自動で広告が入ります）。")
        return 0
    cands = load_json(CANDIDATES, [])
    searched = any(c.get("genre") == genre for c in cands)
    if not searched:
        log(f"[{genre}] A8 で広告を検索します: {' / '.join(conf['a8_search'])}")
        fetch("search", "--genre", genre)
    log(f"[{genre}] 未申請の候補に提携申請します（1回 最大15件）")
    fetch("apply", "--genre", genre, "-n", "15")
    log("即時提携の分は広告リンクを取り込みます")
    fetch("links")
    return 0


def cmd_sync():
    if not a8_logged_in():
        log("[A8] ログインが切れているため、承認の取り込みはスキップしました。")
        return 0
    before = {k for k, v in load_json(LEDGER, {}).items() if isinstance(v, dict) and v.get("url")}
    fetch("links")
    after = {k for k, v in load_json(LEDGER, {}).items() if isinstance(v, dict) and v.get("url")}
    new = sorted(after - before)
    log(f"新しく使えるようになった広告: {len(new)} 件 {' / '.join(new)}")
    n = jidouka_sync()
    if n:
        log(f"自動化ランキング: 広告の状況を「提携済み」にした困りごと {n} 件")
    return 0


def cmd_review():
    ledger = load_json(LEDGER, {})
    todo = {k: v for k, v in ledger.items() if isinstance(v, dict) and v.get("needs_review")}
    if not todo:
        log("整備が必要な案件はありません。")
        return 0
    log("次の案件は label / catch / keywords / table を整えて、needs_review を消してください。"
        "根拠は ad_copies と案件名だけ（書いていない数字は書かない。分からない table の項目は空欄のまま）:")
    for k, v in todo.items():
        cols = GENRES.get(v.get("genre"), {}).get("table_columns", {})
        log(f"\n## {k}（genre: {v.get('genre')}）  table の意味: " + " / ".join(f"{c}={n}" for c, n in cols.items()))
        for c in list(dict.fromkeys(v.get("ad_copies", [])))[:8]:
            log(f"   {c}")
    return 0


# ---------------------------------------------------------------------------
# STATUS.md
# ---------------------------------------------------------------------------
def auto_on():
    r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST"], capture_output=True, text=True)
    if r.returncode != 0:
        return False, ""
    m = re.search(r"(Next Run Time|次回の実行時刻):\s*(.+)", r.stdout)
    return True, (m.group(2).strip() if m else "")


def cmd_status():
    arts = articles()
    ledger = {k: v for k, v in load_json(LEDGER, {}).items() if isinstance(v, dict) and v.get("url")}
    cands = load_json(CANDIDATES, [])
    pids = {v.get("program_id") for v in ledger.values()}
    pending = [c for c in cands if c.get("apply_result") == "申請完了" and c["program_id"] not in pids]
    topics = read_topics()
    state = load_json(STATE, {})
    on, next_run = auto_on()
    labels = {g: c["label"] for g, c in GENRES.items()}

    def by(items, key):
        out = {}
        for it in items:
            out[key(it)] = out.get(key(it), 0) + 1
        return out

    a_by = by(arts, lambda a: a["_genre"])
    l_by = by(ledger.values(), lambda v: v.get("genre") or "cleaning")
    p_by = by(pending, lambda c: c.get("genre") or "")
    t_by = by([t for t in topics if not t[2]], lambda t: t[1])
    rows = []
    for g in list(GENRES) + [x for x in a_by if x not in GENRES]:
        rows.append(f"| {labels.get(g, g)} | {a_by.get(g, 0)} | {l_by.get(g, 0)} | {p_by.get(g, 0)} | "
                    f"{t_by.get(g, '-') if g in GENRES else '-'} |")
    alerts = []
    a8_ok, a8_text = a8_status()
    if state.get("a8_login_ok") is False:  # 自動ログインも通らなかったときだけ（再起動で切れただけなら次の実行で自動で入り直す）
        alerts.append(f"- A8: 自動ログインできませんでした（{state.get('a8_login_fail') or '理由不明'}・"
                      f"{state.get('a8_checked_at', '').replace('T', ' ')}）。広告の取り込み・新しい提携は止まっています"
                      "（記事の作成・公開は続きます）。毎朝自動で再試行します（人のログインは不要）。パスワードを変えた・入力ミスのときだけ setup-once.bat で入れ直してください。")
    elif not a8_ok:
        a8_text += "→ 次の自動実行で自動ログイン"
    note_st = load_json(ROOT / "state" / "note-status.json", {})
    note_log = load_json(ROOT / "data" / "note.json", {})
    if note_st and not note_st.get("ok"):
        alerts.append(f"- note: {note_st.get('text')}（{note_st.get('at')}）。note への投稿だけ見送っています（ブログ記事は続きます）。"
                      "毎朝自動で再試行します（人のログインは不要）。パスワードを変えた・入力ミスのときだけ setup-once.bat で入れ直してください。")
    nc = load_json(ROOT / "data" / "note-comments.json", {})
    if nc.get("stopped"):
        alerts.insert(0, f"- **note のコメント返信を止めています**（{nc['stopped']['reason'][:120]}・{nc['stopped']['at']}）。記事の投稿は続けています。"
                         "制限が解けたのを確かめてから `python scripts/note-comments.py resume` で再開します。")
    nc_review = [c for c in nc.get("comments", {}).values() if c.get("action") == "review" and not c.get("resolved_at")]
    if nc_review:
        alerts.insert(0, "- **要確認のコメント**（note・返信していません。確認したら `python scripts/note-comments.py resolve <key>`）:" + "".join(
            f"\n  - 記事「{c.get('title', '')[:30]}」{c.get('nickname', '')} さん: {c.get('summary') or c.get('text', '')[:40]}（{c.get('reason', '')}）"
            for c in nc_review))
    nc_all = list(nc.get("comments", {}).values())
    note_comment_text = (f"返信 {sum(1 for c in nc_all if c.get('action') == 'replied')} 件・見送り {sum(1 for c in nc_all if c.get('action') == 'skipped')} 件"
                         f"・要確認 {len(nc_review)} 件" + ("・**止めています**" if nc.get("stopped") else ""))
    claude_tok = load_json(ROOT / ".secrets" / "claude-token.json", {})
    if claude_tok.get("expires"):
        left = (datetime.date.fromisoformat(claude_tok["expires"]) - datetime.date.today()).days
        claude_text = f"長期トークン（期限 {claude_tok['expires']}・あと {left} 日）"
        if left <= 30:
            alerts.insert(0, f"- **Claude Code のトークンの期限が近い**（{claude_tok['expires']}・あと {left} 日）。"
                             "期限が切れると記事が書けなくなります。setup-once.bat をダブルクリックしてトークンを取り直してください（3/3 だけ行います）。")
    else:
        claude_text = "**長期トークン未設定**（通常のログインで動作。ログインが切れると記事が0本になる）"
        alerts.append("- Claude Code の長期トークンが未設定です。setup-once.bat をダブルクリックして設定してください。")
    # X は X API（Cloudflare の x-poster が投稿・朝の x-plan.py が記録を data/x.json に写す）。ブラウザでのログイン（x-run.py）は 2026-10-16 にやめた
    import social
    x_alert = social.x_api_alert()
    if x_alert:
        alerts.insert(0, x_alert)
    x_reply = social.x_reply_alert()      # X のリプライは自動で返さない。件数と URL を知らせるだけ
    if x_reply:
        alerts.insert(0, x_reply)
    x_cfg = load_json(ROOT / "data" / "social.json", {}).get("x_api", {})
    x_log = load_json(ROOT / "data" / "x.json", {})
    x_month = x_log.get("months", {}).get(datetime.date.today().strftime("%Y-%m"), {})
    x_keys = social.load_x_keys()
    x_q = load_json(ROOT / "data" / "x-queue" / f"{datetime.date.today().isoformat()}.json", {})
    x_text = ("止めています（data/social.json の x_api.enabled が false）" if not x_cfg.get("enabled") else
              "準備中（X API のキーがまだありません）" if not x_keys else
              f"**オン**（X API・Cloudflare が5分おきに確認して投稿。{x_cfg.get('start')} から"
              f"・今日の予定 {len(x_q.get('items', []))} 件"
              + (f"（{' / '.join(i['at'][11:] for i in x_q['items'])}）" if x_q.get("items") else "")
              + f"・今月 {x_month.get('posts', 0)} 件・概算 ${x_month.get('cost', 0):.2f} / 上限 ${x_cfg.get('budget_usd_month')}）")
    note_text = (f"{'**オン**' if load_json(ROOT / 'data' / 'social.json', {}).get('note', {}).get('enabled') else 'オフ'}"
                 f"（投稿済み {len(note_log)} 本・ログイン: {note_st.get('text', '未確認')}）")
    review = [k for k, v in ledger.items() if v.get("needs_review")]
    if review:
        alerts.append(f"- 新しく提携した広告 {len(review)} 件の説明文が未整備です（次の自動実行で整えます）: {' / '.join(review)}")
    recent = ""
    if LOG.exists():
        lines = [l for l in LOG.read_text(encoding="utf-8", errors="replace").splitlines() if l.startswith("==")]
        recent = "\n".join(f"- {l.strip('= ')}" for l in lines[-5:])
    now_s = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M")
    sched = sorted((a["publish_at"], a.get("title", a["_slug"])) for a in arts if a.get("publish_at", "") > now_s)
    sched_md = "\n".join(f"- {t.replace('T', ' ')}　{title}" for t, title in sched) or "- なし"
    r_ok, r_why = refresh_active()
    r_log = load_json(REFRESH_LOG, [])
    refresh_md = "\n".join(f"| {e['date']} | {e['slug']} | {e['result']} | {e['note']} |" for e in reversed(r_log[-15:]))
    refresh_md = ("| 日付 | 記事 | 結果 | 内容 |\n|---|---|---|---|\n" + refresh_md) if refresh_md else "まだありません"
    days = "月火水木金土日"
    week_start = (datetime.date.today() - datetime.timedelta(days=datetime.date.today().weekday())).isoformat()
    weekly_md = "\n".join(
        f"| {GENRES[g]['label']}（週1本・{days[GENRES[g]['weekly_day']]}曜） | 今週: "
        + ("書いた" if any(a["_genre"] == g and a.get("generated_by") == "autopilot"
                         and week_start <= (a.get("created") or a.get("date") or "") for a in arts) else "まだ")
        + f"・残りネタ {t_by.get(g, 0)} 件 |" for g in weekly_genres())
    if daily_genres() and ranking():
        left, prog = rounds_left(), progress()
        add = prog["added"][-1] if prog["added"] else None
        weekly_md += (f"\n| 自動化記事（毎日1本） | 今日: {'書いた' if any(a['_genre'] in daily_genres() for a in auto_today()) else 'まだ'}"
                      f"・書いた回 {len(prog['done'])}・残り {sum(len(v) for v in left.values())} 個（{len(left)} 回分）"
                      + (f"・ランキングに追加: {add['date']} {add['count']} 個（{' / '.join(add['items'][:3])} など）" if add else "") + " |")
    md = f"""# ブログ工場 STATUS

最終更新: {datetime.datetime.now():%Y-%m-%d %H:%M}（記事の公開・自動実行のたびに自動更新）

{"## ⚠ 要対応" + chr(10) + chr(10).join(alerts) + chr(10) if alerts else ""}
## 全体

| 項目 | 状態 |
|---|---|
| 公開済み記事 | {len(arts) - len(sched)} 本（うち自動生成 {sum(1 for a in arts if a.get('generated_by') == 'autopilot') - len(sched)} 本）＋ 公開予定 {len(sched)} 本 |
| 提携中の広告 | {len(ledger)} 件 |
| 審査待ち | {len(pending)} 件 |
| 残りネタ | {sum(t_by.values())} 件 |
| 毎日の自動投稿 | {"**オン**（毎朝起動→記事を書いて予約公開→シャットダウン。次回: " + next_run + "）" if on else "オフ（「毎日の自動投稿をオンにして」でオン）"} |
| 記事の見直し | {"**稼働中**（週 " + str(refresh_cfg()["per_week"]) + " 本まで・実質的な改善のときだけ更新日を変える）" if r_ok else r_why} |
| 1日の上限 | 自動生成 {MAX_PER_DAY} 本まで |
{weekly_md}
| Claude Code ログイン | {claude_text} |
| A8 ログイン | {a8_text} |
| X 自動ポスト | {x_text} |
| note 自動投稿 | {note_text} |
| note のコメント返信 | {note_comment_text} |

## ジャンル別

| ジャンル | 記事 | 提携中 | 審査待ち | 残りネタ |
|---|---|---|---|---|
{chr(10).join(rows)}

## 公開予定（予約公開。時刻が来た瞬間から自動で表示される）

{sched_md}

## 記事の見直し（直近15件）

{refresh_md}

## 最近の自動実行

{recent or "- まだありません"}
"""
    STATUS.write_text(md, encoding="utf-8")
    log(f"STATUS.md を更新しました（記事 {len(arts)} / 提携中 {len(ledger)} / 審査待ち {len(pending)} / 残りネタ {sum(t_by.values())}）")
    return 0


# ---------------------------------------------------------------------------
# 公開済み記事の見直し（scripts/refresh-prompt.md の手順で Claude Code が行う）
# 更新日（updated）を新しくするのは、本文が実質的に変わったときだけ。日付だけの更新は refresh-done が拒否する
# ---------------------------------------------------------------------------
REFRESH_CFG = DATA / "refresh.json"
REFRESH_LOG = DATA / "refresh-log.json"


def refresh_cfg():
    cfg = {"mode": "auto", "min_articles": 20, "per_week": 3, "min_age_days": 30, "min_changed_chars": 150}
    cfg.update(load_json(REFRESH_CFG, {}))
    return cfg


def article_path(slug):
    for md in (ROOT / "content").rglob("*.md"):
        meta, _ = front_matter(md)
        if (meta.get("slug") or md.stem) == slug:
            return md
    return None


def published_articles():
    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M")
    return [a for a in articles() if not (a.get("publish_at") and a["publish_at"] > now)]


def refresh_active():
    cfg = refresh_cfg()
    n = len(published_articles())
    if cfg["mode"] == "off":
        return False, "記事の見直しはオフです（data/refresh.json の mode）"
    if cfg["mode"] == "auto" and n < cfg["min_articles"]:
        return False, f"公開済み記事が {n} 本です。{cfg['min_articles']} 本以上になったら自動で見直しを始めます"
    return True, ""


def refresh_quota_left():
    week_ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    done = [e for e in load_json(REFRESH_LOG, []) if e["date"] > week_ago]
    return refresh_cfg()["per_week"] - len(done)


def cmd_refresh_next():
    ok, why = refresh_active()
    if not ok:
        log(why)
        return 2
    if refresh_quota_left() <= 0:
        log(f"今週の見直し（{refresh_cfg()['per_week']}本）は済んでいます。今回は見直しません。")
        return 2
    cfg = refresh_cfg()
    checked = {}
    for e in load_json(REFRESH_LOG, []):
        checked[e["slug"]] = max(checked.get(e["slug"], ""), e["date"])
    limit = (datetime.date.today() - datetime.timedelta(days=cfg["min_age_days"])).isoformat()
    cands = []
    for a in published_articles():
        last = max(a.get("updated") or "", a.get("date") or "", checked.get(a["_slug"], ""))
        if last <= limit:
            cands.append((last, a))
    if not cands:
        log(f"見直す記事がありません（どの記事も {cfg['min_age_days']} 日以内に公開・見直し済み）。")
        return 2
    last, a = sorted(cands, key=lambda x: x[0])[0]
    path = article_path(a["_slug"])
    ledger = {k: v for k, v in load_json(LEDGER, {}).items() if isinstance(v, dict) and v.get("url")}
    genre = a["_genre"]
    progs = [(k, v) for k, v in ledger.items() if (v.get("genre") or "cleaning") == genre]
    log(f"見直す記事: {a.get('title', a['_slug'])}")
    log(f"  slug: {a['_slug']} / ファイル: {path.relative_to(ROOT).as_posix()} / ジャンル: {genre}")
    log(f"  公開日: {a.get('date', '-')} / 最終更新: {a.get('updated', '-')} / 最後の見直し: {checked.get(a['_slug'], '-')}")
    log(f"  記事で指定している広告: {a.get('affiliates', '（指定なし＝内容に合わせて自動）')}")
    log(f"  このジャンルの提携中の広告（{len(progs)}件）:")
    for k, v in progs:
        log(f"    - {k}: {v.get('label', '')} {v.get('catch', '')}")
    log(f"今週あと {refresh_quota_left()} 本見直せます。")
    return 0


def git_head_text(path):
    r = subprocess.run(["git", "show", f"HEAD:{path.relative_to(ROOT).as_posix()}"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    return r.stdout if r.returncode == 0 else None


def front_matter_text(text):
    tmp = ROOT / "logs" / "_fm_tmp.md"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    try:
        return front_matter(tmp)
    finally:
        tmp.unlink()


def changed_chars(old, new):
    import difflib
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")


def set_meta_line(path, key, value):
    text = path.read_text(encoding="utf-8")
    head, rest = text.split("\n", 1)
    end = re.search(r"^---\s*$", rest, re.M)
    fm, tail = rest[: end.start()], rest[end.start():]
    lines = [l for l in fm.splitlines() if not re.match(rf"^{key}\s*:", l)]
    if value:
        lines.append(f"{key}: {value}")
    nl = "\r\n" if b"\r\n" in path.read_bytes() else "\n"   # ファイルの改行コードはそのまま
    path.write_text(head + "\n" + "\n".join(lines) + "\n" + tail, encoding="utf-8", newline=nl)


def refresh_record(slug, result, note):
    entries = load_json(REFRESH_LOG, [])
    entries.append({"date": today(), "slug": slug, "result": result, "note": note})
    save_json(REFRESH_LOG, entries[-500:])


def cmd_refresh_done(slug, note):
    path = article_path(slug)
    if not path:
        log(f"記事 {slug} が見つかりません")
        return 1
    old = git_head_text(path)
    if old is None:
        log("この記事はまだ GitHub に送られていません（見直しの対象外）")
        return 1
    old_meta, old_body = front_matter_text(old)
    new_meta, new_body = front_matter(path)
    body_diff = changed_chars(old_body, new_body)
    meta_changed = [k for k in ("title", "description", "affiliates") if old_meta.get(k, "") != new_meta.get(k, "")]
    if body_diff >= refresh_cfg()["min_changed_chars"] or "affiliates" in meta_changed:
        set_meta_line(path, "updated", today())
        refresh_record(slug, "更新", note)
        log(f"更新日を {today()} にしました（本文の変更 {body_diff} 字{'・' + '/'.join(meta_changed) if meta_changed else ''}）。"
            "publish.py で公開してください。")
        return 0
    # 実質的な変更がないのに日付だけ新しくするのは禁止（Google にスパム扱いされる）
    set_meta_line(path, "updated", old_meta.get("updated", ""))
    refresh_record(slug, "見直しのみ", f"変更が小さいため更新日は据え置き（本文 {body_diff} 字）: {note}")
    log(f"本文の変更が {body_diff} 字で、実質的な改善とは言えないため更新日は変えません（見直し済みとして記録）。"
        "小さな修正は publish.py でそのまま公開して構いません。")
    return 1


def cmd_refresh_skip(slug, note):
    path = article_path(slug)
    if path:
        old = git_head_text(path)
        if old is not None:
            set_meta_line(path, "updated", front_matter_text(old)[0].get("updated", ""))
    refresh_record(slug, "見直しのみ", note)
    log(f"見直し済み（変更なし）として記録しました: {slug}")
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    if cmd == "next":
        return cmd_next()
    if cmd == "done" and len(rest) == 2:
        return cmd_done(*rest)
    if cmd == "ads" and rest:
        return cmd_ads(rest[0])
    if cmd == "sync":
        return cmd_sync()
    if cmd == "a8-check":
        return cmd_a8_check()
    if cmd == "weekly-due":
        return cmd_weekly_due()
    if cmd == "jidouka-done" and len(rest) == 2:
        return cmd_jidouka_done(*rest)
    if cmd == "jidouka-add" and rest:
        return cmd_jidouka_add(rest[0])
    if cmd == "jidouka-sync":
        return cmd_jidouka_sync()
    if cmd == "jidouka-research-next":
        return cmd_jidouka_research_next()
    if cmd == "topics-check":
        return cmd_topics_check()
    if cmd == "review":
        return cmd_review()
    if cmd == "status":
        return cmd_status()
    if cmd == "refresh-next":
        return cmd_refresh_next()
    if cmd == "refresh-done" and len(rest) == 2:
        return cmd_refresh_done(*rest)
    if cmd == "refresh-skip" and len(rest) == 2:
        return cmd_refresh_skip(*rest)
    if cmd == "refresh-mode" and rest and rest[0] in ("auto", "on", "off"):
        cfg = load_json(REFRESH_CFG, {})
        cfg["mode"] = rest[0]
        save_json(REFRESH_CFG, cfg)
        log(f"記事の見直し: {rest[0]}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
