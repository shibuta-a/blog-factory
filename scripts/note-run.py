#!/usr/bin/env python3
"""
ブログ記事を note にも自動で展開する（毎朝の自動実行 boot-run.py から呼ばれる）。

  python scripts/note-run.py            … 公開済みで note 未投稿の記事から、1日 max_per_day 本を「別バージョン」にして note に投稿
  python scripts/note-run.py --dry      … 別バージョンを書いて重複チェックまで（投稿しない）
  python scripts/note-run.py --slug X   … 記事を指定（テスト用。1日の上限は見ない）
  python scripts/note-run.py check      … note のログインが生きているか確かめる
  python scripts/note-run.py list       … note に投稿済みの記事

流れ:
  1. 公開済み（公開予定時刻を過ぎた）のブログ記事のうち、note にまだ出していないものを新しい順に選ぶ
  2. Claude Code（claude -p・scripts/note-prompt.md）が note 用の別バージョンを書く → data/note/<slug>.md
  3. 重複チェック: ブログと同じ10文字の並びが 12% 以上、または 40 字以上の一致があれば書き直し（3回書いてだめなら見送り）
  4. note 用の Chrome（ログイン済みプロファイル）で記事を入力して公開 → URL を data/note.json に記録
  5. publish.py でブログ側の記事末尾に note への案内を足す（note に出した記事だけ）
ログインが切れていたら投稿は飛ばし、STATUS.md の「要対応」に「noteに再ログインして」と出す。
"""
import argparse
import datetime
import html
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import social  # noqa: E402
from note_browser import CDP_URL, SHOTS, close_chrome, current_user, save_status, start_chrome  # noqa: E402

ROOT = social.ROOT
NOTE_DIR = ROOT / "data" / "note"
PROMPT = ROOT / "scripts" / "note-prompt.md"
LOG = ROOT / "logs" / "note.log"
ORISHAN_URL = "https://maf-group.jp/orishanlab.html"
MAX_RATIO, MAX_RUN = 0.12, 40


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_md(path):
    text = path.read_text(encoding="utf-8").lstrip("﻿").replace("\r\n", "\n")
    meta, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\s*\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
        body = text[m.end():]
    return meta, body


def load_log():
    return json.loads(social.NOTE_LOG.read_text(encoding="utf-8")) if social.NOTE_LOG.exists() else {}


def save_log(data):
    social.NOTE_LOG.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def due_articles(cfg, done):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M")
    out = []
    for md in (ROOT / "content" / "ja").glob("*.md"):
        meta, _ = read_md(md)
        slug = meta.get("slug") or md.stem
        if meta.get("draft", "").lower() in ("true", "yes", "1") or meta.get("note", "").lower() == "none":
            continue
        at = meta.get("publish_at") or f"{meta.get('date', '9999')}T00:00"
        if at > now or at[:10] < cfg.get("since", "") or slug in done:
            continue
        if meta.get("genre") in cfg.get("skip_genres", []):
            continue
        out.append((at, slug, md, meta))
    return sorted(out, reverse=True)


# ---------------------------------------------------------------------------
# 別バージョンを書く（Claude Code）
# ---------------------------------------------------------------------------
def find_claude():
    for c in (shutil.which("claude"), Path.home() / ".local" / "bin" / "claude.exe"):
        if c and Path(c).exists():
            return str(c)
    return None


def write_version(slug, md, meta, base_url, feedback=""):
    out = NOTE_DIR / f"{slug}.md"
    NOTE_DIR.mkdir(parents=True, exist_ok=True)
    blog_url = f"{base_url}/{slug}"
    own = meta.get("genre") == "orishan"
    prompt = (PROMPT.read_text(encoding="utf-8")
              .replace("{SOURCE}", md.relative_to(ROOT).as_posix())
              .replace("{OUTPUT}", out.relative_to(ROOT).as_posix())
              .replace("{BLOG_LINK_LINE}", f"もう少し詳しい選び方や比較は、ブログ記事「{meta.get('title', '')}」にまとめています。\n{blog_url}")
              .replace("{OWN_LINE}", f"ブログ記事の案内の次の行に、自社サービスの公式ページの案内を1回だけ書く: "
                                     f"「オリジナルシャンパンでの開業について、詳しくは MAF GROUP の公式ページへ。\\n{ORISHAN_URL}」"
                       if own else "自社サービス（オリシャン）の案内は書かない"))
    if feedback:
        prompt += f"\n\n## 前回の書き直しの理由\n{feedback}\n前回の {out.relative_to(ROOT).as_posix()} は捨てて、一から書き直すこと。\n"
    claude = find_claude()
    if not claude:
        log("claude コマンドが見つかりません")
        return None
    r = subprocess.run([claude, "-p", prompt, "--permission-mode", "acceptEdits",
                        "--allowedTools", "Read", "Write", "Edit"],
                       cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=20 * 60)
    if not out.exists():
        log(f"{slug}: 別バージョンが保存されませんでした（{r.stdout[-300:]}{r.stderr[-300:]}）")
        return None
    return out


def ensure_version(slug, md, meta, base_url):
    out = NOTE_DIR / f"{slug}.md"
    blog = md.read_text(encoding="utf-8")
    feedback = ""
    for attempt in range(3):
        if attempt > 0 or not out.exists():
            if not write_version(slug, md, meta, base_url, feedback):
                break
        # ブログへの案内の行（ブログのタイトルをそのまま書く）は判定から外す
        ratio, run = social.overlap(blog, out.read_text(encoding="utf-8").replace(meta.get("title", "") or "\0", ""))
        n_meta, n_body = read_md(out)
        size = len(re.sub(r"\s", "", n_body))
        if ratio < MAX_RATIO and run < MAX_RUN and n_meta.get("title") and size >= 800:
            log(f"{slug}: 別バージョンOK（ブログとの一致 {ratio:.0%}・最長一致 {run} 字・{size} 字）")
            return out
        feedback = (f"ブログとの一致率 {ratio:.0%}（{MAX_RATIO:.0%} 未満にする）・最長の一致 {run} 字（{MAX_RUN} 字未満にする）・"
                    f"本文 {size} 字・タイトル{'あり' if n_meta.get('title') else 'なし'}。ブログの文をなぞらず、自分の言葉で書き直す。")
        log(f"{slug}: 書き直し（{feedback}）")
    log(f"{slug}: 重複チェックを通らなかったため note への投稿を見送り")
    return None


# ---------------------------------------------------------------------------
# note 用の Markdown → 貼り付け用 HTML
# ---------------------------------------------------------------------------
def inline(s):
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r'(?<!href=")(?<!">)(https?://[^\s<]+)', r'<a href="\1">\1</a>', s)
    return s


def to_html(body):
    out, para, items = [], [], []

    def flush():
        if para:
            out.append("<p>" + "<br>".join(inline(x) for x in para) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            items.clear()
    for line in body.replace("\\n", "\n").splitlines():
        s = line.strip()
        if not s:
            flush()
        elif s.startswith("### "):
            flush(); out.append(f"<h3>{inline(s[4:])}</h3>")
        elif s.startswith("## "):
            flush(); out.append(f"<h2>{inline(s[3:])}</h2>")
        elif re.match(r"^[-*] ", s):
            if para:
                flush()
            items.append(s[2:])
        else:
            if items:
                flush()
            para.append(s)
    flush()
    return "".join(out)


# ---------------------------------------------------------------------------
# note への投稿（ブラウザ自動操作）
# ---------------------------------------------------------------------------
def shot(page, name):
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"note-{name}.png"))
    except Exception:
        pass


PASTE_JS = """(html) => {
  const el = document.querySelector('.ProseMirror');
  el.focus();
  const dt = new DataTransfer();
  dt.setData('text/html', html);
  dt.setData('text/plain', html.replace(/<[^>]+>/g, ''));
  el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
}"""


def post_to_note(page, user, title, body_html, tags, publish=True):
    """note の新規記事画面で、タイトル・本文・ハッシュタグを入れて公開する。公開した記事のURLを返す。"""
    page.goto("https://note.com/notes/new", wait_until="domcontentloaded")
    page.wait_for_url(re.compile(r"editor\.note\.com/notes/\w+/edit"), timeout=60000)
    key = re.search(r"/notes/(\w+)/", page.url).group(1)
    title_box = page.locator('textarea[placeholder="記事タイトル"]')
    title_box.wait_for(timeout=30000)
    title_box.fill(title)
    body = page.locator(".ProseMirror").first
    body.click()
    page.evaluate(PASTE_JS, body_html)
    page.wait_for_timeout(2000)
    shot(page, "1-editor")
    if len(body.inner_text().strip()) < 300:
        raise RuntimeError("本文を入力できませんでした（note のエディタの仕様が変わった可能性）")
    page.get_by_role("button", name="公開に進む").click()
    page.wait_for_url(re.compile(r"/publish"), timeout=60000)
    page.wait_for_timeout(2000)
    tag_box = page.locator('input[placeholder*="ハッシュタグ"]').first
    for t in tags:
        tag_box.click()
        tag_box.fill(t)
        tag_box.press("Enter")
        page.wait_for_timeout(500)
    shot(page, "2-publish")
    if not publish:
        return page.url
    page.get_by_role("button", name=re.compile("^投稿する")).click()
    # 公開すると編集画面から離れる（初回は「はじめての作品」画面、以後は記事やシェアの画面）
    page.wait_for_url(lambda u: "/publish" not in u, timeout=60000)
    page.wait_for_timeout(3000)
    shot(page, "3-done")
    url = f"https://note.com/{user}/n/{key}"
    if page.request.get(url).status != 200:
        raise RuntimeError(f"公開後の記事が表示されません: {url}")
    return url


# ---------------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "check", "list"])
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--slug")
    args = ap.parse_args()
    cfg = social.load_config().get("note", {})
    base_url = json.loads((ROOT / "site.json").read_text(encoding="utf-8"))["base_url"].rstrip("/")
    done = load_log()

    if args.cmd == "list":
        for slug, v in sorted(done.items(), key=lambda x: x[1].get("posted_at", "")):
            print(f"{v.get('posted_at')}  {slug}  {v.get('url')}")
        return 0
    if args.cmd == "run" and not cfg.get("enabled") and not args.slug:
        print("note への自動展開はオフです（data/social.json の note.enabled）")
        return 0

    today = datetime.date.today().isoformat()
    if args.slug:
        md = next((p for p in (ROOT / "content" / "ja").glob("*.md") if (read_md(p)[0].get("slug") or p.stem) == args.slug), None)
        if not md:
            sys.exit(f"記事が見つかりません: {args.slug}")
        targets = [(None, args.slug, md, read_md(md)[0])]
    else:
        left = cfg.get("max_per_day", 1) - sum(1 for v in done.values() if v.get("posted_at", "")[:10] == today)
        targets = due_articles(cfg, done)[:max(0, left)] if args.cmd == "run" else []
        if args.cmd == "run" and not targets:
            log("note: 今日投稿する記事はありません")
            return 0

    if args.dry:
        for _, slug, md, meta in targets:
            ensure_version(slug, md, meta, base_url)
        return 0

    from playwright.sync_api import sync_playwright
    if not start_chrome():
        log("note: Chrome を開けませんでした")
        return 1
    posted = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://note.com/", wait_until="domcontentloaded")
        user = current_user(page)
        if not user:
            save_status(False, "ログインが切れています")
            log("note: ログインが切れています → 渋田さんに「noteに再ログインして」と依頼（python scripts/note-login.py）")
            close_chrome(browser)
            return 2
        save_status(True, f"OK（{user}）")
        if args.cmd == "check":
            print(f"note: ログイン中（{user}）")
            close_chrome(browser)
            return 0
        for _, slug, md, meta in targets:
            ver = ensure_version(slug, md, meta, base_url)
            if not ver:
                continue
            n_meta, n_body = read_md(ver)
            tags = [t.strip().lstrip("#") for t in n_meta.get("tags", "").split("|") if t.strip()][:5]
            try:
                url = post_to_note(page, user, n_meta["title"], to_html(n_body), tags)
            except Exception as e:  # note の画面が変わったときなど
                shot(page, f"error-{slug}")
                log(f"note: {slug} の投稿に失敗しました（{type(e).__name__}: {str(e)[:300]}）。画面: .secrets/shots/note-error-{slug}.png")
                continue
            done[slug] = {"url": url, "title": n_meta["title"], "posted_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
            save_log(done)
            posted.append(slug)
            log(f"note: 投稿しました {slug} → {url}")
            time.sleep(5)
        close_chrome(browser)

    if posted:  # ブログ側に note への案内を足す
        subprocess.run([sys.executable, str(ROOT / "scripts" / "publish.py"), "-m", f"note: {','.join(posted)}"], cwd=ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
