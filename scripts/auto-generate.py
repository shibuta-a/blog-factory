#!/usr/bin/env python3
"""
毎日の自動運転を起動する（タスクスケジューラから auto-generate.bat 経由で呼ばれる）。

記事を書くのは Claude Code 自身。このスクリプトは Claude Code をヘッドレス（claude -p）で起動し、
scripts/daily-prompt.md の手順（承認の取り込み → ネタ選び → 広告確保 → 記事執筆 → 公開 → ネタ補充 → STATUS更新）を実行させる。
外部の Claude API（従量課金）は使わない。Claude Code の長期トークン（.secrets/claude-token.json・scripts/claude_env.py）があればそれで、なければログイン中のアカウントで動く。

  python scripts/auto-generate.py            … 2本書いて公開（既定）
  python scripts/auto-generate.py -n 1       … 1本だけ
  python scripts/auto-generate.py --print    … 実行せずに、渡す手順書を表示するだけ
  python scripts/auto-generate.py --refresh  … 新規ではなく、公開済み記事の見直し（scripts/refresh-prompt.md）を1回行う
  python scripts/auto-generate.py --research … 翌日の自動化記事の下調べ（道具の公式サイト確認 → data/jidouka-research/<回>.md・scripts/research-prompt.md）

★ 毎日の自動実行は、渋田さんが「毎日の自動投稿をオンにして」と言ったときだけ
  `powershell -ExecutionPolicy Bypass -File scripts\\schedule.ps1 on` で登録する（オフは off）。

ログ: logs/autopilot.log（実行ごとに「== 日時 ==」の見出しが付く。STATUS.md に直近5回が出る）
1日の上限（自動生成3本）は autopilot.py が守る。このスクリプトを何度動かしても、上限を超えては書かない。
"""
import argparse
import datetime
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_env import claude_env  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "scripts" / "daily-prompt.md"
REFRESH_PROMPT = ROOT / "scripts" / "refresh-prompt.md"
RESEARCH_PROMPT = ROOT / "scripts" / "research-prompt.md"
# 制限時間（分）。自動化記事は公式サイトを確かめてから書くので長めにとる（確認の手順は省かない）
TIMEOUT_MIN = {"new": 180, "refresh": 60, "research": 60}
LOG = ROOT / "logs" / "autopilot.log"

# 自動運転の Claude Code に許す道具（これ以外のコマンドは実行できない）
ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep",
    "WebSearch", "WebFetch",   # 自動化記事の道具の名前・料金・機能を公式サイトで確かめる・ランキングの補充
    "Bash(python scripts/autopilot.py:*)",
    "Bash(python scripts/publish.py:*)",
    "Bash(python scripts/images.py:*)",
]


def find_claude():
    for c in (shutil.which("claude"), Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if c and Path(c).exists():
            return str(c)
    return None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=2, help="書く記事の本数（1〜3。1日の上限は autopilot.py が別に守る）")
    ap.add_argument("--print", action="store_true", help="手順書を表示するだけ")
    ap.add_argument("--refresh", action="store_true", help="公開済み記事の見直しを1回行う")
    ap.add_argument("--research", action="store_true", help="翌日の自動化記事の下調べを行う")
    args = ap.parse_args()
    n = max(1, min(args.n, 3))
    mode = "research" if args.research else "refresh" if args.refresh else "new"
    prompt = {"new": PROMPT, "refresh": REFRESH_PROMPT, "research": RESEARCH_PROMPT}[mode].read_text(encoding="utf-8").replace("{N}", str(n))
    label = {"new": f"{n}本", "refresh": "記事の見直し", "research": "自動化記事の下調べ"}[mode]
    tools = (["Read", "Write", "WebSearch", "WebFetch", "Bash(python scripts/autopilot.py:*)"] if mode == "research"
             else ALLOWED_TOOLS)
    if args.print:
        print(prompt)
        return 0

    claude = find_claude()
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n== {datetime.datetime.now():%Y-%m-%d %H:%M} 自動実行 開始（{label}） ==\n")
        if not claude:
            log.write("Claude Code（claude コマンド）が見つかりません。\n")
            return 1
        cmd = [claude, "-p", prompt, "--permission-mode", "acceptEdits", "--allowedTools", *tools]
        try:
            r = subprocess.run(cmd, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                               capture_output=True, timeout=TIMEOUT_MIN[mode] * 60, env=claude_env())
            log.write(r.stdout)
            if r.stderr.strip():
                log.write("\n[stderr]\n" + r.stderr[-3000:])
            code = r.returncode
        except subprocess.TimeoutExpired:
            log.write(f"{TIMEOUT_MIN[mode]}分で終わらなかったため中断しました。\n")
            code = 1
        log.write(f"\n== {datetime.datetime.now():%Y-%m-%d %H:%M} 自動実行 終了（終了コード {code}） ==\n")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "autopilot.py"), "status"], cwd=ROOT,
                   capture_output=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
