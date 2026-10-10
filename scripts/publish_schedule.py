#!/usr/bin/env python3
"""
予約公開の時刻決め（Python標準ライブラリのみ）。

  python scripts/publish_schedule.py           … まだ公開予定時刻のない新しい自動記事に、時刻を割り振る（publish.py が毎回呼ぶ）
  python scripts/publish_schedule.py --list    … 公開予定（まだ出ていない記事）を表示
  python scripts/publish_schedule.py --due     … 今の時点で出ている予約記事を data/released.json に書く（GitHub Actions が呼ぶ）

決まり:
- 対象は generated_by: autopilot で publish_at がなく、まだ GitHub に送っていない（git 未登録の）記事だけ。
  渋田さんが手で渡した記事は、これまで通りすぐ公開する
- 時刻は「今から15分後〜24時間後」のどこか（毎朝4時台に書くので、0〜24時のどの時間帯にもなる）。分単位で完全ランダム
- 予約記事どうしは最低2時間あける。公開日（日本時間）1日あたり MAX_PER_DATE 本まで
- フロントマターに publish_at（日本時間 YYYY-MM-DDTHH:MM）・created（書いた日）を足し、date を公開日に直す
- build.py は publish_at が未来の記事を出さない。GitHub Actions（.github/workflows/release.yml）が30分おきに
  --due を実行し、時刻が来た記事があれば data/released.json を更新して push → Cloudflare がサイトを作り直す
"""
import datetime
import json
import random
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
RELEASED = ROOT / "data" / "released.json"
JST = datetime.timezone(datetime.timedelta(hours=9))
FMT = "%Y-%m-%dT%H:%M"
MIN_GAP = datetime.timedelta(hours=2)
MAX_PER_DATE = 3


def now_jst():
    return datetime.datetime.now(JST).replace(tzinfo=None, second=0, microsecond=0)


def read_meta(path):
    text = path.read_text(encoding="utf-8").lstrip("﻿")
    meta = {}
    if text.startswith("---"):
        body = text.split("\n", 1)[1]
        end = re.search(r"^---\s*$", body, re.M)
        for line in body[: end.start() if end else 0].splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


def all_posts():
    out = []
    for md in sorted(CONTENT.rglob("*.md")):
        meta = read_meta(md)
        if meta.get("draft", "").lower() in ("true", "yes", "1"):
            continue
        out.append((md, meta))
    return out


def untracked(paths):
    if not paths:
        return set()
    r = subprocess.run(["git", "ls-files", "--", *[p.relative_to(ROOT).as_posix() for p in paths]],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    tracked = {l.strip() for l in r.stdout.splitlines()}
    return {p for p in paths if p.relative_to(ROOT).as_posix() not in tracked}


def set_front_matter(path, updates):
    text = path.read_text(encoding="utf-8").lstrip("﻿")
    head, sep, rest = text.partition("\n")
    body_start = rest
    end = re.search(r"^---\s*$", body_start, re.M)
    fm, tail = body_start[: end.start()], body_start[end.start():]
    lines = fm.splitlines()
    for k, v in updates.items():
        for i, line in enumerate(lines):
            if re.match(rf"^{re.escape(k)}\s*:", line):
                lines[i] = f"{k}: {v}"
                break
        else:
            lines.append(f"{k}: {v}")
    path.write_text(head + sep + "\n".join(lines) + "\n" + tail, encoding="utf-8")


def pick_time(taken, per_date, now):
    """taken: 既存の予約時刻、per_date: 公開日ごとの本数。条件を満たすランダムな時刻を返す。"""
    start, span = now + datetime.timedelta(minutes=15), 24 * 60 - 15
    for gap in (MIN_GAP, datetime.timedelta(hours=1)):
        for _ in range(2000):
            t = start + datetime.timedelta(minutes=random.randrange(span))
            if per_date.get(t.date().isoformat(), 0) >= MAX_PER_DATE:
                continue
            if all(abs(t - x) >= gap for x in taken):
                return t
    return None


def assign():
    now = now_jst()
    posts = all_posts()
    taken, per_date = [], {}
    for md, meta in posts:
        if meta.get("publish_at"):
            try:
                taken.append(datetime.datetime.strptime(meta["publish_at"], FMT))
            except ValueError:
                pass
        d = (meta.get("publish_at") or meta.get("date") or "")[:10]
        if d and meta.get("generated_by") == "autopilot":
            per_date[d] = per_date.get(d, 0) + 1
    candidates = [md for md, meta in posts if meta.get("generated_by") == "autopilot" and not meta.get("publish_at")]
    new = sorted(untracked(candidates))
    for md in new:
        meta = read_meta(md)
        old_d = meta.get("date", "")[:10]
        if old_d:
            per_date[old_d] = max(0, per_date.get(old_d, 0) - 1)
        t = pick_time(taken, per_date, now)
        if t is None:
            print(f"  予約: {md.stem} は空き時刻が見つからないため、すぐ公開します")
            continue
        set_front_matter(md, {"date": t.date().isoformat(), "publish_at": t.strftime(FMT), "created": now.date().isoformat()})
        taken.append(t)
        per_date[t.date().isoformat()] = per_date.get(t.date().isoformat(), 0) + 1
        print(f"  予約: {md.stem} → {t:%Y-%m-%d %H:%M} に公開")
    return 0


def scheduled():
    now = now_jst().strftime(FMT)
    return sorted((m["publish_at"], md.stem) for md, m in all_posts() if m.get("publish_at", "") > now)


def due():
    now = now_jst().strftime(FMT)
    released = sorted(md.stem for md, m in all_posts() if m.get("publish_at") and m["publish_at"] <= now)
    old = json.loads(RELEASED.read_text(encoding="utf-8")) if RELEASED.exists() else []
    new = [s for s in released if s not in old]
    if not new:
        print("公開時刻が来た新しい記事はありません")
        return []
    RELEASED.write_text(json.dumps(released, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("公開時刻が来た記事: " + ", ".join(new))
    return new


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if "--list" in sys.argv:
        for t, slug in scheduled():
            print(f"{t.replace('T', ' ')}  {slug}")
        return 0
    if "--due" in sys.argv:
        new = due()
        out = __import__("os").environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"changed={'true' if new else 'false'}\nslugs={','.join(new)}\n")
        return 0
    return assign()


if __name__ == "__main__":
    sys.exit(main())
