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
X_KEYS = ROOT / ".secrets" / "x_api.json"
X_KEY_NAMES = ("api_key", "api_secret", "access_token", "access_token_secret")
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


def x_request(method, url, keys, body=None, params=None):
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(full, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", oauth_header(method, url, keys, params))
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}


def post_tweet(text, keys):
    return x_request("POST", TWEET_URL, keys, body={"text": text})


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
