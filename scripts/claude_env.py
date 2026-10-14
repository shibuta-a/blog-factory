"""
自動実行の claude -p を、ログインの期限切れで止まらないようにする。

.secrets/claude-token.json（setup-once.bat の 3/3 で `claude setup-token` の長期トークンを保存。有効期限1年）があれば、
環境変数 CLAUDE_CODE_OAUTH_TOKEN に入れて claude -p に渡す。なければ、いつもの Claude Code のログインで動く。
トークンは GitHub に上げない（.secrets/ は .gitignore）。ログにも出さない。
"""
import json
import os
from pathlib import Path

TOKEN_FILE = Path(__file__).resolve().parent.parent / ".secrets" / "claude-token.json"


def claude_env():
    env = dict(os.environ)
    try:
        tok = json.loads(TOKEN_FILE.read_text(encoding="utf-8-sig")).get("token", "").strip()
    except Exception:
        tok = ""
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env
