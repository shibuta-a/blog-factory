"""
note にログインした Chrome を用意する（最初の1回と、ログインが切れたときだけ）。

  python scripts/note-login.py

- note 用の Chrome（.secrets/note-browser-profile）でログイン画面を開き、渋田さんがログインするのを最大30分待つ
  （ID・パスワードは画面に直接入力。このスクリプトはパスワードを一切扱わない・保存しない）
- ログインできたら、その状態はプロファイルに残る。以後の自動投稿はこのプロファイルを使う
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from note_browser import CDP_URL, current_user, save_status, start_chrome  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    from playwright.sync_api import sync_playwright
    if not start_chrome("https://note.com/login"):
        print("[note] Chrome を開けませんでした。")
        return 1
    print("[note] Chrome で note のログイン画面を開きました。ログインしてください（最大30分待ちます）。")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        deadline = time.time() + 30 * 60
        while time.time() < deadline:
            pg = next((x for x in ctx.pages if "note.com" in x.url), page)
            user = current_user(pg)
            if user:
                save_status(True, f"OK（{user}）")
                print(f"[note] ログインを確認しました（note ID: {user}）。この Chrome は閉じて構いません。")
                return 0
            time.sleep(4)
    save_status(False, "ログインが確認できませんでした")
    print("[note] 30分以内にログインが確認できませんでした。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
