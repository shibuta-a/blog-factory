"""
note にログインした Chrome を用意する（ログインが切れたとき用。毎朝の自動投稿は note-run.py が同じことを自分で行う）。

  python scripts/note-login.py

- note 用の Chrome（.secrets/note-browser-profile）を開き、ログイン済みなら何もしない
- 切れていたら .secrets/note.json の ID・パスワードでプログラムが自分でログインする（人の操作なし）
  ID・パスワードは setup-once.bat で最初に1回だけ入れる。画面にもログにも出さない
- 自動ログインが通らなかったら（CAPTCHA・追加認証・パスワード違いなど）、理由を表示して終わる。人にログインを待つことはしない
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from note_browser import CDP_URL, auto_login, close_chrome, current_user, save_status, start_chrome  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    from playwright.sync_api import sync_playwright
    if not start_chrome():
        print("[note] Chrome を開けませんでした。")
        return 1
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://note.com/", wait_until="domcontentloaded")
        user, why = (current_user(page), "")
        if not user:
            user, why = auto_login(page)
        save_status(bool(user), f"OK（{user}）" if user else f"自動ログインできませんでした: {why}")
        close_chrome(browser)
    print(f"[note] ログイン中（{user}）" if user else f"[note] 自動ログインできませんでした: {why}")
    return 0 if user else 1


if __name__ == "__main__":
    sys.exit(main())
