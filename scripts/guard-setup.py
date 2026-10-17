#!/usr/bin/env python3
"""
見張り番・通知・バックアップの最初の設定（人の操作は、この窓の1回だけ）。

  python scripts/guard-setup.py      … 窓を出す（guard-setup.bat をダブルクリックでも同じ）

窓で入れるもの:
  1. 通知メールの送信元（maf-group.jp のメールアカウント。PC① の営業メールと同じもの）
     → 保存の前に、送信サーバーにログインできるか確かめる（メールは送らない）→ .secrets/mail.json
  2. バックアップの合言葉（2回）→ 鍵の組を作る（合言葉そのものはどこにも保存しない。紙に控える）
  3. Search Console の鍵（サービスアカウントの JSON ファイル・任意。あとからでも入れられる）→ .secrets/gsc-service-account.json
窓を閉じたあと、Windows Update の設定（scripts/winupdate.ps1）を管理者で1回だけ動かす（UAC の「はい」を1回）。
結果（値は出さない）は state/guard-setup.json と logs/guard-setup.log。
"""
import json
import shutil
import smtplib
import ssl
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_backup  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAIL = ROOT / ".secrets" / "mail.json"
GSC = ROOT / ".secrets" / "gsc-service-account.json"
STATE = ROOT / "state" / "guard-setup.json"
LOG = ROOT / "logs" / "guard-setup.log"
TO = "asoutobata@gmail.com"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def smtp_login(host, port, user, password):
    """送信サーバーにログインだけしてみる。だめなら理由の文字列、OK なら空文字。"""
    try:
        port = int(port)
        ctx = ssl.create_default_context()
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
        else:
            s = smtplib.SMTP(host, port, timeout=20)
            s.starttls(context=ctx)
        s.login(user, password)
        s.quit()
        return ""
    except smtplib.SMTPAuthenticationError:
        return "アドレスかパスワードが違うようです（送信サーバーがログインを拒否しました）"
    except Exception as e:
        return f"送信サーバーにつながりません（{type(e).__name__}: {str(e)[:80]}）"


def window():
    import tkinter as tk
    from tkinter import filedialog
    old = {}
    try:
        old = json.loads(MAIL.read_text(encoding="utf-8"))
    except Exception:
        pass
    root = tk.Tk()
    root.title("見張り番・通知・バックアップの設定（1回だけ）")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    pad = {"padx": 12, "pady": 3}
    r = 0

    def head(text):
        nonlocal r
        tk.Label(root, text=text, font=("Yu Gothic UI", 10, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 2))
        r += 1

    def note(text):
        nonlocal r
        tk.Label(root, text=text, fg="#555", justify="left").grid(row=r, column=0, columnspan=3, sticky="w", padx=12)
        r += 1

    def field(label, value="", show=None, width=40):
        nonlocal r
        tk.Label(root, text=label).grid(row=r, column=0, sticky="e", **pad)
        e = tk.Entry(root, width=width, show=show or "")
        e.insert(0, value)
        e.grid(row=r, column=1, columnspan=2, sticky="w", **pad)
        r += 1
        return e

    head("1. 通知メールの送信元（maf-group.jp のメールアカウント）")
    note(f"PC① の営業メールで使っている送信元と同じでかまいません。宛先は {TO} です。")
    e_user = field("メールアドレス", old.get("user", ""))
    e_pass = field("パスワード", "", show="●")
    if old.get("password"):
        note("（保存済み。変えるときだけ入れてください）")
    e_host = field("送信サーバー", old.get("host", "mail1030.onamae.ne.jp"))
    e_port = field("ポート", str(old.get("port", 465)), width=8)

    head("2. バックアップの合言葉（PC には保存しません）")
    note("10文字以上。.secrets を暗号にして Cloudflare に週1回送ります。復元するときだけ必要です。\n"
         "必ず紙に書いて保管してください（忘れると復元できません）。")
    if guard_backup.has_keys():
        note("（決めてあります。変えるときだけ入れてください）")
    e_p1 = field("合言葉", "", show="●")
    e_p2 = field("もう一度", "", show="●")

    head("3. Search Console の鍵（任意・あとからでも入れられます）")
    gsc_path = tk.StringVar(value="（入れてあります）" if GSC.exists() else "")
    tk.Label(root, textvariable=gsc_path, width=40, anchor="w", fg="#333").grid(row=r, column=1, sticky="w", **pad)
    tk.Button(root, text="JSON ファイルを選ぶ",
              command=lambda: gsc_path.set(filedialog.askopenfilename(filetypes=[("JSON", "*.json")]) or gsc_path.get())
              ).grid(row=r, column=0, sticky="e", **pad)
    r += 1

    msg = tk.Label(root, text="", fg="#c00", justify="left", wraplength=520)
    msg.grid(row=r + 1, column=0, columnspan=3, sticky="w", padx=12, pady=(4, 0))
    result = {}

    def save(*_):
        msg.config(text="確かめています…", fg="#555")
        root.update()
        user, pw = e_user.get().strip(), e_pass.get()
        host, port = e_host.get().strip(), e_port.get().strip()
        if not pw and old.get("password") and user == old.get("user"):
            pw = old["password"]
        if not (user and pw and host and port.isdigit()):
            msg.config(text="1. のメールアドレス・パスワード・送信サーバー・ポートを入れてください", fg="#c00")
            return
        err = smtp_login(host, port, user, pw)
        if err:
            msg.config(text="1. " + err, fg="#c00")
            return
        p1, p2 = e_p1.get(), e_p2.get()
        if p1 or p2 or not guard_backup.has_keys():
            if len(p1) < 10:
                msg.config(text="2. 合言葉は10文字以上にしてください", fg="#c00")
                return
            if p1 != p2:
                msg.config(text="2. 合言葉が2回で違います", fg="#c00")
                return
        g = gsc_path.get()
        if g and not g.startswith("（"):
            try:
                d = json.loads(Path(g).read_text(encoding="utf-8"))
                assert d.get("client_email") and d.get("private_key")
            except Exception:
                msg.config(text="3. サービスアカウントの鍵（JSON）ではないようです。「鍵を追加」で作った JSON を選んでください", fg="#c00")
                return
        result.update(user=user, password=pw, host=host, port=int(port), passphrase=p1, gsc=g)
        root.destroy()

    tk.Button(root, text="保存する", width=14, command=save).grid(row=r, column=2, sticky="e", padx=12, pady=10)
    root.bind("<Return>", save)
    e_user.focus_set()
    root.mainloop()
    return result or None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    res = window()
    if not res:
        log("窓が閉じられたため、何も保存していません")
        return 1
    st = {"at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    MAIL.parent.mkdir(exist_ok=True)
    MAIL.write_text(json.dumps({"host": res["host"], "port": res["port"], "user": res["user"], "password": res["password"],
                                "from": res["user"], "to": TO}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    st["mail"] = f"OK（{res['user']} で送信サーバーにログインできました）"
    log(st["mail"])
    if res["passphrase"]:
        guard_backup.make_keys(res["passphrase"])
        # その場で暗号化 → 合言葉で開けるかを確かめる（合言葉はこの後すぐ捨てる）
        files = guard_backup.open_blob(guard_backup.make_blob(), res["passphrase"])
        st["backup_keys"] = f"OK（鍵の組を作り、{len(files)} ファイルの暗号化と復元を確かめました。合言葉は保存していません）"
        log(st["backup_keys"])
    res["passphrase"] = ""
    if res["gsc"] and not res["gsc"].startswith("（"):
        shutil.copyfile(res["gsc"], GSC)
        st["gsc"] = "OK（鍵を .secrets/gsc-service-account.json に入れました）"
        log(st["gsc"])
    log("Windows Update の設定（管理者で1回）: UAC の確認が出たら「はい」")
    ps = ROOT / "scripts" / "winupdate.ps1"
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
                    f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"{ps}\"'"])
    try:
        wu = json.loads((ROOT / "state" / "winupdate.json").read_text(encoding="utf-8-sig"))
        st["winupdate"] = f"OK（アクティブ時間 {wu.get('active_hours')}）" if wu.get("ok") else f"NG {wu.get('error')}"
    except Exception:
        st["winupdate"] = "NG（UAC で「いいえ」になったか、動きませんでした）"
    log("Windows Update: " + st["winupdate"])
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log("設定が終わりました。この窓（黒い画面）は閉じてかまいません")
    return 0


if __name__ == "__main__":
    sys.exit(main())
