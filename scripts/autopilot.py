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
  python scripts/autopilot.py review           … 台帳で needs_review（label/catch/table が未整備）の案件を表示
  python scripts/autopilot.py status           … STATUS.md を更新
  python scripts/autopilot.py refresh-next     … 次に見直す公開済み記事を表示（記事が20本未満・今週分済みなら「見直さない」）
  python scripts/autopilot.py refresh-done <slug> "<改善内容>"  … 中身を良くした。本文が実質的に変わっていれば更新日を今日にする
  python scripts/autopilot.py refresh-skip <slug> "<理由>"      … 見直したが直す所がなかった（更新日は変えない）
  python scripts/autopilot.py refresh-mode auto|on|off          … 見直しを 20本たまったら自動開始 / 今すぐ開始 / 停止

スパム対策: 自動生成の記事（フロントマター generated_by: autopilot）は 1日 MAX_PER_DAY 本まで。
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

MAX_PER_DAY = 3          # 自動生成の記事は1日この本数まで（Google のスパム判定を避ける）
LOW_TOPICS = 5           # 残りネタがこれ未満のジャンルは補充する

sys.path.insert(0, str(Path(__file__).resolve().parent))


def log(msg):
    print(msg, flush=True)


def today():
    return datetime.date.today().isoformat()


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
    return [a for a in articles() if a.get("generated_by") == "autopilot" and (a.get("created") or a.get("date")) == today()]


def cmd_next():
    done_today = auto_today()
    if len(done_today) >= MAX_PER_DAY:
        log(f"本日の上限（{MAX_PER_DAY}本）に達しています。今日はこれ以上書かないでください。")
        return 2
    topics = [t for t in read_topics() if not t[2]]
    if not topics:
        log("未使用のネタがありません。topics-check の手順でネタを補充してください。")
        return 3
    # ジャンルを順番に回す（直近に書いたジャンルの次から）。同じジャンルばかり続けない
    order = list(GENRES)
    state = load_json(STATE, {})
    last = state.get("last_genre")
    start = (order.index(last) + 1) % len(order) if last in order else 0
    rotated = order[start:] + order[:start]
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
    if conf.get("ads") == "none":
        log("■ 広告: このジャンルはアフィリ広告を入れない注意喚起ジャンルです（注意喚起の表記・相談窓口は自動で入る）")
    else:
        log(f"■ このジャンルの提携中広告: {len(ads)} 件 {('（' + ' / '.join(ads) + '）') if ads else ''}")
    if conf.get("rakuten"):
        log("■ 楽天の商品リンク: このジャンルは使える。本文で道具・本・収納用品など「物」を紹介する記事なら、"
            "フロントマターに `rakuten: 検索語 | 検索語` を書く（読者が実際に買いそうな物を2〜3個。"
            f"例: {conf['rakuten']}）。物を紹介しない記事には書かない")
    if conf.get("images") == "none":
        log("■ 写真: このジャンルは写真を自動で付けません（image_query / image_queries は書かなくてよい）")
    if conf.get("ads") != "none" and len(ads) < conf.get("min_ads", 2):
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
            state = load_json(STATE, {})
            state["last_genre"] = genre
            save_json(STATE, state)
            log(f"使用済みにしました: {kw} → {slug}")
            return 0
    log(f"topics.txt に未使用のネタ「{keyword}」が見つかりません（印は付けていません）")
    return 1


def cmd_topics_check():
    rem = {g: 0 for g in GENRES}
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
    # 実際にメディア管理画面を開いて、ログイン画面に飛ばされないか確かめる
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL, timeout=60000)
            page = browser.contexts[0].new_page()
            page.goto(MEDIA_CONSOLE, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            ok = logged_in_url(page.url)
            if ok:
                # トップは開けても、提携の画面だけ再認証を求められることがある。取り込みで使う画面まで確かめる
                page.goto(MEDIA_CONSOLE.rstrip("/") + "/program/list/partnered?pageNo=1&pageSize=100",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                ok = logged_in_url(page.url)
            if ok:
                page.close()  # ログイン画面のときはタブを残し、渋田さんがそのままログインできるようにする
    except Exception as e:
        log(f"[A8] 確認中にエラー: {e.__class__.__name__}")
        ok = False
    set_state(a8_login_ok=ok, a8_checked_at=datetime.datetime.now().isoformat(timespec="minutes"))
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
        "[A8] ログインが切れています。渋田さんに「A8に再ログインして」と1回お願いする（開いた Chrome でログインするだけ）。")
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
    if not a8_ok:
        alerts.append(f"- A8: {a8_text}。広告の取り込み・新しい提携は止まっています（記事の作成・公開は続きます）。"
                      "PC を使うときに Claude Code に「A8に再ログインして」と言ってください。")
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
| A8 ログイン | {a8_text} |

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
