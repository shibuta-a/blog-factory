#!/usr/bin/env python3
"""
見張り番（Cloudflare Workers の maf-guard・workers/guard）への合図と、通知・バックアップ・週1回の点検（PC 側）。

  python scripts/guard.py deploy                 … 見張り番を設置・更新し、合言葉（GUARD_TOKEN）とメールの送信元を Cloudflare の秘密の設定に入れる
  python scripts/guard.py status                 … 今日の合図・最後のメール・バックアップの一覧
  python scripts/guard.py beat <名前> [--date D]  … 合図を手で送る（blog / note / xplan / boot-start / boot-end）。中身は PC の記録から作る
  python scripts/guard.py check [--date D] [--test] [--dry]
                                                 … 朝 7:30 の見張りを今すぐ（--test: 件名に【テスト】・--dry: 送らずに中身を表示）
  python scripts/guard.py expect pc1-mail on|off … 朝の見張りでその合図を待つか（PC① の営業メールは今は off。data/guard-pc1-howto.md）
  python scripts/guard.py notify "件名" "本文"     …メールを1通（見張り番から。だめなら PC から直接 maf-group.jp の送信サーバーで）
  python scripts/guard.py backup                 … .secrets を暗号化して Cloudflare へ（合言葉は要らない。毎週月曜の朝に自動）
  python scripts/guard.py restore [--to DIR]     … バックアップを取り出して合言葉で開き、.secrets に書き戻す（新しい PC で復元するとき）
  python scripts/guard.py weekly-check           … 週1回の点検（ディスクの空き・ログの大きさ・トークンの期限・X のクレジット・バックアップ）
  python scripts/guard.py summary [--date D]     … 合図の中身（まとめに載る行）を表示するだけ

合図は送れなかったら state/guard-pending.json にためて、次に送るときに一緒に送る。
キー: .secrets/guard.json（見張り番の URL と合言葉）・.secrets/mail.json（送信元。scripts/guard-setup.py の窓で入れる）
"""
import json
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
WORKER_DIR = ROOT / "workers" / "guard"
CFG = ROOT / ".secrets" / "guard.json"
MAIL = ROOT / ".secrets" / "mail.json"
PENDING = ROOT / "state" / "guard-pending.json"
CLAUDE_LIMIT = ROOT / "state" / "claude-limit.json"
WEEKLY = ROOT / "state" / "weekly-check.json"
LOG = ROOT / "logs" / "guard.log"
WRANGLER = ["npx.cmd" if sys.platform == "win32" else "npx", "--yes", "wrangler@4.136.3"]
TO = "asoutobata@gmail.com"


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def jload(p, dflt=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except Exception:
        return dflt


def today():
    import os
    return os.environ.get("BLOG_TODAY") or date.today().isoformat()


# ---------------------------------------------------------------------------
# 見張り番との通信
# ---------------------------------------------------------------------------
def call(path, data=None, method=None, raw=False, timeout=60):
    cfg = jload(CFG, {})
    if not cfg.get("url"):
        raise RuntimeError("見張り番がまだ設置されていません（python scripts/guard.py deploy）")
    body = data if isinstance(data, (bytes, type(None))) else json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(cfg["url"] + path, data=body, method=method or ("POST" if body is not None else "GET"),
                                 headers={"Authorization": f"Bearer {cfg['token']}", "User-Agent": "blog-factory",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode("utf-8")
    return text if raw else json.loads(text)


def _send_pending(items):
    left = []
    for b in items:
        try:
            call("/beat", b)
        except Exception:
            left.append(b)
    return left


def beat(name, ok=True, lines=None, warn=None, error="", day=None):
    """合図を送る。送れなければためておき、次の合図のときに一緒に送る。通し確認（BLOG_REHEARSAL）では送らない。"""
    import os
    b = {"name": name, "date": day or today(), "ok": ok, "lines": lines or [], "warn": warn or [], "error": error}
    if os.environ.get("BLOG_REHEARSAL"):
        log(f"（通し確認）合図は送りません: {json.dumps(b, ensure_ascii=False)[:300]}")
        return True
    pend = jload(PENDING, []) + [b]
    left = _send_pending(pend)
    PENDING.parent.mkdir(exist_ok=True)
    PENDING.write_text(json.dumps(left, ensure_ascii=False), encoding="utf-8")
    log(f"合図 {name} {'OK' if ok else 'NG'}" + ("" if len(left) == 0 else f"（送れず {len(left)} 件をためています）"))
    return not left


def smtp_send(subject, body):
    """PC から直接、maf-group.jp の送信サーバーで送る（見張り番に届かないときの予備）。"""
    m = jload(MAIL, {})
    if not m.get("password"):
        raise RuntimeError("送信元が未設定です（scripts/guard-setup.py）")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("ブログ工場の見張り番", "utf-8")), m["from"]))
    msg["To"] = m.get("to", TO)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=m["from"].split("@")[1])
    ctx = ssl.create_default_context()
    if int(m["port"]) == 465:
        s = smtplib.SMTP_SSL(m["host"], 465, timeout=30, context=ctx)
    else:
        s = smtplib.SMTP(m["host"], int(m["port"]), timeout=30)
        s.starttls(context=ctx)
    s.login(m["user"], m["password"])
    s.sendmail(m["from"], [m.get("to", TO)], msg.as_string())
    s.quit()


def notify(subject, body):
    """メールを1通。まず見張り番（Cloudflare）から、だめなら PC から直接。"""
    import os
    if os.environ.get("BLOG_REHEARSAL"):
        log(f"（通し確認）通知は送りません: {subject}")
        return True
    try:
        r = call("/notify", {"subject": subject, "body": body})
        if r.get("ok"):
            log(f"通知（見張り番から）: {subject}")
            return True
        err = r.get("error")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    try:
        smtp_send(subject, body)
        log(f"通知（PC から直接。見張り番からは送れず: {err}）: {subject}")
        return True
    except Exception as e:
        log(f"通知を送れませんでした: {subject}（見張り番: {err} / PC: {type(e).__name__}: {e}）")
        return False


# ---------------------------------------------------------------------------
# 合図の中身（朝のまとめに載る行）
# ---------------------------------------------------------------------------
def front(path):
    t = path.read_text(encoding="utf-8")
    m = re.match(r"---\s*\n(.*?)\n---", t, re.S)
    meta = {}
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def todays_articles(day):
    out = []
    for p in sorted((ROOT / "content" / "ja").glob("*.md")):
        meta = front(p)
        if meta.get("generated_by") == "autopilot" and (meta.get("created") or meta.get("date")) == day:
            out.append(meta)
    return sorted(out, key=lambda m: m.get("publish_at", ""))


def claude_limit(day):
    st = jload(CLAUDE_LIMIT, {})
    return st if st.get("date") == day else None


def blog_beat(day):
    arts = todays_articles(day)
    lines = [f"今日の記事: {len(arts)} 本"]
    for a in arts:
        at = a.get("publish_at", "").replace("T", " ")[5:].replace("-", "/")
        lines.append(f"・{a.get('title', a.get('slug'))}（{'自動化記事・' if a.get('genre') == 'jidouka' else ''}公開 {at or 'すぐ'}）")
    rl = jload(ROOT / "data" / "refresh-log.json", [])
    for r in (rl if isinstance(rl, list) else rl.get("log", [])):
        if str(r.get("date", r.get("at", ""))).startswith(day):
            lines.append(f"・見直し: {r.get('title') or r.get('slug')}")
    warn = []
    lim = claude_limit(day)
    if lim:
        warn.append(f"Claude の使用量の上限に当たりました（{lim.get('at')}・{lim.get('stage')}）。書けたのは {len(arts)} 本"
                    f"（自動化記事 {'あり' if any(a.get('genre') == 'jidouka' for a in arts) else 'なし'}）。"
                    "見直し・下調べは見送り、明日は自動化記事の1本だけにして様子を見ます")
    ok = len(arts) > 0
    return dict(ok=ok, lines=lines, warn=warn,
                error="" if ok else ("Claude の使用量の上限で記事を書けませんでした" if lim else "今日の記事が0本です（logs/autopilot.log）"))


def note_beat(day):
    lines, warn = [], []
    posted = [(k, v) for k, v in (jload(ROOT / "data" / "note.json", {}) or {}).items()
              if isinstance(v, dict) and str(v.get("posted_at", "")).startswith(day)]
    if posted:
        for k, v in posted:
            lines.append(f"投稿しました: {v.get('title')} {v.get('url')}")
    else:
        st = jload(ROOT / "state" / "note-status.json", {})
        lines.append("今日の投稿: なし" + (f"（{st.get('text')}）" if st.get("text") and str(st.get("at", "")).startswith(day) else ""))
    cm = (jload(ROOT / "data" / "note-comments.json", {}) or {}).get("comments", {})
    replied = [c for c in cm.values() if str(c.get("replied_at", "")).startswith(day)]
    skipped = [c for c in cm.values() if c.get("action") == "skip" and str(c.get("handled_at", "")).startswith(day)]
    review = [c for c in cm.values() if c.get("action") == "review" and not c.get("resolved")]
    lines.append(f"コメント返信: {len(replied)} 件" + (f"・見送り {len(skipped)} 件" if skipped else "") +
                 f"・要確認 {'あり ' + str(len(review)) + ' 件' if review else 'なし'}")
    for c in review[:5]:
        warn.append(f"note の要確認のコメント: {c.get('nickname')}「{c.get('text', '')[:60]}」 {c.get('url')}")
    return dict(ok=True, lines=lines, warn=warn)


def x_credit_left():
    soc = (jload(ROOT / "data" / "social.json", {}) or {}).get("x_api", {})
    credit, since = soc.get("credit_usd"), soc.get("credit_set_on", "")
    if credit is None:
        return None
    used = sum(float(p.get("cost", 0)) for p in (jload(ROOT / "data" / "x.json", {}) or {}).get("posts", [])
               if str(p.get("posted_at", "")) >= since)
    return round(float(credit) - used, 2)


def xplan_beat(day):
    soc = (jload(ROOT / "data" / "social.json", {}) or {}).get("x_api", {})
    if not soc.get("enabled"):
        return dict(ok=True, lines=["X はオフです"])
    q = jload(ROOT / "data" / "x-queue" / f"{day}.json")
    xl = jload(ROOT / "data" / "x.json", {}) or {}
    lines, warn = [], []
    if not q:
        return dict(ok=False, lines=["今日の予定表がありません"], error="X の予定表が作られていません（logs/x-plan.log）")
    items = q.get("items", [])
    kinds = {"link": "記事の紹介", "tip": "文章"}
    lines.append(f"今日の投稿予定: {len(items)} 件（" + " / ".join(f"{i['at'][11:]} {kinds.get(i.get('kind'), i.get('kind'))}" for i in items) + "）")
    left = x_credit_left()
    if left is not None:
        month = datetime.now().strftime("%Y-%m")
        mc = (xl.get("months", {}).get(month, {}) or {}).get("cost", 0)
        lines.append(f"クレジット残高の見込み: ${left}（今月の概算 ${mc}）")
        if left < float(soc.get("credit_low_usd", 2)) + 3:
            warn.append(f"X のクレジット残高の見込みが ${left} です。少なくなったら Developer Console の Billing で買い足してください")
    if (xl.get("credit") or {}).get("depleted"):
        warn.append("X のクレジットが切れています（402）。Developer Console の Billing で買い足してください")
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
    rep = [r for r in (xl.get("replies") or {}).get("items", []) if str(r.get("at", "")) >= since]
    lines.append(f"X への返信: {'あり ' + str(len(rep)) + ' 件（直近7日）' if rep else 'なし'}")
    for r in rep[:5]:
        warn.append(f"X に返信が来ています: @{r.get('author')}「{r.get('text', '')[:50]}」 {r.get('url')}")
    return dict(ok=True, lines=lines, warn=warn)


def status_alerts():
    """STATUS.md の「要対応」の行（まとめの要確認に載せる。note・X・Claude 上限の分は別に出しているので除く）。"""
    try:
        t = (ROOT / "STATUS.md").read_text(encoding="utf-8")
    except Exception:
        return []
    m = re.search(r"## ⚠ 要対応\n(.*?)(?:\n## |\Z)", t, re.S)
    out = []
    for line in (m.group(1).splitlines() if m else []):
        if line.startswith("- "):
            s = re.sub(r"\*\*|`", "", line[2:]).strip()
            if not re.search(r"要確認のコメント|Xに返信|X に返信", s):
                out.append(s[:220])
    return out


def end_beat(day):
    lines, warn = [], list(status_alerts())
    wk = jload(WEEKLY, {})
    if wk.get("date") and wk["date"] >= (date.fromisoformat(day) - timedelta(days=6)).isoformat():
        if wk.get("date") == day:
            lines.append("週1回の点検: " + ("問題なし" if not wk.get("warn") else f"{len(wk['warn'])} 件"))
            for l in wk.get("lines", []):
                lines.append("・" + l)
        warn += wk.get("warn", []) if wk.get("date") == day else []
    rep = jload(ROOT / "state" / "weekly-report.json", {})
    if rep.get("date") == day:
        lines.append(f"先週の成績表: {'送りました' if rep.get('sent') else '送れませんでした'}")
    return dict(ok=True, lines=lines, warn=warn)


BUILDERS = {"blog": blog_beat, "note": note_beat, "xplan": xplan_beat, "boot-end": end_beat,
            "boot-start": lambda d: dict(ok=True, lines=[f"開始 {datetime.now():%H:%M}"])}


def send_stage(name, day=None):
    """boot-run.py から: その段階の中身を作って合図を送る。中身づくりで失敗しても合図だけは送る。"""
    day = day or today()
    try:
        b = BUILDERS[name](day)
    except Exception as e:
        b = dict(ok=True, lines=[], warn=[f"{name} のまとめを作れませんでした（{type(e).__name__}: {e}）"])
    return beat(name, b.get("ok", True), b.get("lines"), b.get("warn"), b.get("error", ""), day)


# ---------------------------------------------------------------------------
# 週1回の点検
# ---------------------------------------------------------------------------
def weekly_check():
    lines, warn = [], []
    free = shutil.disk_usage("C:\\").free / 1e9
    lines.append(f"ディスクの空き: {free:.0f} GB")
    if free < 20:
        warn.append(f"C ドライブの空きが {free:.0f} GB です（20 GB を切りました）")
    logs = [(p, p.stat().st_size) for p in (ROOT / "logs").glob("*") if p.is_file()]
    total = sum(s for _, s in logs) / 1e6
    lines.append(f"ログの大きさ: 合計 {total:.1f} MB")
    big = [f"{p.name} {s / 1e6:.0f} MB" for p, s in logs if s > 20e6]
    if total > 300 or big:
        warn.append("ログが大きくなっています: " + (", ".join(big) or f"合計 {total:.0f} MB"))
    shots = sum(p.stat().st_size for p in (ROOT / ".secrets" / "shots").glob("*") if p.is_file()) / 1e6 \
        if (ROOT / ".secrets" / "shots").exists() else 0
    if shots > 500:
        warn.append(f"作業の画面写真（.secrets/shots）が {shots:.0f} MB あります")
    tok = jload(ROOT / ".secrets" / "claude-token.json", {})
    if tok.get("expires"):
        left = (date.fromisoformat(tok["expires"][:10]) - date.today()).days
        lines.append(f"Claude のトークン: 期限 {tok['expires'][:10]}（あと {left} 日）")
        if left < 60:
            warn.append(f"Claude のトークンの期限が近い（{tok['expires'][:10]}・あと {left} 日）。setup-once.bat の 3/3 で作り直してください")
    else:
        warn.append("Claude の長期トークン（.secrets/claude-token.json）の期限が分かりません")
    gsc = jload(ROOT / ".secrets" / "gsc-service-account.json")
    lines.append("Search Console の鍵: " + ("あり（期限なし）" if gsc else "なし"))
    rc, out = wrangler("whoami", cwd=WORKER_DIR, check=False)
    cf_ok = rc == 0 and "not authenticated" not in out.lower()
    lines.append("Cloudflare のログイン（PC）: " + ("OK" if cf_ok else "切れています"))
    if not cf_ok:
        warn.append("PC の Cloudflare のログイン（wrangler）が切れています。見張り番・X の投稿係は動き続けますが、更新と閲覧数の取り込みができません")
    left = x_credit_left()
    if left is not None:
        lines.append(f"X のクレジット残高の見込み: ${left}")
        if left < 5:
            warn.append(f"X のクレジット残高の見込みが ${left} です")
    bk = jload(ROOT / "state" / "backup.json", {})
    lines.append(f"バックアップ: {bk.get('at', 'まだ')}" + (f"（{bk.get('files')} ファイル）" if bk.get("files") else ""))
    if not bk.get("at") or bk["at"][:10] < (date.today() - timedelta(days=8)).isoformat():
        warn.append("鍵のバックアップが8日以上できていません（python scripts/guard.py backup）")
    wu = jload(ROOT / "state" / "winupdate.json", {})
    lines.append("Windows Update: " + (f"アクティブ時間 {wu.get('active_hours')}" if wu.get("ok") else "未設定"))
    res = {"date": today(), "lines": lines, "warn": warn}
    WEEKLY.parent.mkdir(exist_ok=True)
    WEEKLY.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


# ---------------------------------------------------------------------------
# バックアップ
# ---------------------------------------------------------------------------
def backup():
    import guard_backup
    if not guard_backup.has_keys():
        log("バックアップ: 鍵の組がありません（scripts/guard-setup.py の窓で合言葉を決める）")
        return False
    blob = guard_backup.make_blob()
    r = call("/backup", blob.encode("utf-8"), timeout=120)
    n = len(json.loads(blob)["files"])
    st = {"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "files": n, "size_kb": round(len(blob) / 1024)}
    (ROOT / "state" / "backup.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    log(f"バックアップ: {n} ファイルを暗号化して Cloudflare へ（{st['size_kb']} KB）")
    return bool(r.get("ok"))


def restore(args):
    """バックアップを開いて書き戻す。見張り番の合言葉が無い新しい PC では、wrangler（Cloudflare のログイン）で KV から取る。"""
    import getpass
    import guard_backup
    to = Path(args[args.index("--to") + 1]) if "--to" in args else ROOT / ".secrets"
    try:
        blob = call("/backup?key=backup:latest", raw=True)
    except Exception:
        toml = (WORKER_DIR / "wrangler.toml").read_text(encoding="utf-8")
        kv = re.search(r'id = "([0-9a-f]{32})"', toml).group(1)
        rc, blob = wrangler("kv", "key", "get", "backup:latest", "--namespace-id", kv, "--remote", cwd=WORKER_DIR)
        blob = blob[blob.index("{"):blob.rindex("}") + 1]
    print(f"バックアップ（{json.loads(blob)['created']}・{len(json.loads(blob)['files'])} ファイル）を開きます")
    files = guard_backup.open_blob(blob, getpass.getpass("合言葉: "))
    to.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (to / name).write_bytes(data)
        print(f"  {to / name}")
    print("復元しました")
    return 0


# ---------------------------------------------------------------------------
# 設置
# ---------------------------------------------------------------------------
def wrangler(*args, cwd=WORKER_DIR, input_text=None, check=True):
    r = subprocess.run([*WRANGLER, *args], cwd=cwd, input=input_text, text=True, encoding="utf-8",
                       errors="replace", capture_output=True)
    out = (r.stdout or "") + (r.stderr or "")
    if check and r.returncode != 0:
        sys.exit(f"wrangler {' '.join(args[:3])} が失敗しました:\n{out[-1500:]}")
    return r.returncode, out


def deploy():
    import secrets as pysecrets
    cfg = jload(CFG, {})
    cfg.setdefault("token", pysecrets.token_urlsafe(32))
    _, out = wrangler("deploy")
    m = re.search(r"https://[\w.-]+\.workers\.dev", out)
    if m:
        cfg["url"] = m.group(0)
    CFG.write_text(json.dumps(cfg, indent=1) + "\n", encoding="utf-8")
    wrangler("secret", "put", "GUARD_TOKEN", input_text=cfg["token"])
    mail = jload(MAIL, {})
    if mail.get("password"):
        for name, val in {"SMTP_HOST": mail["host"], "SMTP_PORT": str(mail["port"]), "SMTP_USER": mail["user"],
                          "SMTP_PASS": mail["password"], "MAIL_FROM": mail["from"], "MAIL_TO": mail.get("to", TO)}.items():
            wrangler("secret", "put", name, input_text=val)
        print("メールの送信元を Cloudflare の秘密の設定に入れました")
    else:
        print("メールの送信元がまだありません（scripts/guard-setup.py の窓で入れたら、もう一度 deploy）")
    # X の投稿係にも同じ合言葉を渡す（投稿の結果を見張り番に送るため）
    wrangler("secret", "put", "GUARD_TOKEN", cwd=ROOT / "workers" / "x-poster", input_text=cfg["token"])
    print(f"見張り番を設置しました: {cfg.get('url')}（毎朝 7:30）")
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    cmd = a[0] if a else "status"
    day = a[a.index("--date") + 1] if "--date" in a else today()
    if cmd == "deploy":
        return deploy()
    if cmd == "status":
        print(json.dumps(call("/status"), ensure_ascii=False, indent=1))
        return 0
    if cmd == "beat":
        return 0 if send_stage(a[1], day) else 1
    if cmd == "summary":
        for name in ("blog", "note", "xplan", "boot-end"):
            print(name, json.dumps(BUILDERS[name](day), ensure_ascii=False, indent=1))
        return 0
    if cmd == "check":
        q = f"?date={day}" + ("&test=1" if "--test" in a else "") + ("&send=0" if "--dry" in a else "")
        r = call("/check" + q, b"", timeout=120)
        print(r["subject"])
        print(r["body"])
        if r.get("mail"):
            print("\n送信:", r["mail"])
        return 0
    if cmd == "expect":   # 待つ合図の切り替え（例: expect pc1-mail on）
        print(call("/config", {"expect": {a[1]: a[2] == "on"}}))
        return 0
    if cmd == "notify":
        return 0 if notify(a[1], a[2]) else 1
    if cmd == "backup":
        return 0 if backup() else 1
    if cmd == "restore":
        return restore(a)
    if cmd == "weekly-check":
        r = weekly_check()
        print("\n".join(r["lines"]))
        print("要確認:\n" + "\n".join("・" + w for w in r["warn"]) if r["warn"] else "要確認: なし")
        return 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
