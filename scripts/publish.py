#!/usr/bin/env python3
"""
記事を世界に公開する1コマンド。

  python scripts/publish.py                      # content/ の今の状態でビルド → git push
  python scripts/publish.py 記事.md               # 記事.md を content/ja/ にコピーしてから公開
  python scripts/publish.py 記事.md --lang en     # 英語版として content/en/ に置いて公開

流れ: (記事を content/<言語>/ に保存) → images.py（写真）→ build.py → git add/commit/push
       → GitHub に届くと Cloudflare Pages が自動でサイトを更新（1〜2分）
"""
import argparse
import datetime
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

    # 写真がまだない記事に、著作権フリーの写真を自動で用意する（失敗しても公開は続ける）
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "images.py")], cwd=ROOT, text=True, encoding="utf-8")
    if r.returncode != 0:
        print("（写真の用意をスキップしました。記事は写真なしで公開されます）")

    run([sys.executable, str(ROOT / "scripts" / "build.py")])

    run(["git", "add", "-A"])
    if run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        print("変更なし。公開済みの状態と同じです。")
        return
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = args.message or (f"publish: {', '.join(added)}" if added else f"publish: update {stamp}")
    run(["git", "commit", "-m", msg])
    run(["git", "push"])
    print("公開しました。1〜2分でサイトに反映されます。")


if __name__ == "__main__":
    main()
