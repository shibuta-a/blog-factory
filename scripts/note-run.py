#!/usr/bin/env python3
"""
ブログ記事を note にも自動で展開する（毎朝の自動実行 boot-run.py から呼ばれる）。

  python scripts/note-run.py            … 公開済みで note 未投稿の記事から、1日 max_per_day 本を「別バージョン」にして note に投稿
  python scripts/note-run.py --dry      … 別バージョンを書いて重複チェックまで（投稿しない）
  python scripts/note-run.py --slug X   … 記事を指定（テスト用。1日の上限は見ない）
  python scripts/note-run.py check      … note のログインが生きているか確かめる
  python scripts/note-run.py list       … note に投稿済みの記事
  python scripts/note-run.py header     … 投稿済みで見出し画像のない記事に、あとから見出し画像を付ける

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
from note_browser import CDP_URL, SHOTS, close_chrome, current_user, published_notes, save_status, start_chrome  # noqa: E402

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


HEADER_SIZE = (1280, 670)  # note の見出し画像の推奨サイズ
FONT = Path(r"C:\Windows\Fonts\YuGothB.ttc")


def header_image(slug, title):
    """note の見出し画像を用意する。ブログのアイキャッチ（Openverse の CC0/PD 写真）を 1280×670 に切り出す。
    写真のない記事（images: none のジャンルなど）は、タイトル入りの無地の画像を作る。作れなければ None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    out = ROOT / "state" / "note-img" / f"{slug}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    images = json.loads((ROOT / "data" / "images.json").read_text(encoding="utf-8")) if (ROOT / "data" / "images.json").exists() else {}
    ec = (images.get(slug) or {}).get("eyecatch") or {}
    src = ROOT / "static" / ec.get("file", "-")
    W, H = HEADER_SIZE
    if ec and src.exists():
        im = Image.open(src).convert("RGB")
        scale = max(W / im.width, H / im.height)
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
        left, top = (im.width - W) // 2, (im.height - H) // 2
        im.crop((left, top, left + W, top + H)).save(out, quality=90)
        return out
    if not FONT.exists():
        return None
    im = Image.new("RGB", (W, H), (34, 58, 94))
    d = ImageDraw.Draw(im)
    d.rectangle((48, 48, W - 48, H - 48), outline=(201, 168, 92), width=4)
    font = ImageFont.truetype(str(FONT), 64)
    lines, cur = [], ""
    for ch in title:                       # 幅に合わせて折り返す（最大3行）
        if d.textlength(cur + ch, font=font) > W - 220:
            lines.append(cur)
            cur = ""
        cur += ch
    lines = (lines + [cur])[:3]
    y = (H - len(lines) * 92) // 2
    for line in lines:
        d.text(((W - d.textlength(line, font=font)) // 2, y), line, font=font, fill=(255, 255, 255))
        y += 92
    small = ImageFont.truetype(str(FONT), 30)
    d.text((W - 110 - d.textlength("MAF ブログ", font=small), H - 100), "MAF ブログ", font=small, fill=(201, 168, 92))
    im.save(out, quality=90)
    return out


def wait_js(page, js, timeout=60):
    """js（真偽を返す関数）が真になるまで1秒ごとに確かめる。
    note の画面は CSP で Playwright の wait_for_function（文字列の評価）が使えないため、evaluate で見る。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if page.evaluate(js):
                return
        except Exception:
            pass   # 画面の切り替わり中など
        page.wait_for_timeout(1000)
    raise TimeoutError(f"{timeout}秒待っても画面が変わりませんでした")


def set_header_image(page, path):
    """編集画面のタイトル上の画像ボタン →「画像をアップロード」→ 切り抜きの確定。"""
    page.locator('textarea[placeholder="記事タイトル"]').locator("xpath=preceding::button[1]").click()
    with page.expect_file_chooser(timeout=15000) as fc:
        page.get_by_role("button", name=re.compile("画像をアップロード")).click()
    fc.value.set_files(str(path))
    page.wait_for_timeout(2500)
    shot(page, "0-header-crop")
    save = page.get_by_role("button", name="保存", exact=True)
    save.last.wait_for(timeout=30000)   # 切り抜きの画面が出るまで待つ
    save.last.click()
    save.last.wait_for(state="hidden", timeout=30000)
    # アップロードが終わって、タイトルの上に画像が出るまで待つ（新規の記事でも、更新でも）
    ok = True
    try:
        wait_js(page, "() => { const t = document.querySelector('textarea[placeholder=\"記事タイトル\"]');"
                      " const ty = t.getBoundingClientRect().top;"
                      " return [...document.querySelectorAll('img')].some(i => { const r = i.getBoundingClientRect();"
                      " return r.width > 300 && r.bottom <= ty + 5 && i.complete && i.naturalWidth > 0; }); }", 90)
        wait_js(page, "() => [...document.querySelectorAll('button')]"
                      ".some(b => b.innerText.trim() === '公開に進む' && !b.disabled)", 60)
        page.wait_for_timeout(1500)
    except Exception as e:
        ok = str(e).splitlines()[0][:150]
    if ok is not True:
        raise RuntimeError(f"見出し画像を設定できませんでした（note の画面の仕様が変わった可能性: {ok}）")


def add_header_to_posted(page, url, image):
    """投稿済みの記事に、あとから見出し画像を付けて「更新する」。"""
    key = url.rstrip("/").split("/")[-1]
    page.goto(f"https://editor.note.com/notes/{key}/edit/", wait_until="domcontentloaded")
    page.locator('textarea[placeholder="記事タイトル"]').wait_for(timeout=30000)
    page.wait_for_timeout(3000)
    title_y = page.locator('textarea[placeholder="記事タイトル"]').bounding_box()["y"]
    has = page.evaluate("(ty) => [...document.querySelectorAll('img')].some(i => { const r = i.getBoundingClientRect();"
                        " return r.width > 300 && r.bottom <= ty; })", title_y)
    if not has:
        set_header_image(page, image)
    page.get_by_role("button", name="公開に進む").click()
    page.wait_for_url(re.compile(r"/publish"), timeout=60000)
    page.wait_for_timeout(2000)
    page.get_by_role("button", name=re.compile("^更新する")).click()
    wait_js(page, "() => !location.pathname.includes('/publish') || document.body.innerText.includes('シェアしてみましょう')", 60)
    page.wait_for_timeout(2000)


PASTE_JS = """(html) => {
  const el = document.querySelector('.ProseMirror');
  el.focus();
  const dt = new DataTransfer();
  dt.setData('text/html', html);
  dt.setData('text/plain', html.replace(/<[^>]+>/g, ''));
  el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
}"""


def post_to_note(page, user, title, body_html, tags, image=None, publish=True):
    """note の新規記事画面で、見出し画像・タイトル・本文・ハッシュタグを入れて公開する。(記事のURL, 見出し画像を付けられたか) を返す。"""
    page.goto("https://note.com/notes/new", wait_until="domcontentloaded")
    page.wait_for_url(re.compile(r"editor\.note\.com/notes/\w+/edit"), timeout=60000)
    key = re.search(r"/notes/(\w+)/", page.url).group(1)
    title_box = page.locator('textarea[placeholder="記事タイトル"]')
    title_box.wait_for(timeout=30000)
    title_box.fill(title)
    # 見出し画像（サムネ）は必須。付けられなければ白サムネのまま出さず、例外で投稿をやめる（次回に再試行）
    if not image:
        raise HeaderError("見出し画像を作れませんでした")
    try:
        set_header_image(page, image)
    except Exception as e:
        shot(page, "error-header")
        raise HeaderError(f"見出し画像を付けられませんでした（{type(e).__name__}: {str(e)[:200]}）") from e
    has_header = True
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
        return page.url, has_header
    page.get_by_role("button", name=re.compile("^投稿する")).click()
    # 公開後の画面は note 側の都合で変わる（別画面へ移る・「シェアしてみましょう」・「記事が公開されました」
    # 「○日連続投稿！」のお祝いポップアップなど）。画面の文言は目安にとどめ、成否は公開APIに記事が出たかで決める
    try:
        wait_js(page, "() => !location.pathname.includes('/publish') || /シェアしてみましょう|記事が公開されました|連続投稿/"
                      ".test(document.body.innerText)", 60)
    except TimeoutError:
        pass
    page.wait_for_timeout(3000)
    shot(page, "3-done")
    url = wait_published(user, key)
    if not url:
        raise RuntimeError(f"投稿後3分たっても公開中の記事一覧に出ません（記事ID {key}）")
    return url, has_header


class HeaderError(RuntimeError):
    """見出し画像を付けられなかった（白サムネで出さないため、投稿をやめて次回に再試行する）"""


def wait_published(user, key, timeout=180):
    """公開中の記事一覧（公開API）に記事 key が出たら、その URL を返す。出なければ None。"""
    end = time.time() + timeout
    while True:
        found = published_notes(user) or {}
        if key in found:
            return found[key]["url"]
        if time.time() >= end:
            return None
        time.sleep(10)


def norm_title(s):
    return re.sub(r"[\s　]", "", s or "")


def find_same_title(user, title):
    """自分の note に同じタイトルの公開記事があれば、その URL を返す（二重投稿の防止）。
    一覧を取れなければ例外（確かめられないまま投稿しない）。"""
    found = published_notes(user)
    if found is None:
        raise RuntimeError("note の公開記事一覧を取れませんでした（二重投稿を防ぐため今回は投稿しません）")
    return next((v["url"] for v in found.values() if norm_title(v["title"]) == norm_title(title)), None)


# ---------------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "check", "list", "header"])
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
    if args.slug and args.cmd != "header":
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
        if args.cmd == "header":  # 投稿済みで見出し画像のない記事に、あとから付ける
            for slug, v in done.items():
                if v.get("header") or (args.slug and slug != args.slug):
                    continue
                img = header_image(slug, v.get("title", ""))
                try:
                    add_header_to_posted(page, v["url"], img)
                    v["header"] = True
                    save_log(done)
                    log(f"note: 見出し画像を付けました {slug} → {v['url']}")
                except Exception as e:
                    shot(page, f"error-header-{slug}")
                    log(f"note: {slug} の見出し画像に失敗（{type(e).__name__}: {str(e)[:300]}）")
            close_chrome(browser)
            return 0
        for _, slug, md, meta in targets:
            ver = ensure_version(slug, md, meta, base_url)
            if not ver:
                continue
            n_meta, n_body = read_md(ver)
            tags = [t.strip().lstrip("#") for t in n_meta.get("tags", "").split("|") if t.strip()][:5]
            try:
                same = find_same_title(user, n_meta["title"])
                if same:  # 前回、投稿できていたのに記録を取りこぼした場合など
                    done[slug] = {"url": same, "title": n_meta["title"], "header": True,
                                  "posted_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "_recovered": True}
                    save_log(done)
                    posted.append(slug)
                    log(f"note: {slug} は同じタイトルの記事がすでに note にあるため投稿しません（記録だけ補いました → {same}）")
                    continue
                url, has_header = post_to_note(page, user, n_meta["title"], to_html(n_body), tags,
                                   image=header_image(slug, n_meta["title"]))
            except HeaderError as e:
                log(f"note: {slug} は{e}。白サムネで出さないよう投稿をやめ、次回に再試行します")
                continue
            except Exception as e:  # note の画面が変わったときなど
                shot(page, f"error-{slug}")
                log(f"note: {slug} の投稿に失敗しました（{type(e).__name__}: {str(e)[:300]}）。画面: .secrets/shots/note-error-{slug}.png")
                continue
            done[slug] = {"url": url, "title": n_meta["title"], "header": has_header,
                          "posted_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
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
