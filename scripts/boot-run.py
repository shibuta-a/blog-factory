"""起動したら記事を書いて公開し、誰も触っていなければシャットダウンする（タスク BlogFactory-Boot から呼ばれる）。

きっかけは2つ（どちらで動いても1日1回だけ）:
  ・ログオン時（BIOS の時刻起動 → 自動ログオン）
  ・毎日の bios_wake_time の数分後（その時刻に PC がすでに点いていた日の保険。このときは起動から時間がたっているので落とさない）

本数は 1本15% / 2本70% / 3本15%（1日の上限3本は autopilot.py が別に守る）。
書いた記事は publish.py が公開予定時刻（24時間以内のランダム）を付けて GitHub へ送り、GitHub Actions が時刻どおりに公開する。
新規記事のあと、公開済み記事の見直し（scripts/refresh-prompt.md）を1本行う（記事が20本以上・今週3本まで）。
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
    log(f"記事を {n} 本書きます")
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
    log("シャットダウンの判定へ")
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "power-guard.py"),
                    "--exit-code", str(code), "--task", "BlogFactory-Boot"], cwd=ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
