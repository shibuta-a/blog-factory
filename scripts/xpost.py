#!/usr/bin/env python3
"""
X の投稿係（Cloudflare Workers の workers/x-poster・5分おきの定期実行）の設置と確認。2026-10-16 から X の投稿はこれが主役。

仕組み:
  朝の scripts/x-plan.py → data/x-queue/<日付>.json（と img/）→ GitHub（公開リポジトリ。Cloudflare は raw.githubusercontent.com から読む）
  Cloudflare の x-poster が5分おきに今日の予定表を見て、時刻が来た投稿を X API で出す（送る前に KV に「送信中」の印 → 二重投稿しない）

  python scripts/xpost.py deploy     … Worker を設置・更新し、KV を用意し、キー（.secrets/x-api.json）と ADMIN_TOKEN を Cloudflare の秘密の設定に入れる
  python scripts/xpost.py status     … 最後の実行・今月の件数と概算費用・クレジット切れ・最近の記録
  python scripts/xpost.py dry-run [--queue data/x-queue/xxx.json] [--now 2026-10-17T23:59]
                                     … 本物の Cloudflare 上で、X API に送る直前まで動かす（認証の確認だけ行う）
  python scripts/xpost.py sync       … Cloudflare の投稿の記録を data/x.json に写す（月ごとの件数と概算費用。朝の x-plan.py が毎回行う）
  python scripts/xpost.py check      … .secrets/x-api.json のキーで認証だけ確かめる（投稿しない）

Worker の URL と ADMIN_TOKEN は .secrets/x-worker.json（GitHub には上げない）。
"""
import json
import re
import secrets
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import social  # noqa: E402

ROOT = social.ROOT
WORKER_DIR = ROOT / "workers" / "x-poster"
WORKER_CFG = ROOT / ".secrets" / "x-worker.json"
WRANGLER = ["npx.cmd" if sys.platform == "win32" else "npx", "--yes", "wrangler@4.136.3"]
SECRET_NAMES = social.X_ENV_NAMES   # api_key → X_API_KEY など


def worker_cfg():
    try:
        return json.loads(WORKER_CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def wrangler(*args, input_text=None, check=True):
    r = subprocess.run([*WRANGLER, *args], cwd=WORKER_DIR, input=input_text, text=True, encoding="utf-8",
                       errors="replace", capture_output=True)
    out = (r.stdout or "") + (r.stderr or "")
    if check and r.returncode != 0:
        sys.exit(f"wrangler {' '.join(args)} が失敗しました:\n{out[-1500:]}")
    return r.returncode, out


def cmd_deploy():
    keys = social.load_x_keys()
    if not keys:
        sys.exit("X のキーがありません（python scripts/x-keys.py）")
    _, out = wrangler("whoami", check=False)
    if "not authenticated" in out.lower():
        sys.exit("Cloudflare にログインしていません（npx wrangler login）")
    toml = WORKER_DIR / "wrangler.toml"
    text = toml.read_text(encoding="utf-8")
    if 'id = "KV_ID"' in text:
        _, out = wrangler("kv", "namespace", "list")
        found = [n["id"] for n in json.loads(out[out.index("["):out.rindex("]") + 1]) if n["title"].endswith("x-poster-posted")]
        if not found:
            _, out = wrangler("kv", "namespace", "create", "x-poster-posted")
            found = [a or b for a, b in re.findall(r'id\s*=\s*"([0-9a-f]{32})"|"id":\s*"([0-9a-f]{32})"', out)]
        if not found:
            sys.exit("KV の作成結果が読めませんでした:\n" + out[-800:])
        toml.write_text(text.replace('id = "KV_ID"', f'id = "{found[0]}"'), encoding="utf-8", newline="\n")
        print(f"KV（投稿済みの印・記録）: {found[0]}")
    seed_kv(re.search(r'id = "([0-9a-f]{32})"', toml.read_text(encoding="utf-8")).group(1))
    _, out = wrangler("deploy")
    m = re.search(r"https://[\w.-]+\.workers\.dev", out)
    cfg = worker_cfg()
    if m:
        cfg["url"] = m.group(0)
    cfg.setdefault("admin_token", secrets.token_urlsafe(32))
    WORKER_CFG.write_text(json.dumps(cfg, indent=1) + "\n", encoding="utf-8")
    for k, name in SECRET_NAMES.items():
        wrangler("secret", "put", name, input_text=keys[k])
    wrangler("secret", "put", "ADMIN_TOKEN", input_text=cfg["admin_token"])
    print(f"投稿係を設置しました: {cfg.get('url')}（5分おき）。キーは Cloudflare の秘密の設定に入れました")
    return 0


def seed_kv(kv_id):
    """定期実行が動き出す前に、これまでの投稿（data/x.json）を KV に入れる（同じ投稿を二度出さないため）。KV に記録があれば何もしない。"""
    rc, out = wrangler("kv", "key", "get", "items", "--namespace-id", kv_id, "--remote", check=False)
    if rc == 0 and out.strip().startswith("{") and "posted" in out:
        return
    xl = json.loads(social.X_LOG.read_text(encoding="utf-8")) if social.X_LOG.exists() else {}
    posts = xl.get("posts", [])
    items = {p["id"]: {"state": "posted", "at": p["posted_at"], "tweet_id": p.get("tweet_id")} for p in posts}
    vals = {"items": json.dumps(items, ensure_ascii=False), "posts": json.dumps(posts, ensure_ascii=False)}
    if posts:
        vals["last_posted_at"] = max(p["posted_at"] for p in posts)
    tmp = ROOT / "state" / "kv-seed.tmp"
    tmp.parent.mkdir(exist_ok=True)
    for k, v in vals.items():   # 値はファイルで渡す（コマンドラインだと引用符・改行が壊れる）
        tmp.write_text(v, encoding="utf-8", newline="\n")
        wrangler("kv", "key", "put", k, "--path", str(tmp), "--namespace-id", kv_id, "--remote")
    tmp.unlink()
    print(f"これまでの投稿 {len(posts)} 件を KV に入れました（二度出さないため）")


def get(path, method="GET", data=None):
    cfg = worker_cfg()
    if not cfg.get("url"):
        sys.exit("投稿係がまだ設置されていません（python scripts/xpost.py deploy）")
    req = urllib.request.Request(cfg["url"] + path, method=method, data=data,
                                 headers={"Authorization": f"Bearer {cfg.get('admin_token', '')}", "User-Agent": "blog-factory"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def cmd_status():
    st = json.loads(get("/status"))
    print(f"最後の実行: {st.get('last')}")
    print(f"今月（{st['month']}）: {st['month_posts']} 件・概算 ${st['month_cost']}" + ("・**クレジット切れ**" if st["credit"].get("depleted") else ""))
    for line in st.get("log", [])[-10:]:
        print("  " + line)
    return 0


def cmd_dry_run(args):
    qs = "&".join(f"{k}={urllib.request.quote(args[args.index('--' + k) + 1])}" for k in ("queue", "now") if "--" + k in args)
    print(get("/dry-run" + ("?" + qs if qs else ""), method="POST", data=b""), end="")
    return 0


def sync():
    """Cloudflare の記録 → data/x.json（posts・months・credit・failed）。取れなければ False（data/x.json はそのまま）。"""
    try:
        lg = json.loads(get("/log"))
    except Exception as e:
        print(f"Cloudflare の記録を取れませんでした（{type(e).__name__}）")
        return False
    old = json.loads(social.X_LOG.read_text(encoding="utf-8")) if social.X_LOG.exists() else {}
    posts = {p["id"]: p for p in old.get("posts", [])}          # GitHub Actions 時代の記録（10/16 の固定用など）も残す
    posts.update({p["id"]: p for p in lg.get("posts", [])})
    months = {}
    for p in posts.values():
        m = months.setdefault(p["posted_at"][:7], {"posts": 0, "link_posts": 0, "cost": 0.0})
        m["posts"] += 1
        m["link_posts"] += 1 if p.get("kind") == "link" else 0
        m["cost"] = round(m["cost"] + float(p.get("cost", 0)), 4)
    new = {"_about": "X API の投稿の記録（Cloudflare の x-poster の記録を scripts/xpost.py sync が写したもの）。cost は概算（data/social.json の x_api の単価）",
           "posts": sorted(posts.values(), key=lambda p: p["posted_at"]), "months": months,
           "credit": lg.get("credit") or {}, "failed": lg.get("failed") or {}}
    if old.get("replies"):
        new["replies"] = old["replies"]          # リプライの確認（social.x_check_replies）の記録は残す
    social.X_LOG.write_text(json.dumps(new, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return True


def cmd_check():
    code, data = social.x_request("GET", "https://api.x.com/2/users/me", social.load_x_keys())
    print(f"OK: @{data['data']['username']}" if code == 200 else f"NG（{code}）: {json.dumps(data, ensure_ascii=False)[:300]}")
    return 0 if code == 200 else 1


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    cmd = a[0] if a else "status"
    if cmd == "deploy":
        return cmd_deploy()
    if cmd == "status":
        return cmd_status()
    if cmd == "dry-run":
        return cmd_dry_run(a)
    if cmd == "sync":
        return 0 if sync() else 1
    if cmd == "check":
        return cmd_check()
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
