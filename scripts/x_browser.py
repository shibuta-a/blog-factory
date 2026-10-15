"""
X（@maf_media_jp）をブラウザ自動操作するための共通部品（A8・note と同じ方式: このPCの Chrome を X 専用のプロファイルで開き、Playwright で後から接続）。

- ログイン状態などのブラウザのデータは .secrets/x-browser-profile/（GitHub非公開）
- ログインが切れていたら .secrets/x.json（login_id / password / username。渋田さんが小さい窓で1回だけ入力）で自動でログインし直す（auto_login）。
  2段階認証が「認証アプリ」方式なら、x.json の totp_secret からコードを作って入れる。パスワード・コードは画面にもログにも出さない
- CAPTCHA・アカウントの制限・凍結の表示が出たら突破しようとせず、理由を返す（呼び出し側がその日の X を止め、STATUS.md に出す）
- Chrome はポート 9335（A8 は 9333、note は 9334）
"""
import json
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / ".secrets" / "x-browser-profile"
SHOTS = ROOT / ".secrets" / "shots"
CREDS = ROOT / ".secrets" / "x.json"
CDP_PORT = 9335
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
# 表示されたら X の自動処理をすべて止める言葉（アカウントの制限・凍結・警告）
BLOCK_WORDS = ("アカウントは凍結", "凍結されています", "アカウントがロック", "ロックされています", "一時的に制限", "機能が制限",
               "ログインが現在許可されていません", "account is suspended", "account has been locked", "temporarily restricted", "suspended")


def creds():
    try:
        return json.loads(CREDS.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def save_creds(**kw):
    d = creds()
    d.update(kw)
    CREDS.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def chrome_running():
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=3):
            return True
    except Exception:
        return False


def start_chrome(url="https://x.com/home"):
    """X 用の Chrome を（まだなければ）開く。開けたら True。"""
    if chrome_running():
        return True
    chrome = next((p for p in CHROME_PATHS if Path(p).exists()), None)
    if not chrome:
        return False
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([chrome, f"--user-data-dir={PROFILE}", f"--remote-debugging-port={CDP_PORT}",
                      "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
                      "--lang=ja", "--window-size=1280,900", url],
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    for _ in range(30):
        if chrome_running():
            return True
        time.sleep(1)
    return False


def shot(page, name):
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"x-{name}.png"))
    except Exception:
        pass


def body_text(page):
    try:
        return page.evaluate("() => document.body.innerText")
    except Exception:
        return ""


def blocked(page):
    """アカウントの制限・凍結・警告の表示があれば、その言葉。なければ ""。"""
    t = body_text(page)
    return next((w for w in BLOCK_WORDS if w in t), "")


def current_user(page):
    """ログイン中ならユーザー名（@なし）を返す。ログインしていなければ None。"""
    try:
        if "x.com/home" not in page.url:
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        for _ in range(20):
            if "/login" in page.url or "/i/flow" in page.url or page.url.rstrip("/").endswith("x.com"):
                return None
            btn = page.locator('[data-testid="SideNav_AccountSwitcher_Button"]')
            if btn.count():
                txt = btn.first.inner_text()
                at = [w for w in txt.split() if w.startswith("@")]
                return at[0].lstrip("@") if at else None
            page.wait_for_timeout(1000)
    except Exception:
        return None
    return None


def totp_now():
    sec = creds().get("totp_secret", "")
    if not sec:
        return ""
    import pyotp
    return pyotp.TOTP(sec).now()


def _visible(page, sel):
    loc = page.locator(sel)
    try:
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def _click_text(page, *names):
    for n in names:
        b = page.get_by_role("button", name=n)
        if b.count():
            b.first.click()
            return True
    return False


USER_INPUTS = 'input[name="username_or_email"], input[autocomplete^="username"]'
NEXT_WORDS = ("続ける", "次へ", "Next", "Continue")
LOGIN_WORDS = ("ログイン", "Log in")


def _click_enabled(page, words):
    """表示されていて押せる、その名前のボタンを押す。押せたら True。"""
    for w in words:
        for b in page.get_by_role("button", name=w, exact=True).all():
            try:
                if b.is_visible() and b.is_enabled():
                    b.click()
                    return True
            except Exception:
                pass
    return False


def auto_login(page):
    """保存済みの情報で X にログインする。(ログインできたユーザー名, 失敗の理由)。"""
    c = creds()
    if not (c.get("login_id") and c.get("password")):
        return None, ".secrets/x.json にログイン情報がないため自動ログインできません"
    # 新しいログイン画面（jf.x.com）は、断られても画面に何も出さず、返事の中にだけ理由を書く（2026-10-15 確認）
    denied = []

    def on_response(r):
        if "jf.x.com" in r.url and r.request.method == "POST":
            try:
                t = r.body().decode("utf-8", "replace")
            except Exception:
                return
            hit = next((w for w in BLOCK_WORDS if w in t), "")
            if hit:
                denied.append(hit)
    page.on("response", on_response)
    try:
        page.goto("https://x.com/login", wait_until="domcontentloaded", timeout=60000)
        page.locator(USER_INPUTS).first.wait_for(timeout=30000)
        done = set()
        for step in range(10):
            page.wait_for_timeout(2500)
            if current_user_quick(page):
                break
            if denied:
                shot(page, "login-blocked")
                return None, f"X がログインを断りました（「お使いのアカウントへのログインが現在許可されていません」）"
            if blocked(page):
                shot(page, "login-blocked")
                return None, f"X にアカウントの制限・凍結の表示が出ました（{blocked(page)}）"
            if page.locator('iframe[src*="arkoselabs"], iframe[id*="arkose"], iframe[src*="captcha"]').count():
                shot(page, "login-captcha")
                return None, "CAPTCHA（ロボット確認）が出たため自動ログインできませんでした"
            t = body_text(page)
            code_box = page.locator('input[data-testid="ocfEnterTextTextInput"], input[autocomplete="one-time-code"], input[name="verification_code"]')
            if _visible(page, 'input[name="password"]:not([inert])') and "password" not in done:
                if _visible(page, USER_INPUTS) and "user" not in done:
                    _type(page.locator(USER_INPUTS).first, c["login_id"])
                _type(page.locator('input[name="password"]').first, c["password"])
                done |= {"user", "password"}
                _click_enabled(page, LOGIN_WORDS + NEXT_WORDS) or page.keyboard.press("Enter")
                continue
            if _visible(page, USER_INPUTS) and "user" not in done:
                _type(page.locator(USER_INPUTS).first, c["login_id"])
                done.add("user")
                _click_enabled(page, NEXT_WORDS) or page.keyboard.press("Enter")
                continue
            if code_box.count() and code_box.first.is_visible():
                box = code_box.first
                if any(w in t for w in ("認証コード", "確認コード", "コードを入力", "verification code", "認証アプリ", "authentication code")):
                    if ("メール" in t or "email" in t.lower()) and "認証アプリ" not in t:
                        shot(page, "login-email-code")
                        return None, "メールに届く確認コードを求められました（Gmail を自動で読む許可がまだありません）"
                    code = totp_now()
                    if not code:
                        shot(page, "login-2fa")
                        return None, "2段階認証のコードを求められました（認証アプリの秘密鍵が .secrets/x.json にありません）"
                    _type(box, code)
                    done.add("code")
                else:  # 「電話番号またはユーザー名を入力」（いつもと違うログインのときの確認）
                    _type(box, c.get("username", "maf_media_jp"))
                    done.add("confirm")
                _click_enabled(page, NEXT_WORDS + LOGIN_WORDS) or page.keyboard.press("Enter")
                continue
            if "/home" in page.url:
                break
        page.wait_for_timeout(3000)
    except Exception as e:
        shot(page, "login-error")
        return None, f"ログイン画面の操作に失敗しました（{type(e).__name__}。X の画面の仕様が変わった可能性）"
    user = current_user(page)
    if user:
        return user, ""
    shot(page, "login-failed")
    t = body_text(page)
    if "パスワードが正しくありません" in t or "Wrong password" in t:
        return None, "パスワードが違うと X に言われました（.secrets/x.json を確認）"
    return None, "保存済みの情報でログインできませんでした（画面: .secrets/shots/x-login-failed.png）"


def _type(loc, text):
    """新しいログイン画面の入力欄は fill では値が入らないので、キー入力で入れる（間隔は一定）。"""
    loc.click()
    loc.press_sequentially(text, delay=20)


def current_user_quick(page):
    return "/home" in page.url and page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').count() > 0


def ensure_login(page):
    """ログイン済みならそのまま、切れていれば自動ログイン。(ユーザー名, 理由)"""
    user = current_user(page)
    if user:
        return user, ""
    return auto_login(page)
