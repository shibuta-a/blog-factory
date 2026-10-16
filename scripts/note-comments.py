#!/usr/bin/env python3
"""
note（maf）の記事に付いたコメントへの返信と、返信した相手へのフォロー返し（毎朝 boot-run.py の note の段階から呼ばれる）。
note の投稿と同じ専用プロファイルの Chrome（scripts/note_browser.py・ポート 9334）と自動ログインを使う。

  python scripts/note-comments.py                 … 毎朝: まだ扱っていないコメントを判定して、返信・フォロー返し・記録
  python scripts/note-comments.py list            … 記録（data/note-comments.json）を表示
  python scripts/note-comments.py reply <コメントのkey> --text "返信文"   … 決めた文で1件返信してフォローを返す
  python scripts/note-comments.py resolve <コメントのkey>   … 「要確認のコメント」を確認済みにする（STATUS.md から消える）
  python scripts/note-comments.py resume          … 制限・警告で止めた返信を再開する

決まり:
  ・対象は自分の公開記事のコメントのうち、自分以外の人の最初のコメント（返信の往復はしない）で、まだ自分が返信していないもの
  ・判定は Claude Code（claude -p・scripts/note-comment-prompt.md）: reply（感想・質問）/ skip（勧誘・宣伝・外部リンク・意味不明）/
    review（クレーム・込み入った質問・お金や契約の相談 → 返信せず STATUS.md の先頭に「要確認のコメント」）
  ・返信文はプログラムでも確かめる（相手の名前＋さん・150字以内・リンクなし（質問への案内だけ例外）・自動で動いていることに触れない・過去の返信と似ていない）
  ・1日の返信は10件まで、フォローは10件まで。返信と返信の間は40〜90秒あける。フォローは返信した相手だけ（フォロー済みなら何もしない）
  ・note から制限や警告の表示が出たら、コメント返信をすべて止めて STATUS.md の先頭に書く（記事の投稿は止めない）
  ・結果は data/note-comments.json（同じコメントは二度扱わない）
通し確認（BLOG_REHEARSAL=1）では、判定と返信欄への入力までして送らない（フォローも記録もしない）。
"""
import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import social  # noqa: E402
from claude_env import claude_env  # noqa: E402
from note_browser import CDP_URL, SHOTS, auto_login, close_chrome, current_user, published_notes, save_status, start_chrome  # noqa: E402

ROOT = social.ROOT
RECORD = ROOT / "data" / "note-comments.json"
PROMPT = ROOT / "scripts" / "note-comment-prompt.md"
LOG = ROOT / "logs" / "note-comments.log"
REHEARSAL = bool(os.environ.get("BLOG_REHEARSAL"))
MAX_REPLIES, MAX_FOLLOWS = 10, 10
GAP_SEC = (40, 90)
ALLOWED_URLS = ["https://blog-factory-cf7.pages.dev/", "https://maf-group.jp/act/", "https://maf-group.jp/orishanlab.html",
                "https://note.com/witty_gnu2910/"]
BANNED = ["自動", "bot", "ボット", "AIが書", "AIで書", "フォローしました", "フォロー返し", "フォローを返"]
WARN_WORDS = ["制限", "警告", "一時的に", "ご利用いただけません", "凍結", "規約", "スパム", "しばらく時間をおいて"]


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {'[通し確認] ' if REHEARSAL else ''}{msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load():
    try:
        d = json.loads(RECORD.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("_about", "note のコメントの扱いの記録（scripts/note-comments.py）。action: replied / skipped / review。stopped があれば返信を止めている")
    d.setdefault("comments", {})
    return d


def save(d):
    RECORD.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def now_s():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def today_count(d, field):
    t = datetime.date.today().isoformat()
    return sum(1 for c in d["comments"].values() if (c.get(field) or "")[:10] == t)


# ---------------------------------------------------------------------------
# note の画面・API（ログイン中のブラウザから）
# ---------------------------------------------------------------------------
def api(page, path):
    return page.evaluate("async (p) => { const r = await fetch(p, {credentials: 'include'}); return r.ok ? await r.json() : {_status: r.status}; }", path)


def comment_text(c):
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text":
                out.append(n.get("value", ""))
            for ch in n.get("children") or []:
                walk(ch)
            if n.get("tag_name") == "p":
                out.append("\n")
    walk(c.get("comment") or {})
    return "".join(out).strip()


def note_comments(page, key):
    items, pg = [], 1
    while pg:
        d = api(page, f"/api/v3/notes/{key}/note_comments?page={pg}&per_page=20&order=newest")
        items += d.get("data") or []
        pg = d.get("next_page")
    return items


def warning_shown(page):
    """note の制限・警告の表示（ダイアログ・お知らせ枠だけを見る。記事の本文の言葉は数えない）。"""
    try:
        text = page.evaluate("""() => [...document.querySelectorAll('[role=alert],[role=alertdialog],[role=dialog],[role=status],[class*=toast],[class*=Toast],[class*=snackbar]')]
            .filter(e => e.getBoundingClientRect().height > 0 && !e.innerText.includes('深呼吸')).map(e => e.innerText).join(' ')""")
    except Exception:
        return ""
    return next((w for w in WARN_WORDS if w in text), "") and text.strip()[:200]


def confirm_dialog(page):
    """note がコメントの前に出す確認（「コメントをするまえに… ちょっと深呼吸」）。出ていれば確認の項目にチェックを入れて「送信する」を押す。
    返信文はこの手前で、攻撃的な内容でないこと（判定 reply のものだけ・禁止語・長さ）を確かめてある。"""
    dlg = page.locator("[role=dialog]", has_text="深呼吸")
    if dlg.count() and dlg.first.is_visible():
        page.wait_for_timeout(random.uniform(1000, 2000))
        dlg.first.locator("label", has_text="攻撃的なものではなく").click()   # 「攻撃的なものではなく、規約の範囲内で…」の確認
        page.wait_for_timeout(random.uniform(800, 1500))
        dlg.first.get_by_role("button", name="送信する").click()
        page.wait_for_timeout(1500)
        return True
    return False


def shot(page, name):
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"note-comment-{name}.png"))
    except Exception:
        pass


def open_comment(page, url, ckey):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    li = page.locator(f"#note-comment-{ckey}")
    for _ in range(30):      # コメント欄はスクロールで読み込まれる
        if li.count():
            break
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(400)
    li.scroll_into_view_if_needed()
    page.wait_for_timeout(1000)
    return li


def send_reply(page, url, note_key, ckey, text, really=True):
    """コメント ckey に返信する。(成功したか, 返信の key or 理由)"""
    li = open_comment(page, url, ckey)
    if not li.count():
        return False, "コメントが画面に見つかりません"
    btns = li.locator(":scope > div button[data-id=ButtonIcon]")
    reply_btn = btns.nth(btns.count() - 1)          # いいね・返信（吹き出し）のうち最後が返信
    reply_btn.click()
    page.wait_for_timeout(1500)
    ta = li.locator("textarea[aria-label=コメント入力欄]").first
    ta.wait_for(timeout=15000)
    ta.click()
    page.wait_for_timeout(500)
    ta.fill(text)
    page.wait_for_timeout(random.uniform(1500, 3000))
    if ta.input_value().strip() != text.strip():
        return False, "返信欄に文を入れられませんでした"
    shot(page, f"{ckey}-1-filled")
    if not really:
        ta.fill("")
        return True, "（通し確認）返信欄に入力して止めました"
    ta.locator("xpath=ancestor::form[1]").locator("button[type=submit]").click()
    page.wait_for_timeout(2500)
    confirm_dialog(page)
    page.wait_for_timeout(3000)
    shot(page, f"{ckey}-2-sent")
    w = warning_shown(page)
    if w:
        return False, f"WARN:{w}"
    for _ in range(6):        # 送れたかは API で確かめる（画面の文言には頼らない）
        c = next((c for c in note_comments(page, note_key) if c["key"] == ckey), None)
        if c and (c.get("is_creator_replied") or c.get("latest_creator_reply")):
            r = c.get("latest_creator_reply") or {}
            return True, r.get("key") if isinstance(r, dict) else None
        page.wait_for_timeout(5000)
    return False, "送信後もコメントに返信が付いていません"


def follow(page, urlname):
    """(結果, 説明)。結果: followed / already / failed / warn"""
    info = (api(page, f"/api/v2/creators/{urlname}").get("data") or {})
    if info.get("isFollowing"):
        return "already", "フォロー済み"
    page.goto(f"https://note.com/{urlname}", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    # 相手がこちらをフォロー済みだと「フォローバック」、そうでなければ「フォロー」（「フォロー中」は押さない）
    btn = page.locator("button:visible", has_text=re.compile(r"^\s*フォロー(バック)?\s*$")).first
    try:
        btn.wait_for(timeout=15000)
        page.wait_for_timeout(random.uniform(1000, 2500))
        btn.click()
        page.wait_for_timeout(3000)
    except Exception as e:
        return "failed", f"フォローボタンを押せませんでした（{type(e).__name__}）"
    w = warning_shown(page)
    if w:
        return "warn", w
    ok = (api(page, f"/api/v2/creators/{urlname}").get("data") or {}).get("isFollowing")
    return ("followed", "フォローしました") if ok else ("failed", "押したがフォロー中になりませんでした")


# ---------------------------------------------------------------------------
# 判定（Claude Code）と返信文の確認
# ---------------------------------------------------------------------------
def find_claude():
    for c in (shutil.which("claude"), Path.home() / ".local" / "bin" / "claude.exe"):
        if c and Path(c).exists():
            return str(c)
    return None


def history(d, limit=30):
    rs = [c["reply"] for c in d["comments"].values() if c.get("reply")]
    return rs[-limit:]


def reply_problems(text, name, hist, question=False):
    why = []
    if not text or len(text) < 20:
        why.append("短すぎる")
    if len(text) > 160:
        why.append(f"長すぎる（{len(text)}字）")
    # 表示名の先頭の部分（2字以上）＋「さん」で呼んでいるか（「理央のAIノート」→「理央さん」も可）
    if name and not any(f"{name[:k]}さん" in text for k in range(min(2, len(name)), len(name) + 1)):
        why.append(f"「{name}さん」のように相手の名前で呼んでいない")
    urls = re.findall(r"https?://\S+", text)
    if urls and (not question or len(urls) > 1 or not any(u.startswith(a) for u in urls for a in ALLOWED_URLS)):
        why.append("リンクが入っている")
    hit = [w for w in BANNED if w.lower() in text.lower()]
    if hit:
        why.append("使わない言葉: " + "・".join(hit))
    for h in hist:
        ratio, run = social.overlap(h, text, n=8)
        if ratio > 0.3 or run >= 20:
            why.append("過去の返信と似ている")
            break
    return why


def call_name(nickname):
    """表示名から呼び名（先頭の、絵文字・記号・数字・区切りより前の部分）。例: 「よし🚀40代からの副業×AI×note」→「よし」"""
    m = re.match(r"[^🀀-🿿☀-➿️｜|×@＠/／\s　(（【\[0-9０-９:：・,，、。!！?？~〜-]+", nickname or "")
    return m.group(0).strip() if m else ""


def judge(items, d, feedback=""):
    work = ROOT / "state" / "note-comments-draft.json"
    work.parent.mkdir(exist_ok=True)
    if work.exists():
        work.unlink()
    prompt = (PROMPT.read_text(encoding="utf-8")
              .replace("{OUTPUT}", work.relative_to(ROOT).as_posix())
              .replace("{COMMENTS}", json.dumps(items, ensure_ascii=False, indent=1))
              .replace("{ALLOWED_URLS}", " / ".join(ALLOWED_URLS))
              .replace("{HISTORY}", "\n".join(f"- {h}" for h in history(d)) or "（まだありません）"))
    if feedback:
        prompt += f"\n\n## 前回の書き直しの理由\n{feedback}\n"
    claude = find_claude()
    if not claude:
        log("claude コマンドが見つかりません")
        return None
    try:
        subprocess.run([claude, "-p", prompt, "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Write"],
                       cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=15 * 60, env=claude_env())
        return {it["key"]: it for it in json.loads(work.read_text(encoding="utf-8").strip().strip("`"))["items"]}
    except Exception as e:
        log(f"判定の結果が読めませんでした（{type(e).__name__}）")
        return None


def decide(items, d):
    """{key: 判定}。返信文が条件を満たさなければ2回まで書き直し、だめなら review に回す。"""
    res, feedback = {}, ""
    for _ in range(3):
        got = judge(items, d, feedback)
        if not got:
            feedback = "JSON が保存されていなかった・読めなかった"
            continue
        issues = []
        for it in items:
            g = got.get(it["key"])
            if not g or g.get("action") not in ("reply", "skip", "review"):
                issues.append(f"{it['key']}: 判定がない")
                continue
            if g["action"] == "reply":
                why = reply_problems(g.get("reply", ""), call_name(it["nickname"]), history(d), "？" in it["text"] or "?" in it["text"])
                if why:
                    issues.append(f"{it['key']}: " + " / ".join(why))
                    continue
            res[it["key"]] = g
        items = [it for it in items if it["key"] not in res]
        if not items:
            break
        feedback = "\n".join(issues)
        log(f"判定・返信文の書き直し（{feedback}）")
    for it in items:   # 書き直しても条件を満たさない → 人が確認
        res[it["key"]] = {"action": "review", "reason": "返信文が条件を満たさなかった", "summary": it["text"][:30]}
    return res


# ---------------------------------------------------------------------------
def open_browser(p):
    if not start_chrome():
        return None, None, None, "Chrome を開けませんでした"
    browser = p.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0]
    page = ctx.new_page()
    page.goto("https://note.com/", wait_until="domcontentloaded")
    user = current_user(page)
    if not user:
        user, why = auto_login(page)
        if not user:
            save_status(False, f"自動ログインできませんでした: {why}")
            return browser, page, None, why
        log(f"ログインが切れていたため、自動でログインし直しました（{user}）")
    return browser, page, user, ""


def do_reply(page, d, rec, url, note_key, ckey, text):
    """返信してフォローを返す。止めるべき警告が出たら True を返す。"""
    ok, info = send_reply(page, url, note_key, ckey, text, really=not REHEARSAL)
    if REHEARSAL:
        log(f"{rec['nickname']} さんへの返信: {info}（フォローは {'済み' if follow_state(page, rec['urlname']) else '未'}・本番なら返します）")
        return False
    if not ok:
        if str(info).startswith("WARN:"):
            stop(d, f"返信のあとに note の表示: {info[5:]}")
            return True
        log(f"{rec['nickname']} さんへの返信に失敗（{info}）。このコメントは次回また試します")
        d["comments"].pop(ckey, None)
        save(d)
        return False
    rec.update(action="replied", reply=text, replied_at=now_s(), reply_key=info,
               reply_url=f"{url}#note-comment-{info}" if info else url)
    save(d)
    log(f"返信しました: {rec['nickname']} さん（{rec['title'][:20]}…）→ {rec['reply_url']}")
    if today_count(d, "followed_at") >= MAX_FOLLOWS:
        rec["follow"] = "今日のフォローの上限（10件）のため見送り"
    else:
        res, why = follow(page, rec["urlname"])
        if res == "warn":
            stop(d, f"フォローのあとに note の表示: {why}")
            return True
        rec["follow"] = why
        if res == "followed":
            rec["followed_at"] = now_s()
        log(f"フォロー返し: {rec['nickname']} さん → {why}")
    save(d)
    return False


def follow_state(page, urlname):
    return (api(page, f"/api/v2/creators/{urlname}").get("data") or {}).get("isFollowing")


def stop(d, why):
    d["stopped"] = {"at": now_s(), "reason": why}
    save(d)
    log(f"note から制限・警告の表示が出たため、コメント返信をすべて止めます（{why}）。記事の投稿は続けます")


def run():
    d = load()
    if d.get("stopped"):
        log(f"コメント返信は止めています（{d['stopped']['reason']}・{d['stopped']['at']}）。何もしません")
        return 0
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, page, user, why = open_browser(p)
        if not user:
            log(f"note にログインできないため、コメント返信は見送ります（{why}）")
            return 2
        notes = published_notes(user) or {}
        slug_by_key = {re.sub(r".*/n/", "", v.get("url", "")): s for s, v in
                       json.loads((ROOT / "data" / "note.json").read_text(encoding="utf-8")).items()}
        me_items = []
        for key, n in notes.items():
            for c in note_comments(page, key):
                u = c.get("user") or {}
                if (not c.get("is_root") or u.get("urlname") == user or c["key"] in d["comments"]
                        or c.get("is_creator_replied") or c.get("latest_creator_reply") or c.get("is_blocked")):
                    continue
                slug = slug_by_key.get(key)
                art = ROOT / "data" / "note" / f"{slug}.md"
                me_items.append({"key": c["key"], "note_key": key, "title": n["title"], "url": n["url"],
                                 "article_file": art.relative_to(ROOT).as_posix() if slug and art.exists() else None,
                                 "nickname": u.get("nickname", ""), "urlname": u.get("urlname", ""),
                                 "text": comment_text(c), "created_at": c.get("created_at", "")})
        log(f"新しいコメント {len(me_items)} 件（公開記事 {len(notes)} 本）")
        if not me_items:
            close_chrome(browser)
            return 0
        me_items.sort(key=lambda x: x["created_at"])
        decided = decide([{k: it[k] for k in ("key", "title", "article_file", "nickname", "text")} for it in me_items], d)
        replied = today_count(d, "replied_at")
        for it in me_items:
            g = decided.get(it["key"], {"action": "review", "reason": "判定なし", "summary": it["text"][:30]})
            rec = {k: it[k] for k in ("note_key", "title", "url", "nickname", "urlname", "text", "created_at")}
            rec.update(summary=g.get("summary", ""), reason=g.get("reason", ""), handled_at=now_s())
            if g["action"] == "skip":
                rec["action"] = "skipped"
                log(f"見送り: {it['nickname']} さん（{g.get('reason')}）")
            elif g["action"] == "review":
                rec["action"] = "review"
                log(f"要確認: {it['nickname']} さん「{g.get('summary')}」（{g.get('reason')}）")
            else:
                if replied >= MAX_REPLIES:
                    log(f"今日の返信の上限（{MAX_REPLIES}件）に達したので、{it['nickname']} さんへの返信は明日にします")
                    continue
                if replied and not REHEARSAL:
                    time.sleep(random.uniform(*GAP_SEC))
                rec["action"] = "replying"
                if not REHEARSAL:
                    d["comments"][it["key"]] = rec
                if do_reply(page, d, rec, it["url"], it["note_key"], it["key"], g["reply"]):
                    break
                replied += 1
                continue
            if not REHEARSAL:
                d["comments"][it["key"]] = rec
                save(d)
        close_chrome(browser)
    return 0


def reply_one(ckey, text):
    d = load()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, page, user, why = open_browser(p)
        if not user:
            log(f"note にログインできません（{why}）")
            return 2
        for key, n in (published_notes(user) or {}).items():
            c = next((c for c in note_comments(page, key) if c["key"] == ckey), None)
            if not c:
                continue
            if c.get("is_creator_replied") or c.get("latest_creator_reply"):
                log("このコメントにはもう返信しています（何もしません）")
                return 0
            u = c.get("user") or {}
            rec = {"note_key": key, "title": n["title"], "url": n["url"], "nickname": u.get("nickname", ""),
                   "urlname": u.get("urlname", ""), "text": comment_text(c), "created_at": c.get("created_at", ""),
                   "summary": "", "reason": "指示された文で返信", "handled_at": now_s(), "action": "replying"}
            d["comments"][ckey] = rec
            do_reply(page, d, rec, n["url"], key, ckey, text)
            close_chrome(browser)
            return 0 if rec.get("action") == "replied" else 1
        log(f"コメント {ckey} が見つかりません")
        return 1


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    cmd = a[0] if a else "run"
    if cmd == "list":
        for k, c in load()["comments"].items():
            print(f"{c.get('handled_at')} {c.get('action'):<8} {c.get('nickname')}「{c.get('summary') or c.get('text', '')[:30]}」{c.get('reply_url', '')}")
        return 0
    if cmd == "resume":
        d = load()
        d.pop("stopped", None)
        save(d)
        log("コメント返信を再開します")
        return 0
    if cmd == "resolve":
        d = load()
        d["comments"][a[1]]["resolved_at"] = now_s()
        save(d)
        return 0
    if cmd == "follow":   # 返信済みでフォローを返せていない相手に、フォローだけ返す
        d = load()
        rec = d["comments"][a[1]]
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser, page, user, why = open_browser(p)
            res, why = follow(page, rec["urlname"])
            rec["follow"] = why
            if res == "followed":
                rec["followed_at"] = now_s()
            save(d)
            log(f"フォロー返し: {rec['nickname']} さん → {why}")
            close_chrome(browser)
        return 0 if res in ("followed", "already") else 1
    if cmd == "reply":
        return reply_one(a[1], a[a.index("--text") + 1])
    return run()


if __name__ == "__main__":
    sys.exit(main())
