"""操作の記録係（常駐・画面なし）。

ログオン時にタスク IdleBeacon が起動する。20秒ごとに state/idle-beacon.json へ書く:
  session_start     … 今回の「起きている時間」の始まり（ログオン、または休止/スリープからの復帰）
  session_kind      … "logon" か "resume"
  input_since_boot  … 今回起きてから人の入力（マウス・キーボード）があったか。一度 true になったら次に起きるまで戻さない
  last_input_change … 最後に入力の変化を見た時刻
  written_at        … この記録を書いた時刻（古ければ power-guard.py は信用しない）

自分のユーザーで動かすこと（SYSTEM ではマウス・キーボードの入力が見えない）。
"""
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state", "idle-beacon.json")
LOG = os.path.join(ROOT, "logs", "idle-beacon.log")
INTERVAL = 20        # 秒
IGNORE_SEC = 3       # 起きた直後のこの秒数の入力は数えない（Windows が自分で入れるもの）
RESUME_GAP = 60      # 前回の確認からこれ以上空いていたら、休止/スリープから復帰したとみなす


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def last_input_tick():
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return info.dwTime


def idle_seconds(tick):
    if tick is None:
        return None
    return ((ctypes.windll.kernel32.GetTickCount() - tick) & 0xFFFFFFFF) / 1000


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def write_state(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def new_session(kind, start):
    log(f"新しいセッション: {kind}")
    return {"session_start": start, "session_kind": kind, "input_since_boot": False,
            "last_input_change": None, "pid": os.getpid()}


def main():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)

    # 二重起動しない（すでに新しい記録を書いている別の記録係がいれば終わる）
    try:
        with open(STATE, encoding="utf-8") as f:
            old = json.load(f)
        if old.get("pid") != os.getpid() and time.time() - old.get("written_at", 0) < INTERVAL * 2:
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, old.get("pid", 0))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return
    except (OSError, ValueError):
        pass

    s = new_session("logon", time.time())
    time.sleep(IGNORE_SEC)
    base = last_input_tick()
    last_loop = time.time()

    while True:
        now = time.time()
        tick = last_input_tick()

        if now - last_loop > RESUME_GAP:
            # 休止/スリープから復帰した。復帰の瞬間は分からないので、早めに見積もる（max_uptime の判定が厳しくなる側）
            s = new_session("resume", last_loop + INTERVAL)
            # GetTickCount は眠っている時間も数えるので、「最後の入力から何秒たったか」で復帰後の入力かどうか分かる
            # （休止する直前に人が「休止」を押した操作は、復帰後の入力に数えない）
            idle = idle_seconds(tick)
            if idle is None or idle < (now - s["session_start"]) - IGNORE_SEC:
                s["input_since_boot"] = True
                s["last_input_change"] = now
                log(f"復帰後に入力あり（最後の入力から {idle} 秒）→ 入力ありにする")
            base = tick
        elif tick is None:
            # 分からないときは安全のため入力あり
            if not s["input_since_boot"]:
                log("入力時刻を取得できない → 入力ありにする")
            s["input_since_boot"] = True
        elif tick != base:
            if not s["input_since_boot"]:
                log("人の入力を検知 → 今回は落とさない")
            s["input_since_boot"] = True
            s["last_input_change"] = now
            base = tick

        s["written_at"] = now
        try:
            write_state(s)
        except OSError as e:
            log(f"記録の書き込みに失敗: {e}")
        last_loop = now
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 常駐なので、落ちた理由だけは残す
        log(f"異常終了: {e!r}")
        sys.exit(1)
