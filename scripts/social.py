"""
note・X への自動展開の共通部品（Python標準ライブラリのみ）。

- X のポスト文を作る（フロントマター x_text ＋ ハッシュタグ ＋ 記事URL。X の文字数の数え方で 280 以内に収める）
- X API への投稿（OAuth 1.0a。キーは .secrets/x_api.json。テスト投稿用。毎回の自動投稿は Cloudflare の workers/x-poster が行う）
- ブログと note の文章がどれだけ同じか（重複コンテンツ対策の判定）
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "social.json"
NOTE_LOG = ROOT / "data" / "note.json"
X_KEYS = ROOT / ".secrets" / "x-api.json"   # scripts/x-keys.py の窓で保存（GitHub には上げない）
X_KEY_NAMES = ("api_key", "api_secret", "access_token", "access_token_secret")
X_ENV_NAMES = {"api_key": "X_API_KEY", "api_secret": "X_API_SECRET",
               "access_token": "X_ACCESS_TOKEN", "access_token_secret": "X_ACCESS_SECRET"}  # GitHub の Secrets の名前
TWEET_URL = "https://api.x.com/2/tweets"


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# X のポスト文
# ---------------------------------------------------------------------------
def x_weight(text):
    """X の文字数（日本語などは2、英数字は1、URL は一律23）。上限は 280。"""
    text = re.sub(r"https?://\S+", "x" * 23, text)
    n = 0
    for ch in text:
        c = ord(ch)
        n += 1 if (c <= 4351 or 8192 <= c <= 8205 or 8208 <= c <= 8223 or 8242 <= c <= 8247) else 2
    return n


def x_post_text(meta, url, tags, with_link=True):
    """記事のフロントマターから X のポスト文を作る。x_text がなければタイトルと要約から作る。"""
    body = (meta.get("x_text") or "").strip()
    if not body:
        desc = re.sub(r"\s+", "", meta.get("description", ""))
        body = f"{meta.get('title', '')}\n\n{desc}"
    body = body.replace("\\n", "\n")
    tag_line = " ".join(t for t in tags if t not in body)
    tail = "\n".join(x for x in (tag_line, url if with_link else "") if x)
    budget = 280 - x_weight("\n\n" + tail) if tail else 280
    while x_weight(body) > budget:      # 長すぎたら本文の末尾を削って「…」
        body = body[:-2].rstrip() + "…"
        if body.endswith("……"):
            body = body[:-1]
    return f"{body}\n\n{tail}" if tail else body


# ---------------------------------------------------------------------------
# X API（OAuth 1.0a 署名）
# ---------------------------------------------------------------------------
def load_x_keys():
    """.secrets/x-api.json のキー。GitHub Actions では環境変数 X_API_KEY などから読む。"""
    import os
    env = {k: os.environ.get(v, "") for k, v in X_ENV_NAMES.items()}
    if all(env.values()):
        return {k: v.strip() for k, v in env.items()}
    if not X_KEYS.exists():
        return None
    keys = json.loads(X_KEYS.read_text(encoding="utf-8"))
    if not all(str(keys.get(k, "")).strip() and "ここに" not in str(keys.get(k)) for k in X_KEY_NAMES):
        return None
    return {k: str(keys[k]).strip() for k in X_KEY_NAMES}


def _q(s):
    return urllib.parse.quote(str(s), safe="~-._")


def oauth_header(method, url, keys, params=None):
    oauth = {
        "oauth_consumer_key": keys["api_key"],
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": keys["access_token"],
        "oauth_version": "1.0",
    }
    allp = {**(params or {}), **oauth}
    base = "&".join([method.upper(), _q(url), _q("&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(allp.items())))])
    signing_key = f"{_q(keys['api_secret'])}&{_q(keys['access_token_secret'])}"
    oauth["oauth_signature"] = base64.b64encode(
        hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_q(k)}="{_q(v)}"' for k, v in sorted(oauth.items()))


def x_request(method, url, keys, body=None, params=None, form=None):
    """X API を呼ぶ。body は JSON（v2）、form は application/x-www-form-urlencoded（v1.1。署名に含める）。"""
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    data = (json.dumps(body).encode() if body is not None else
            urllib.parse.urlencode(form, quote_via=urllib.parse.quote).encode() if form is not None else None)
    req = urllib.request.Request(full, method=method, data=data)
    req.add_header("Authorization", oauth_header(method, url, keys, {**(params or {}), **(form or {})}))
    if body is not None:
        req.add_header("Content-Type", "application/json")
    elif form is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}


def post_tweet(text, keys, media_ids=None):
    body = {"text": text}
    if media_ids:
        body["media"] = {"media_ids": list(media_ids)}
    return x_request("POST", TWEET_URL, keys, body=body)


def upload_image(png_bytes, keys):
    """画像を X に上げて media_id を返す（v2 の /2/media/upload・JSON で base64）。(code, media_id or None, data)"""
    code, data = x_request("POST", "https://api.x.com/2/media/upload", keys,
                           body={"media": base64.b64encode(png_bytes).decode(), "media_category": "tweet_image"})
    return code, (data.get("data") or {}).get("id"), data


# ---------------------------------------------------------------------------
# 重複コンテンツの判定（ブログと note で同じ文章を出さない）
# ---------------------------------------------------------------------------
def _plain(md):
    md = re.sub(r"^---.*?^---\s*$", "", md, flags=re.S | re.M)
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    md = re.sub(r"\]\([^)]*\)", "]", md)
    return re.sub(r"[\s#>*=\-|`\[\]()!、。・：:「」『』（）]+", "", md)


def overlap(blog_md, note_md, n=10):
    """note の文章のうち、ブログと同じ10文字の並びが出てくる割合（0〜1）と、いちばん長い一致の文字数。"""
    a, b = _plain(blog_md), _plain(note_md)
    if len(b) < n:
        return 0.0, 0
    grams = {a[i:i + n] for i in range(len(a) - n + 1)}
    hits = [b[i:i + n] in grams for i in range(len(b) - n + 1)]
    longest = run = 0
    for h in hits:
        run = run + 1 if h else 0
        longest = max(longest, run)
    return sum(hits) / len(hits), (longest + n - 1 if longest else 0)


# ---------------------------------------------------------------------------
# X API の費用・クレジット（data/x.json は GitHub Actions の scripts/x-actions.py が書く）
# ---------------------------------------------------------------------------
X_LOG = ROOT / "data" / "x.json"


def x_api_alert(xlog=None, cfg=None):
    """STATUS.md の先頭に出す X API の知らせ（なければ None）。"""
    if xlog is None:
        xlog = json.loads(X_LOG.read_text(encoding="utf-8")) if X_LOG.exists() else {}
    cfg = cfg or load_config().get("x_api", {})
    cr = xlog.get("credit") or {}
    if cr.get("depleted"):
        return (f"- **X API のクレジットが切れています**（{cr.get('at')} の投稿で「残高不足」）。X の投稿だけ止まっています。"
                "console.x.com の Billing でクレジットを足すと、次の30分の確認から自動で再開します。")
    if cfg.get("credit_usd"):
        since = cfg.get("credit_set_on") or "2000-01-01"
        used = sum(p.get("cost", 0) for p in xlog.get("posts", []) if p.get("posted_at", "") >= since)
        left = float(cfg["credit_usd"]) - used
        if left < float(cfg.get("credit_low_usd", 2)):
            return (f"- **X API のクレジットの残りが少なくなっています**（見込み 約${left:.2f}・{since} に ${cfg['credit_usd']} から計算）。"
                    "console.x.com の Billing で確かめて、足したら data/social.json の x_api.credit_usd と credit_set_on を書き換えてください。")
    return None
