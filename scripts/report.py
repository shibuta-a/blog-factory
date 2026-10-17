#!/usr/bin/env python3
"""
何が稼いでいるかを測る（成績の取り込み・週の成績表・検索で押されない記事の直し・読まれている記事への広告の寄せ）。

  python scripts/report.py collect        … 毎朝（boot-run.py の 6.5）: 取り込んで state/report/ に1日分ずつ保存
        ・A8（自動ログイン済み）: プログラム別（今月の imp・click・発生件数・発生金額・未確定）・確定（承認）・
          パラメータ計測（記事別。広告リンクの &id1=<記事の目印> は build.py が付ける）
        ・閲覧数: ブログの門番 functions/_middleware.js が Cloudflare の D1（maf-blog-stats の pv）に数えたものの前日分
          （記事別・参照元・?utm_source）。Cloudflare Web Analytics は有効にするのに管理画面の操作が要るため、同じことを Cloudflare 側で数えている
        ・Search Console（.secrets/gsc-service-account.json があるときだけ。2026-10-17 時点では外している）: 記事別の表示回数・クリック・順位・検索語
  python scripts/report.py weekly [--dry]  … 月曜の朝: 先週（月〜日）の成績表を Gmail へ（--dry は送らずに表示）
        ＋ 閲覧数の多い記事を data/ad-focus.json に書き、広告の見直しの優先に入れる（6-3）
  python scripts/report.py ctr-fix [--dry] … 月曜の朝: 表示が多いのにクリック率が低い記事を3本まで選び、タイトルと説明文を書き直して公開（6-2）
        記録は data/ctr-log.json。4週間後にクリック率を比べて成績表に載せる
  python scripts/report.py show [日付]     … 取り込んだ1日分を表示

取り込めなかったもの（A8 のログイン切れ・Search Console の鍵がない など）は飛ばし、理由を logs/report.log に残す。
売上の数字は公開リポジトリ（GitHub）に上げない（state/ は .gitignore）。data/ に置くのは記事名・広告名・クリック率の記録だけ。
"""
import base64
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import autopilot  # noqa: E402
import affiliate  # noqa: E402
import guard  # noqa: E402

OUT = ROOT / "state" / "report"
LOG = ROOT / "logs" / "report.log"
SITE = json.loads((ROOT / "site.json").read_text(encoding="utf-8"))
BASE_URL = SITE["base_url"].rstrip("/") + "/"
HOST = urllib.parse.urlparse(BASE_URL).netloc
GSC_KEY = ROOT / ".secrets" / "gsc-service-account.json"
CTR_LOG = ROOT / "data" / "ctr-log.json"
AD_FOCUS = ROOT / "data" / "ad-focus.json"
CTR_PROMPT = ROOT / "scripts" / "ctr-prompt.md"
A8 = "https://media-console.a8.net"

# 検索で押されない記事の選び方（6-2）
CTR_MIN_IMPR = 50          # 直近28日の表示回数がこれ以上
CTR_MAX_POSITION = 20      # 平均順位がこれ以内（2ページ目まで）
CTR_MAX_PER_WEEK = 3
CTR_RECHECK_DAYS = 28      # 書き直しから4週間後に比べる
CTR_COOLDOWN_DAYS = 56     # 一度直した記事は8週間は直さない
EXPECTED_CTR = [(1, .28), (2, .15), (3, .11), (4, .08), (5, .06), (10, .03), (20, .012)]   # 順位ごとのクリック率の目安
BANNED = ["絶対", "必ず", "No.1", "NO.1", "ナンバーワン", "1位", "一位", "最安", "最強", "完全", "激安", "業界初", "損"]
FOCUS_TOP = 5              # 6-3: 閲覧数の多い記事の数


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def jload(p, dflt=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return dflt


def jsave(p, data):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def today():
    return date.fromisoformat(guard.today())


def num(s):
    try:
        return float(str(s).replace(",", "").replace("円", "").replace("%", "").strip() or 0)
    except ValueError:
        return 0.0


def articles():
    """slug → {title, genre, code}（A8 の記事の目印 id1 からも引けるように）。"""
    out = {}
    for a in autopilot.articles():
        out[a["_slug"]] = {"title": a.get("title", a["_slug"]), "genre": a["_genre"], "date": a.get("date", ""),
                           "code": affiliate.article_code(a["_slug"])}
    return out


def slug_of_path(path):
    s = path.strip("/").removesuffix(".html").split("/")[-1]
    return s or "（トップ）"


# ---------------------------------------------------------------------------
# A8
# ---------------------------------------------------------------------------
def tables(page):
    return page.evaluate("""() => [...document.querySelectorAll('table')].map(t => [...t.rows].map(r =>
        [...r.cells].map(c => c.innerText.trim().replace(/\\s+/g, ' '))))""")


def a8_collect():
    if not autopilot.a8_logged_in():
        return {"error": "A8 にログインできませんでした（理由は STATUS.md）"}
    from playwright.sync_api import sync_playwright
    from a8_browser import CDP_URL
    res = {"programs": {}, "confirmed": [], "params": []}
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP_URL, timeout=60000)
        ctx = b.contexts[0]
        page = ctx.new_page()
        try:
            # プログラム別（サマリ・今月）。20件ずつなので 100件 表示に切り替える
            page.goto(f"{A8}/report/program/summary", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            try:
                page.locator('select[aria-label="表示件数"]').first.select_option("100", timeout=5000)
                page.wait_for_timeout(3000)
            except Exception:
                pass
            t = tables(page)
            for row in (t[0] if t else [])[2:]:
                m = re.search(r"(s\d{14})\s*$", row[0])
                if not m or len(row) < 11:
                    continue
                res["programs"][m.group(1)] = {
                    "name": row[0][: m.start()].strip(), "prev2": num(row[1]), "prev": num(row[2]), "month": num(row[3]),
                    "imp": num(row[4]), "click": num(row[5]), "count": num(row[7]), "amount": num(row[8]), "pending": num(row[10])}
            # 確定（承認）とパラメータ計測（記事別）は、表の形がまだ見られていない（成果が0件）ので、行をそのまま残す
            for key, path in (("confirmed", "/report/confirmed/program"), ("params", "/report/parameter")):
                page.goto(A8 + path, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                rows = [r for tb in tables(page) for r in tb if r and any(re.search(r"\d", c) for c in r)]
                heads = [r for tb in tables(page) for r in tb[:2]]
                res[key] = {"head": heads, "rows": rows}
        finally:
            page.close()
    return res


# ---------------------------------------------------------------------------
# 閲覧数（Cloudflare の D1・maf-blog-stats。ブログの門番が数える）
# ---------------------------------------------------------------------------
STATS_DB = "maf-blog-stats"


def d1(sql):
    rc, out = guard.wrangler("d1", "execute", STATS_DB, "--remote", "--json", "--command", sql, check=False)
    shutil_rm()
    if rc != 0:
        raise RuntimeError(out.strip()[-300:])
    return json.loads(out[out.index("["):])[0]["results"]


def shutil_rm():
    import shutil
    shutil.rmtree(guard.WORKER_DIR / ".wrangler", ignore_errors=True)


def views_collect(day):
    """day（1日分）の閲覧数: 記事別・参照元のホスト別・utm_source 別。"""
    rows = d1(f"SELECT path, ref, src, n FROM pv WHERE day = '{day}'")
    pages, refs, utm = defaultdict(lambda: {"views": 0}), defaultdict(int), {}
    for r in rows:
        pages[r["path"]]["views"] += r["n"]
        refs[r["ref"] or "（直接・不明）"] += r["n"]
        if r["src"]:
            k = f"utm:{day}:{r['src']}:{r['path']}"
            utm[k] = utm.get(k, 0) + r["n"]
    return {"pages": dict(pages), "referers": dict(refs), "utm": utm}


# ---------------------------------------------------------------------------
# Search Console（サービスアカウント）
# ---------------------------------------------------------------------------
_gsc_token = {}


def gsc_token():
    if _gsc_token.get("exp", 0) > time.time() + 60:
        return _gsc_token["tok"]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = json.loads(GSC_KEY.read_text(encoding="utf-8"))
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    now = int(time.time())
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claim = b64(json.dumps({"iss": key["client_email"], "scope": "https://www.googleapis.com/auth/webmasters.readonly",
                            "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}).encode())
    pk = serialization.load_pem_private_key(key["private_key"].encode(), None)
    sig = b64(pk.sign(f"{head}.{claim}".encode(), padding.PKCS1v15(), hashes.SHA256()))
    data = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": f"{head}.{claim}.{sig}"}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=data), timeout=30) as r:
        tok = json.loads(r.read().decode())["access_token"]
    _gsc_token.update(tok=tok, exp=now + 3500)
    return tok


def gsc_query(start, end, dims, row_limit=1000, page=None):
    body = {"startDate": str(start), "endDate": str(end), "dimensions": dims, "rowLimit": row_limit}
    if page:
        body["dimensionFilterGroups"] = [{"filters": [{"dimension": "page", "operator": "equals", "expression": page}]}]
    url = ("https://www.googleapis.com/webmasters/v3/sites/" + urllib.parse.quote(BASE_URL, safe="") +
           "/searchAnalytics/query")
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {gsc_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            rows = json.loads(r.read().decode()).get("rows", [])
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Search Console {e.code}: {e.read().decode()[:300]}")
    return [{"keys": r["keys"], "clicks": r["clicks"], "impr": r["impressions"], "ctr": r["ctr"], "pos": r["position"]}
            for r in rows]


def gsc_collect(day):
    end = day - timedelta(days=2)         # Search Console は2〜3日遅れ
    return {"end": str(end),
            "pages_7d": gsc_query(end - timedelta(days=6), end, ["page"]),
            "pages_28d": gsc_query(end - timedelta(days=27), end, ["page"]),
            "queries_28d": gsc_query(end - timedelta(days=27), end, ["page", "query"], 2000)}


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------
def collect():
    day = today()
    snap = {"date": str(day), "at": datetime.now().strftime("%H:%M")}
    try:
        snap["a8"] = a8_collect()
        log(f"A8: プログラム {len(snap['a8'].get('programs', {}))} 件" + (f"（{snap['a8']['error']}）" if snap["a8"].get("error") else ""))
    except Exception as e:
        snap["a8"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        log(f"A8 の取り込みに失敗: {snap['a8']['error']}")
    yday = day - timedelta(days=1)
    try:
        snap["views"] = {"date": str(yday), **views_collect(yday)}
        n = sum(v["views"] for v in snap["views"]["pages"].values())
        log(f"閲覧数（{yday}）: {n} 回")
    except Exception as e:
        snap["views"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        log(f"閲覧数の取り込みに失敗: {snap['views']['error']}")
    if GSC_KEY.exists():
        try:
            snap["gsc"] = gsc_collect(day)
            log(f"Search Console: 記事 {len(snap['gsc']['pages_28d'])} 件（28日）")
        except Exception as e:
            snap["gsc"] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
            log(f"Search Console の取り込みに失敗: {snap['gsc']['error']}")
    else:
        snap["gsc"] = {"error": "鍵がありません（.secrets/gsc-service-account.json）"}
        log("Search Console: 鍵がないため飛ばしました")
    jsave(OUT / f"{day}.json", snap)
    return 0


def snaps(start, end):
    """start〜end（両端を含む）の取り込み（日付 → 中身）。"""
    out = {}
    d = start
    while d <= end:
        s = jload(OUT / f"{d}.json")
        if s:
            out[d] = s
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# 週の成績表
# ---------------------------------------------------------------------------
def week_views(start, end):
    """閲覧数（views の date が start〜end の分）を記事別・参照元別・utm 別に足す。"""
    pages, refs, utm, days = defaultdict(int), defaultdict(int), defaultdict(int), 0
    for s in snaps(start + timedelta(days=1), end + timedelta(days=1)).values():   # views は前日分
        v = s.get("views") or {}
        if v.get("error") or not v.get("date") or not (str(start) <= v["date"] <= str(end)):
            continue
        days += 1
        for path, x in v.get("pages", {}).items():
            pages[slug_of_path(path)] += x["views"]
        for h, n in v.get("referers", {}).items():
            refs[h] += n
        for k, n in (v.get("utm") or {}).items():
            if k.startswith("utm:"):
                utm[k.split(":")[2]] += n
    return pages, refs, utm, days


def a8_week(start, end):
    """プログラム別のクリック・発生件数・発生金額の1週間の増え方（今月の累計の毎日の差）。足りなければ今月の累計。"""
    ss = snaps(start, end + timedelta(days=1))
    ss = {d: s for d, s in ss.items() if (s.get("a8") or {}).get("programs")}
    if not ss:
        return {}, "なし"
    days = sorted(ss)
    if len(days) < 2:
        return ss[days[-1]]["a8"]["programs"], "今月の累計"
    total = defaultdict(lambda: {"click": 0, "count": 0, "amount": 0, "name": ""})
    for a, b in zip(days, days[1:]):
        pa, pb = ss[a]["a8"]["programs"], ss[b]["a8"]["programs"]
        for pid, x in pb.items():
            prev = pa.get(pid, {}) if a.month == b.month else {}
            t = total[pid]
            t["name"] = x["name"]
            for k in ("click", "count", "amount"):
                t[k] += max(0, x[k] - prev.get(k, 0))
    return dict(total), f"{days[0]:%m/%d}〜{days[-1]:%m/%d} の増え方"


def genre_of_slug(arts, slug):
    g = (arts.get(slug) or {}).get("genre", "")
    return autopilot.GENRES.get(g, {}).get("name", g) or "その他"


def weekly(dry=False):
    day = today()
    start, end = day - timedelta(days=7), day - timedelta(days=1)
    arts = articles()
    title = lambda s: (arts.get(s) or {}).get("title", s)
    L = [f"先週（{start:%m/%d}〜{end:%m/%d}）の成績表です。", ""]

    pages, refs, utm, vdays = week_views(start, end)
    ppages, _, _, pdays = week_views(start - timedelta(days=7), end - timedelta(days=7))
    L.append(f"■ 閲覧数の多い記事 トップ5（合計 {sum(pages.values())} 回・{vdays} 日分）")
    top = [(s, n) for s, n in sorted(pages.items(), key=lambda x: -x[1]) if s in arts][:5]
    L += [f"  {i}. {title(s)}　{n} 回" for i, (s, n) in enumerate(top, 1)] or ["  データなし（閲覧数は 2026-10-17 から数え始めました）"]

    progs, how = a8_week(start, end)
    L += ["", f"■ クリック・発生の多い広告 トップ5（A8・{how}）"]
    rank = sorted(progs.values(), key=lambda x: (-x["amount"], -x["count"], -x["click"]))
    rank = [x for x in rank if x["click"] or x["count"]][:5]
    L += [f"  {i}. {x['name'][:40]}　クリック {int(x['click'])}・発生 {int(x['count'])} 件・{int(x['amount']):,} 円"
          for i, x in enumerate(rank, 1)] or ["  クリック・発生なし"]
    last = snaps(day - timedelta(days=1), day)
    last = last[max(last)] if last else {}
    pend = sum(x.get("pending", 0) for x in ((last.get("a8") or {}).get("programs") or {}).values())
    conf_rows = ((last.get("a8") or {}).get("confirmed") or {}).get("rows", [])
    L.append(f"  未確定の報酬（累計）: {int(pend):,} 円・承認（確定）の表: {len(conf_rows)} 行")
    params = ((last.get("a8") or {}).get("params") or {}).get("rows", [])
    by_code = {a["code"]: s for s, a in arts.items()}
    prow = [(by_code[c], r) for r in params for c in r if c in by_code]
    if prow:
        L.append("  記事別（パラメータ計測・今月）:")
        L += [f"   ・{title(s)[:30]}: " + " / ".join(c for c in r if c not in by_code) for s, r in prow[:8]]

    L += ["", "■ X・note から来た人数"]
    def ref_sum(*hosts):
        return sum(n for h, n in refs.items() if any(h == x or h.endswith("." + x) for x in hosts))
    L.append(f"  X: {utm.get('x', 0)} 人（リンクの印 utm_source=x）・参照元が X の閲覧 {ref_sum('t.co', 'x.com', 'twitter.com')} 回")
    L.append(f"  note: {utm.get('note', 0)} 人（utm_source=note）・参照元が note の閲覧 {ref_sum('note.com')} 回")
    L.append(f"  検索: Google {ref_sum('google.com', 'google.co.jp', 'www.google.com', 'www.google.co.jp')} 回・Yahoo {ref_sum('search.yahoo.co.jp')} 回")

    L += ["", "■ 伸びているジャンル（閲覧数・先週と前の週）"]
    if pdays:
        g_now, g_prev = defaultdict(int), defaultdict(int)
        for s, n in pages.items():
            g_now[genre_of_slug(arts, s)] += n
        for s, n in ppages.items():
            g_prev[genre_of_slug(arts, s)] += n
        grow = sorted(g_now, key=lambda g: -(g_now[g] - g_prev.get(g, 0)))[:3]
        L += [f"  {g}: {g_prev.get(g, 0)} → {g_now[g]} 回" for g in grow if g_now[g]] or ["  まだ差が出ていません"]
    else:
        L.append("  比べる前の週のデータがまだありません（2週目から出ます）")

    gsc = latest_gsc()
    L += ["", "■ Google 検索（Search Console・直近7日）"]
    if gsc:
        rows = sorted(gsc["pages_7d"], key=lambda r: -r["impr"])[:5]
        L.append(f"  表示 {int(sum(r['impr'] for r in gsc['pages_7d']))} 回・クリック {int(sum(r['clicks'] for r in gsc['pages_7d']))} 回")
        L += [f"  ・{title(slug_of_path(urllib.parse.urlparse(r['keys'][0]).path))[:34]}　表示 {int(r['impr'])}・クリック {int(r['clicks'])}・順位 {r['pos']:.1f}"
              for r in rows]
    else:
        L.append("  今は取り込んでいません（Search Console は外しています）")

    clog = jload(CTR_LOG, [])
    L += ["", "■ 検索結果のタイトル・説明文の書き直し"]
    newf = [c for c in clog if c["date"] >= str(start)]
    L += [f"  今週直した: {title(c['slug'])[:30]}（表示 {c['before']['impr']}・クリック率 {c['before']['ctr']:.1%}）" for c in newf] or ["  今週直した記事はありません"]
    L += [f"  4週間後の結果: {c['new_title'][:28]}　クリック率 {c['before']['ctr']:.1%} → {c['after']['ctr']:.1%}"
          f"（表示 {c['before']['impr']} → {c['after']['impr']}）" for c in clog if c.get("after") and c["after"].get("reported") == str(day)]

    focus = jload(AD_FOCUS, {})
    if focus.get("slugs"):
        L += ["", "■ 報酬の高い広告を寄せた、読まれている記事", *[f"  ・{title(s)[:40]}" for s in focus["slugs"]]]
    body = "\n".join(L) + "\n\n（ブログ工場・毎週月曜の朝に自動で送っています。数字の元: A8・ブログの閲覧数（Cloudflare）・X/note のリンクの印）"
    subject = f"【週報】先週の成績表（{start:%m/%d}〜{end:%m/%d}）"
    if dry:
        print(subject + "\n\n" + body)
        return 0
    ok = guard.notify(subject, body)
    jsave(ROOT / "state" / "weekly-report.json", {"date": str(day), "sent": ok, "subject": subject})
    log(f"成績表: {'送りました' if ok else '送れませんでした'}")
    ad_focus(top, arts)
    return 0 if ok else 1


def ad_focus(top, arts):
    """6-3: 閲覧数の多い記事を data/ad-focus.json に（build がテーマに合う広告のうち報酬の高い順に並べる）。
    本文の印や affiliates: で広告を決めている記事は、見直し（refresh）の優先に入れて Claude に置き直させる。4週間に1回まで。"""
    slugs = [s for s, _ in top][:FOCUS_TOP]
    if not slugs:
        return
    old = jload(AD_FOCUS, {})
    hist = old.get("history", {})
    jsave(AD_FOCUS, {"_about": "閲覧数の多い記事（scripts/report.py が毎週更新）。affiliate.pick がテーマに合う広告のうち報酬の高いものを先に出す",
                     "updated": str(today()), "slugs": slugs, "history": hist})
    for s in slugs:
        if hist.get(s, "") > str(today() - timedelta(days=28)):
            continue
        path = autopilot.article_path(s)
        meta, body = autopilot.front_matter(path) if path else ({}, "")
        if "<!-- AFFILIATE:" in body or (meta.get("affiliates") or "").strip():
            autopilot.refresh_priority_add(s, "閲覧数が多い記事（先週トップ5）。テーマに合う提携中の広告のうち報酬の目安が高いものを優先して置く")
            hist[s] = str(today())
    data = jload(AD_FOCUS, {})
    data["history"] = hist
    jsave(AD_FOCUS, data)


# ---------------------------------------------------------------------------
# 6-2: 検索で見られているのに押されない記事のタイトルと説明文を直す
# ---------------------------------------------------------------------------
def expected_ctr(pos):
    for p, c in EXPECTED_CTR:
        if pos <= p:
            return c
    return 0.005


def latest_gsc():
    for d in sorted(snaps(today() - timedelta(days=7), today()), reverse=True):
        g = snaps(d, d)[d].get("gsc") or {}
        if "pages_28d" in g:
            return g
    return None


def ctr_candidates(gsc):
    clog = jload(CTR_LOG, [])
    recent = {c["slug"] for c in clog if c["date"] > str(today() - timedelta(days=CTR_COOLDOWN_DAYS))}
    arts = articles()
    out = []
    for r in gsc["pages_28d"]:
        slug = slug_of_path(urllib.parse.urlparse(r["keys"][0]).path)
        if slug not in arts or slug in recent:
            continue
        if r["impr"] >= CTR_MIN_IMPR and r["pos"] <= CTR_MAX_POSITION and r["ctr"] < expected_ctr(r["pos"]) * 0.6:
            out.append((slug, r))
    return sorted(out, key=lambda x: -x[1]["impr"])[:CTR_MAX_PER_WEEK]


def ask_claude(prompt):
    from claude_env import claude_env
    import shutil
    claude = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
    r = subprocess.run([claude, "-p", prompt, "--output-format", "text"], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=10 * 60, env=claude_env())
    return r.returncode, r.stdout + ("\n" + r.stderr if r.returncode else "")


def check_rewrite(d, old_title):
    t, desc = d.get("title", "").strip(), d.get("description", "").strip()
    if not (20 <= len(t) <= 45):
        return f"タイトルが {len(t)} 字（28〜40字に）"
    if not (60 <= len(desc) <= 130):
        return f"説明文が {len(desc)} 字（80〜120字に）"
    bad = [w for w in BANNED if w in t + desc]
    if bad:
        return f"使わない言葉: {'・'.join(bad)}"
    if t == old_title:
        return "タイトルが同じ"
    return ""


def ctr_fix(dry=False):
    gsc = latest_gsc()
    if not gsc:
        log("検索の直し: Search Console のデータがないため飛ばしました")
        return 0
    ctr_compare(gsc)
    cands = ctr_candidates(gsc)
    if not cands:
        log(f"検索の直し: 対象なし（表示 {CTR_MIN_IMPR} 回以上・{CTR_MAX_POSITION} 位以内・クリック率が目安の6割未満の記事がない）")
        return 0
    queries = defaultdict(list)
    for r in gsc["queries_28d"]:
        queries[r["keys"][0]].append((r["impr"], r["keys"][1]))
    clog = jload(CTR_LOG, [])
    changed = []
    for slug, r in cands:
        path = autopilot.article_path(slug)
        meta, body = autopilot.front_matter(path)
        heads = re.findall(r"^##\s+(.+)$", body, re.M)[:8]
        qs = [q for _, q in sorted(queries.get(r["keys"][0], []), reverse=True)[:10]]
        prompt = (CTR_PROMPT.read_text(encoding="utf-8")
                  .replace("{TITLE}", meta.get("title", "")).replace("{DESCRIPTION}", meta.get("description", ""))
                  .replace("{HEADINGS}", " / ".join(heads)).replace("{IMPR}", str(int(r["impr"])))
                  .replace("{CLICKS}", str(int(r["clicks"]))).replace("{CTR}", f"{r['ctr']:.1%}")
                  .replace("{POSITION}", f"{r['pos']:.1f}").replace("{QUERIES}", "、".join(qs) or "（なし）"))
        new, why = None, ""
        for _ in range(3):
            rc, out = ask_claude(prompt + (f"\n\n前回の案はだめでした（{why}）。直して返してください。" if why else ""))
            if rc != 0:
                why = "Claude を動かせませんでした: " + out.strip()[-200:]
                if guard_limit(out):
                    log("検索の直し: Claude の使用量の上限に当たったため中断")
                    return 1
                continue
            m = re.search(r"\{.*\}", out, re.S)
            try:
                d = json.loads(m.group(0)) if m else {}
            except Exception:
                d = {}
            why = check_rewrite(d, meta.get("title", "")) if d else "JSON がありません"
            if not why:
                new = d
                break
        if not new:
            log(f"検索の直し: {slug} は案が条件を満たさず見送り（{why}）")
            continue
        log(f"検索の直し: {slug}\n  前: {meta.get('title')}\n  後: {new['title']}\n  理由: {new.get('reason', '')}")
        if dry:
            continue
        autopilot.set_meta_line(path, "title", new["title"])
        autopilot.set_meta_line(path, "description", new["description"])
        clog.append({"date": str(today()), "slug": slug, "old_title": meta.get("title", ""), "new_title": new["title"],
                     "old_description": meta.get("description", ""), "new_description": new["description"],
                     "reason": new.get("reason", ""), "queries": qs[:5],
                     "before": {"impr": int(r["impr"]), "clicks": int(r["clicks"]), "ctr": round(r["ctr"], 4),
                                "pos": round(r["pos"], 1), "period": f"{gsc['end']} までの28日"}})
        changed.append(slug)
    if changed:
        jsave(CTR_LOG, clog)
        rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "publish.py"), "-m", f"ctr: タイトルと説明文の書き直し {','.join(changed)}"],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace").returncode
        log(f"検索の直し: {len(changed)} 本を公開（終了コード {rc}）")
    return 0


def guard_limit(out):
    import importlib
    ag = importlib.import_module("auto-generate")
    return ag.limit_hit(out, 1, "ctr-fix")


def ctr_compare(gsc):
    """書き直しから4週間たった記事の、書き直し後28日のクリック率を記録する（成績表に載る）。"""
    clog = jload(CTR_LOG, [])
    changed = False
    for c in clog:
        if c.get("after"):
            continue
        d0 = date.fromisoformat(c["date"])
        if d0 + timedelta(days=CTR_RECHECK_DAYS) > date.fromisoformat(gsc["end"]):
            continue
        try:
            rows = gsc_query(d0 + timedelta(days=1), d0 + timedelta(days=CTR_RECHECK_DAYS), ["page"], page=BASE_URL + c["slug"])
        except Exception as e:
            log(f"4週間後の比較に失敗: {c['slug']} {e}")
            continue
        r = rows[0] if rows else {"impr": 0, "clicks": 0, "ctr": 0, "pos": 0}
        c["after"] = {"impr": int(r["impr"]), "clicks": int(r["clicks"]), "ctr": round(r["ctr"], 4), "pos": round(r["pos"], 1),
                      "reported": str(today())}
        changed = True
        log(f"4週間後: {c['slug']} クリック率 {c['before']['ctr']:.1%} → {c['after']['ctr']:.1%}")
    if changed:
        jsave(CTR_LOG, clog)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    cmd = a[0] if a else "collect"
    if cmd == "collect":
        return collect()
    if cmd == "weekly":
        return weekly("--dry" in a)
    if cmd == "ctr-fix":
        return ctr_fix("--dry" in a)
    if cmd == "show":
        print(json.dumps(jload(OUT / f"{a[1] if len(a) > 1 else today()}.json"), ensure_ascii=False, indent=1)[:6000])
        return 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
