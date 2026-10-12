"""起動したら記事を書いて公開し、誰も触っていなければシャットダウンする（タスク BlogFactory-Boot から呼ばれる）。

きっかけは2つ（どちらで動いても1日1回だけ）:
  ・ログオン時（BIOS の時刻起動 → 自動ログオン）
  ・毎日の bios_wake_time の数分後（その時刻に PC がすでに点いていた日の保険。このときは起動から時間がたっているので落とさない）

本数は 1本15% / 2本70% / 3本15%（1日の上限3本は autopilot.py が別に守る）。
週1本の自社サービス記事（オリシャン・既定は水曜）を書く日は、それ＋通常1本の計2本。
書いた記事は publish.py が公開予定時刻（24時間以内のランダム）を付けて GitHub へ送る。Cloudflare の門番（functions/_middleware.js）がアクセスのたびに判定し、時刻が来た瞬間から表示する。
新規記事のあと、公開済み記事の見直し（scripts/refresh-prompt.md）を1本行う（記事が20本以上・今週3本まで）。
最後に、公開済みの記事を note 用の別バージョンにして note に投稿する（scripts/note-run.py・1日1本）。
X へのポストは PC ではなく Cloudflare の workers/x-poster が公開時刻に行う。
"""
import json
import os
import random
import subprocess
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DONE = os.path.join(ROOT, "state", "boot-done.txt")
LOG = os.path.join(ROOT, "logs", "boot-run.log")


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def main():
    today = date.today().isoformat()
    os.makedirs(os.path.dirname(DONE), exist_ok=True)
    if os.path.exists(DONE) and open(DONE, encoding="utf-8").read().strip() == today:
        log("今日はもう動いた → 何もしない")
        return 0
    # 最初に印を付ける（途中で落ちても同じ日に2回は書かない）
    with open(DONE, "w", encoding="utf-8") as f:
        f.write(today)

    r = random.randrange(100)
    n = 1 if r < 15 else 2 if r < 85 else 3
    # 週1本の自社サービス記事（オリシャン）の日は、それ＋通常1本の計2本にする
    weekly = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "autopilot.py"), "weekly-due"],
                            cwd=ROOT, capture_output=True).returncode == 0
    if weekly:
        n = 2
    log(f"記事を {n} 本書きます" + ("（うち1本は週1本の自社サービス記事）" if weekly else ""))
    with open(os.path.join(ROOT, "logs", "auto-generate.log"), "a", encoding="utf-8") as out:
        code = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "auto-generate.py"), "-n", str(n)],
                              cwd=ROOT, stdout=out, stderr=subprocess.STDOUT).returncode
    log(f"記事の生成が終了（終了コード {code}）")

    # 公開済み記事の見直し（記事が20本以上・今週の見直しが3本未満のときだけ。それ以外は refresh-next が「見直さない」と返す）
    r2 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "autopilot.py"), "refresh-next"],
                        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r2.returncode == 0:
        log("公開済み記事の見直しを1本行います")
        with open(os.path.join(ROOT, "logs", "auto-generate.log"), "a", encoding="utf-8") as out:
            rc = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "auto-generate.py"), "--refresh"],
                                cwd=ROOT, stdout=out, stderr=subprocess.STDOUT).returncode
        log(f"見直しが終了（終了コード {rc}）")
        code = code or rc
    else:
        log("見直しなし: " + (r2.stdout.strip().splitlines() or [""])[0])
    # note への展開（公開済みの記事を、別バージョンにして1日1本。ログインが切れていたら STATUS.md で再ログインを頼む）
    try:
        with open(os.path.join(ROOT, "logs", "note-run.log"), "a", encoding="utf-8") as out:
            rn = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "note-run.py")],
                                cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, timeout=40 * 60).returncode
        log(f"note への展開が終了（終了コード {rn}）")
    except subprocess.TimeoutExpired:
        log("note への展開が40分で終わらなかったため中断")
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "autopilot.py"), "status"], cwd=ROOT, capture_output=True)
    log("シャットダウンの判定へ")
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "power-guard.py"),
                    "--exit-code", str(code), "--task", "BlogFactory-Boot"], cwd=ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
