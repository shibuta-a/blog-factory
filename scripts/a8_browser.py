"""
A8.net をブラウザ自動操作するための共通設定（Playwright + このPCの Chrome）。

A8.net はブラウザを閉じるとログインが消える（セッション方式）。そのため、
  1. `python scripts/a8-login.py` が「デバッグ用ポート 9333 付き」で Chrome を開き、渋田さんが1回ログイン
  2. その Chrome を開いたまま、`scripts/fetch-affiliates.py` が後から接続して操作する
という2段構え。ログイン状態などのブラウザのデータは .secrets/a8-browser-profile/（GitHub非公開）に置く。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / ".secrets"
PROFILE = SECRETS / "a8-browser-profile"

CDP_PORT = 9333
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
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
