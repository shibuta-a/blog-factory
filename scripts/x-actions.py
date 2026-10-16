#!/usr/bin/env python3
"""
X の投稿係（GitHub Actions の .github/workflows/x-post.yml が30分おきに動かす。PC が切れていても動く）。
Python 標準ライブラリだけ。キーは GitHub の Secrets（X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET）から環境変数で受け取る。

  python scripts/x-actions.py                      … 本番: 今日（日本時間）の予定表 data/x-queue/<日付>.json から、時刻が来た投稿を1件出す
  python scripts/x-actions.py --loop               … Actions の本番: 上を、今日の次の投稿の時刻まで待っては出す、をくり返す（5時間半で自分をもう一度起動して引き継ぐ）
  python scripts/x-actions.py --dry-run [--now 2026-10-17T23:59] [--queue data/x-queue/rehearsal-2026-10-17.json]
        … 通し確認: 認証（GET /2/users/me）まで確かめ、投稿は API に送る直前で止める。記録も書かない

決まり:
  ・1回の実行で出すのは1件まで。前の投稿から min_gap_minutes（60分）はあける
  ・リンク付きは記事のページが表示される（200）ことを確かめてから出す。まだなら次の回にまた見る
  ・投稿に失敗したら、その投稿はその日は再試行しない（混雑・一時的なエラー（429・5xx・通信）だけは3回まで次の回に再試行）
  ・残高不足（402 など）→ data/x.json の credit.depleted と STATUS.md の先頭に知らせる。次に投稿が通れば自動で消す
  ・投稿数と概算費用を data/x.json に記録する（月ごと）
"""
import base64  # noqa: F401  （social.upload_image が使う）
import datetime
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import social  # noqa: E402

ROOT = social.ROOT
QUEUE = ROOT / "data" / "x-queue"
XLOG = social.X_LOG
STATUS = ROOT / "STATUS.md"
JST = datetime.timezone(datetime.timedelta(hours=9))
FMT = "%Y-%m-%dT%H:%M"
RETRY_CODES = {429, 500, 502, 503, 504, 0}


def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_xlog(xl):
    XLOG.write_text(json.dumps(xl, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def page_ok(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "blog-factory-x-check"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def is_credit_error(code, data):
    s = json.dumps(data, ensure_ascii=False).lower()
    return code == 402 or "credit" in s or "insufficient" in s or "payment" in s


def update_status_alert(xl, cfg):
    """STATUS.md の「⚠ 要対応」に X API の知らせを出す／消す（ローカルの autopilot status も同じ文を出す）。"""
    if not STATUS.exists():
        return False
    text = STATUS.read_text(encoding="utf-8")
    alert = social.x_api_alert(xl, cfg)
    lines = [l for l in text.split("\n") if not l.startswith("- **X API の")]
    if alert:
        if "## ⚠ 要対応" in lines:
            lines.insert(lines.index("## ⚠ 要対応") + 1, alert)
        else:
            i = next((k for k, l in enumerate(lines) if l.startswith("## 全体")), 3)
            lines[i:i] = ["## ⚠ 要対応", alert, ""]
    new = "\n".join(lines)
    if new != text:
        STATUS.write_text(new, encoding="utf-8", newline="\n")
        return True
    return False


def once(args):
    dry = "--dry-run" in args
    now = (datetime.datetime.strptime(args[args.index("--now") + 1], FMT) if "--now" in args and args[args.index("--now") + 1]
           else datetime.datetime.now(JST).replace(tzinfo=None, second=0, microsecond=0))
    cfg = social.load_config()["x_api"]
    if "--status-only" in args:
        update_status_alert(load_json(XLOG, {}), cfg)
        return 0
    qpath = (ROOT / args[args.index("--queue") + 1]) if "--queue" in args and args[args.index("--queue") + 1] \
        else QUEUE / f"{now.date().isoformat()}.json"
    log(f"いま（日本時間）: {now:%Y-%m-%d %H:%M}{'（通し確認: 投稿はしない）' if dry else ''} / 予定表: {qpath.relative_to(ROOT).as_posix()}")
    if not cfg.get("enabled"):
        log("x_api.enabled が false → 何もしません")
        return 0
    q = load_json(qpath, None)
    if not q:
        log("今日の予定表がありません → 何もしません")
        return 0
    xl = load_json(XLOG, {})
    xl.setdefault("_about", "X API の投稿の記録（GitHub Actions の scripts/x-actions.py が書く）。cost は概算（data/social.json の x_api の単価）")
    posts = xl.setdefault("posts", [])
    fails = xl.setdefault("failed", {})
    done = {p["id"] for p in posts}
    last = max((p["posted_at"] for p in posts), default="")
    pending = [it for it in q["items"] if it["id"] not in done and fails.get(it["id"], {}).get("final") is not True]
    due = [it for it in pending if it["at"] <= now.strftime(FMT)]
    log(f"今日の予定 {len(q['items'])} 件・投稿済み {sum(1 for it in q['items'] if it['id'] in done)} 件・時刻が来たもの {len(due)} 件")
    for it in q["items"]:
        st = ("投稿済み" if it["id"] in done else "やめた" if fails.get(it["id"], {}).get("final") else
              "時刻が来た" if it in due else "待ち")
        log(f"  {it['at'][11:]} {it['kind']:<9} {st}")
    if not due:
        return 0
    if last and now - datetime.datetime.strptime(last, FMT) < datetime.timedelta(minutes=cfg.get("min_gap_minutes", 60)):
        log(f"前の投稿（{last}）から {cfg.get('min_gap_minutes', 60)} 分たっていないので、次の回にします")
        return 0
    it = due[0]
    log(f"\n次に出す投稿: {it['id']}（{it['kind']}・予定 {it['at'][11:]}・{social.x_weight(it['text'])}/280）\n{it['text']}")
    if it.get("check_url"):
        if not page_ok(it["check_url"]):
            log(f"記事のページがまだ表示されません（{it['check_url']}）→ 次の回にもう一度見ます")
            return 0
        log(f"記事のページの表示を確認しました（{it['check_url']}・200）")
    img = None
    if it.get("image"):
        img = (ROOT / it["image"]).read_bytes()
        log(f"画像: {it['image']}（{len(img) // 1024} KB）")
    keys = social.load_x_keys()
    if not keys:
        log("X のキーがありません（GitHub の Secrets: X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET）")
        return 1
    if dry:
        code, data = social.x_request("GET", "https://api.x.com/2/users/me", keys)
        who = (data.get("data") or {}).get("username")
        log(f"認証の確認: {'OK @' + who if code == 200 else 'NG ' + str(code) + ' ' + json.dumps(data, ensure_ascii=False)[:300]}")
        log("通し確認のため、ここで止めます（本番はこの次に " + ("POST /2/media/upload → " if img else "")
            + "POST /2/tweets を送り、data/x.json に記録します）")
        return 0 if code == 200 else 1

    def fail(code, data, where):
        f = fails.setdefault(it["id"], {"tries": 0})
        f["tries"] += 1
        f.update(at=now.strftime(FMT), code=code, where=where, error=json.dumps(data, ensure_ascii=False)[:300])
        f["final"] = code not in RETRY_CODES or f["tries"] >= 3
        if is_credit_error(code, data):
            xl["credit"] = {"depleted": True, "at": now.strftime(FMT)}
            f["final"] = False      # クレジットを足せば同じ日のうちは出せる
        log(f"{where} に失敗（{code}）: {f['error']}" + ("（この投稿は今日はもう出しません）" if f["final"] else "（次の回に再試行）"))
        save_xlog(xl)
        update_status_alert(xl, cfg)
        return 1

    media_ids = None
    if img:
        code, mid, data = social.upload_image(img, keys)
        if not mid:
            return fail(code, data, "画像のアップロード")
        media_ids = [mid]
    code, data = social.post_tweet(it["text"], keys, media_ids)
    if code not in (200, 201):
        return fail(code, data, "投稿")
    tid = (data.get("data") or {}).get("id")
    cost = float(it.get("est_cost", 0))
    posts.append({"id": it["id"], "date": q["date"], "kind": it["kind"], "at": it["at"], "posted_at": now.strftime(FMT),
                  "tweet_id": tid, "url": it.get("url"), "text": it["text"], "cost": cost})
    m = xl.setdefault("months", {}).setdefault(now.strftime("%Y-%m"), {"posts": 0, "link_posts": 0, "cost": 0.0})
    m["posts"] += 1
    m["link_posts"] += 1 if it["kind"] == "link" else 0
    m["cost"] = round(m["cost"] + cost, 4)
    fails.pop(it["id"], None)
    if (xl.get("credit") or {}).get("depleted"):
        xl["credit"] = {"depleted": False, "recovered_at": now.strftime(FMT)}
    save_xlog(xl)
    update_status_alert(xl, cfg)
    log(f"投稿しました: https://x.com/{cfg.get('account')}/status/{tid}（今月 {m['posts']} 件・概算 ${m['cost']:.2f}）")
    return 0


LOOP_LIMIT_SEC = 5.5 * 3600   # 1回の実行で待つのはここまで（Actions の1ジョブは6時間まで）→ 自分をもう一度起動して引き継ぐ


def now_jst():
    return datetime.datetime.now(JST).replace(tzinfo=None, second=0, microsecond=0)


def next_wait():
    """今日まだ出していない投稿までの待ち秒数（なければ None）。時刻が来ているのに出ていない（記事がまだ表示されない・前の投稿から60分たっていない・一時的なエラー）ときは10分後にまた見る。"""
    now = now_jst()
    q = load_json(QUEUE / f"{now.date().isoformat()}.json", None)
    if not q:
        return None
    xl = load_json(XLOG, {})
    done = {p["id"] for p in xl.get("posts", [])}
    fails = xl.get("failed", {})
    pend = [it for it in q["items"] if it["id"] not in done and fails.get(it["id"], {}).get("final") is not True]
    if not pend:
        return None
    sec = (datetime.datetime.strptime(min(it["at"] for it in pend), FMT) - now).total_seconds() + 30
    return sec if sec > 60 else 600


def redispatch():
    import subprocess
    r = subprocess.run(["gh", "workflow", "run", "x-post.yml", "-R", os.environ.get("GITHUB_REPOSITORY", "shibuta-a/blog-factory"),
                        "-f", "dry_run=false"], capture_output=True, text=True)
    log("次の待ち番を起動しました" if r.returncode == 0 else f"次の待ち番を起動できませんでした（{r.stderr.strip()[:200]}）→ cron の保険で続きます")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if "--loop" not in args or "--dry-run" in args:
        return once(args)
    import subprocess
    import time
    start = time.time()
    while True:
        rc = once([a for a in args if a != "--loop"])
        if os.environ.get("GITHUB_ACTIONS"):
            subprocess.run(["bash", str(ROOT / "scripts" / "x-record.sh")])
        if rc == 1 and not social.load_x_keys():
            return 1
        wait = next_wait()
        if wait is None:
            log("今日の投稿はもう残っていません（待ち番を終わります）")
            return 0
        if time.time() - start + wait > LOOP_LIMIT_SEC:
            log(f"次の投稿まで {wait / 60:.0f} 分。ここで区切って次の待ち番に引き継ぎます")
            redispatch()
            return 0
        log(f"次の投稿まで {wait / 60:.0f} 分待ちます（{(now_jst() + datetime.timedelta(seconds=wait)):%H:%M} ごろ）")
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
