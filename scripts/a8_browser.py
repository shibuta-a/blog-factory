"""
A8.net をブラウザ自動操作するための共通設定（Playwright + このPCの Chrome）。

A8.net はブラウザを閉じるとログインが消える（セッション方式）。そのため、
  1. `python scripts/a8-login.py` が「デバッグ用ポート 9333 付き」で Chrome を開き、渋田さんが1回ログイン
  2. その Chrome を開いたまま、`scripts/fetch-affiliates.py` が後から接続して操作する
という2段構え。ログイン状態などのブラウザのデータは .secrets/a8-browser-profile/（GitHub非公開）に置く。
ログインが切れていたら、.secrets/a8.json（login_id / password。渋田さんが setup-once.bat で直接入力）で自動でログインし直す。
パスワードは画面にもログにも出さない。A8 のログイン画面に「ログイン状態を保持」の項目はない（2026-10-15 確認）。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / ".secrets"
PROFILE = SECRETS / "a8-browser-profile"

CDP_PORT = 9333
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
CREDS = SECRETS / "a8.json"
LOGIN_URL = "https://www.a8.net/"
MEDIA_CONSOLE = "https://media-console.a8.net/"

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def logged_in_url(url):
    """ログイン後のメディア管理画面なら True。"""
    return "media-console.a8.net" in url and not any(
        w in url.lower() for w in ("login", "re-authentication", "logout"))


def load_creds():
    """(ログインID, パスワード)。未設定なら None。"""
    try:
        j = json.loads(CREDS.read_text(encoding="utf-8-sig"))
        return (j["login_id"].strip(), j["password"]) if j.get("login_id") and j.get("password") else None
    except Exception:
        return None


def blocked_reason(page):
    """ログイン画面で止まったとき、CAPTCHA や追加認証が出ているならその説明を返す。"""
    try:
        text = page.evaluate("() => document.body.innerText")
        frames = " ".join(f.url for f in page.frames)
    except Exception:
        return ""
    if "recaptcha" in frames or "hcaptcha" in frames or "turnstile" in frames or "ロボットではありません" in text:
        return "CAPTCHA（ロボット確認）が出たため自動ログインできませんでした"
    if any(w in text for w in ("認証コード", "ワンタイム", "二段階", "2段階", "確認コード")):
        return "追加認証（認証コード）を求められたため自動ログインできませんでした"
    return ""


def auto_login(page):
    """保存済みの ID・パスワードで A8 のメディア会員にログインする。(成功したか, 失敗の理由)。"""
    creds = load_creds()
    if not creds:
        return False, ".secrets/a8.json に ID・パスワードがないため自動ログインできません（setup-once.bat で入力）"
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        form = page.locator('form[name="asLogin"]:visible').first
        form.locator('input[name="login"]').fill(creds[0])
        form.locator('input[name="passwd"]').fill(creds[1])
        with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
            form.locator('input[name="login_as_btn"]').click()
        page.wait_for_timeout(4000)
    except Exception as e:
        return False, f"ログイン画面の操作に失敗しました（{type(e).__name__}。A8 の画面の仕様が変わった可能性）"
    if logged_in_url(page.url):
        return True, ""
    return False, blocked_reason(page) or "ID・パスワードでログインできませんでした（パスワード変更・入力ミスの可能性）"
