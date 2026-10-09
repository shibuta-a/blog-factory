"""
A8.net にログインした Chrome を用意する（fetch-affiliates.py を動かす前の準備）。

  python scripts/a8-login.py

- すでにログイン済みの Chrome が開いていれば、何もせず終わる
- 開いていなければ Chrome でログイン画面を開き、渋田さんがログインするのを最大30分待つ
  （ID・パスワードは画面に直接入力。このスクリプトはパスワードを一切扱わない・保存しない）
- ログインした Chrome は閉じずにそのまま。閉じるとログインが消える
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from a8_browser import CDP_PORT, CDP_URL, CHROME_PATHS, LOGIN_URL, PROFILE, logged_in_url  # noqa: E402


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
                          "--remote-debugging-address=127.0.0.1", "--no-first-run", "--window-size=1280,900", LOGIN_URL],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    print("[A8] Chrome でログイン画面を開きました。メディア会員としてログインしてください（最大30分待ちます）。")
    deadline = time.time() + 30 * 60
    while time.time() < deadline:
        urls = tabs() or []
        if any(logged_in_url(u) for u in urls):
            print("[A8] ログインを確認しました。Chrome は閉じずにそのままにしてください。")
            return 0
        time.sleep(3)
    print("[A8] 30分以内にログインが確認できませんでした。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
