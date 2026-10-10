#!/usr/bin/env python3
"""
記事を世界に公開する1コマンド。

  python scripts/publish.py                      # content/ の今の状態でビルド → git push
  python scripts/publish.py 記事.md               # 記事.md を content/ja/ にコピーしてから公開
  python scripts/publish.py 記事.md --lang en     # 英語版として content/en/ に置いて公開

流れ: (記事を content/<言語>/ に保存) → publish_schedule.py（新しい自動記事に公開予定時刻）→ images.py（写真）→ build.py
       → STATUS.md 更新 → git add/commit → git pull --rebase（GitHub 側の変更を取り込む）→ git push
       → GitHub に届くと Cloudflare Pages が自動でサイトを更新（1〜2分）
"""
import argparse
import datetime
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd, check=True):
    print("$", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, text=True, encoding="utf-8", capture_output=True)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode != 0 and check:
        print(r.stderr.strip(), file=sys.stderr)
        sys.exit(r.returncode)
    return r


def undo_date_only_updates():
    """公開済み記事で、本文もタイトル等も同じなのに updated（更新日）だけ変わっていたら元に戻す。
    中身を変えずに日付だけ新しくするのは Google にスパム扱いされるため（実質的な改善の判定は autopilot.py refresh-done）。"""
    r = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", "content"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    for rel in r.stdout.split():
        path = ROOT / rel
        old = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT, capture_output=True)
        if old.returncode != 0 or not path.exists():
            continue
        strip = lambda b: re.sub(r"^updated\s*:.*\n?", "", b.decode("utf-8", "replace").replace("\r\n", "\n"), flags=re.M)
        new = path.read_bytes()
        if new != old.stdout and strip(new) == strip(old.stdout):
            path.write_bytes(old.stdout)   # 送信済みの内容そのもの（改行コードも）に戻す
            print(f"（{rel}: 中身が同じで更新日だけ変わっていたので、元に戻しました）")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="記事を保存→ビルド→git push")
    ap.add_argument("files", nargs="*", help="公開するMarkdownファイル（省略時は content/ をそのまま公開）")
    ap.add_argument("--lang", default="ja", help="言語コード（ja / en / zh）")
    ap.add_argument("-m", "--message", help="コミットメッセージ")
    args = ap.parse_args()

    added = []
    dest_dir = ROOT / "content" / args.lang
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in args.files:
        src = Path(f).resolve()
        if not src.exists():
            sys.exit(f"ファイルが見つかりません: {src}")
        dest = dest_dir / src.name
        if src != dest:
            shutil.copy(src, dest)
        added.append(dest.relative_to(ROOT).as_posix())

    undo_date_only_updates()

    # 新しい自動生成の記事に、公開予定時刻（今から24時間以内のランダムな時刻）を付ける。手で渡した記事はすぐ公開
    run([sys.executable, str(ROOT / "scripts" / "publish_schedule.py")])

    # 写真がまだない記事に、著作権フリーの写真を自動で用意する（失敗しても公開は続ける）
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "images.py")], cwd=ROOT, text=True, encoding="utf-8")
    if r.returncode != 0:
        print("（写真の用意をスキップしました。記事は写真なしで公開されます）")

    run([sys.executable, str(ROOT / "scripts" / "build.py")])
    subprocess.run([sys.executable, str(ROOT / "scripts" / "autopilot.py"), "status"], cwd=ROOT, capture_output=True)  # STATUS.md

    run(["git", "add", "-A"])
    if run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        print("変更なし。公開済みの状態と同じです。")
        return
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = args.message or (f"publish: {', '.join(added)}" if added else f"publish: update {stamp}")
    run(["git", "commit", "-m", msg])
    # GitHub 側に別の変更があっても衝突しないよう、先に取り込んでから送る
    for attempt in range(3):
        run(["git", "pull", "--rebase", "--autostash"])
        if run(["git", "push"], check=attempt == 2).returncode == 0:
            break
    print("公開しました。1〜2分でサイトに反映されます。")


if __name__ == "__main__":
    main()
