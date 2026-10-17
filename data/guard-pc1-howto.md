# PC① の営業メールに「合図」を足す手順（今はまだ触らない）

PC②（ブログ工場）の見張り番は、Cloudflare の **maf-guard**（https://maf-guard.asoutobata.workers.dev）にいます。
毎朝 7:30 にその日の合図がそろっているか確かめ、足りなければ Gmail（asoutobata@gmail.com）に「【要対応】…」を送ります。
受け口と見張りの設定はもう用意してあり、**「営業メールの合図を待つ」は今はオフ**です。PC① に合図を足したら、オンにします。

## 1. 合言葉を PC① に置く

- 合言葉は PC② の `C:\blog-factory\.secrets\guard.json` の `token` です（**GitHub やチャットに貼らない**）。
- PC① の `C:\AIASMR\mail\auto\.secrets\guard.json`（無ければフォルダを作る）に、次の形で保存します。
  ```json
  {"url": "https://maf-guard.asoutobata.workers.dev", "token": "（PC② の guard.json の token）"}
  ```
- `.secrets` が GitHub に上がらないこと（.gitignore）を確かめます。

## 2. 営業メールの自動送信が終わったところで合図を送る

送信の処理の最後（成功でも失敗でも必ず通る所）に、次のどちらかを足します。`name` は必ず `pc1-mail`。
`date` は日本時間のその日、`ok` は成功なら true、`lines` はまとめに載る数行、`error` は失敗の理由です。

### Python の場合（標準ライブラリだけ）

```python
import json, urllib.request
from datetime import date
from pathlib import Path

def guard_beat(ok, lines, error=""):
    """見張り番に「営業メールが終わった・結果」を送る。送れなくても営業メールの処理は止めない。"""
    try:
        cfg = json.loads(Path(r"C:\AIASMR\mail\auto\.secrets\guard.json").read_text(encoding="utf-8"))
        body = {"name": "pc1-mail", "date": date.today().isoformat(), "ok": ok, "lines": lines, "error": error}
        req = urllib.request.Request(cfg["url"] + "/beat", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json",
                                              "User-Agent": "pc1-mail"})
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        print(f"見張り番に合図を送れませんでした: {e}")

# 例: 送信が終わったら
# guard_beat(True, [f"送信 {sent} 件・失敗 {failed} 件"])
# 例外で止まったら
# guard_beat(False, [], error=str(e)[:200])
```

### PowerShell の場合

```powershell
$cfg = Get-Content "C:\AIASMR\mail\auto\.secrets\guard.json" -Raw | ConvertFrom-Json
$body = @{ name = "pc1-mail"; date = (Get-Date -Format "yyyy-MM-dd"); ok = $true; lines = @("送信 12 件・失敗 0 件") } | ConvertTo-Json
Invoke-RestMethod -Uri "$($cfg.url)/beat" -Method Post -Headers @{ Authorization = "Bearer $($cfg.token)" } `
  -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

## 3. 試す

1. PC① で営業メールの処理を1回動かす（または上の関数だけを `guard_beat(True, ["テスト"])` で呼ぶ）
2. PC② で `python scripts/guard.py status` → `beats` に `pc1-mail` が出ていれば届いています

## 4. 見張りをオンにする

PC② で:

```
python scripts/guard.py expect pc1-mail on
```

これで毎朝 7:30 の見張りが営業メールの合図も待ちます。合図がない日は「【要対応】営業メールの自動送信（PC①）の合図がありません…」が届きます。
まとめには「■ 営業メール（PC①）」として、合図の `lines` が載ります。オフに戻すときは `… expect pc1-mail off`。

## 注意

- 営業メールが朝 7:30 より後に動く場合は、7:30 の見張りでは「まだ来ていない」になります。そのときは PC② の
  `workers/guard/wrangler.toml` の crons に営業メールの後の時刻を足し、`worker.js` で営業メールだけその時刻に見るように変えます（相談してください）。
- 合図を送る処理が失敗しても、営業メールの送信そのものは止めないようにしてください（上の例は try で包んであります）。
