#!/usr/bin/env python3
"""
自動化いきもの診断（ブログの /shindan/）。12問に答えると、その人の「自動化のクセ」に合う実在の生き物（約100種）が出る、お遊びの診断。

  ページは build.py のたびに作る（Python 標準ライブラリだけ。Cloudflare のビルドでも動く）:
    /shindan/            … 入口と質問（回答の計算はブラウザの中だけ）
    /shindan/r/<id>.html … 生き物ごとの結果ページ（?a=回答 があれば、その人の面倒くさがり度・多い面倒を出す）
    /shindan/zukan.html  … 図鑑（出会った生き物は、その人のブラウザの localStorage にだけ記録して色付きで出す）

  PC で使うもの:
    python scripts/shindan.py merge     … state/shindan-parts/*.json（型ごとの生き物）と写真の記録 → data/shindan-species.json
    python scripts/shindan.py rakuten   … 「楽天で可」の自動化に、楽天の商品を1つずつ用意（data/shindan-rakuten.json）
    python scripts/shindan.py share     … 生き物ごとのシェア画像（1200×630）を static/shindan/share/ に作る（Playwright）
    python scripts/shindan.py sim [N]   … 回答をランダムに N 回（既定 1万回）試し、出る割合を data/shindan-sim.json に（node）
    python scripts/shindan.py deploy    … 数える係（workers/shindan・診断専用の Worker と D1 maf-shindan）を設置し、全種の行を入れる
    python scripts/shindan.py licenses  … 写真のライセンスの一覧

■ 型（3つの物差し）: 自分でやる派 S / 任せる派 M、計画派 P / その場派 I、慎重派 C / 新しもの好き派 N → 8つの型
  型の中は、面倒くさがり度（low/mid/high）× 面倒が多い場面（work/life）× ひとりで/みんなで（solo/team）の12マスに1種ずつ。
  面倒くさがり度がとても高い人には、型によってレア枠の種が出る。
■ レア度: 出た数を Cloudflare の診断専用の D1 で数える（送るのは生き物の id だけ。個人の情報は集めない）。
■ 広告: 「ハマる自動化」は、自動化ランキングで「提携済み」（A8）・「楽天で可」（楽天の商品）のものだけ。PR表記つき。
"""
import datetime
import html
import json
import random
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SPECIES = DATA / "shindan-species.json"
RK = DATA / "shindan-rakuten.json"
SIM = DATA / "shindan-sim.json"
TPL = ROOT / "templates" / "shindan"
STATIC = ROOT / "static" / "shindan"
API = "https://maf-shindan.asoutobata.workers.dev"
SLUG = "shindan"  # A8 のパラメータ計測の目印（affiliate.article_code）
TITLE = "自動化いきもの診断"
FONT = "https://fonts.googleapis.com/css2?family=Zen+Maru+Gothic:wght@500;700&display=swap"

TYPES = {
    "SPC": {"name": "準備ためこみ屋", "axes": ["自分でやる派", "計画派", "慎重派"], "self": True, "new": False,
            "desc": "面倒は、来る前に片付けておきたい。ひな形・控え・買い置きをこつこつためて、いざという時にあわてないタイプです。",
            "good": "定期便・ひな形・控えのような「先回りの自動化」"},
    "SPN": {"name": "コツコツ建築家", "axes": ["自分でやる派", "計画派", "新しもの好き派"], "self": True, "new": True,
            "desc": "新しい道具を見つけたら、自分の仕組みに一つずつ組み込んでいく。気づけば仕事も家も、自分専用の仕組みが建っているタイプです。",
            "good": "アプリ同士をつなぐ・決まった時刻に動かす「仕組みづくり」"},
    "SIC": {"name": "マイペース職人", "axes": ["自分でやる派", "その場派", "慎重派"], "self": True, "new": False,
            "desc": "慣れたやり方を、自分のペースで確実に。急に全部は変えないけれど、一度なじんだ道具は長く大事に使うタイプです。",
            "good": "毎日の手順を一つだけ置きかえる「小さな自動化」"},
    "SIN": {"name": "器用なひらめき屋", "axes": ["自分でやる派", "その場派", "新しもの好き派"], "self": True, "new": True,
            "desc": "その場にあるものを、ひらめきで道具に変える。新しいアプリもすぐ試して、思いがけない使い方を見つけるタイプです。",
            "good": "AI やスマート家電を使った「その場の工夫」"},
    "MPC": {"name": "頼れる番頭", "axes": ["任せる派", "計画派", "慎重派"], "self": False, "new": False,
            "desc": "任せるところは任せて、段取りと確認はしっかり。人やサービスを上手に使いながら、全体をきちんと回すタイプです。",
            "good": "家事代行・宅配・会計ソフトのような「任せて確かめる自動化」"},
    "MPN": {"name": "チームの司令塔", "axes": ["任せる派", "計画派", "新しもの好き派"], "self": False, "new": True,
            "desc": "人と道具の得意を見きわめて、全体を組み立てる。新しいサービスも取り入れながら、みんなが楽になる流れを作るタイプです。",
            "good": "予約・日程調整・共有のような「みんなで使う自動化」"},
    "MIC": {"name": "気まぐれ省エネ派", "axes": ["任せる派", "その場派", "慎重派"], "self": False, "new": False,
            "desc": "なるべく力を使わずに、慣れた便利なものに頼る。がんばらないことを、上手に続けられるタイプです。",
            "good": "宅配・定期便・ロボット家電のような「置いておくだけの自動化」"},
    "MIN": {"name": "近道の天才", "axes": ["任せる派", "その場派", "新しもの好き派"], "self": False, "new": True,
            "desc": "もっと楽な道はないかと、いつも近道を探している。人の力や新しい仕組みに上手に乗って、最短で片付けるタイプです。",
            "good": "AI やサービスに丸ごと任せる「近道の自動化」"},
}
FACES = ["ラッコ", "シベリアシマリス", "ハシビロコウ", "コアラ", "カピバラ"]  # 入口とシェア画像に並べる顔
TYPE_ORDER = ["SPC", "SPN", "SIC", "SIN", "MPC", "MPN", "MIC", "MIN"]

# 質問。A: 自分でやる(+1)/任せる(-1)・B: 計画(+1)/その場(-1)・C: 慎重(+1)/新しもの好き(-1)・
# W: 仕事(+1)/暮らし(-1)・S: ひとり(+1)/みんな(-1)・lz: 面倒くさがり(1)・m: 自動化ランキングの順位（その人に多い面倒）
QUESTIONS = [
    {"q": "大掃除の日。あなたは？", "opts": [
        {"t": "自分で手順を決めて、一気に片付ける", "A": 1},
        {"t": "換気扇まわりだけは、プロにお願いしたい", "A": -1, "m": 22},
        {"t": "自分でやるけど、気づけばお昼すぎまで始まらない", "A": 1, "lz": 1},
        {"t": "誰かがやってくれるのを、そっと待つ", "A": -1, "lz": 1}]},
    {"q": "旅行の準備はどうする？", "opts": [
        {"t": "1週間前に持ち物リストを作る", "B": 1},
        {"t": "前の夜に、思い出しながら詰める", "B": -1},
        {"t": "前回のリストを、そのまま使い回す", "B": 1, "lz": 1},
        {"t": "足りなければ、現地で買えばいい", "B": -1, "lz": 1}]},
    {"q": "話題の新しいアプリ。どうする？", "opts": [
        {"t": "とりあえず入れて、触ってみる", "C": -1},
        {"t": "評判がそろってから入れる", "C": 1},
        {"t": "今ので困っていないので、そのまま", "C": 1, "lz": 1},
        {"t": "便利と聞けばすぐ入れる。設定はあとで", "C": -1, "lz": 1}]},
    {"q": "いちばん「代わりにやってほしい」のは？", "opts": [
        {"t": "会議の議事録", "W": 1, "S": 1, "m": 8},
        {"t": "みんなの日程調整", "W": 1, "S": -1, "m": 42},
        {"t": "毎日の皿洗い", "W": -1, "S": 1, "m": 6},
        {"t": "家族の予定合わせ", "W": -1, "S": -1, "m": 60}]},
    {"q": "毎月くり返す、集計や書類づくり。", "opts": [
        {"t": "自分でひな形を作って、早く終わらせる", "A": 1, "m": 16},
        {"t": "ソフトや詳しい人の力を借りる", "A": -1, "m": 2},
        {"t": "自分でやるけど、締め切り直前まで手をつけない", "A": 1, "lz": 1, "m": 2},
        {"t": "できれば、丸ごと誰かにお願いしたい", "A": -1, "lz": 1, "m": 9}]},
    {"q": "晩ごはんの決め方は？", "opts": [
        {"t": "週末に1週間分の献立を決める", "B": 1, "m": 3},
        {"t": "冷蔵庫を開けてから考える", "B": -1, "m": 3},
        {"t": "決まった定番を、順番にくり返す", "B": 1, "lz": 1, "m": 3},
        {"t": "その日の気分で、外食や宅配にすることも", "B": -1, "lz": 1, "m": 4}]},
    {"q": "家電を買いかえるなら？", "opts": [
        {"t": "長く売れている定番のモデル", "C": 1},
        {"t": "新しい機能がついた最新のモデル", "C": -1, "m": 5},
        {"t": "選ぶのが面倒なので、今と同じメーカーの後継機", "C": 1, "lz": 1},
        {"t": "スマホから動かせて、勝手に動いてくれるもの", "C": -1, "lz": 1, "m": 74}]},
    {"q": "うれしい時間の使い方は？", "opts": [
        {"t": "集中して、自分の仕事を進める", "W": 1, "S": 1},
        {"t": "みんなで話し合って、いいものを作る", "W": 1, "S": -1},
        {"t": "ひとりで、のんびり趣味の時間", "W": -1, "S": 1},
        {"t": "家族や友だちと出かける", "W": -1, "S": -1}]},
    {"q": "スマホやパソコンの設定で困ったら？", "opts": [
        {"t": "自分で調べて直す", "A": 1},
        {"t": "詳しい人に聞いて、さっと教えてもらう", "A": -1},
        {"t": "自分でなんとかする。直るまで放っておくこともある", "A": 1, "lz": 1, "m": 79},
        {"t": "詳しい人に「代わりにやって」とお願いする", "A": -1, "lz": 1}]},
    {"q": "休みの日は？", "opts": [
        {"t": "前の日までに予定を決めておく", "B": 1},
        {"t": "起きてから、天気と気分で決める", "B": -1},
        {"t": "やることリストを作るけど、半分は来週に回る", "B": 1, "lz": 1},
        {"t": "気づくと夕方になっている", "B": -1, "lz": 1}]},
    {"q": "仕事や家のやり方を「変えてみよう」と言われたら？", "opts": [
        {"t": "まず小さく試して、問題がないか確かめる", "C": 1},
        {"t": "おもしろそう。すぐやってみる", "C": -1},
        {"t": "今ので困っていないなら、そのままがいい", "C": 1, "lz": 1},
        {"t": "楽になるなら大歓迎。くわしい説明は省いてほしい", "C": -1, "lz": 1}]},
    {"q": "「助かった！」と言いたいのは？", "opts": [
        {"t": "メールの返信文を考えてくれる", "W": 1, "S": 1, "m": 14},
        {"t": "同じ質問に、自動で答えてくれる", "W": 1, "S": -1, "m": 64},
        {"t": "日用品が切れる前に届く", "W": -1, "S": 1, "m": 23},
        {"t": "習い事や送迎の調整が楽になる", "W": -1, "S": -1, "m": 96}]},
]
LAZY = {"low": 3, "mid": 5, "rare": 8, "base": 8, "step": round(88 / 9, 4)}  # 面倒くさがり（lz）の合計 0〜9
TIERS = {"gekirare": 0.4, "rare": 0.7}  # 出た割合（%）がこれ未満なら 激レア / レア（平均は1%）
MIN_TOTAL = 300  # 実際の結果がこの人数に届くまでは、ランダム1万回の割合でレア度を出す
FALLBACK = {"work": [8, 9, 14], "life": [3, 6, 5]}  # 回答に面倒の印が足りないとき
WORK_FIELDS = {"経理・事務", "会社員", "個人事業主", "飲食店", "美容室・サロン", "小売・ネットショップ", "建設・リフォーム",
               "医療・クリニック", "不動産・管理", "教育", "介護", "SNS・発信"}
DELEGATE_ADS = {"宅配食・ミールキット", "宅配食・調理家電", "家事代行", "宅配クリーニング", "宅配買取", "見守りサービス",
                "ファッションレンタル", "通信講座", "動画配信（VOD）", "音楽配信", "予約システム・電話自動応答", "予約システム"}
NEW_WORDS = ("AI", "スマート", "ロボット", "文字起こし", "ウェアラブル", "電子契約", "日程調整", "自動")
RAKUTEN_QUERIES = {5: "ロボット掃除機", 6: "食器洗い乾燥機 工事不要", 13: "ドラム式洗濯乾燥機", 23: "トイレットペーパー まとめ買い",
                   35: "スマートウォッチ 睡眠", 36: "防カビ くん煙剤 浴室", 57: "スマートスピーカー", 74: "スマートリモコン",
                   75: "スマートロック", 76: "自動給餌器", 85: "排水口 ネット", 93: "読書ノート", 98: "洗剤 詰め替え 大容量",
                   99: "自動水やり器 タイマー"}


def jload(p, default=None):
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def jsave(p, data):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def esc(s):
    return html.escape(str(s or ""), quote=True)


def species():
    return (jload(SPECIES, {}) or {}).get("species", [])


# ---------------------------------------------------------------------------
# 自動化ランキングと広告
# ---------------------------------------------------------------------------
def ranking():
    return jload(DATA / "jidouka-ranking.json", []) or []


def article_links():
    """自動化ランキングの順位 → ブログの記事（公開日が来たものだけ）。"""
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date().isoformat()
    done = (jload(DATA / "jidouka-progress.json", {}) or {}).get("done", {})
    return {int(k): f"/{v['slug']}.html" for k, v in done.items() if v.get("slug") and v.get("date", "9") <= today}


def a8_programs(item, ledger):
    """その困りごとに使える A8 の提携中の広告（scripts/autopilot.py の jidouka_programs と同じ決め方）。"""
    if item.get("広告の状況") != "提携済み":
        return []
    conf = jload(DATA / "jidouka-ads.json", {}) or {}
    genres_all = jload(DATA / "genres.json", {}) or {}
    daily = [g for g, c in genres_all.items() if isinstance(c, dict) and c.get("daily")]
    ad = item.get("広告のジャンル", "")
    gm, words = conf.get("genre_map", {}).get(ad, []), conf.get("words", {}).get(ad, [])
    out = []
    for k, v in ledger.items():
        if not (isinstance(v, dict) and v.get("url") and v.get("enabled") is not False):
            continue
        if v.get("genre") in gm or (v.get("genre") in daily and any(w in k + v.get("label", "") for w in words)):
            out.append(dict(v, name=k))
    return sorted(out, key=lambda p: -(p.get("priority") or 0))


def ad_for(item, ledger, rk):
    if item.get("広告の状況") == "提携済み":
        progs = a8_programs(item, ledger)
        return {"kind": "a8", "p": progs[0]} if progs else None
    if item.get("広告の状況") == "楽天で可":
        it = rk.get(str(item["順位"]))
        return {"kind": "rk", "p": it} if it else None
    return None


def pick_automations(type_code, scene, ledger, rk, n=3):
    """その型と場面に合う、広告のある自動化を n 個。"""
    t = TYPES[type_code]
    cands = []
    for it in ranking():
        ad = ad_for(it, ledger, rk)
        if not ad:
            continue
        work = it.get("分野") in WORK_FIELDS
        delegate = it.get("広告のジャンル") in DELEGATE_ADS
        new = any(w in it.get("自動化の方法", "") + it.get("道具の例", "") for w in NEW_WORDS)
        score = (4 if work == (scene == "work") else 0) + (2 if delegate != t["self"] else 0) \
            + (1 if new == t["new"] else 0) + it.get("自動化のしやすさ", 3) * 0.3 - it["順位"] * 0.01
        cands.append((score, it, ad))
    cands.sort(key=lambda x: -x[0])
    out, used = [], set()
    for score, it, ad in cands:
        keys = {it.get("広告のジャンル"), ad["p"].get("name") or ad["p"].get("url")}  # 同じ広告を2回出さない
        if keys & used:
            continue
        used |= keys
        out.append((it, ad))
        if len(out) == n:
            break
    return out


def render_ad(ad):
    import affiliate
    p = ad["p"]
    if ad["kind"] == "a8":
        note = p.get("cta_note") or p.get("catch") or ""
        box = (f'<div class="sd-ad"><p class="sd-ad-name">{esc(p.get("label") or p["name"])}</p>'
               + (f'<p class="sd-ad-catch">{esc(note)}</p>' if note else "")
               + f'<a class="sd-ad-btn" href="{esc(p["url"])}" target="_blank" rel="nofollow sponsored noopener">公式サイトを見る</a>'
               + affiliate._pixel(p).replace('class="aff-px"', 'class="sd-px"') + "</div>")
        return affiliate.tag_links(box, SLUG)
    img = f'<img src="{esc(p["image"])}" width="72" height="72" alt="" loading="lazy">' if p.get("image") else ""
    return (f'<div class="sd-ad"><div class="sd-ad-row">{img}<div><p class="sd-ad-name">{esc(p["name"])}</p>'
            f'<p class="sd-ad-catch">{esc(p.get("shop", ""))}（楽天市場）</p></div></div>'
            f'<a class="sd-ad-btn is-rk" href="{esc(p["url"])}" target="_blank" rel="nofollow sponsored noopener">楽天市場で見る</a></div>')


# ---------------------------------------------------------------------------
# ページ
# ---------------------------------------------------------------------------
def head(title, desc, url, image, extra=""):
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{esc(url)}">
<meta property="og:image" content="{esc(image)}">
<meta name="twitter:card" content="summary_large_image">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONT}">
<link rel="stylesheet" href="/shindan/app.css">
{extra}</head>
<body>
<header class="sd-header">
  <a class="sd-blog" href="/">MAF ブログ</a>
  <nav><a href="/shindan/">診断</a><a href="/shindan/zukan.html">図鑑</a></nav>
</header>
"""


FOOT = """<footer class="sd-footer">
  <p>楽しむための診断です。生き物の特徴は百科事典で確かめたものだけを書いています。</p>
  <p><a href="/shindan/">自動化いきもの診断</a> ・ <a href="/shindan/zukan.html">図鑑</a> ・ <a href="/">MAF ブログ</a></p>
</footer>
<script src="/shindan/data.js"></script>
<script src="/shindan/app.js"></script>
</body>
</html>
"""


def credit(ph):
    lic = esc(ph.get("license"))
    lic = f'<a href="{esc(ph["license_url"])}" target="_blank" rel="noopener">{lic}</a>' if ph.get("license_url") else lic
    return (f'写真：{esc(ph.get("author") or "不明")} / {lic} / '
            f'<a href="{esc(ph.get("page"))}" target="_blank" rel="noopener">Wikimedia Commons</a>（縮小・切り抜きして使用）')


def note_paid():
    st = jload(DATA / "note" / "ranking" / "state.json", {}) or {}
    return (st.get("url"), st.get("title")) if st.get("status") == "published" and st.get("url") else (None, None)


def index_page(sp, base):
    picks = [x for n in FACES for x in sp if x["name"] == n][:5] or sp[:5]
    faces = "".join(f'<img src="/shindan/img/{esc(s["id"])}-s.jpg" width="64" height="64" alt="{esc(s["name"])}">'
                    for s in picks)
    desc = f"12問に答えると、あなたの「自動化のクセ」に合う生き物が、実在する{len(sp)}種の中から出ます。面倒くさがり度や、ハマる自動化もわかります。"
    return head(f"{TITLE}｜あなたの自動化のクセは、どの生き物？", desc, f"{base}/shindan/", f"{base}/shindan/share/index.jpg") + f"""<main class="sd-wrap">
  <section id="sd-intro">
    <div class="sd-hero">
      <h1><small>12問・2分でわかる</small>{TITLE}</h1>
      <p>家事や仕事の「面倒」との付き合い方から、あなたに似た生き物を、実在する <b>{len(sp)}種</b> の中から選びます。</p>
      <div class="sd-faces">{faces}</div>
      <button class="sd-btn" id="sd-start" type="button">診断をはじめる</button>
    </div>
    <ul class="sd-points">
      <li>8つの型 × {len(sp)}種。レアな生き物もいます</li>
      <li>面倒くさがり度と、あなたにハマる自動化がわかります</li>
      <li>名前やメールアドレスは聞きません。回答はこのブラウザの中だけで計算します</li>
    </ul>
    <p class="sd-small">楽しむための診断です。心理検査ではありません。</p>
    <p><a class="sd-btn is-sub" href="/shindan/zukan.html">生き物図鑑を見る</a></p>
  </section>
  <section id="sd-quiz" class="sd-card" hidden></section>
</main>
""" + FOOT


def result_page(s, sp_by_id, base, ledger, rk, sim, links):
    t = TYPES[s["type"]]
    scene = "work" if s.get("rare") else (s.get("cell") or {}).get("scene", "life")
    url = f"{base}/shindan/r/{s['id']}.html"
    title = f"{s['name']}（{t['name']}）｜{TITLE}"
    desc = f"自動化いきもの診断で「{s['name']}」が出ました。{t['name']}：{s.get('catch', '')}"
    ph = s["photo"]
    facts = "".join(f'<li>{esc(f["text"])}<a href="{esc(f["source"])}" target="_blank" rel="noopener">出典</a></li>' for f in s["facts"])
    aru = "".join(f"<li>{esc(a)}</li>" for a in s["aruaru"])
    autos = pick_automations(s["type"], scene, ledger, rk)
    auto_html = "".join(
        f'<li><h3><small>{esc(it["困りごと"])}（自動化ランキング {it["順位"]}位）</small>{esc(it["自動化の方法"])}</h3>'
        + (f'<p><a href="{esc(links[it["順位"]])}">ブログでやり方を読む</a></p>' if it["順位"] in links else "")
        + render_ad(ad) + "</li>" for it, ad in autos)
    rel = []
    for it, _ in autos:
        if it["順位"] in links:
            rel.append(links[it["順位"]])
    rel += [u for _, u in sorted(links.items())]
    rel = list(dict.fromkeys(rel))[:3]
    titles = article_titles()
    rel_html = "".join(f'<li><a href="{esc(u)}">{esc(titles.get(u, u))}</a></li>' for u in rel)
    note_url, note_title = note_paid()
    note_html = (f'<a class="sd-note" href="{esc(note_url)}" target="_blank" rel="noopener"><b>{esc(note_title)}（note）</b>'
                 '暮らしと仕事の「面倒」100項目を、自動化しやすい順に並べた完全版です。</a>') if note_url else ""
    tweet = f"自動化いきもの診断で「{s['name']}（{t['name']}）」が出ました。"
    xurl = "https://x.com/intent/post?" + urllib.parse.urlencode({"text": tweet, "url": url, "hashtags": "自動化いきもの診断"})
    expected = (sim.get("share") or {}).get(s["id"], 0)
    sources = list(dict.fromkeys([f["source"] for f in s["facts"]] + s.get("sources", [])))
    pos = esc(s.get("pos") or "50% 50%")
    fallback = FALLBACK[scene]
    tags = {r["順位"]: r for r in ranking()}
    mendou_static = "".join(f'<li><span class="sd-mname">{esc(tags[m]["困りごと"])}</span>'
                            f'<span class="sd-mrank">自動化ランキング {m}位</span></li>' for m in fallback if m in tags)
    return head(title, desc, url, f"{base}/shindan/share/{s['id']}.jpg") + f"""<main class="sd-wrap" data-sd-result data-id="{esc(s['id'])}">
  <p class="sd-pr">※このページにはプロモーション（広告）が含まれています。</p>
  <section class="sd-result-top">
    <figure class="sd-photo"><img src="/shindan/img/{esc(s['id'])}.jpg" alt="{esc(s['name'])}" width="800" height="600" style="object-position:{pos}" fetchpriority="high"></figure>
    <p class="sd-credit">{credit(ph)}</p>
    <p class="sd-you">あなたの自動化のクセは…</p>
    <h1 class="sd-name">{esc(s['name'])}</h1>
    <p class="sd-type">{esc(t['name'])}{"　★レア枠" if s.get("rare") else ""}</p>
    <p class="sd-catch">{esc(s.get('catch', ''))}</p>
    <p class="sd-fun">楽しむための診断です。</p>
  </section>

  <section class="sd-card sd-stats">
    <div class="sd-rarity" id="sd-rarity" data-expected="{expected}">
      <p class="sd-label">レア度</p>
      <p class="sd-big"><span class="sd-share">…</span><span class="sd-tier">…</span></p>
      <p class="sd-rnote">集計を読みこんでいます。</p>
    </div>
    <div class="sd-lazy">
      <p class="sd-label">面倒くさがり度</p>
      <div class="sd-mine" hidden>
        <p class="sd-big" id="sd-pct">—</p>
        <div class="sd-meter"><span id="sd-pctbar"></span></div>
        <p class="sd-pctnote" id="sd-pctnote"></p>
      </div>
      <div class="sd-notmine"><p class="sd-pctnote">診断をすると、あなたの面倒くさがり度が出ます。</p></div>
    </div>
  </section>

  <section class="sd-card">
    <h2>{esc(s['name'])}の<span class="sd-em">本当の特徴</span></h2>
    <ul class="sd-facts">{facts}</ul>
    <p class="sd-like">{esc(s['likeness'])}</p>
  </section>

  <section class="sd-card">
    <h2>{esc(s['name'])}さんの<span class="sd-em">あるある</span></h2>
    <ul class="sd-aru">{aru}</ul>
  </section>

  <section class="sd-card">
    <h2>型：{esc(t['name'])}</h2>
    <ul class="sd-axes">{"".join(f"<li>{esc(a)}</li>" for a in t['axes'])}</ul>
    <p>{esc(t['desc'])}</p>
    <p class="sd-small">得意なのは、{esc(t['good'])}。</p>
  </section>

  <section class="sd-card">
    <h2>あなたに<span class="sd-em">多い面倒</span></h2>
    <ul class="sd-mendou" id="sd-mendou">{mendou_static}</ul>
    <p class="sd-small sd-notmine">※この生き物が出た人に多い面倒です。診断をすると、あなたの回答から出します。</p>
  </section>

  <section class="sd-card">
    <h2>{esc(t['name'])}に<span class="sd-em">ハマる自動化</span> 3つ</h2>
    <p class="sd-pr">※広告（A8.net・楽天市場）を含みます。</p>
    <ul class="sd-auto">{auto_html}</ul>
  </section>

  <section class="sd-card sd-share">
    <h2>結果をシェアする</h2>
    <a class="sd-btn sd-xbtn" id="sd-x" href="{esc(xurl)}" target="_blank" rel="noopener">X でシェアする</a>
    <a class="sd-btn is-sub" href="/shindan/">もう一度診断する</a>
    <a class="sd-btn is-sub" href="/shindan/zukan.html">生き物図鑑を見る</a>
  </section>

  <section class="sd-card">
    <h2>もっと自動化したい人へ</h2>
    {note_html}
    <ul class="sd-links">{rel_html}</ul>
  </section>

  <p class="sd-src">特徴の出典：{"、".join(f'<a href="{esc(u)}" target="_blank" rel="noopener">{esc(urllib.parse.unquote(u).split("/wiki/")[-1])}（Wikipedia）</a>' for u in sources)}</p>
</main>
""" + FOOT


_TITLES = {}


def article_titles():
    if not _TITLES:
        for md in (ROOT / "content" / "ja").glob("*.md"):
            m = re.search(r"^title:\s*(.+)$", md.read_text(encoding="utf-8"), re.M)
            if m:
                _TITLES[f"/{md.stem}.html"] = m.group(1).strip().strip('"').strip("'")
    return _TITLES


RARE_CLS = ' class="is-rare-sp"'


def zukan_page(sp, base):
    blocks = []
    for code in TYPE_ORDER:
        mine = [s for s in sp if s["type"] == code]
        if not mine:
            continue
        cards = "".join(
            f'<li data-id="{esc(s["id"])}"{RARE_CLS if s.get("rare") else ""}><a href="/shindan/r/{esc(s["id"])}.html">'
            f'<img src="/shindan/img/{esc(s["id"])}-s.jpg" width="320" height="320" alt="{esc(s["name"])}" loading="lazy" '
            f'style="object-position:{esc(s.get("pos") or "50% 50%")}"><span>{esc(s["name"])}</span></a></li>' for s in mine)
        blocks.append(f'<div class="sd-zhead"><h2>{esc(TYPES[code]["name"])}</h2><small>{len(mine)}種</small></div>'
                      f'<ul class="sd-grid">{cards}</ul>')
    desc = f"自動化いきもの診断に出てくる{len(sp)}種の生き物の図鑑。出会った生き物は色付きで表示されます（記録はこのブラウザの中だけ）。"
    return head(f"生き物図鑑｜{TITLE}", desc, f"{base}/shindan/zukan.html", f"{base}/shindan/share/index.jpg") + f"""<main class="sd-wrap">
  <section class="sd-card" id="sd-zukan">
    <h1 style="margin:0;font-size:1.4rem">生き物図鑑</h1>
    <p class="sd-small">出会った生き物：<b id="sd-metcount">0</b> / {len(sp)}種（★はレア枠）。出会った記録は、このブラウザの中にだけ残します（サーバーには送りません）。</p>
    {"".join(blocks)}
    <a class="sd-btn" href="/shindan/">診断して、生き物に出会う</a>
  </section>
</main>
""" + FOOT


def data_js(sp):
    cmap = {}
    for s in sp:
        c = s.get("cell") or {}
        key = "rare" if s.get("rare") else f"{c['lazy']}-{c['scene']}-{c['style']}"
        cmap.setdefault(s["type"], {})[key] = s["id"]
    rk = {r["順位"]: r for r in ranking()}
    links = article_links()
    used = {o["m"] for q in QUESTIONS for o in q["opts"] if o.get("m")} | {m for v in FALLBACK.values() for m in v}
    tags = {m: {"name": rk[m]["困りごと"], "rank": m, "url": links.get(m, "")} for m in sorted(used) if m in rk}
    d = {"api": API, "questions": QUESTIONS, "map": cmap, "lazy": LAZY, "tiers": TIERS, "minTotal": MIN_TOTAL,
         "tags": tags, "fallback": FALLBACK}
    return "window.SHINDAN = " + json.dumps(d, ensure_ascii=False, separators=(",", ":")) + ";\n"


def build(public, programs=None, base=""):  # programs は build.py と形をそろえるためだけ（台帳はここで読む）
    """build.py から呼ぶ。public/shindan/ にページを作り、サイトマップ用の (パス, 日付) を返す。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import affiliate
    sp = species()
    if not sp:
        return []
    ledger = jload(DATA / "affiliates.json", {}) or {}  # 台帳そのまま（{案件名: {...}}）
    rk = {k: v for k, v in (jload(RK, {}) or {}).items() if not k.startswith("_")}
    sim = jload(SIM, {}) or {}
    links = article_links()
    out = Path(public) / "shindan"
    (out / "r").mkdir(parents=True, exist_ok=True)
    for name in ("app.js", "app.css"):
        (out / name).write_text((TPL / name).read_text(encoding="utf-8"), encoding="utf-8")
    (out / "data.js").write_text(data_js(sp), encoding="utf-8")
    (out / "index.html").write_text(index_page(sp, base), encoding="utf-8")
    (out / "zukan.html").write_text(zukan_page(sp, base), encoding="utf-8")
    by_id = {s["id"]: s for s in sp}
    for s in sp:
        (out / "r" / f"{s['id']}.html").write_text(result_page(s, by_id, base, ledger, rk, sim, links), encoding="utf-8")
    day = (jload(SPECIES, {}) or {}).get("updated", "")
    print(f"診断: {len(sp)} 種のページ -> {out}")
    return [("shindan/", day), ("shindan/zukan.html", day)] + [(f"shindan/r/{s['id']}.html", day) for s in sp]


# ---------------------------------------------------------------------------
# PC で使うもの
# ---------------------------------------------------------------------------
def cmd_merge():
    parts = sorted((ROOT / "state" / "shindan-parts").glob("*.json"))
    sp, replaced = [], []
    for p in parts:
        d = jload(p)
        replaced += [dict(r, type=d["type"]) for r in d.get("replaced", [])]
        for s in d["species"]:
            cr = jload(ROOT / "state" / "shindan-photos" / f"{s['id']}.json")
            if not cr:
                raise SystemExit(f"写真の記録がありません: {s['id']}")
            s = {k: v for k, v in s.items() if k != "photo_file"}
            s["photo"] = {k: cr[k] for k in ("file", "commons", "page", "author", "license", "license_url")}
            s["sources"] = list(dict.fromkeys([f["source"] for f in s["facts"]] + s.get("sources", [])))
            sp.append(s)
    order = {c: i for i, c in enumerate(TYPE_ORDER)}
    sp.sort(key=lambda s: (order[s["type"]], s.get("rare", False)))
    jsave(SPECIES, {"_about": "自動化いきもの診断（/shindan/）の生き物。facts の quote は source（百科事典）の本文にある文。"
                              "photo は Wikimedia Commons の写真（PD・CC0・CC BY・CC BY-SA だけ）。scripts/shindan.py・scripts/shindan_tools.py",
                    "updated": datetime.date.today().isoformat(), "replaced": replaced, "species": sp})
    print(f"{len(sp)} 種を {SPECIES} に（入れ替え {len(replaced)} 件）")


def cmd_rakuten():
    import rakuten
    keys = jload(ROOT / ".secrets" / "rakuten.json")
    if not keys:
        raise SystemExit("楽天のキー（.secrets/rakuten.json）がありません")
    out = {"_about": "自動化いきもの診断の「ハマる自動化」に出す楽天の商品（自動化ランキングの順位ごと）。scripts/shindan.py rakuten"}
    for rank, q in RAKUTEN_QUERIES.items():
        c = rakuten.choose(rakuten.search(keys, q), [], set(), set(), q)
        if c:
            out[str(rank)] = {"name": rakuten.clean_name(c["name"]), "url": c["url"], "image": c["image"], "shop": c["shop"],
                              "query": q, "fetched_at": datetime.date.today().isoformat()}
            print(f"  {rank}: {out[str(rank)]['name']}")
        import time
        time.sleep(rakuten.WAIT)
    jsave(RK, out)


def cmd_sim(n=10000):
    """回答をランダムに n 回。app.js の計算そのものを node で動かす。"""
    import tempfile
    sp = species()
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data.js").write_text(data_js(sp), encoding="utf-8")
    js = f"""
global.window = global; require({json.dumps(str(tmp / 'data.js'))}); require({json.dumps(str(TPL / 'app.js'))});
const D = window.SHINDAN, c = {{}}; let fail = 0;
for (let k = 0; k < {n}; k++) {{
  const ans = D.questions.map(q => Math.floor(Math.random() * q.opts.length));
  const r = D.compute(ans); if (!r || !r.id) {{ fail++; continue; }} c[r.id] = (c[r.id] || 0) + 1;
}}
console.log(JSON.stringify({{counts: c, fail}}));
"""
    (tmp / "sim.js").write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(tmp / "sim.js")], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(r.stderr)
    res = json.loads(r.stdout)
    counts = res["counts"]
    names = {s["id"]: s["name"] for s in sp}
    share = {i: round(counts.get(i, 0) / n * 100, 3) for i in names}
    missing = [names[i] for i in names if not counts.get(i)]
    top = sorted(share.items(), key=lambda x: -x[1])
    jsave(SIM, {"_about": "回答をランダムに試したときの、生き物ごとの出る割合（%）。レア度の目安（実際の結果が少ないうち）",
                "n": n, "at": datetime.date.today().isoformat(), "fail": res["fail"], "missing": missing, "share": share})
    print(f"ランダム {n} 回: 出なかった種 {len(missing)} {missing}  失敗 {res['fail']}")
    print("多い: " + "、".join(f"{names[i]} {p}%" for i, p in top[:5]))
    print("少ない: " + "、".join(f"{names[i]} {p}%" for i, p in top[-5:]))
    tiers = {"激レア": 0, "レア": 0, "コモン": 0}
    for _, p in top:
        tiers["激レア" if p < TIERS["gekirare"] else "レア" if p < TIERS["rare"] else "コモン"] += 1
    print("レア度の内訳:", tiers)
    return 0 if not missing and not res["fail"] else 1


def cmd_deploy():
    sp = species()
    wd = ROOT / "workers" / "shindan"
    npx = "npx.cmd" if sys.platform == "win32" else "npx"
    vals = ",".join(f"('{s['id']}')" for s in sp if re.fullmatch(r"[a-z0-9-]+", s["id"]))
    sql = ("CREATE TABLE IF NOT EXISTS counts (id TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0); "
           f"INSERT OR IGNORE INTO counts (id) VALUES {vals};")
    for args in (["d1", "execute", "maf-shindan", "--remote", "--command", sql], ["deploy"]):
        r = subprocess.run([npx, "--yes", "wrangler@4.136.3", *args], cwd=wd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        print((r.stdout + r.stderr).strip()[-400:])
        if r.returncode != 0:
            return 1
    import shutil
    shutil.rmtree(wd / ".wrangler", ignore_errors=True)
    return 0


SHARE_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
@font-face {{ font-family: Z; src: url("{font_b}"); font-weight: 700; }}
@font-face {{ font-family: Z; src: url("{font_m}"); font-weight: 500; }}
* {{ margin: 0; box-sizing: border-box; }}
body {{ width: 1200px; height: 630px; font-family: Z; color: #24384A; background: linear-gradient(135deg,#E6F4FF,#E8F8F0 60%,#FFF8D9); position: relative; overflow: hidden; }}
.ph {{ position: absolute; left: 40px; top: 40px; width: 560px; height: 550px; border-radius: 36px; overflow: hidden; box-shadow: 0 10px 30px rgba(36,56,74,.15); }}
.ph img {{ width: 100%; height: 100%; object-fit: cover; object-position: {pos}; }}
.tx {{ position: absolute; left: 640px; top: 60px; width: 520px; }}
.k {{ font-size: 30px; font-weight: 700; color: #3BA4E8; }}
.y {{ margin-top: 34px; font-size: 30px; font-weight: 500; color: #4F6B80; }}
.n {{ margin-top: 6px; font-size: {nsize}px; font-weight: 700; line-height: 1.25; }}
.t {{ display: inline-block; margin-top: 22px; padding: 8px 26px; border-radius: 999px; background: #5CCFB0; color: #fff; font-size: 34px; font-weight: 700; }}
.c {{ margin-top: 24px; font-size: 30px; font-weight: 700; line-height: 1.5; }}
.u {{ position: absolute; left: 640px; bottom: 40px; font-size: 22px; color: #4F6B80; }}
.cr {{ position: absolute; left: 52px; bottom: 50px; max-width: 536px; font-size: 13px; color: #fff; text-shadow: 0 1px 3px rgba(0,0,0,.8); }}
.r {{ color: #FF8A3D; }}
</style></head><body>
<div class="ph"><img src="{img}"></div>
<div class="cr">{credit}</div>
<div class="tx"><div class="k">自動化いきもの診断</div><div class="y">わたしの自動化のクセは…</div>
<div class="n">{name}</div><div class="t">{type}{rare}</div><div class="c">{catch}</div></div>
<div class="u">blog-factory-cf7.pages.dev/shindan/</div>
</body></html>"""


def cmd_share(only=None):
    from playwright.sync_api import sync_playwright
    sp = species()
    out = STATIC / "share"
    out.mkdir(parents=True, exist_ok=True)
    fonts = ROOT / "assets" / "fonts"
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1200, "height": 630})
        for s in sp:
            if only and s["id"] not in only:
                continue
            ph = s["photo"]
            name = esc(s["name"])
            page = SHARE_HTML.format(
                font_b=(fonts / "ZenMaruGothic-Bold.ttf").as_uri(), font_m=(fonts / "ZenMaruGothic-Medium.ttf").as_uri(),
                img=(STATIC / "img" / f"{s['id']}.jpg").as_uri(), pos=esc(s.get("pos") or "50% 50%"),
                name=name, nsize=86 if len(s["name"]) <= 5 else 70 if len(s["name"]) <= 7 else 56 if len(s["name"]) <= 9 else 46,
                type=esc(TYPES[s["type"]]["name"]), rare='<span class="r">　★レア</span>' if s.get("rare") else "",
                catch=esc(s.get("catch", "")),
                credit=esc(f"Photo: {ph.get('author') or '不明'} / {ph.get('license')} / Wikimedia Commons"))
            tmp = out / "_tmp.html"
            tmp.write_text(page, encoding="utf-8")
            pg.goto(tmp.as_uri())
            pg.wait_for_timeout(150)
            pg.screenshot(path=str(out / f"{s['id']}.jpg"), type="jpeg", quality=86)
        # 入口・図鑑用（5種の写真を並べる）
        picks = [x for n in FACES for x in sp if x["name"] == n][:5] or sp[:5]
        imgs = "".join(f'<img src="{(STATIC / "img" / (x["id"] + ".jpg")).as_uri()}" style="width:180px;height:180px;object-fit:cover;'
                       f'border-radius:50%;border:6px solid #fff;margin:0 6px;object-position:{esc(x.get("pos") or "50% 50%")}">' for x in picks)
        idx = (f'<!doctype html><html><head><meta charset="utf-8"><style>@font-face{{font-family:Z;src:url("{(fonts / "ZenMaruGothic-Bold.ttf").as_uri()}")}}'
               '*{margin:0}body{width:1200px;height:630px;font-family:Z;color:#24384A;text-align:center;'
               'background:linear-gradient(135deg,#E6F4FF,#E8F8F0 60%,#FFF8D9);padding-top:70px}'
               '.k{font-size:40px;color:#3BA4E8}.h{font-size:92px;margin-top:6px}.f{margin-top:44px}.s{margin-top:40px;font-size:32px;color:#4F6B80}</style></head><body>'
               f'<div class="k">12問でわかる</div><div class="h">自動化いきもの診断</div><div class="f">{imgs}</div>'
               f'<div class="s">あなたの「自動化のクセ」は、実在する{len(sp)}種のどれ？</div></body></html>')
        tmp = out / "_tmp.html"
        tmp.write_text(idx, encoding="utf-8")
        pg.goto(tmp.as_uri())
        pg.wait_for_timeout(200)
        pg.screenshot(path=str(out / "index.jpg"), type="jpeg", quality=86)
        tmp.unlink()
        b.close()
    print(f"シェア画像: {out}")


def cmd_licenses():
    from collections import Counter
    sp = species()
    c = Counter(s["photo"]["license"] for s in sp)
    for k, v in c.most_common():
        print(f"  {k}: {v}")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import shindan_tools
    bad = [s["id"] for s in sp if not shindan_tools.license_ok(s["photo"]["license"])]
    print(f"条件外: {bad or 'なし'}")
    return 1 if bad else 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "merge":
        return cmd_merge()
    if cmd == "rakuten":
        return cmd_rakuten()
    if cmd == "sim":
        return cmd_sim(int(a[1]) if len(a) > 1 else 10000)
    if cmd == "deploy":
        return cmd_deploy()
    if cmd == "share":
        return cmd_share(set(a[1:]) or None)
    if cmd == "licenses":
        return cmd_licenses()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
