#!/usr/bin/env python3
"""
X（@maf_media_jp）のその日の投稿を作って予定表（data/x-queue/）に置き、GitHub に送る（毎朝 boot-run.py の「5. X」から呼ばれる）。
実際の投稿は PC が切れていても出せるように Cloudflare Workers（workers/x-poster・5分おき）が GitHub の予定表を読んで行う（scripts/xpost.py）。

  python scripts/x-plan.py                 … 今日の分を作って GitHub へ（もうあれば何もしない）
  python scripts/x-plan.py --date 2026-10-17 [--force] [--no-push] [--out data/x-queue/rehearsal-2026-10-17.json]
  python scripts/x-plan.py show [日付]     … 予定表を表示

  python scripts/x-plan.py samples         … 投稿の型5種類のサンプルを1つずつ作って data/content-samples-x.md と data/content-samples-x/ に保存（投稿しない）

決まり（data/social.json の x_api・書き方は data/content-guide.md の「共通の土台」と「X」）:
  ・時間帯は windows（朝 7:00〜9:00・昼 12:00〜13:00・夜 19:00〜21:00）の中でランダム
  ・1〜10日目（start の日が1日目）: 1日2件。別々の帯で、2件の間は3時間以上。11日目から: 1日3件（各帯に1件）
  ・リンク付きは1日1件だけ（画像＋説明文＋リンク。その日の自動化記事の紹介。水曜はオリシャンの記事）
  ・残りは文章の投稿。型（実物を見せる・体験＋意見・まとめ・Before/After）を日替わりで回し、火・金は1件を問いかけの型にする（週2回）
  ・どの投稿にも内容に合った図解カードの画像を付ける（カードの言葉が条件を満たさないときだけ画像なし）
  ・リンク付きは記事の公開予定時刻（publish_at）の20分後以降に置く。合う枠がなければ、すでに公開済みの同じ種類の最新記事を紹介する
  ・リンクには ?utm_source=x
  ・1か月の概算が予算（budget_usd_month）を超えそうなら、文章だけの投稿を減らす
  ・X へのリプライは自動で返さない。朝に数えて data/x.json の replies に入れ、STATUS.md の先頭に件数と URL を出す（返信は人が行う）
文面は Claude Code（claude -p・scripts/x-prompt.md）が書き、ここで禁止語・1行目の長さ・ハッシュタグ・型の形・過去との重複を確かめる。
"""
import calendar
import datetime
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import social  # noqa: E402
from claude_env import claude_env  # noqa: E402

ROOT = social.ROOT
QUEUE = ROOT / "data" / "x-queue"
XLOG = ROOT / "data" / "x.json"
PROMPT = ROOT / "scripts" / "x-prompt.md"
LOGFILE = ROOT / "logs" / "x-plan.log"
REHEARSAL = bool(os.environ.get("BLOG_REHEARSAL"))
FMT = "%Y-%m-%dT%H:%M"
BANNED = ["自動投稿", "自動で投稿", "予約投稿", "自動で回", "SNSを自動", "bot", "ボット", "このアカウント", "毎朝PC",
          "必ず", "誰でも", "今すぐ", "絶対", "万円", "0分に", "放置で", "劇的", "爆速", "100%", "確実に", "稼げ"]
KIND_LABEL = {"link": "記事の紹介", "jitsubutsu": "実物を見せる型", "taiken": "体験＋意見の型", "matome": "まとめの型",
              "beforeafter": "Before/After の型", "toikake": "問いかけの型",
              "tip": "自動化の小ワザ", "hitokoto": "困りごとへのひと言", "tsumazuki": "つまずいた話"}   # 下の3つは 10/17 までの型
SAMPLE_KINDS = ["jitsubutsu", "taiken", "matome", "beforeafter", "toikake"]
GUIDE = ROOT / "data" / "content-guide.md"


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {'[通し確認] ' if REHEARSAL else ''}{msg}"
    print(line, flush=True)
    LOGFILE.parent.mkdir(exist_ok=True)
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def cfg():
    return social.load_config()["x_api"]


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 記事
# ---------------------------------------------------------------------------
def read_meta(path):
    text = path.read_text(encoding="utf-8").lstrip("﻿")
    meta = {}
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    meta["_path"] = path
    meta.setdefault("slug", path.stem)
    return meta


def public_at(meta):
    """記事が見えるようになる時刻（publish_at。なければ公開日の 0:00）。"""
    if meta.get("publish_at"):
        return datetime.datetime.strptime(meta["publish_at"], FMT)
    return datetime.datetime.strptime((meta.get("date") or "2000-01-01")[:10], "%Y-%m-%d")


def pick_article(day, genre, slot_times):
    """紹介する記事と、リンク付き投稿の時刻。(meta, time, 今日の記事か)"""
    arts = [read_meta(p) for p in (ROOT / "content" / "ja").glob("*.md")]
    arts = [a for a in arts if a.get("genre") == genre and a.get("x") != "none"]
    if not arts:
        return None, None, False
    todays = [a for a in arts if (a.get("created") or a.get("date", ""))[:10] == day.isoformat()]
    for a in sorted(todays, key=public_at):
        ok = [t for t in slot_times if t >= public_at(a) + datetime.timedelta(minutes=20)]
        if ok:
            return a, random.choice(ok), True
    # 今日の記事が枠の時刻までに出ない → すでに出ている同じ種類の最新記事
    t = random.choice(slot_times)
    shown = [a for a in arts if public_at(a) <= t - datetime.timedelta(minutes=20)]
    if not shown:
        return None, None, False
    return max(shown, key=public_at), t, False


# ---------------------------------------------------------------------------
# 時刻
# ---------------------------------------------------------------------------
def day_number(day):
    return (day - datetime.date.fromisoformat(cfg()["start"])).days + 1


def at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.datetime.combine(day, datetime.time(h, m))


def slot_times(day, n, earliest):
    """その日の n 件の時刻（昇順）。earliest より前は使わない。"""
    c = cfg()
    wins = [(max(at(day, a), earliest), at(day, b)) for a, b in c["windows"]]
    wins = [(lo, hi) for lo, hi in wins if lo <= hi]          # もう過ぎた帯（起動が遅れた日）は使わない
    rand = lambda lo, hi: lo + datetime.timedelta(minutes=random.randint(0, int((hi - lo).total_seconds() // 60)))
    if day_number(day) > c["phase1_days"]:
        return sorted(rand(lo, hi) for lo, hi in sorted(random.sample(wins, min(n, len(wins)))))
    gap = datetime.timedelta(hours=c["phase1_min_gap_hours"])
    ts = []
    for _ in range(5000):
        ts = sorted(rand(lo, hi) for lo, hi in random.sample(wins, min(n, len(wins))))
        if all(b - a >= gap for a, b in zip(ts, ts[1:])):
            return ts
    return ts[:1]    # 3時間あけられない（起動が遅れた日）→ 1件だけ


def text_kinds(day, n):
    """その日の文章の投稿の型（日替わりで回す。question_weekdays の日は最後の1件を問いかけの型に）。"""
    c = cfg()
    cyc, qdays = c["types_cycle"], c.get("question_weekdays", [])
    per_day = lambda d: (c["phase1_per_day"] if day_number(d) <= c["phase1_days"] else c["phase2_per_day"]) - 1
    start, d = 0, datetime.date.fromisoformat(c["start"])
    while d < day:                     # これまでに回した型の数（問いかけの日の1件は数えない＝型を飛ばさない）
        start += per_day(d) - (1 if d.weekday() in qdays and per_day(d) else 0)
        d += datetime.timedelta(days=1)
    q = 1 if n and day.weekday() in qdays else 0
    return [cyc[(start + i) % len(cyc)] for i in range(n - q)] + ["toikake"] * q


# ---------------------------------------------------------------------------
# 費用
# ---------------------------------------------------------------------------
def month_spent(day):
    xl = load_json(XLOG, {})
    return float(xl.get("months", {}).get(day.strftime("%Y-%m"), {}).get("cost", 0.0))


def decide_text_count(day, n_text):
    """1か月の概算が予算を超えそうなら、文章だけの投稿を減らす。(件数, 説明)"""
    c = cfg()
    left_days = calendar.monthrange(day.year, day.month)[1] - day.day + 1
    spent = month_spent(day)
    for k in range(n_text, -1, -1):
        est = spent + left_days * (c["price_link_post"] + c["price_media"] + k * c["price_text_post"])
        if est <= c["budget_usd_month"]:
            note = f"今月の概算 ${est:.2f}（使った分 ${spent:.2f}＋残り {left_days} 日分）"
            return k, note + ("" if k == n_text else f" → 予算 ${c['budget_usd_month']} に収めるため文章だけの投稿を {n_text - k} 件減らしました")
    return 0, f"今月の概算が予算 ${c['budget_usd_month']} を超えそうです（文章だけの投稿は0件・リンク付きだけ）"


# ---------------------------------------------------------------------------
# 文面
# ---------------------------------------------------------------------------
def history_texts(limit=40):
    """過去の投稿と、これまでの予定表の文面（重複を避けるため）。"""
    texts = [p.get("text", "") for p in load_json(XLOG, {}).get("posts", [])]
    for f in sorted(QUEUE.glob("20*.json"))[-14:]:
        texts += [it["text"] for it in load_json(f, {}).get("items", [])]
    seen, out = set(), []
    for t in reversed(texts):
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def problems_of(meta):
    body = meta["_path"].read_text(encoding="utf-8")
    hs = re.findall(r"^##\s+\d+\.\s*(.+?)(?:を自動化する.*)?$", body, re.M)
    return " / ".join(hs) or meta.get("title", "")


def guide_text():
    """data/content-guide.md の「共通の土台」と「X」の部分（note の部分は渡さない）。"""
    text = GUIDE.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(?=## )", text)
    keep = [p for p in parts if p.startswith("## 共通の土台") or p.startswith("## X")]
    return "\n\n".join(p.strip() for p in keep)


def dm_ok(day):
    return day.weekday() in cfg().get("dm_weekdays", [])


def ask_claude(day, art, kinds, out, feedback="", with_link=True):
    wed = day.weekday() == 2 and with_link
    specs = "\n".join(f"{i}. {k}（{KIND_LABEL[k]}）" for i, k in enumerate(kinds, 1)) or "（今日はなし。texts は空の配列）"
    link_section = ("リンク付きの投稿 1 件: 下の「紹介する記事」の紹介。記事の中の困りごとから 1 つを選び、その困りごとと解決の仕方を、人の言葉で短く書く"
                    "（画像＋説明文＋リンクの形。リンクだけの投稿にしない）。URL は書かない（あとでプログラムが足す）。"
                    "あわせて、画像（困りごと → 自動化後 のカード）に載せる短い言葉も書く\n" if with_link else
                    "リンク付きの投稿は今回はなし。\n")
    prompt = (PROMPT.read_text(encoding="utf-8")
              .replace("{GUIDE}", guide_text())
              .replace("{OUTPUT}", out.relative_to(ROOT).as_posix())
              .replace("{DATE}", day.isoformat())
              .replace("{LINK_SECTION}", link_section)
              .replace("{TEXT_SPECS}", specs)
              .replace("{LINK_JSON_NOTE}", "" if with_link else '今回は "link": null にする。')
              .replace("{DM_LINE}", "今日は文章の投稿のうち1件だけ、最後に「気になることがあればDMでどうぞ」程度のひと言を入れてよい（売り込み口調にしない・入れなくてもよい）。"
                                    if dm_ok(day) and with_link else "今日は「DM」には触れない。")
              .replace("{WEEKDAY_LINE}", "今日は水曜。リンク付きの投稿は、小さな商売を仕組みにした例としてオリシャン（オリジナルシャンパンの開業パッケージ）を題材にする。"
                                         "数字・事実は data/orishan-facts.md にあるものだけを使い、収益や売上の数字は書かない。" if wed else "")
              .replace("{TITLE}", art.get("title", ""))
              .replace("{DESCRIPTION}", art.get("description", ""))
              .replace("{SOURCE}", art["_path"].relative_to(ROOT).as_posix())
              .replace("{PROBLEMS}", problems_of(art))
              .replace("{HISTORY}", "\n".join(f"- {t.replace(chr(10), ' ')}" for t in history_texts()) or "（まだありません）"))
    if feedback:
        prompt += f"\n\n## 前回の書き直しの理由\n{feedback}\n{out.relative_to(ROOT).as_posix()} を一から書き直すこと。\n"
    claude = None
    import shutil
    for c in (shutil.which("claude"), Path.home() / ".local" / "bin" / "claude.exe"):
        if c and Path(c).exists():
            claude = str(c)
            break
    if not claude:
        log("claude コマンドが見つかりません")
        return None
    if out.exists():
        out.unlink()
    try:
        subprocess.run([claude, "-p", prompt, "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Write"],
                       cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True,
                       timeout=15 * 60, env=claude_env())
    except subprocess.TimeoutExpired:
        log("文面づくりが15分で終わりませんでした")
    try:
        return json.loads(re.sub(r"^```\w*|```$", "", out.read_text(encoding="utf-8").strip()).strip())
    except Exception as e:
        log(f"文面の JSON が読めませんでした（{e}）")
        return None


def problems_in(text, history, with_url=False, kind="link", dm=False):
    """投稿として出せない理由（なければ空）。"""
    why = []
    hit = [w for w in BANNED if w.lower() in text.lower()]
    if hit:
        why.append("使わない言葉: " + "・".join(hit))
    if re.search(r"https?://", text) and not with_url:
        why.append("URL が入っている")
    w = social.x_weight(text + ("\n\nhttps://x.co" if with_url else ""))
    if w > 280:
        why.append(f"長すぎる（{w}/280）")
    if len(text.strip()) < 20:
        why.append("短すぎる")
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if lines and len(lines[0]) > 30:
        why.append(f"1行目が長い（{len(lines[0])}字・30字以内）")
    tags = re.findall(r"[#＃][^\s#＃。、]+", re.sub(r"https?://\S+", "", text))
    if len(tags) > cfg().get("hashtags_max", 1):
        why.append(f"ハッシュタグが {len(tags)} 個（0〜{cfg().get('hashtags_max', 1)} 個）")
    if not dm and re.search(r"DM|ＤＭ|ダイレクトメッセージ", text):
        why.append("今日は DM に触れない")
    body = [l for l in lines if not re.fullmatch(r"([#＃][^\s#＃]+\s*)+", l)]
    ends_q = bool(body) and body[-1].rstrip("。").endswith(("？", "?"))
    if kind == "toikake" and not ends_q:
        why.append("問いかけの型なのに、最後が質問で終わっていない")
    if kind not in ("toikake",) and ends_q:
        why.append("問いかけの型以外は、最後を質問で終えない")
    if kind == "matome" and len(re.findall(r"(?m)^\s*[1-3１-３][.．、)）]", text)) < 3:
        why.append("まとめの型なのに、番号付きの3行（1. 2. 3.）がない")
    if kind == "beforeafter" and not (re.search(r"前[：:]", text) and re.search(r"今[：:]", text)):
        why.append("Before/After の型なのに「前：◯◯／今：◯◯」の形がない")
    for h in history:
        ratio, run = social.overlap(h, text, n=8)
        if ratio > 0.35 or run >= 25:
            why.append("過去の投稿と似ている")
            break
    return why


def _strs(xs, n_min, n_max, max_len):
    return (isinstance(xs, list) and n_min <= len(xs) <= n_max
            and all(isinstance(x, str) and x.strip() and len(x) <= max_len for x in xs))


def card_ok(card, kind="link"):
    """カードの言葉の形（型ごと）。字数はプロンプトの指定に少し余裕を持たせる。"""
    try:
        if kind in ("link", "beforeafter"):
            ok = (_strs([card["before_title"], card["after_title"]], 2, 2, 14)
                  and _strs(card["before"], 1, 3, 18) and _strs(card["after"], 1, 3, 18))
        elif kind == "jitsubutsu":
            ok = (_strs([card["thing"], card["screen_title"]], 2, 2, 18) and _strs([card["change"]], 1, 1, 26)
                  and isinstance(card["fields"], list) and 3 <= len(card["fields"]) <= 5
                  and all(_strs(f, 2, 2, 16) for f in card["fields"]))
        elif kind == "taiken":
            ok = _strs([card["label"]], 1, 1, 16) and _strs(card["quote"], 1, 3, 20)
        elif kind == "matome":
            ok = _strs([card["title"]], 1, 1, 20) and _strs(card["items"], 3, 3, 18)
        elif kind == "toikake":
            ok = _strs(card["question"], 1, 2, 18) and (not card.get("sub") or _strs([card["sub"]], 1, 1, 26))
        else:
            return False
        words = json.dumps(card, ensure_ascii=False).lower()
        return ok and not any(b.lower() in words for b in BANNED)
    except Exception:
        return False


def write_texts(day, art, kinds, work, with_link=True):
    """(リンク付きの本文, カード, [(kind, text, card or None)])。だめなら3回まで書き直し、通らないものは落とす。"""
    hist = history_texts()
    dm = dm_ok(day) and with_link
    feedback, draft = "", None
    for attempt in range(3):
        draft = ask_claude(day, art, kinds, work, feedback, with_link)
        if not draft:
            feedback = "JSON が保存されていなかった・読めなかった"
            continue
        issues = []
        lt = (draft.get("link") or {}).get("text", "")
        if with_link:
            issues += [f"リンク付き: {w}" for w in problems_in(lt, hist, with_url=True, dm=False)]
            if not card_ok((draft.get("link") or {}).get("card")):
                issues.append("リンク付き: カードの言葉の形が違う")
        texts = draft.get("texts") or []
        if [t.get("kind") for t in texts] != kinds:
            issues.append(f"文章の投稿の型と順番が違う（{' / '.join(kinds)} にする）")
        for i, t in enumerate(texts, 1):
            issues += [f"文章{i}: {w}" for w in problems_in(t.get("text", ""), hist + [lt], kind=t.get("kind"), dm=dm)]
            if not card_ok(t.get("card"), t.get("kind")):
                issues.append(f"文章{i}: カードの言葉の形が違う（{t.get('kind')} の形にする）")
        if sum(1 for t in texts if "DM" in t.get("text", "")) > 1:
            issues.append("DM に触れるのは1件まで")
        if not issues:
            break
        feedback = " / ".join(issues)
        log(f"文面の書き直し（{feedback}）")
    if not draft:
        return None, None, []
    lt = (draft.get("link") or {}).get("text", "")
    card = (draft.get("link") or {}).get("card")
    if with_link and (problems_in(lt, hist, with_url=True) or not card_ok(card)):
        log("リンク付きの文面が条件を満たさないため、記事のタイトルから作ります")
        lt, card = None, None
    keep = []
    for t in draft.get("texts") or []:
        k = t.get("kind")
        why = problems_in(t.get("text", ""), hist + [lt or ""] + [x[1] for x in keep], kind=k,
                          dm=dm and not any("DM" in x[1] for x in keep))
        if k not in kinds or why:
            log(f"文章の投稿を1件落としました（{k}: {' / '.join(why) or '型が違う'}）")
            continue
        c = t.get("card") if card_ok(t.get("card"), k) else None
        if not c:
            log(f"{k} のカードの言葉が条件を満たさないため、この投稿は画像なしにします")
        keep.append((k, t["text"].strip(), c))
    return lt, card, keep[:len(kinds)]


def fallback_link_text(art):
    t = re.split(r"[｜|]", art.get("title", ""))[0].strip()
    return f"{t}。今のやり方と、仕組みに任せる方法を順番にまとめました。"


def fallback_card(art):
    probs = [p.strip() for p in problems_of(art).split("/") if p.strip()][:3]
    return {"before_title": "毎回の手作業", "before": [p[:14] for p in probs] or ["くり返しの作業"],
            "after_title": "仕組みに任せる", "after": ["道具につないで", "決まった流れで処理"]}


# ---------------------------------------------------------------------------
# カード画像（1200×675・濃紺 #0E1420・シアン #5FC9D8・ブルー #4E6EE6）。型ごとに中身が変わる
#   link / beforeafter … 左右の比較（link は「困りごと → 自動化後」、beforeafter は「前 → 今」）
#   jitsubutsu … 作ったものの画面（ダミーデータ・「画面はイメージ」の札付き）
#   taiken … 場面と意見の要点　matome … 番号付き3行　toikake … 質問
# ---------------------------------------------------------------------------
def esc(s):
    import html
    return html.escape(str(s))


CARD_CSS = """
body{margin:0;width:1200px;height:675px;background:#0E1420;font-family:"Yu Gothic UI","Yu Gothic","Meiryo","Noto Sans JP",sans-serif;color:#fff;position:relative;overflow:hidden;--s:1}
.grid{position:absolute;inset:0;background-image:linear-gradient(rgba(95,201,216,.06) 1px,transparent 1px),linear-gradient(90deg,rgba(95,201,216,.06) 1px,transparent 1px);background-size:50px 50px}
.wrap{position:absolute;left:60px;right:60px;top:70px;bottom:70px;display:flex;align-items:stretch}
.box{flex:1;border-radius:22px;padding:44px 42px;box-sizing:border-box;overflow:hidden}
.b{background:#162034;border:2px solid #22314A}
.a{background:#101B2E;border:2px solid #5FC9D8}
.lab{font-size:calc(var(--s)*24px);font-weight:700;letter-spacing:.08em;margin-bottom:14px;color:#5FC9D8}
.b .lab{color:#9FB3C8}
h2{margin:0 0 30px;font-size:calc(var(--s)*46px);line-height:1.3;font-weight:700}
.b h2{color:#DDE6F0}
ul{margin:0;padding:0;list-style:none}
li{font-size:calc(var(--s)*31px);line-height:1.5;margin:0 0 14px;padding-left:30px;position:relative;font-weight:500}
.b li{color:#9FB3C8} .b li:before{content:"";position:absolute;left:0;top:calc(var(--s)*19px);width:14px;height:3px;background:#4E6EE6}
.a li:before{content:"";position:absolute;left:0;top:calc(var(--s)*14px);width:12px;height:12px;border-radius:50%;background:#5FC9D8}
.arrow{width:90px;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:#4E6EE6;font-size:96px;font-weight:700}
.big{font-size:calc(var(--s)*54px);line-height:1.45;font-weight:700;margin:0}
.sub{font-size:calc(var(--s)*28px);color:#9FB3C8;margin-top:28px}
ol{margin:0;padding:0;list-style:none;counter-reset:n}
.box ol li{counter-increment:n;padding-left:84px;margin:0 0 30px;font-size:calc(var(--s)*42px);font-weight:700;line-height:60px}
.box ol li:before{content:counter(n);position:absolute;left:0;top:0;width:60px;height:60px;border-radius:50%;background:#4E6EE6;color:#fff;
  font-size:34px;display:flex;align-items:center;justify-content:center}
.q{position:absolute;right:70px;bottom:40px;font-size:220px;font-weight:700;color:rgba(95,201,216,.18);line-height:1}
.win{background:#F4F7FB;color:#1A2333;border-radius:14px;overflow:hidden;flex:1.3;display:flex;flex-direction:column;border:0}
.mid{display:flex;flex-direction:column;justify-content:center;padding:44px 60px}
.bar{background:#22314A;color:#DDE6F0;padding:12px 22px;font-size:calc(var(--s)*24px);display:flex;gap:10px;align-items:center}
.dot{width:12px;height:12px;border-radius:50%;background:#5FC9D8;display:inline-block}
table{border-collapse:collapse;width:100%;font-size:calc(var(--s)*32px);color:#1A2333}
td{padding:20px 28px;border-bottom:1px solid #D9E1EC}
td:first-child{color:#5A6B82;width:42%} td:last-child{font-weight:700;text-align:right}
.tag{position:absolute;right:76px;top:30px;font-size:20px;color:#9FB3C8;border:1px solid #3A4B66;border-radius:999px;padding:4px 16px}
.side{flex:1;padding:10px 0 10px 44px;display:flex;flex-direction:column;justify-content:center}
"""


def card_body(card, kind):
    li = lambda xs: "".join(f"<li>{esc(x)}</li>" for x in xs)
    if kind in ("link", "beforeafter"):
        lb, la = ("困りごと", "自動化後") if kind == "link" else ("前", "今")
        return f"""<div class="wrap">
<div class="box b"><div class="lab">{lb}</div><h2>{esc(card['before_title'])}</h2><ul>{li(card['before'])}</ul></div>
<div class="arrow">›</div>
<div class="box a"><div class="lab">{la}</div><h2>{esc(card['after_title'])}</h2><ul>{li(card['after'])}</ul></div></div>"""
    if kind == "jitsubutsu":
        rows = "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td></tr>" for a, b in card["fields"])
        return f"""<div class="tag">画面はイメージ（ダミーデータ）</div><div class="wrap">
<div class="win box" style="padding:0"><div class="bar"><span class="dot"></span>{esc(card['screen_title'])}</div><table>{rows}</table></div>
<div class="side"><div class="lab">作ったもの</div><h2>{esc(card['thing'])}</h2><div class="sub" style="color:#DDE6F0;font-size:calc(var(--s)*32px)">{esc(card['change'])}</div></div></div>"""
    if kind == "taiken":
        return f"""<div class="wrap"><div class="box a mid"><div class="lab">{esc(card['label'])}</div>
<p class="big">{"<br>".join(esc(x) for x in card['quote'])}</p></div></div>"""
    if kind == "matome":
        return f"""<div class="wrap"><div class="box a mid"><h2>{esc(card['title'])}</h2><ol>{li(card['items'])}</ol></div></div>"""
    if kind == "toikake":
        return f"""<div class="wrap"><div class="box a mid"><div class="lab">あなたの職場では？</div>
<p class="big">{"<br>".join(esc(x) for x in card['question'])}</p>{f'<div class="sub">{esc(card["sub"])}</div>' if card.get('sub') else ''}</div></div><div class="q">?</div>"""
    raise ValueError(kind)


def card_html(card, kind="link"):
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{CARD_CSS}</style></head><body><div class="grid"></div>'
            f'{card_body(card, kind)}</body></html>')


def render_card(card, png, kind="link"):
    from playwright.sync_api import sync_playwright
    png.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="chrome")
        pg = b.new_page(viewport={"width": 1200, "height": 675})
        pg.set_content(card_html(card, kind))
        pg.wait_for_timeout(300)
        # 文字があふれていないか（はみ出したら文字を小さくする）
        for s in (1, 0.9, 0.8, 0.72, 0.65):
            pg.evaluate(f"() => document.body.style.setProperty('--s', '{s}')")
            over = pg.evaluate("""() => [...document.querySelectorAll('.box, .side')].some(b => b.scrollHeight > b.clientHeight + 2
                                  || b.scrollWidth > b.clientWidth + 2)""")
            if not over:
                break
        pg.screenshot(path=str(png))
        b.close()


# ---------------------------------------------------------------------------
# 予定表を作る
# ---------------------------------------------------------------------------
def plan(day, force=False, push=True, out=None):
    c = cfg()
    if not c.get("enabled"):
        log("X: data/social.json の x_api.enabled が false のため、何もしません")
        return 0
    if not social.load_x_keys() and not REHEARSAL:
        log("X: キーがまだありません（python scripts/x-keys.py で窓を出して貼る）。今日の X は見送ります")
        return 2
    n = day_number(day)
    if n < 1:
        log(f"X: 本番は {c['start']} からです（今日は作りません）")
        return 0
    # 通し確認では本番の予定表と別の名前にする（Cloudflare の投稿係は <日付>.json しか読まない）
    out = Path(out) if out else QUEUE / f"{'rehearsal-' if REHEARSAL else ''}{day.isoformat()}.json"
    out = out if out.is_absolute() else ROOT / out
    if out.exists() and not force:
        log(f"X: {out.relative_to(ROOT).as_posix()} はもうあります（作り直すときは --force）")
        return 0
    if push and not REHEARSAL:
        git("pull", "--rebase", "--autostash")
    import importlib.util
    spec = importlib.util.spec_from_file_location("xpost", ROOT / "scripts" / "xpost.py")
    xpost = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(xpost)
    if xpost.worker_cfg().get("url"):          # Cloudflare の投稿の記録 → data/x.json（今月の概算費用・クレジット切れ）
        log("X: Cloudflare の投稿の記録を data/x.json に写しました" if xpost.sync() else "X: Cloudflare の記録を取れず、前回の data/x.json で計算します")
    if not REHEARSAL:
        r = social.x_check_replies()       # 自動では返さない。件数と URL を data/x.json に入れ、STATUS.md の先頭に出す
        log(f"X: リプライの確認 → {r}")
    n_total = c["phase1_per_day"] if n <= c["phase1_days"] else c["phase2_per_day"]
    now = datetime.datetime.now().replace(second=0, microsecond=0)
    earliest = at(day, "00:00") if REHEARSAL or day > now.date() else now + datetime.timedelta(minutes=20)
    times = slot_times(day, n_total, earliest)
    if not times:
        log("X: 今日の投稿時間帯はもう過ぎています（今日は見送り）")
        return 0
    n_text, cost_note = decide_text_count(day, len(times) - 1)
    log(f"X: {day} は {n} 日目・{len(times)} 枠（{' / '.join(f'{t:%H:%M}' for t in times)}）・{cost_note}")
    genre = "orishan" if day.weekday() == 2 else "jidouka"
    art, link_t, fresh = pick_article(day, genre, times)
    if not art and genre == "orishan":
        genre = "jidouka"
        art, link_t, fresh = pick_article(day, genre, times)
    if not art:
        log("X: 紹介できる記事がありません（リンク付きの投稿なし）")
        link_t = None
    else:
        log(f"X: リンク付き → {art['slug']}（{'今日の記事' if fresh else '今日の記事はまだ出ないので、公開済みの最新記事'}・"
            f"記事の公開 {public_at(art):%m/%d %H:%M}・投稿 {link_t:%H:%M}）")
    text_times = [t for t in times if t != link_t]
    random.shuffle(text_times)
    text_times = sorted(text_times[:n_text])
    work = ROOT / "state" / f"x-draft-{day.isoformat()}.json"
    work.parent.mkdir(exist_ok=True)
    items, base = [], json.loads((ROOT / "site.json").read_text(encoding="utf-8"))["base_url"]
    kinds = text_kinds(day, len(text_times))
    if kinds:
        log(f"X: 文章の投稿の型 → {' / '.join(KIND_LABEL[k] for k in kinds)}" + ("・DM に触れてよい日" if dm_ok(day) else ""))
    lt, card, texts = (write_texts(day, art, kinds, work) if art else (None, None, []))
    if art:
        url = f"{base}/{art['slug']}?utm_source=x"
        lt = lt or fallback_link_text(art)
        card = card or fallback_card(art)
        img = QUEUE / "img" / f"{out.stem}-link.png"
        render_card(card, img)
        items.append({"kind": "link", "at": link_t.strftime(FMT), "text": f"{lt.strip()}\n\n{url}", "url": url,
                      "check_url": f"{base}/{art['slug']}", "slug": art["slug"], "image": img.relative_to(ROOT).as_posix(),
                      "card": card, "est_cost": c["price_link_post"] + c["price_media"]})
    for j, (t, (kind, text, tcard)) in enumerate(zip(text_times, texts), 1):
        it = {"kind": kind, "at": t.strftime(FMT), "text": text, "est_cost": c["price_text_post"]}
        if tcard:
            img = QUEUE / "img" / f"{out.stem}-t{j}.png"
            render_card(tcard, img, kind)
            it.update(image=img.relative_to(ROOT).as_posix(), card=tcard, est_cost=c["price_text_post"] + c["price_media"])
        items.append(it)
    items.sort(key=lambda x: x["at"])
    for i, it in enumerate(items, 1):
        it["id"] = f"{day.isoformat()}-{i}"
    q = {"date": day.isoformat(), "day_number": n, "created_at": now.strftime(FMT), "cost_note": cost_note, "items": items}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(q, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    for it in items:
        log(f"X 予定 {it['at'][11:]} [{KIND_LABEL.get(it['kind'], it['kind'])}]（{social.x_weight(it['text'])}/280）"
            + (" 画像あり" if it.get("image") else "") + " " + it["text"].replace("\n", " ")[:80])
    if push and not REHEARSAL:
        git("add", str(out.relative_to(ROOT)), "data/x-queue/img", "data/x.json")
        git("commit", "-m", f"x: {day} の投稿予定（{len(items)} 件）")
        git("pull", "--rebase", "--autostash")
        log("X: 予定表を GitHub に送りました" if git("push").returncode == 0 else "X: 予定表の push に失敗しました")
    return 0


def show(day):
    q = load_json(QUEUE / f"{day.isoformat()}.json", None)
    if not q:
        print(f"{day} の予定はありません")
        return 1
    done = {p["id"] for p in load_json(XLOG, {}).get("posts", [])}
    print(f"{q['date']}（{q['day_number']} 日目）{q.get('cost_note', '')}")
    for it in q["items"]:
        print(f"\n--- {it['at'][11:]} [{KIND_LABEL.get(it['kind'], it['kind'])}] {'投稿済み' if it['id'] in done else '予定'}"
              f"（{social.x_weight(it['text'])}/280）{' 画像: ' + it['image'] if it.get('image') else ''}\n{it['text']}")
    return 0


def samples(day):
    """投稿の型5種類を1つずつ書いて、カード画像とあわせて data/content-samples-x.md に保存する（投稿しない・予定表も作らない）。"""
    arts = [read_meta(p) for p in (ROOT / "content" / "ja").glob("*.md")]
    arts = [a for a in arts if a.get("genre") == "jidouka" and public_at(a) <= datetime.datetime.now()]
    art = max(arts, key=public_at)
    work = ROOT / "state" / "x-samples.json"
    work.parent.mkdir(exist_ok=True)
    _, _, texts = write_texts(day, art, SAMPLE_KINDS, work, with_link=False)
    outdir = ROOT / "data" / "content-samples-x"
    md = ["# X の投稿サンプル（型1〜5・data/content-guide.md の新しいルールで作成・投稿はしていない）", "",
          f"作成: {datetime.datetime.now():%Y-%m-%d %H:%M}（scripts/x-plan.py samples）。材料の記事: {art.get('title')}", ""]
    for i, k in enumerate(SAMPLE_KINDS, 1):
        got = next((t for t in texts if t[0] == k), None)
        md += [f"## {i}. {KIND_LABEL[k]}（{k}）", ""]
        if not got:
            md += ["（条件を満たす文面ができませんでした。logs/x-plan.log を参照）", ""]
            continue
        _, text, tcard = got
        md += ["```", text, "```", "", f"1行目 {len(text.strip().split(chr(10))[0])} 字・{social.x_weight(text)}/280", ""]
        if tcard:
            png = outdir / f"{i}-{k}.png"
            render_card(tcard, png, k)
            md += [f"![{KIND_LABEL[k]}](content-samples-x/{png.name})", ""]
        else:
            md += ["（カードの言葉が条件を満たさず、画像なし）", ""]
    (ROOT / "data" / "content-samples-x.md").write_text("\n".join(md), encoding="utf-8", newline="\n")
    log(f"X: サンプル {len(texts)}/{len(SAMPLE_KINDS)} 件を data/content-samples-x.md に保存しました")
    return 0 if len(texts) == len(SAMPLE_KINDS) else 1


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    today = datetime.date.fromisoformat(os.environ.get("BLOG_TODAY") or datetime.date.today().isoformat())
    if args and args[0] == "show":
        return show(datetime.date.fromisoformat(args[1]) if len(args) > 1 else today)
    if args and args[0] == "samples":
        return samples(today)
    day = datetime.date.fromisoformat(args[args.index("--date") + 1]) if "--date" in args else today
    out = args[args.index("--out") + 1] if "--out" in args else None
    return plan(day, force="--force" in args, push="--no-push" not in args and not out, out=out)


if __name__ == "__main__":
    sys.exit(main())
