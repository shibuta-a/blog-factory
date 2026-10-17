"""起動したら記事を書いて公開し、誰も触っていなければシャットダウンする（タスク BlogFactory-Boot から呼ばれる）。

  python scripts/boot-run.py              … 毎朝の本番（1日1回だけ）
  python scripts/boot-run.py --rehearsal  … 通し確認。明日の日付として同じ流れを最後まで動かし、各段階の時間を測る。
                                             記事は書いてビルドするが GitHub に送らない（公開しない）・note は下書きまで・
                                             シャットダウンは判定だけ。1日1回の印も付けない（終わったら作った記事は手で元に戻す）

きっかけは2つ（どちらで動いても1日1回だけ）:
  ・ログオン時（BIOS の時刻起動 → 自動ログオン）
  ・毎日の bios_wake_time の数分後（その時刻に PC がすでに点いていた日の保険。このときは起動から時間がたっているので落とさない）

見張り番（Cloudflare の maf-guard・scripts/guard.py）: 開始・記事・X・note・最後の各段階で「終わった・結果」の合図を送る。
  毎朝 7:30 に見張り番が合図を確かめ、Gmail に「【OK】今朝のまとめ」か「【要対応】…」を送る（PC が起動しなかった日も届く）
Claude の使用量の上限に当たったら（auto-generate.py が state/claude-limit.json に残す）: 書けた分は公開し、見直し・下調べ・
  記事の直しは見送り、見張り番のまとめで知らせる。次の日は自動化記事の1本だけにする

流れ（各段階の時間は logs/boot-run.log に「[時間]」で残る）:
  1. A8 のログイン確認（切れていれば保存済みの ID・パスワードで自動ログイン。通らなくても記事は書く）
  2. 記事2本（scripts/auto-generate.py・制限180分）:
     ・ふだんの日: 自動化記事（自動化ランキングの1回分）＋ 通常のジャンルの記事1本
     ・週1本の自社サービス記事（オリシャン・既定は水曜）の日: 自動化記事 ＋ オリシャン記事
     自動化記事は公式サイトで確かめてから書く（前の日に下調べした data/jidouka-research/<回>.md を使う）
     書いた記事は publish.py が公開予定時刻（24時間以内のランダム）を付けて GitHub へ送る
  3. 公開済み記事の見直し（記事が20本以上・今週3本まで）
  4. X（scripts/x-plan.py）: その日の投稿（本文・カード画像・投稿予定時刻）を作って data/x-queue/ に置き、GitHub へ。
     投稿そのものは Cloudflare Workers（workers/x-poster）が5分おきに X API で行う（PC が切れていても出る）
     ※ note より先に行う（note は最大90分待つため。X の最初の投稿 7:00・見張り番の確認 7:30 より前に予定表をそろえる）
  5. note（1日1本）。note の予約投稿はプレミアム限定のため、PC の起動から 0〜90 分のランダムな時刻まで待ってから投稿する
     続けて note のコメントへの返信とフォロー返し（scripts/note-comments.py・1日10件まで・要確認のものは STATUS.md の先頭）
  6. 翌日の自動化記事の下調べ（道具の公式サイト確認 → data/jidouka-research/<回>.md・制限60分）
  6.5 成績の取り込み（scripts/report.py collect: A8 の成果・閲覧数・Search Console）。月曜だけ: 週1回の点検・鍵のバックアップ・
     検索で押されない記事のタイトルと説明文の直し・先週の成績表のメール
  7. STATUS.md・note の記録を GitHub へ → シャットダウンの判定（すべて終わってから。data/power.json の max_uptime_min はこの全部が収まる長さ）
"""
import ctypes
import os
import random
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DONE = os.path.join(ROOT, "state", "boot-done.txt")
LOG = os.path.join(ROOT, "logs", "boot-run.log")
NOTE_WAIT_MAX_MIN = 90   # note の投稿は PC の起動から 0〜この分数のランダムな時刻
NOTE_TIMEOUT_MIN = 60
COMMENTS_TIMEOUT_MIN = 30   # 10件 × 返信の間隔（40〜90秒）＋判定が収まる長さ
REHEARSAL = "--rehearsal" in sys.argv


def boot_time():
    ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    return datetime.now() - timedelta(milliseconds=ctypes.windll.kernel32.GetTickCount64())


def wait_for_note(started):
    """note の投稿時刻をばらす。PC の起動（すでに点いていた日は、この実行の開始）から 0〜90 分のランダムな時刻まで待つ。"""
    base = boot_time()
    if started - base > timedelta(minutes=30):   # BIOS の時刻起動ではなく、点けっぱなしの日
        base = started
    at = base + timedelta(minutes=random.uniform(0, NOTE_WAIT_MAX_MIN))
    sec = (at - datetime.now()).total_seconds()
    if sec <= 0:
        log("note の投稿時刻（起動から0〜90分のランダム）はもう過ぎているので、すぐ投稿します")
    elif REHEARSAL:
        log(f"（通し確認）本番なら note の投稿は {at:%H:%M} まで待ちます（{sec / 60:.0f} 分）。今回は待たない")
    else:
        log(f"note の投稿は {at:%H:%M} まで待ちます（起動から0〜{NOTE_WAIT_MAX_MIN}分のランダム）")
        time.sleep(sec)


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {'[通し確認] ' if REHEARSAL else ''}{msg}\n")


def main():
    today = date.today().isoformat()
    env = dict(os.environ)
    if REHEARSAL:
        env["BLOG_REHEARSAL"] = "1"
        env["BLOG_TODAY"] = (date.today() + timedelta(days=1)).isoformat()
        log(f"== 通し確認を開始（{env['BLOG_TODAY']} の朝として動かす。公開・投稿・GitHub への送信・シャットダウンはしない） ==")
    else:
        os.makedirs(os.path.dirname(DONE), exist_ok=True)
        if os.path.exists(DONE) and open(DONE, encoding="utf-8").read().strip() == today:
            log("今日はもう動いた → 何もしない")
            return 0
        # 最初に印を付ける（途中で落ちても同じ日に2回は書かない）
        with open(DONE, "w", encoding="utf-8") as f:
            f.write(today)
    started = datetime.now()
    times = []

    def beat(name):
        """見張り番（Cloudflare）に「この段階が終わった・結果」の合図を送る（scripts/guard.py。送れなくても先へ進む）。"""
        try:
            py("guard.py", "beat", name, timeout=3 * 60)
        except subprocess.TimeoutExpired:
            log(f"見張り番への合図（{name}）が3分で終わらなかったため先へ進みます")

    def limit_today():
        import json
        p = os.path.join(ROOT, "state", "claude-limit.json")
        try:
            st = json.load(open(p, encoding="utf-8"))
        except Exception:
            return None
        return st if st.get("date") == env.get("BLOG_TODAY", today) else None

    def stage(name, fn):
        t = time.time()
        try:
            return fn()
        finally:
            times.append((name, (time.time() - t) / 60))
            log(f"[時間] {name}: {times[-1][1]:.1f} 分")

    def py(script, *args, out=None, timeout=None):
        """scripts/ のスクリプトを動かして終了コードを返す（out: 出力を足すログファイル名）。"""
        cmd = [sys.executable, os.path.join(ROOT, "scripts", script), *args]
        if out:
            with open(os.path.join(ROOT, "logs", out), "a", encoding="utf-8") as f:
                return subprocess.run(cmd, cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=timeout).returncode
        return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, timeout=timeout).returncode

    beat("boot-start")

    # 1. A8 のログイン確認（切れていれば .secrets/a8.json で自動ログイン。通らなくても記事は書く・理由は STATUS.md に出る）
    def a8():
        try:
            log("A8: ログインOK" if py("autopilot.py", "a8-check", timeout=10 * 60) == 0
                else "A8: ログインできず（記事は続行。理由は STATUS.md）")
        except subprocess.TimeoutExpired:
            log("A8: 確認が10分で終わらなかったため中断（記事は続行）")
    stage("A8 のログイン確認", a8)

    # 2. 記事2本: 自動化記事 ＋ 通常のジャンル1本（週1本の自社サービス記事の日は、自動化記事 ＋ オリシャン記事）
    def articles():
        import json
        weekly = py("autopilot.py", "weekly-due") == 0
        n = 2
        try:   # 前の日に Claude の使用量の上限に当たっていたら、今日は自動化記事の1本だけにする（自動化記事を優先）
            lim = json.load(open(os.path.join(ROOT, "state", "claude-limit.json"), encoding="utf-8"))
            if lim.get("date") == (date.fromisoformat(env.get("BLOG_TODAY", today)) - timedelta(days=1)).isoformat():
                n = 1
                log("昨日 Claude の使用量の上限に当たったため、今日は自動化記事の1本だけにします")
        except Exception:
            pass
        if n == 2:
            log("記事を 2 本書きます（自動化記事 ＋ " + ("週1本の自社サービス記事）" if weekly else "通常のジャンル1本）"))
        code = py("auto-generate.py", "-n", str(n), out="auto-generate.log")
        if limit_today():
            log("Claude の使用量の上限に当たりました（書けた分は公開済み。見直しと下調べは見送り、見張り番の通知に載せます）")
        log(f"記事の生成が終了（終了コード {code}）")
        return code
    code = stage("記事2本の作成・公開", articles)

    # 自動化記事が書けたか（自動化ランキングの進み具合に今日の印があるか）
    def check_jidouka():
        import json
        prog = os.path.join(ROOT, "data", "jidouka-progress.json")
        done = json.load(open(prog, encoding="utf-8")).get("done", {}) if os.path.exists(prog) else {}
        day = env.get("BLOG_TODAY", today)
        hit = [f"第{k}回 → {v.get('slug')}" for k, v in done.items() if v.get("date") == day]
        log("自動化記事: " + (" / ".join(hit) if hit else "今日の印がありません（書けなかった可能性。logs/autopilot.log を確認）"))
        return bool(hit)
    stage("自動化記事の確認", check_jidouka)

    # 3. 公開済み記事の見直し（記事が20本以上・今週の見直しが3本未満のときだけ。それ以外は refresh-next が「見直さない」と返す）
    def refresh():
        nonlocal code
        if limit_today():
            log("見直しなし: Claude の使用量の上限に当たったため")
            return
        r2 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "autopilot.py"), "refresh-next"],
                            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r2.returncode != 0:
            log("見直しなし: " + (r2.stdout.strip().splitlines() or [""])[0])
            return
        log("公開済み記事の見直しを1本行います")
        rc = py("auto-generate.py", "--refresh", out="auto-generate.log")
        log(f"見直しが終了（終了コード {rc}）")
        code = code or rc
    stage("記事の見直し", refresh)
    beat("blog")

    # 4. note への展開（公開済みの記事を、別バージョンにして1日1本。ログインが切れていたら自動ログイン、だめなら note だけ見送り）
    def note():
        try:
            if py("note-run.py", "due") == 0:
                wait_for_note(started)
            rn = py("note-run.py", out="note-run.log", timeout=NOTE_TIMEOUT_MIN * 60)
            log(f"note への展開が終了（終了コード {rn}）")
        except subprocess.TimeoutExpired:
            log(f"note への展開が{NOTE_TIMEOUT_MIN}分で終わらなかったため中断")
        # note のコメントへの返信とフォロー返し（1日10件まで。制限・警告が出たら返信だけ止める。記事の投稿は止めない）
        try:
            rc = py("note-comments.py", out="note-comments-run.log", timeout=COMMENTS_TIMEOUT_MIN * 60)
            log(f"note のコメント返信が終了（終了コード {rc}・詳しくは logs/note-comments.log）")
        except subprocess.TimeoutExpired:
            log(f"note のコメント返信が{COMMENTS_TIMEOUT_MIN}分で終わらなかったため中断")

    # 5. X（今日の投稿の予定表を作って GitHub へ。投稿は Cloudflare の x-poster が X API で行う。だめなら X だけ見送り）
    def x():
        try:
            rx = py("x-plan.py", out="x-plan-run.log", timeout=30 * 60)
            log(f"X の予定表づくりが終了（終了コード {rx}・詳しくは logs/x-plan.log）")
        except subprocess.TimeoutExpired:
            log("X の予定表づくりが30分で終わらなかったため中断")
    # X は note より先に（note は起動から最大90分待つので、X の予定表と合図を 7:00 の最初の投稿・7:30 の見張りより前に済ませる）
    stage("X", x)
    beat("xplan")
    stage("note", note)
    beat("note")

    # 6. 翌日の自動化記事の下調べ（道具の公式サイト確認。朝の執筆はこれを使う）
    def research():
        if limit_today():
            log("下調べなし: Claude の使用量の上限に当たったため")
            return
        if py("autopilot.py", "jidouka-research-next") != 0:
            log("下調べ: 次の2回分は済んでいます")
            return
        rc = py("auto-generate.py", "--research", out="auto-generate.log")
        log(f"翌日の自動化記事の下調べが終了（終了コード {rc}）")
    stage("翌日分の下調べ", research)

    # 6.5 成績の取り込み（毎日: A8 の成果・閲覧数・Search Console → data/report/）と、月曜だけ: 先週の成績表のメール・
    #     検索で押されない記事の直し・週1回の点検・鍵のバックアップ
    def weekly():
        try:
            rc = py("report.py", "collect", out="report-run.log", timeout=20 * 60)
            log(f"成績の取り込みが終了（終了コード {rc}・詳しくは logs/report.log）")
        except subprocess.TimeoutExpired:
            log("成績の取り込みが20分で終わらなかったため中断")
        if date.fromisoformat(env.get("BLOG_TODAY", today)).weekday() != 0:
            return
        for script, args, minutes, what in (("guard.py", ["weekly-check"], 5, "週1回の点検"),
                                             ("guard.py", ["backup"], 5, "鍵のバックアップ"),
                                             ("report.py", ["ctr-fix"], 60, "検索で押されない記事の直し"),
                                             ("report.py", ["weekly"], 10, "先週の成績表")):
            if script == "report.py" and args == ["ctr-fix"] and limit_today():
                log(f"{what}: Claude の使用量の上限に当たったため見送り")
                continue
            try:
                rc = py(script, *args, out="report-run.log", timeout=minutes * 60)
                log(f"{what}が終了（終了コード {rc}）")
            except subprocess.TimeoutExpired:
                log(f"{what}が{minutes}分で終わらなかったため中断")
    stage("成績・点検（月曜は成績表・バックアップ）", weekly)

    # 7. 記録
    def record():
        py("autopilot.py", "status")
        if REHEARSAL:
            log("（通し確認）STATUS.md などは GitHub に送りません")
            return
        # 最後に更新された STATUS.md と note の原稿・下調べを GitHub に送る（publish.py のあとに書き換わった分が残らないように）
        git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        git("add", "STATUS.md", "data/note", "data/note.json", "data/note-comments.json", "data/jidouka-research",
            "data/report")
        if git("diff", "--cached", "--quiet").returncode != 0:
            git("commit", "-m", f"status: {today} の自動実行の記録")
            git("pull", "--rebase", "--autostash")
            log("STATUS.md などを GitHub に送りました" if git("push").returncode == 0 else "STATUS.md の push に失敗（次回また送る）")
    stage("記録", record)
    beat("boot-end")

    total = (datetime.now() - started).total_seconds() / 60
    log("[時間] 合計: " + f"{total:.1f} 分（" + " / ".join(f"{n} {m:.1f}" for n, m in times) + "）")
    log("シャットダウンの判定へ")
    stage("シャットダウンの判定", lambda: subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "power-guard.py"), "--exit-code", str(code),
         "--task", "BlogFactory-Boot", *(["--dry-run"] if REHEARSAL else [])], cwd=ROOT, env=env))
    if REHEARSAL:
        log("== 通し確認が終了 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
