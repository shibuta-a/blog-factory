#!/usr/bin/env python3
"""
X（@maf_media_jp）の毎朝の処理（boot-run.py の「5. X」から呼ばれる）。A8・note と同じく、X 専用の Chrome プロファイルを自動操作する。

  python scripts/x-run.py          … 毎朝: ログインを確かめ（切れていれば .secrets/x.json で自動ログイン）、投稿する
  python scripts/x-run.py check    … ログインできるか確かめるだけ
  python scripts/x-run.py resume   … 「X の自動処理をすべて止める」状態を解除する（制限が解けたのを確かめてから）

止め方（data/ ではなく state/x-status.json に残す。STATUS.md の先頭に出る）:
  ・CAPTCHA・確認コード・ログイン失敗 → その日の X だけ止める（翌朝また試す）
  ・アカウントの制限・凍結・警告・「ログインが現在許可されていません」 → X の自動処理をすべて止める（stop_all）。
    ブログと note は止めない。制限が解けたのを確かめてから resume で再開する（突破は試みない）

2026-10-15: X が自動操作のブラウザからのログインを断ったため（「お使いのアカウントへのログインが現在許可されていません」）、
投稿の部分（予約・カード画像・本文）はログインできてから作る。今はログインの確認と止める仕組みだけ。
"""
import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import x_browser as xb  # noqa: E402

ROOT = xb.ROOT
STATE = ROOT / "state" / "x-status.json"
REHEARSAL = bool(os.environ.get("BLOG_REHEARSAL"))


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(**kw):
    st = load_state()
    st.update(kw, at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def log(msg):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def login():
    """(ユーザー名, 理由)。ブラウザは開いたまま返さず、ここで閉じる。"""
    from playwright.sync_api import sync_playwright
    if not xb.start_chrome():
        return None, "X 用の Chrome を開けませんでした"
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(xb.CDP_URL)
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        user, why = xb.ensure_login(page)
        try:
            browser.close()
        except Exception:
            pass
    return user, why


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "resume":
        save_state(stop_all=False, ok=None, text="再開（次の朝からログインと投稿を試します）")
        log("X: 自動処理を再開します")
        return 0
    st = load_state()
    if st.get("stop_all") and cmd == "run":
        log(f"X: 自動処理をすべて止めています（{st.get('text')}・{st.get('at')}）。何もしません")
        return 0
    user, why = login()
    if not user:
        stop_all = any(w in why for w in ("制限", "凍結", "断りました", "許可されていません"))
        save_state(ok=False, stop_all=stop_all or bool(st.get("stop_all")), text=why)
        log(f"X: ログインできませんでした（{why}）→ " + ("X の自動処理をすべて止めます（ブログ・note は続けます）" if stop_all
                                                 else "今日の X は見送ります（翌朝また試します）"))
        return 2
    save_state(ok=True, stop_all=False, text=f"ログイン中（@{user}）")
    log(f"X: ログイン中（@{user}）")
    if cmd == "check":
        return 0
    log("X: 投稿の部分はまだ作っていません（ログインが通るようになってから作る）" + ("（通し確認）" if REHEARSAL else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
