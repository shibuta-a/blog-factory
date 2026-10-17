"""
.secrets の暗号化バックアップ（合言葉は PC に保存しない）。

しくみ:
  ・最初の1回（scripts/guard-setup.py の窓）で合言葉を決める → 鍵の組（RSA 3072）を作る
      公開鍵 .secrets/backup-public.pem   … 暗号にするだけの鍵（これだけでは解けない）
      秘密鍵 .secrets/backup-private.enc.pem … 合言葉で暗号にした秘密鍵（合言葉がないと使えない）
    合言葉そのものはどこにも保存しない
  ・毎週の自動バックアップ（合言葉は要らない）: .secrets の小さいファイル（*.json・*.pem など）を zip にして、
    使い捨ての AES-256-GCM 鍵で暗号化 → その鍵を公開鍵で包む → 合言葉で守った秘密鍵と一緒に1つの JSON にして
    Cloudflare（見張り番 maf-guard の KV）へ。ブラウザのログイン状態（*-browser-profile）と画面写真は入れない（自動で入り直せる）
  ・復元: python scripts/guard.py restore → 合言葉を入れる → .secrets に書き戻す
"""
import base64
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import os

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / ".secrets"
PUBLIC = SECRETS / "backup-public.pem"
PRIVATE = SECRETS / "backup-private.enc.pem"
MAX_FILE = 1024 * 1024           # 1MB を超えるファイルは入れない（ブラウザのデータなど）
SKIP_EXT = {".png", ".jpg", ".jpeg", ".log"}
OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def make_keys(passphrase):
    """鍵の組を作って保存する。合言葉は保存しない。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    SECRETS.mkdir(exist_ok=True)
    PRIVATE.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.BestAvailableEncryption(passphrase.encode("utf-8"))))
    PUBLIC.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                     serialization.PublicFormat.SubjectPublicKeyInfo))


def has_keys():
    return PUBLIC.exists() and PRIVATE.exists()


def files_to_backup():
    out = []
    for p in sorted(SECRETS.iterdir()):
        if p.is_file() and p.suffix.lower() not in SKIP_EXT and p.stat().st_size <= MAX_FILE:
            out.append(p)
    return out


def make_blob():
    """暗号化したバックアップ（JSON 文字列）を作る。合言葉は使わない。"""
    buf = io.BytesIO()
    files = files_to_backup()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, p.name)
    aes = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    data = AESGCM(aes).encrypt(nonce, buf.getvalue(), None)
    pub = serialization.load_pem_public_key(PUBLIC.read_bytes())
    b64 = lambda b: base64.b64encode(b).decode()
    return json.dumps({
        "v": 1, "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "files": [p.name for p in files],
        "private_key_encrypted": PRIVATE.read_text(encoding="ascii"),
        "wrapped_key": b64(pub.encrypt(aes, OAEP)), "nonce": b64(nonce), "data": b64(data),
        "how": "python scripts/guard.py restore（合言葉が要る）",
    })


def open_blob(blob, passphrase):
    """バックアップを合言葉で開き、{ファイル名: 中身} を返す。合言葉が違えば ValueError。"""
    d = json.loads(blob)
    key = serialization.load_pem_private_key(d["private_key_encrypted"].encode("ascii"), passphrase.encode("utf-8"))
    aes = key.decrypt(base64.b64decode(d["wrapped_key"]), OAEP)
    raw = AESGCM(aes).decrypt(base64.b64decode(d["nonce"]), base64.b64decode(d["data"]), None)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return {n: z.read(n) for n in z.namelist()}


def check_passphrase(passphrase):
    """保存済みの秘密鍵が合言葉で開けるか（窓の中での確認用）。"""
    try:
        serialization.load_pem_private_key(PRIVATE.read_bytes(), passphrase.encode("utf-8"))
        return True
    except Exception:
        return False
