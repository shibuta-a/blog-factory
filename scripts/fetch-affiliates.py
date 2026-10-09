#!/usr/bin/env python3
"""
A8.net の提携作業をブラウザ自動操作でまとめて行い、広告リンクを台帳に保存する。

前提: A8.net にログイン済みの Chrome が「デバッグ用ポート 9333」付きで起動していること。
      （起動とログインは `python scripts/a8-login.py`。A8.net はブラウザを閉じるとログインが消えるため、
        ログインした Chrome を開いたまま、このスクリプトを実行する）

  python scripts/fetch-affiliates.py search          … 候補を検索して data/a8-candidates.json に保存（提携はしない）
  python scripts/fetch-affiliates.py apply [-n 20]   … 候補のうち未提携のものに提携申請（即時提携を優先）
  python scripts/fetch-affiliates.py links           … 提携中のプログラムの広告リンクを取得して data/affiliates.json を更新
  python scripts/fetch-affiliates.py all             … search → apply → links を続けて実行

検索ワードやジャンルの判定は下の KEYWORDS / RELEVANT を変えれば、別ジャンル（宅配食・通信講座など）にも使える。
"""
import argparse
import datetime
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from a8_browser import CDP_URL  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CANDIDATES = DATA / "a8-candidates.json"
LEDGER = DATA / "affiliates.json"
SHOTS = ROOT / ".secrets" / "shots"

BASE = "https://media-console.a8.net"

# 検索するキーワード（ジャンルを広げるときはここに足す）
KEYWORDS = ["宅配クリーニング", "布団クリーニング", "ふとんクリーニング", "クリーニング 保管",
            "家事代行", "ホワイト急便", "Nexcy", "しももと", "リナビス", "リネット", "カジタク"]
# 候補に残す条件（名前・関連キーワードにどれかを含むもの）
RELEVANT = ["クリーニング", "家事代行", "家事", "布団", "ふとん"]
# 対象外（クリーニングでも今回のジャンルと違うもの）
EXCLUDE = ["エアコン", "ハウスクリーニング", "靴", "くつ", "スニーカー"]

# 提携申請フォームの任意項目（このブログの実態に合わせる）
APPLY_CHANNELS = ["SEO"]
APPLY_STYLES = ["比較・ランキング", "情報・ノウハウ記事"]

WAIT = 2.0  # 画面操作の間隔（秒）。相手のサーバーに負荷をかけないため


def log(msg):
    print(msg, flush=True)


def connect(p):
    browser = p.chromium.connect_over_cdp(CDP_URL, timeout=90000)
    ctx = browser.contexts[0]
    page = ctx.new_page()
    page.set_default_timeout(30000)
    return browser, page


def goto(page, url):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(int(WAIT * 1000))
    if "re-authentication" in page.url or "/login" in page.url:
        sys.exit("[A8] ログインが切れています。`python scripts/a8-login.py` でログインし直してください。")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def parse_cards(page):
    return page.eval_on_selector_all(".pgCard", """cards => cards.map(c => {
        const q = s => c.querySelector(s);
        const link = [...c.querySelectorAll('a[href*="programId="]')].map(a => a.getAttribute('href'))[0] || '';
        const m = link.match(/programId=(s\\d+)/);
        const rows = {};
        c.querySelectorAll('tr, dl > div, .pgData li').forEach(r => {
          const k = r.querySelector('th, dt'); const v = r.querySelector('td, dd');
          if (k && v) rows[k.innerText.trim()] = v.innerText.trim().replace(/\\s+/g, ' ');
        });
        return {
          program_id: m ? m[1] : '',
          name: (q('.pgName') || {}).innerText || '',
          advertiser: (q('.ecName') || {}).innerText || '',
          status: (q('.pgStatus') || {}).innerText || '',
          text: c.innerText.replace(/\\s+/g, ' ').slice(0, 1500),
          rows,
        };
    })""")


def search(page, keyword, auto_only=False):
    q = {"keywords": keyword, "pageNo": 1, "pageSize": 100, "sortKey": "NORMAL"}
    url = f"{BASE}/program/search/keyword?" + urllib.parse.urlencode(q)
    if auto_only:
        url += "&detailAttributes=AUTO_CONTRACT"
    goto(page, url)
    page.wait_for_timeout(1500)
    return parse_cards(page)


def is_relevant(c):
    hay = c["name"] + " " + c["text"]
    return any(w in hay for w in RELEVANT) and not any(w in c["name"] for w in EXCLUDE)


def cmd_search(page):
    found = {}
    auto_ids = set()
    for kw in KEYWORDS:
        cards = search(page, kw)
        auto = search(page, kw, auto_only=True)
        auto_ids |= {c["program_id"] for c in auto}
        rel = [c for c in cards if c["program_id"] and is_relevant(c)]
        log(f"検索「{kw}」: {len(cards)} 件中 {len(rel)} 件が対象（即時提携 {len(auto)} 件）")
        for c in rel:
            found.setdefault(c["program_id"], c)
    for pid, c in found.items():
        c["auto_contract"] = pid in auto_ids
        m = re.search(r"成果報酬\s*(.+?)\s*EPC", c["text"])
        c["reward"] = m.group(1).strip() if m else ""
        m = re.search(r"確定率\s*([\d.]+%|-)", c["text"])
        c["decided_rate"] = m.group(1) if m else ""
        c.pop("rows", None)
    DATA.mkdir(exist_ok=True)
    out = sorted(found.values(), key=lambda c: (not c["auto_contract"], c["name"]))
    CANDIDATES.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"候補 {len(out)} 件を {CANDIDATES.relative_to(ROOT)} に保存")
    for c in out:
        log(f"  [{c['status']}] {'即時' if c['auto_contract'] else '審査'} {c['program_id']} {c['name']} / {c['reward']}")
    return out


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
def apply_one(page, c):
    goto(page, f"{BASE}/program/detail-not-partnered?programId={c['program_id']}")
    btn = page.locator("form[action='/program/agreement/apply'] button", has_text="提携申請")
    if btn.count() == 0:
        body = page.inner_text("body")
        state = "提携中" if "提携中" in body[:3000] else ("申請中" if "申請中" in body[:3000] else "申請ボタンなし")
        return state
    form = page.locator("form[action='/program/agreement/apply']")
    for label in APPLY_CHANNELS + APPLY_STYLES:
        cb = form.locator("label", has_text=label).locator("input[type=checkbox]")
        if cb.count() and not cb.first.is_checked():
            cb.first.check(force=True)
    btn.first.click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(int(WAIT * 1000))
    # 確認画面が出る場合は確定ボタンを押す
    for _ in range(2):
        confirm = page.locator("button, input[type=submit]").filter(
            has_text=re.compile("申請する|提携する|確定|同意して"))
        if "complete" in page.url or "完了" in page.inner_text("body")[:2000]:
            break
        if confirm.count():
            confirm.first.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(int(WAIT * 1000))
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"apply-{c['program_id']}.png"))
    body = page.inner_text("body")
    if "提携を申請しました" in body or "提携申請完了" in body:
        return "申請完了"  # 即時提携かどうかは「参加中プログラム」に出るかで確認する（links で判定）
    return "結果不明（スクリーンショット確認）"


def cmd_apply(page, limit, ids=None):
    if not CANDIDATES.exists():
        sys.exit("先に search を実行してください。")
    cands = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    todo = [c for c in cands if "未提携" in c.get("status", "") and not c.get("apply_result")]
    if ids:
        todo = [c for c in cands if c["program_id"] in ids]
    todo.sort(key=lambda c: not c.get("auto_contract"))
    todo = todo[:limit]
    log(f"提携申請: {len(todo)} 件（即時提携を優先）")
    results = []
    for c in todo:
        try:
            r = apply_one(page, c)
        except Exception as e:  # 1件の失敗で止めない
            r = f"エラー: {e.__class__.__name__}: {str(e)[:120]}"
        c["apply_result"] = r
        c["applied_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        results.append(c)
        log(f"  {r:　<12} {c['program_id']} {c['name']}")
        time.sleep(WAIT)
    CANDIDATES.write_text(json.dumps(cands, ensure_ascii=False, indent=1), encoding="utf-8")
    return results


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------
def partnered_programs(page):
    goto(page, f"{BASE}/program/list/partnered?pageNo=1&pageSize=100")
    page.wait_for_timeout(1500)
    return page.eval_on_selector_all("a[href*='programId=']", """as => {
        const seen = {}; const out = [];
        as.forEach(a => {
          const m = a.getAttribute('href').match(/programId=(s\\d+)/); if (!m || seen[m[1]]) return;
          const card = a.closest('.pgCard, tr, li, .section') || a;
          const name = (card.querySelector('.pgName') || a).innerText.trim();
          seen[m[1]] = 1; out.push({program_id: m[1], name, href: a.getAttribute('href')});
        });
        return out;
    }""")


def text_links(page):
    """広告リンク作成画面の全素材から「テキストリンク」のコードだけを集める。"""
    codes = page.eval_on_selector_all("textarea", "ts => ts.map(t => t.value)")
    out = []
    for code in codes:
        m = re.search(r'<a href="(https://px\.a8\.net/svt/ejp\?a8mat=[^"]+)"[^>]*>(.*?)</a>', code, re.S)
        if not m or "<img" in m.group(2):
            continue  # バナー（画像）・AMP用は除く
        px = re.search(r'src="(https://www\d*\.a8\.net/0\.gif\?a8mat=[^"]+)"', code)
        text = re.sub(r"<[^>]+>", " ", m.group(2))
        text = re.sub(r"\s+", " ", text).strip()
        out.append({"url": m.group(1), "report_tag": px.group(1) if px else "", "text": text, "source_code": code})
    return out


def best_text_link(links):
    """短くて素直な文言のテキストリンクを選ぶ（記事のボタン文言は台帳の cta で自由に変えられる）。"""
    if not links:
        return None
    return sorted(links, key=lambda l: (len(l["text"]) < 4, len(l["text"])))[0]


def cmd_links(page):
    progs = partnered_programs(page)
    cands = {c["program_id"]: c for c in json.loads(CANDIDATES.read_text(encoding="utf-8"))} if CANDIDATES.exists() else {}
    ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {}
    by_pid = {v.get("program_id"): k for k, v in ledger.items() if isinstance(v, dict)}
    log(f"提携中プログラム: {len(progs)} 件")
    added = 0
    for p in progs:
        pid = p["program_id"]
        c = cands.get(pid)
        if not c:  # 今回のジャンル以外の提携中プログラム（証券・ドメイン等）は台帳に入れない
            log(f"  スキップ（対象ジャンル外）: {pid} {p['name'][:30]}")
            continue
        goto(page, f"{BASE}/program/create-link?programId={pid}&pageSize=ALL")
        links = text_links(page)
        best = best_text_link(links)
        if not best:
            SHOTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOTS / f"link-{pid}.png"), full_page=True)
            log(f"  テキストリンクなし: {pid} {c['name']}")
            continue
        key = by_pid.get(pid) or c["name"]
        entry = ledger.get(key, {})
        entry.update({
            "program_id": pid,
            "advertiser": c.get("advertiser", ""),
            "url": best["url"],
            "report_tag": best["report_tag"],
            "link_text": best["text"],
            "source_code": best["source_code"],
            "ad_copies": [l["text"] for l in links][:12],
            "reward": c.get("reward", ""),
            "fetched_at": datetime.date.today().isoformat(),
        })
        entry.setdefault("label", c["name"])
        entry.setdefault("keywords", ["クリーニング"])
        entry.setdefault("genre_keywords", ["クリーニング", "布団", "ふとん", "家事"])
        ledger[key] = entry
        added += 1
        log(f"  取得: {pid} {key}（テキスト素材 {len(links)} 件）")
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log(f"台帳を更新: {added} 件 → {LEDGER.relative_to(ROOT)}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["search", "apply", "links", "all"])
    ap.add_argument("-n", type=int, default=20, help="apply で申請する最大件数")
    ap.add_argument("--ids", nargs="*", help="apply で申請するプログラムIDを指定（省略時は候補の未提携すべてから）")
    args = ap.parse_args()
    with sync_playwright() as p:
        _, page = connect(p)
        try:
            if args.command in ("search", "all"):
                cmd_search(page)
            if args.command in ("apply", "all"):
                cmd_apply(page, args.n, args.ids)
            if args.command in ("links", "all"):
                cmd_links(page)
        finally:
            page.close()


if __name__ == "__main__":
    main()
