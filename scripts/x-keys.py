#!/usr/bin/env python3
"""
X API のキー（4つ）を受け取り、確かめて、Cloudflare の投稿係（workers/x-poster）に渡す。人の操作は最初の1回、窓に4つ貼るだけ。

  python scripts/x-keys.py           … 窓を出す → .secrets/x-api.json に保存 → GET /2/users/me で @maf_media_jp か確かめる（投稿しない）
                                        → Cloudflare の秘密の設定に入れる（scripts/xpost.py deploy）→ プロフィールを API で設定してみる
  python scripts/x-keys.py check     … 保存済みのキーで認証だけ確かめる
  python scripts/x-keys.py secrets   … 保存済みのキーを Cloudflare の秘密の設定に入れ直す（Worker の更新も行う）
  python scripts/x-keys.py profile   … プロフィール（名前・自己紹介・ウェブサイト・アイコン・ヘッダー）を API で設定してみる

キーの値は画面（●●）・ログ・チャットに出さない。結果は logs/x-setup.log と state/x-api-setup.json。
"""
import base64
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import social  # noqa: E402

ROOT = social.ROOT
LOG = ROOT / "logs" / "x-setup.log"
STATE = ROOT / "state" / "x-api-setup.json"
ACCOUNT = "maf_media_jp"
LABELS = {"api_key": "API Key", "api_secret": "API Key Secret",
          "access_token": "Access Token", "access_token_secret": "Access Token Secret"}
# プロフィールの中身（x-build-task「2. プロフィールの設定」と同じ）
PROFILE = {
    "name": "maf｜その作業、システムにできます",
    "description": ("現場の手作業を、AIで仕組みに変えています。点検カルテ、飲食店のレジ、見積書など。"
                    "飲食店を経営していた側の目線で作ります。「この作業、自動化できる？」はDMへどうぞ。"),
    "url": "https://maf-group.jp/act/",
}
ICON = ROOT / "data" / "x" / "x-icon.png"
HEADER = ROOT / "data" / "x" / "x-header.png"


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def save_state(**kw):
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st.update(kw, at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def ask_keys():
    """入力欄4つの小さい窓。値は ● で隠す。保存を押すまで待つ。"""
    import tkinter as tk
    root = tk.Tk()
    root.title("X API のキー（1回だけ）")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, text="X の Developer Console のキーを4つ貼ってください（画面には●で表示されます）",
             padx=12, pady=8).grid(row=0, column=0, columnspan=2, sticky="w")
    entries, result = {}, {}
    for i, (k, label) in enumerate(LABELS.items(), start=1):
        tk.Label(root, text=label, padx=12).grid(row=i, column=0, sticky="e")
        e = tk.Entry(root, show="●", width=48)
        e.grid(row=i, column=1, padx=(0, 12), pady=3)
        entries[k] = e
    msg = tk.Label(root, text="", fg="#c00", padx=12)
    msg.grid(row=6, column=0, columnspan=2, sticky="w")

    def ok(*_):
        vals = {k: e.get().strip() for k, e in entries.items()}
        if not all(vals.values()):
            msg.config(text="4つとも入れてください")
            return
        result.update(vals)
        root.destroy()
    tk.Button(root, text="保存する", width=14, command=ok).grid(row=5, column=1, sticky="e", padx=12, pady=8)
    root.bind("<Return>", ok)
    entries["api_key"].focus_set()
    root.mainloop()
    return result or None


def check(keys):
    code, data = social.x_request("GET", "https://api.x.com/2/users/me", keys)
    if code == 200:
        u = data["data"]
        ok = u["username"].lower() == ACCOUNT
        log(f"認証OK: @{u['username']}（{u['name']}）" + ("" if ok else f" ← @{ACCOUNT} ではありません"))
        save_state(auth_ok=ok, username=u["username"], user_id=u["id"])
        return ok
    log(f"認証NG（{code}）: {json.dumps(data, ensure_ascii=False)[:400]}")
    save_state(auth_ok=False, auth_error=f"{code} {json.dumps(data, ensure_ascii=False)[:200]}")
    return False


def profile(keys):
    """v1.1 のプロフィール API を試す。できなかったものは理由を残す（ブラウザ操作で代わりにやることはしない）。"""
    results = {}

    def call(name, path, form):
        for host in ("https://api.x.com", "https://api.twitter.com"):
            code, data = social.x_request("POST", f"{host}/1.1/account/{path}", keys, form=form)
            if code != 404:
                break
        ok = code in (200, 201, 202, 204)
        results[name] = "OK" if ok else f"NG（{code} {json.dumps(data, ensure_ascii=False)[:160]}）"
        log(f"プロフィール {name}: {results[name]}")
    call("名前・自己紹介・ウェブサイト", "update_profile.json", dict(PROFILE))
    call("アイコン", "update_profile_image.json", {"image": base64.b64encode(ICON.read_bytes()).decode()})
    call("ヘッダー", "update_profile_banner.json", {"banner": base64.b64encode(HEADER.read_bytes()).decode()})
    results["固定投稿"] = "NG（X API に投稿を固定する機能がない）"
    log("プロフィール 固定投稿: X API には投稿を固定する機能がないため行いません（ブラウザ操作もしません）")
    save_state(profile=results)
    return results


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "setup"
    if cmd == "setup":
        keys = ask_keys()
        if not keys:
            log("窓が閉じられたため、キーは保存していません")
            return 1
        social.X_KEYS.parent.mkdir(exist_ok=True)
        social.X_KEYS.write_text(json.dumps(keys, indent=1) + "\n", encoding="utf-8")
        old = social.X_KEYS.parent / "x_api.json"   # 以前のひな形（中身なし）
        if old.exists():
            old.unlink()
        log(f"キーを保存しました（{social.X_KEYS.relative_to(ROOT)}・GitHub には上げません）")
    keys = social.load_x_keys()
    if not keys:
        log("保存済みのキーがありません。python scripts/x-keys.py で窓を出してください")
        return 1
    if cmd == "profile":
        profile(keys)
        return 0
    if cmd == "secrets":
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "xpost.py"), "deploy"]).returncode
    if not check(keys):
        return 1
    if cmd == "check":
        return 0
    subprocess.run([sys.executable, str(ROOT / "scripts" / "xpost.py"), "deploy"])
    profile(keys)
    return 0


if __name__ == "__main__":
    sys.exit(main())
