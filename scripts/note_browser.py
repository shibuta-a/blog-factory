"""
note をブラウザ自動操作するための共通部品（A8 と同じ方式: このPCの Chrome ＋ Playwright で後から接続）。

- ログイン状態などのブラウザのデータは .secrets/note-browser-profile/（GitHub非公開）
- note はブラウザを閉じてもログインが残る。切れていたら .secrets/note.json（login_id / password。
  渋田さんが setup-once.bat で直接入力）で自動でログインし直す（auto_login）。パスワードは画面にもログにも出さない
- 自動ログインが通らない（CAPTCHA など）ときだけ、`python scripts/note-login.py` で開いた Chrome から手でログインする
- 自動投稿のときは同じプロファイルで Chrome を開き（ポート 9334）、Playwright で接続して操作する
"""
import json
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / ".secrets" / "note-browser-profile"
SHOTS = ROOT / ".secrets" / "shots"
CREDS = ROOT / ".secrets" / "note.json"
CDP_PORT = 9334
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def chrome_running():
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=3):
            return True
    except Exception:
        return False


def start_chrome(url="https://note.com/"):
    """note 用の Chrome を（まだなければ）開く。開けたら True。"""
    if chrome_running():
        return True
    chrome = next((p for p in CHROME_PATHS if Path(p).exists()), None)
    if not chrome:
        return False
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([chrome, f"--user-data-dir={PROFILE}", f"--remote-debugging-port={CDP_PORT}",
                      "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
                      "--window-size=1280,900", url],
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    for _ in range(30):
        if chrome_running():
            return True
        time.sleep(1)
    return False


def current_user(page):
    """ログイン中なら note の ID（urlname）を返す。ログインしていなければ None。"""
    try:
        data = page.evaluate("""async () => {
            const r = await fetch('https://note.com/api/v2/current_user', {credentials: 'include'});
            return r.ok ? await r.json() : null;
        }""")
        return ((data or {}).get("data") or {}).get("urlname")
    except Exception:
        return None


def auto_login(page):
    """保存済みのメールアドレス（または note ID）・パスワードで note にログインする。(ログインできた note ID, 失敗の理由)。"""
    try:
        j = json.loads(CREDS.read_text(encoding="utf-8-sig"))
        login, pw = (j.get("login_id") or j.get("email") or "").strip(), j.get("password", "")
    except Exception:
        login = pw = ""
    if not (login and pw):
        return None, ".secrets/note.json にログイン情報がないため自動ログインできません（setup-once.bat で入力）"
    try:
        page.goto("https://note.com/login", wait_until="domcontentloaded", timeout=60000)
        page.locator('input[name="login"]').wait_for(timeout=30000)
        page.locator('input[name="login"]').fill(login)
        page.locator('input[name="password"]').fill(pw)
        page.locator('form button[type="submit"]').first.click()
        for _ in range(30):
            page.wait_for_timeout(1000)
            if "/login" not in page.url:
                break
        page.wait_for_timeout(2000)
    except Exception as e:
        return None, f"ログイン画面の操作に失敗しました（{type(e).__name__}。note の画面の仕様が変わった可能性）"
    user = current_user(page)
    if user:
        return user, ""
    try:
        text = page.evaluate("() => document.body.innerText")
        # 見えている枠だけ（隠れた reCAPTCHA の問題枠は数えない）
        frames = " ".join(page.evaluate("""() => [...document.querySelectorAll('iframe')]
            .filter(f => { const r = f.getBoundingClientRect(); return r.width > 100 && r.height > 100 && getComputedStyle(f).visibility !== 'hidden'; })
            .map(f => f.src)"""))
    except Exception:
        text = frames = ""
    # note のログイン画面には見えない reCAPTCHA が常に入っている（anchor）。画像を選ぶ問題（bframe）が出たときだけ CAPTCHA とみなす
    if "ご確認ください" in text or "正しくありません" in text:
        return None, "メールアドレス（note ID）かパスワードが違うと note に言われました（.secrets/note.json を確認）"
    if "recaptcha/enterprise/bframe" in frames or "recaptcha/api2/bframe" in frames or "ロボットではありません" in text:
        return None, "CAPTCHA（ロボット確認）が出たため自動ログインできませんでした"
    if any(w in text for w in ("認証コード", "確認コード", "二段階", "2段階")):
        return None, "追加認証（認証コード）を求められたため自動ログインできませんでした"
    return None, "メールアドレス・パスワードでログインできませんでした（パスワード変更・入力ミスの可能性）"


def published_notes(user, max_pages=30):
    """公開中の自分の note 記事を {key: {"title", "url"}} で返す（ログイン不要の公開API。下書きは含まない）。
    取れなかったら None（ネットワークの不調など）。"""
    out = {}
    try:
        for page in range(1, max_pages + 1):
            req = urllib.request.Request(f"https://note.com/api/v2/creators/{user}/contents?kind=note&page={page}",
                                         headers={"User-Agent": "Mozilla/5.0 blog-factory"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8")).get("data") or {}
            for n in data.get("contents") or []:
                out[n["key"]] = {"title": n.get("name", ""), "url": f"https://note.com/{user}/n/{n['key']}"}
            if data.get("isLastPage", True):
                break
        return out
    except Exception:
        return None


def close_chrome(browser):
    """接続した Chrome を閉じる（note はログインが残るので、閉じてよい）。"""
    try:
        cdp = browser.new_browser_cdp_session()
        cdp.send("Browser.close")
    except Exception:
        pass


def save_status(ok, text):
    path = ROOT / "state" / "note-status.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"ok": ok, "text": text, "at": time.strftime("%Y-%m-%d %H:%M")},
                               ensure_ascii=False), encoding="utf-8")
