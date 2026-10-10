"""作業が終わったあと、誰も触っていなければシャットダウン（mode=shutdown）／休止状態（mode=hibernate）にする。

  python scripts/power-guard.py --exit-code 0      … 作業の最後に呼ぶ（auto-generate.bat / random-schedule.ps1 から）
  python scripts/power-guard.py --dry-run          … 判定だけ表示して、落とさない
  python scripts/power-guard.py --cancel           … 猶予中の休止を取り消す（cancel-sleep.bat と同じ）

★一番大事な決まり: 起きてから一度でもマウスかキーボードを触ったら、絶対に落とさない。
落とすのは、次のどれにも当てはまらないときだけ（設定は data/power.json）:
  1. 設定で無効          2. 作業が最後まで終わっていない      3. 起きてから max_uptime_min を過ぎた
  4. 記録係の記録が無い・古い   5. 起きてから入力があった
  6. shutdown: BIOS の起動時刻（bios_wake_time ± boot_window_min）の起動ではない（人が電源を入れた）
     hibernate: 復帰した理由がタイマーではない（人が電源ボタンで起こした）、または分からない
  7. ブログ工場の別のタスクがまだ動いている      8. 取り消しの印がある
猶予（grace_sec）の間もマウス・キーボードを見ていて、触られたら取り消す。
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "data", "power.json")
BEACON = os.path.join(ROOT, "state", "idle-beacon.json")
CANCEL = os.path.join(ROOT, "state", "cancel-sleep")
LOG = os.path.join(ROOT, "logs", "power-guard.log")
BEACON_MAX_AGE = 120   # 秒。これより古い記録は信用しない
ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def last_input_tick():
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    return info.dwTime if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)) else None


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def powershell(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + cmd],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return r.stdout.strip()


def wake_source(since):
    """since 以降の「スリープ解除」イベント（Power-Troubleshooter ID 1）の解除元を返す。無ければ None。"""
    start = datetime.fromtimestamp(since - 300).strftime("%Y-%m-%dT%H:%M:%S")
    out = powershell(
        "$e = Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Power-Troubleshooter';"
        f"Id=1;StartTime=[datetime]'{start}'}} -MaxEvents 1 -ErrorAction SilentlyContinue;"
        "if ($e) { $x=[xml]$e.ToXml(); ($x.Event.EventData.Data | ? Name -eq 'WakeSourceText').'#text' + ' | type=' +"
        " ($x.Event.EventData.Data | ? Name -eq 'WakeSourceType').'#text' }")
    return out or None


def other_tasks_running():
    out = powershell("Get-ScheduledTask -TaskName 'BlogFactory-*' | ? State -eq 'Running' | % TaskName")
    return [t for t in out.splitlines() if t.strip()]


def reasons_not_to_sleep(cfg, exit_code, own_task):
    r = []
    if not cfg.get("enabled", False):
        r.append("設定で休止が無効")
    if exit_code != 0:
        r.append(f"作業が最後まで終わっていない（終了コード {exit_code}）")

    try:
        with open(BEACON, encoding="utf-8") as f:
            b = json.load(f)
    except (OSError, ValueError):
        return r + ["記録係の記録が無い（IdleBeacon が動いていない）"]

    now = time.time()
    if now - b.get("written_at", 0) > BEACON_MAX_AGE:
        r.append(f"記録係の記録が古い（{int(now - b.get('written_at', 0))}秒前）")
    if b.get("input_since_boot", True):
        r.append("起きてから人の入力があった")
    up = (now - b.get("session_start", 0)) / 60
    if up > cfg.get("max_uptime_min", 25):
        r.append(f"起きてから {up:.0f} 分たっている（上限 {cfg.get('max_uptime_min', 25)} 分）＝人が使っている起動とみなす")
    if cfg.get("mode") == "shutdown":
        # BIOS の時刻起動（bios_wake_time）で立ち上がったときだけ落とす。人が電源ボタンで入れた起動は落とさない
        if b.get("session_kind") != "logon":
            r.append("休止/スリープからの復帰（シャットダウンからの起動ではない）")
        boot = now - ctypes.windll.kernel32.GetTickCount64() / 1000
        h, m = map(int, cfg.get("bios_wake_time", "04:00").split(":"))
        wake = datetime.fromtimestamp(boot).replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
        diff = min(abs(boot - wake), abs(boot - (wake - 86400)), abs(boot - (wake + 86400))) / 60
        if diff > cfg.get("boot_window_min", 15):
            r.append(f"BIOSの起動時刻（{cfg.get('bios_wake_time')}）の起動ではない（{datetime.fromtimestamp(boot):%H:%M} に起動）＝人が電源を入れた")
    elif b.get("session_kind") == "resume":
        src = wake_source(b["session_start"])
        log(f"復帰の理由: {src}")
        if not src:
            r.append("復帰の理由が分からない")
        elif not any(k in src for k in cfg.get("timer_wake_keywords", [])):
            r.append(f"タイマー以外で復帰した（{src}）")
    if os.path.exists(CANCEL) and os.path.getmtime(CANCEL) >= b.get("session_start", 0):
        r.append("取り消しの印がある")
    others = [t for t in other_tasks_running() if t != own_task]
    if others:
        r.append(f"別のタスクが動いている（{', '.join(others)}）")
    return r


def grace(sec):
    """猶予の間、入力か取り消しがあれば False。"""
    base = last_input_tick()
    t0 = time.time()
    while time.time() - t0 < sec:
        time.sleep(2)
        tick = last_input_tick()
        if tick is None or tick != base:
            log("猶予中に入力あり → 休止を取り消し")
            return False
        if os.path.exists(CANCEL) and os.path.getmtime(CANCEL) >= t0:
            log("猶予中に取り消しの印 → 休止を取り消し")
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exit-code", type=int, default=0, help="作業の終了コード（0以外なら落とさない）")
    ap.add_argument("--task", default="", help="自分のタスク名（動作中チェックから除く）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cancel", action="store_true")
    a = ap.parse_args()

    if a.cancel:
        os.makedirs(os.path.dirname(CANCEL), exist_ok=True)
        with open(CANCEL, "w") as f:
            f.write(datetime.now().isoformat())
        log("取り消しの印を付けた（今回起きている間は休止しない）")
        return 0

    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)["shutdown"]

    r = reasons_not_to_sleep(cfg, a.exit_code, a.task)
    if r:
        log("落とさない: " + " / ".join(r))
        return 0
    if a.dry_run:
        log("（確認だけ）条件がそろっているので、本番なら休止します")
        return 0

    word = "シャットダウン" if cfg.get("mode") == "shutdown" else "休止状態に"
    log(f"誰も触っていない → {cfg.get('grace_sec', 60)}秒後に{word}します（マウスを動かせば取り消し）")
    if not grace(cfg.get("grace_sec", 60)):
        return 0
    log(f"{word}します")
    if cfg.get("mode") == "shutdown":
        subprocess.run(["shutdown", "/s", "/t", "0"])
    else:
        subprocess.run(["shutdown", "/h"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 判定で失敗したら、安全のため落とさない
        log(f"落とさない: 判定中にエラー {e!r}")
        sys.exit(0)
