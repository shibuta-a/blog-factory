"""
A8.net にログインした Chrome を用意する（fetch-affiliates.py を動かす前の準備）。

  python scripts/a8-login.py

- すでにログイン済みの Chrome（ポート 9333）が開いていれば、何もせず終わる
- 開いていなければ Chrome を開き、.secrets/a8.json の ID・パスワードでプログラムが自分でログインする（人の操作なし）
  ID・パスワードは setup-once.bat で最初に1回だけ入れる。画面にもログにも出さない
- 自動ログインが通らなかったら（CAPTCHA・追加認証・パスワード違いなど）、理由を表示して終わる。人にログインを待つことはしない
- ログインした Chrome は閉じずにそのまま（A8 は閉じるとログインが消える）
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from a8_browser import CDP_PORT, CDP_URL, CHROME_PATHS, MEDIA_CONSOLE, PROFILE, auto_login, logged_in_url  # noqa: E402


def tabs():
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=3) as r:
            return [t["url"] for t in json.loads(r.read().decode("utf-8")) if t.get("type") == "page"]
    except Exception:
        return None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    urls = tabs()
    if urls and any(logged_in_url(u) for u in urls):
        print("[A8] ログイン済みの Chrome が開いています。そのまま使えます。")
        return 0
    if urls is None:
        chrome = next((p for p in CHROME_PATHS if Path(p).exists()), None)
        if not chrome:
            print("[A8] Google Chrome が見つかりません。")
            return 1
        PROFILE.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([chrome, f"--user-data-dir={PROFILE}", f"--remote-debugging-port={CDP_PORT}",
                          "--remote-debugging-address=127.0.0.1", "--no-first-run", "--window-size=1280,900", MEDIA_CONSOLE],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        for _ in range(30):
            time.sleep(1)
            if tabs() is not None:
                break
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL, timeout=60000)
        page = browser.contexts[0].new_page()
        page.goto(MEDIA_CONSOLE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        ok, why = (True, "") if logged_in_url(page.url) else auto_login(page)
        page.close()
    print("[A8] ログインしました（保存済みの ID・パスワードで自動ログイン）。" if ok else f"[A8] 自動ログインできませんでした: {why}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
