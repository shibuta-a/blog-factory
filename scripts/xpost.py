#!/usr/bin/env python3
"""
★ 2026-10-15 中止: X API は使わない（data/social.json の x.enabled: false の間、このスクリプトは何もしない）。
X への自動ポスト（記事の公開と同時に、紹介ポスト＋リンクを X に出す）。

仕組み: 記事の公開予定時刻には PC が止まっていることが多いので、ポストはクラウド（Cloudflare Workers）で行う。
  build.py → public/_x_queue.json（記事ごとのポスト文と公開時刻）→ GitHub
  Cloudflare の workers/x-poster が10分ごとに読み、公開時刻が来た記事を1件ずつポスト（1日 max_per_day 件まで）

  python scripts/xpost.py keys       … .secrets/x_api.json のひな形を作る（渋田さんがキー4つを貼る）
  python scripts/xpost.py check      … キーが正しいか（どのアカウントでポストするか）を確かめる
  python scripts/xpost.py preview    … これからポストする予定の文面と文字数を表示
  python scripts/xpost.py test       … 実際に1件ポストして確かめる（--text "文面" で文面を指定）
  python scripts/xpost.py deploy     … Cloudflare にポスト係を設置・更新し、キーを Cloudflare の秘密の設定に入れる
  python scripts/xpost.py status     … ポスト係の状態（最後の実行・ポスト済み・エラー）

設定は data/social.json の x（enabled / since / with_link / max_per_day）。記事ごとに出したくなければフロントマターに x: none。
ポスト文はフロントマターの x_text（自動執筆の Claude Code が書く）＋ジャンルのハッシュタグ＋記事URL。
"""
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import social  # noqa: E402

ROOT = social.ROOT
WORKER_DIR = ROOT / "workers" / "x-poster"
WRANGLER = ["npx.cmd" if sys.platform == "win32" else "npx", "--yes", "wrangler@4.136.3"]
SECRET_NAMES = {"api_key": "X_API_KEY", "api_secret": "X_API_SECRET",
                "access_token": "X_ACCESS_TOKEN", "access_token_secret": "X_ACCESS_SECRET"}


def keys_or_exit():
    keys = social.load_x_keys()
    if not keys:
        sys.exit("X のキーがまだありません。.secrets/x_api.json に4つのキーを入れてください（python scripts/xpost.py keys でひな形）")
    return keys


def cmd_keys():
    if social.X_KEYS.exists():
        print(f"もうあります: {social.X_KEYS}")
        return 0
    social.X_KEYS.parent.mkdir(exist_ok=True)
    social.X_KEYS.write_text(json.dumps({k: "ここに貼る" for k in social.X_KEY_NAMES}, indent=2) + "\n", encoding="utf-8")
    print(f"ひな形を作りました: {social.X_KEYS}")
    return 0


def cmd_check():
    code, data = social.x_request("GET", "https://api.x.com/2/users/me", keys_or_exit())
    if code == 200:
        print(f"OK: @{data['data']['username']}（{data['data']['name']}）としてポストします")
        return 0
    print(f"NG（{code}）: {json.dumps(data, ensure_ascii=False)[:500]}")
    return 1


def cmd_preview():
    q = json.loads((ROOT / "public" / "_x_queue.json").read_text(encoding="utf-8"))
    print(f"自動ポスト: {'オン' if q['enabled'] else 'オフ'} / {q['since']} 以降に公開の記事 / 1日 {q['max_per_day']} 件まで")
    for it in q["items"]:
        print(f"\n--- {it['at'].replace('T', ' ')}  {it['slug']}（{social.x_weight(it['text'])}/280）\n{it['text']}")
    if not q["items"]:
        print("（予定なし）")
    return 0


def cmd_test(text=None):
    keys = keys_or_exit()
    cfg = social.load_config()
    if not text:
        text = ("MAFブログの公式アカウントです。\n忙しい人・家にいる人の「暮らしとお金」を、守って便利にする情報を毎日お届けしています。"
                "\n\n#暮らしの知恵 #時短\n" + json.loads((ROOT / "site.json").read_text(encoding="utf-8"))["base_url"])
    print(f"ポストします（{social.x_weight(text)}/280）:\n{text}\n")
    code, data = social.post_tweet(text, keys)
    if code == 201:
        print(f"ポストしました: https://x.com/i/web/status/{data['data']['id']}")
        return 0
    print(f"失敗（{code}）: {json.dumps(data, ensure_ascii=False)[:800]}")
    if code == 402 or "credit" in json.dumps(data).lower():
        print("→ X API のクレジット（前払い残高）が足りません。console.x.com の Billing で確認してください。")
    if code == 403 and cfg:
        print("→ アプリの権限が「Read and write」になっていないか、権限を変えた後に Access Token を作り直していない可能性があります。")
    return 1


def wrangler(*args, input_text=None, check=True):
    r = subprocess.run([*WRANGLER, *args], cwd=WORKER_DIR, input=input_text, text=True, encoding="utf-8",
                       errors="replace", capture_output=True)
    out = (r.stdout or "") + (r.stderr or "")
    if check and r.returncode != 0:
        sys.exit(f"wrangler {' '.join(args)} が失敗しました:\n{out[-1500:]}")
    return r.returncode, out


def cmd_deploy():
    keys = keys_or_exit()
    code, out = wrangler("whoami", check=False)
    if "not authenticated" in out.lower():
        print("Cloudflare にログインします。ブラウザが開いたら「Allow（許可）」を押してください。")
        subprocess.run([*WRANGLER, "login"], cwd=WORKER_DIR)
    toml = WORKER_DIR / "wrangler.toml"
    text = toml.read_text(encoding="utf-8")
    if 'id = "KV_ID"' in text:
        _, out = wrangler("kv", "namespace", "list")
        found = [n["id"] for n in json.loads(out[out.index("["):out.rindex("]") + 1]) if n["title"].endswith("x-poster-posted")]
        if not found:
            _, out = wrangler("kv", "namespace", "create", "x-poster-posted")
            found = re.findall(r'id\s*=\s*"([0-9a-f]{32})"|"id":\s*"([0-9a-f]{32})"', out)
            found = [a or b for a, b in found]
        if not found:
            sys.exit("KV の作成結果が読めませんでした:\n" + out[-800:])
        toml.write_text(text.replace('id = "KV_ID"', f'id = "{found[0]}"'), encoding="utf-8", newline="\n")
        print(f"KV（ポスト済みの記録）: {found[0]}")
    _, out = wrangler("deploy")
    m = re.search(r"https://[\w.-]+\.workers\.dev", out)
    for k, name in SECRET_NAMES.items():
        wrangler("secret", "put", name, input_text=keys[k])
    print("X のキーを Cloudflare の秘密の設定に入れました（GitHub には上げていません）")
    if m:
        cfg_text = social.CONFIG.read_text(encoding="utf-8")
        cfg = json.loads(cfg_text)
        cfg["x"]["worker_url"] = m.group(0)
        social.CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"ポスト係を設置しました: {m.group(0)}（10分ごとに動きます）")
    return 0


def cmd_status():
    url = social.load_config()["x"].get("worker_url")
    if not url:
        print("ポスト係はまだ設置されていません（python scripts/xpost.py deploy）")
        return 1
    with urllib.request.urlopen(url + "/status", timeout=20) as r:
        st = json.loads(r.read().decode("utf-8"))
    print(f"最後の実行: {st.get('last')}")
    print(f"ポスト済み: {len(st.get('posted', {}))} 件")
    for line in st.get("log", [])[-10:]:
        print("  " + line)
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "preview"
    if not social.load_config().get("x", {}).get("enabled"):
        print("X API への自動ポストは中止しました（2026-10-15・data/social.json の x.enabled: false）。"
              "note の X 連携では記事が自動で X に出ないことを 2026-10-15 に確認。X の投稿方式は未定。")
        return 0
    if cmd == "test":
        text = sys.argv[sys.argv.index("--text") + 1] if "--text" in sys.argv else None
        return cmd_test(text)
    return {"keys": cmd_keys, "check": cmd_check, "preview": cmd_preview,
            "deploy": cmd_deploy, "status": cmd_status}.get(cmd, lambda: sys.exit(__doc__))()


if __name__ == "__main__":
    sys.exit(main())
