#!/usr/bin/env python3
"""
X の投稿係を GitHub Actions の待ち番から Cloudflare Workers（workers/x-poster）に切り替える（1回だけ。cloudflare-login.bat から動く）。

  1. Cloudflare にログイン（まだなら。ブラウザで「Allow」を押す・人の操作はここだけ）
  2. GitHub Actions の待ち番（x-post）を止める（Cloudflare と両方が動くと同じ投稿が2回出るため、設置より先に）
  3. 変更（待ち番の削除・投稿係の更新・テスト用の予定表）を GitHub に送る
  4. 投稿係を Cloudflare に設置し、キーを Cloudflare の秘密の設定に入れる（scripts/xpost.py deploy）
  5. テスト用の予定表で、本物の Cloudflare 上で X API に送る直前まで動かす（xpost.py dry-run）

結果は logs/x-cloudflare-setup.log。
"""
import datetime
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "x-cloudflare-setup.log"
WRANGLER = ["npx.cmd" if sys.platform == "win32" else "npx", "--yes", "wrangler@4.136.3"]
GH = str(Path(os.environ.get("LOCALAPPDATA", "")) / "gh-cli" / "bin" / "gh.exe")
REPO = "shibuta-a/blog-factory"
TEST_QUEUE = "data/x-queue/test-2026-10-17.json"


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd, cwd=ROOT, check=True):
    r = subprocess.run(cmd, cwd=cwd, text=True, encoding="utf-8", errors="replace", capture_output=True)
    out = (r.stdout or "") + (r.stderr or "")
    if check and r.returncode != 0:
        log(f"失敗: {' '.join(cmd[:4])}\n{out[-1500:]}")
        sys.exit(1)
    return r.returncode, out


def logged_in():
    _, out = run([*WRANGLER, "whoami"], cwd=ROOT / "workers" / "x-poster", check=False)
    return "not authenticated" not in out.lower()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    log("=== X の投稿係を Cloudflare に切り替え ===")
    if not logged_in():
        log("1. Cloudflare のログイン画面を開きます。ブラウザでログインして「Allow」を押してください")
        subprocess.run([*WRANGLER, "login"], cwd=ROOT / "workers" / "x-poster")
        if not logged_in():
            log("ログインできませんでした（時間切れ）。もう一度 cloudflare-login.bat を開いてください")
            return 1
    log("1. Cloudflare にログイン済み")

    _, out = run([GH, "run", "list", "-R", REPO, "-w", "x-post.yml", "-L", "30", "--json", "databaseId,status"], check=False)
    import json
    try:
        runs = [r["databaseId"] for r in json.loads(out[out.index("["):]) if r["status"] in ("in_progress", "queued", "waiting")]
    except ValueError:
        runs = []
    for rid in runs:
        run([GH, "run", "cancel", str(rid), "-R", REPO], check=False)
    run([GH, "workflow", "disable", "x-post.yml", "-R", REPO], check=False)
    log(f"2. GitHub の待ち番を止めました（止めた実行 {len(runs)} 件・定期実行も無効）")

    for p in (".github", "scripts/x-actions.py", "scripts/x-record.sh", "scripts/x-cloudflare-setup.py", ".gitignore",
              "workers/x-poster/worker.js", "README.md", TEST_QUEUE, "cloudflare-login.bat"):   # 1つずつ（ない名前が1つあると全部失敗するため）
        run(["git", "add", "-A", "--", p], check=False)
    run(["git", "commit", "-m", "X: 投稿は Cloudflare Workers の定期実行（5分おき）が主役・GitHub Actions の待ち番をやめる"], check=False)
    run(["git", "pull", "--rebase", "--autostash"])
    run(["git", "push"])
    log("3. 変更を GitHub に送りました")

    rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "xpost.py"), "deploy"], cwd=ROOT).returncode
    if rc != 0:
        log("4. 投稿係の設置に失敗しました（上の表示を見てください）")
        return 1
    run(["git", "add", "workers/x-poster/wrangler.toml"], check=False)
    run(["git", "commit", "-m", "X: 投稿係の KV の ID"], check=False)
    run(["git", "pull", "--rebase", "--autostash"])
    run(["git", "push"], check=False)
    log("4. 投稿係を Cloudflare に設置し、キーを秘密の設定に入れました")

    rc, out = run([sys.executable, str(ROOT / "scripts" / "xpost.py"), "dry-run", "--queue", TEST_QUEUE,
                   "--now", "2026-10-17T23:59"], check=False)
    log("5. 通し確認（本物の Cloudflare 上・X API に送る直前で止める）:\n" + out)
    rc2, st = run([sys.executable, str(ROOT / "scripts" / "xpost.py"), "status"], check=False)
    log("投稿係の状態:\n" + st)
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
