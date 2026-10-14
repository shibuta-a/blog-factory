"""
note をブラウザ自動操作するための共通部品（A8 と同じ方式: このPCの Chrome ＋ Playwright で後から接続）。

- ログイン状態などのブラウザのデータは .secrets/note-browser-profile/（GitHub非公開）
- 渋田さんが最初に1回だけ `python scripts/note-login.py` で開いた Chrome から note にログインする
  （ID・パスワードはスクリプトでは一切扱わない。note はブラウザを閉じてもログインが残る）
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
