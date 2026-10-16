#!/usr/bin/env python3
"""
X（@maf_media_jp）のその日の投稿を作って予定表（data/x-queue/）に置き、GitHub に送る（毎朝 boot-run.py の「5. X」から呼ばれる）。
実際の投稿は PC が切れていても出せるように Cloudflare Workers（workers/x-poster・5分おき）が GitHub の予定表を読んで行う（scripts/xpost.py）。

  python scripts/x-plan.py                 … 今日の分を作って GitHub へ（もうあれば何もしない）
  python scripts/x-plan.py --date 2026-10-17 [--force] [--no-push] [--out data/x-queue/rehearsal-2026-10-17.json]
  python scripts/x-plan.py show [日付]     … 予定表を表示

決まり（data/social.json の x_api）:
  ・1〜10日目（start の日が1日目）: 1日2件。7:00〜22:00 のばらばらな時刻、2件の間は3時間以上
  ・11日目から: 1日3件。朝 7:00〜9:00・昼 11:30〜13:30・夜 18:00〜21:30 の中でそれぞれランダム
  ・リンク＋画像付きは1日1件だけ（その日の自動化記事の紹介。水曜はオリシャンの記事）。残りは文章だけ
  ・リンク付きは記事の公開予定時刻（publish_at）の20分後以降に置く。合う枠がなければ、すでに公開済みの同じ種類の最新記事を紹介する
  ・リンクには ?utm_source=x
  ・1か月の概算が予算（budget_usd_month）を超えそうなら、文章だけの投稿を減らす
文面は Claude Code（claude -p・scripts/x-prompt.md）が書き、ここで禁止語・長さ・過去との重複を確かめる。
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
KIND_LABEL = {"link": "記事の紹介", "tip": "自動化の小ワザ", "hitokoto": "困りごとへのひと言", "tsumazuki": "つまずいた話"}


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
    if day_number(day) <= c["phase1_days"]:
        lo, hi = max(at(day, c["phase1_window"][0]), earliest), at(day, c["phase1_window"][1])
        gap = datetime.timedelta(hours=c["phase1_min_gap_hours"])
        span = int((hi - lo).total_seconds() // 60)
        for _ in range(5000):
            ts = sorted(lo + datetime.timedelta(minutes=random.randint(0, max(span, 0))) for _ in range(n))
            if all(b - a >= gap for a, b in zip(ts, ts[1:])):
                return ts
        return ts[:1] if ts else []    # 時間が足りない（起動が遅れた日）→ 1件だけ
    out = []
    for w in c["phase2_windows"][:n]:
        lo, hi = max(at(day, w[0]), earliest), at(day, w[1])
        if lo <= hi:
            out.append(lo + datetime.timedelta(minutes=random.randint(0, int((hi - lo).total_seconds() // 60))))
    return out


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


def ask_claude(day, art, n_text, out, feedback=""):
    kinds = ["tip", "hitokoto", "tsumazuki"]
    random.shuffle(kinds)
    wed = day.weekday() == 2
    prompt = (PROMPT.read_text(encoding="utf-8")
              .replace("{OUTPUT}", out.relative_to(ROOT).as_posix())
              .replace("{DATE}", day.isoformat())
              .replace("{N_TOTAL}", str(1 + n_text))
              .replace("{N_TEXT}", str(n_text))
              .replace("{TEXT_KINDS}", "、".join(kinds[:max(n_text, 1)]))
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


def problems_in(text, history, with_url=False):
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
    for h in history:
        ratio, run = social.overlap(h, text, n=8)
        if ratio > 0.35 or run >= 25:
            why.append("過去の投稿と似ている")
            break
    return why


def card_ok(card):
    try:
        words = [card["before_title"], card["after_title"], *card["before"], *card["after"]]
        return (all(isinstance(w, str) and w.strip() for w in words)
                and 1 <= len(card["before"]) <= 3 and 1 <= len(card["after"]) <= 3
                and max(len(card["before_title"]), len(card["after_title"])) <= 14
                and max(len(w) for w in card["before"] + card["after"]) <= 18
                and not any(b.lower() in "".join(words).lower() for b in BANNED))
    except Exception:
        return False


def write_texts(day, art, n_text, work):
    """(リンク付きの本文, カード, [(kind, text)])。だめなら3回まで書き直し、通らないものは落とす。"""
    hist = history_texts()
    feedback, draft = "", None
    for attempt in range(3):
        draft = ask_claude(day, art, n_text, work, feedback)
        if not draft:
            feedback = "JSON が保存されていなかった・読めなかった"
            continue
        issues = []
        lt = (draft.get("link") or {}).get("text", "")
        issues += [f"リンク付き: {w}" for w in problems_in(lt, hist, with_url=True)]
        if not card_ok((draft.get("link") or {}).get("card")):
            issues.append("カードの言葉の形が違う")
        texts = draft.get("texts") or []
        if len(texts) != n_text:
            issues.append(f"文章だけの投稿が {len(texts)} 件（{n_text} 件にする）")
        for i, t in enumerate(texts, 1):
            issues += [f"文章{i}: {w}" for w in problems_in(t.get("text", ""), hist + [lt])]
        if not issues:
            break
        feedback = " / ".join(issues)
        log(f"文面の書き直し（{feedback}）")
    if not draft:
        return None, None, []
    lt = (draft.get("link") or {}).get("text", "")
    card = (draft.get("link") or {}).get("card")
    if problems_in(lt, hist, with_url=True) or not card_ok(card):
        log("リンク付きの文面が条件を満たさないため、記事のタイトルから作ります")
        lt, card = None, None
    keep = []
    for t in draft.get("texts") or []:
        why = problems_in(t.get("text", ""), hist + [lt or ""] + [k[1] for k in keep])
        if why:
            log(f"文章だけの投稿を1件落としました（{' / '.join(why)}）")
        else:
            keep.append((t.get("kind", "tip"), t["text"].strip()))
    return lt, card, keep[:n_text]


def fallback_link_text(art):
    t = re.split(r"[｜|]", art.get("title", ""))[0].strip()
    return f"{t}。今のやり方と、仕組みに任せる方法を順番にまとめました。"


def fallback_card(art):
    probs = [p.strip() for p in problems_of(art).split("/") if p.strip()][:3]
    return {"before_title": "毎回の手作業", "before": [p[:14] for p in probs] or ["くり返しの作業"],
            "after_title": "仕組みに任せる", "after": ["道具につないで", "決まった流れで処理"]}


# ---------------------------------------------------------------------------
# カード画像（困りごと（左）→ 自動化後（右）・1200×675・濃紺 #0E1420・シアン #5FC9D8・ブルー #4E6EE6）
# ---------------------------------------------------------------------------
def esc(s):
    import html
    return html.escape(str(s))


def card_html(card):
    li = lambda xs: "".join(f"<li>{esc(x)}</li>" for x in xs)
    return f"""<html><head><meta charset="utf-8"><style>
body{{margin:0;width:1200px;height:675px;background:#0E1420;font-family:"Yu Gothic UI","Yu Gothic","Meiryo","Noto Sans JP",sans-serif;color:#fff;position:relative;overflow:hidden}}
.grid{{position:absolute;inset:0;background-image:linear-gradient(rgba(95,201,216,.06) 1px,transparent 1px),linear-gradient(90deg,rgba(95,201,216,.06) 1px,transparent 1px);background-size:50px 50px}}
.wrap{{position:absolute;left:60px;right:60px;top:70px;bottom:70px;display:flex;align-items:stretch;gap:0}}
.box{{flex:1;border-radius:22px;padding:44px 42px;box-sizing:border-box}}
.b{{background:#162034;border:2px solid #22314A}}
.a{{background:#101B2E;border:2px solid #5FC9D8}}
.lab{{font-size:24px;font-weight:700;letter-spacing:.08em;margin-bottom:14px}}
.b .lab{{color:#9FB3C8}} .a .lab{{color:#5FC9D8}}
h2{{margin:0 0 30px;font-size:46px;line-height:1.3;font-weight:700}}
.b h2{{color:#DDE6F0}}
ul{{margin:0;padding:0;list-style:none}}
li{{font-size:31px;line-height:1.5;margin:0 0 14px;padding-left:30px;position:relative;font-weight:500}}
.b li{{color:#9FB3C8}} .b li:before{{content:"";position:absolute;left:0;top:19px;width:14px;height:3px;background:#4E6EE6}}
.a li:before{{content:"";position:absolute;left:0;top:14px;width:12px;height:12px;border-radius:50%;background:#5FC9D8}}
.arrow{{width:90px;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:#4E6EE6;font-size:96px;font-weight:700}}
</style></head><body><div class="grid"></div><div class="wrap">
<div class="box b"><div class="lab">困りごと</div><h2>{esc(card['before_title'])}</h2><ul>{li(card['before'])}</ul></div>
<div class="arrow">›</div>
<div class="box a"><div class="lab">自動化後</div><h2>{esc(card['after_title'])}</h2><ul>{li(card['after'])}</ul></div>
</div></body></html>"""


def render_card(card, png):
    from playwright.sync_api import sync_playwright
    png.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="chrome")
        pg = b.new_page(viewport={"width": 1200, "height": 675})
        pg.set_content(card_html(card))
        pg.wait_for_timeout(300)
        # 文字があふれていないか（はみ出したら文字を小さくする）
        for size in (31, 28, 25, 22):
            over = pg.evaluate("""() => [...document.querySelectorAll('.box')].some(b => b.scrollHeight > b.clientHeight + 2
                                  || b.scrollWidth > b.clientWidth + 2)""")
            if not over:
                break
            pg.evaluate(f"""() => {{ document.querySelectorAll('li').forEach(l => l.style.fontSize = '{size - 3}px');
                                    document.querySelectorAll('h2').forEach(h => h.style.fontSize = '{size + 10}px'); }}""")
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
    lt, card, texts = (write_texts(day, art, len(text_times), work) if art else (None, None, []))
    if art:
        url = f"{base}/{art['slug']}?utm_source=x"
        lt = lt or fallback_link_text(art)
        card = card or fallback_card(art)
        img = QUEUE / "img" / f"{out.stem}-link.png"
        render_card(card, img)
        items.append({"kind": "link", "at": link_t.strftime(FMT), "text": f"{lt.strip()}\n\n{url}", "url": url,
                      "check_url": f"{base}/{art['slug']}", "slug": art["slug"], "image": img.relative_to(ROOT).as_posix(),
                      "card": card, "est_cost": c["price_link_post"] + c["price_media"]})
    for t, (kind, text) in zip(text_times, texts):
        items.append({"kind": kind, "at": t.strftime(FMT), "text": text, "est_cost": c["price_text_post"]})
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


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    today = datetime.date.fromisoformat(os.environ.get("BLOG_TODAY") or datetime.date.today().isoformat())
    if args and args[0] == "show":
        return show(datetime.date.fromisoformat(args[1]) if len(args) > 1 else today)
    day = datetime.date.fromisoformat(args[args.index("--date") + 1]) if "--date" in args else today
    out = args[args.index("--out") + 1] if "--out" in args else None
    return plan(day, force="--force" in args, push="--no-push" not in args and not out, out=out)


if __name__ == "__main__":
    sys.exit(main())
