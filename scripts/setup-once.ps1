# 完全自動運転のための初期設定（人の操作が要るのはこれだけ・最初に1回）
#   setup-once.bat をダブルクリックで実行する
#   1. A8 のログインID・パスワードを .secrets/a8.json にメモ帳で入力
#   2. note のメールアドレス（または note ID）・パスワードを .secrets/note.json にメモ帳で入力
#   3. Claude Code の長期トークンを発行（ブラウザで許可）→ 表示されたトークンを貼り付け → .secrets/claude-token.json
# パスワード・トークンは画面にもログにも出さない。.secrets/ は GitHub に上げない（.gitignore）。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$sec = Join-Path $root ".secrets"
New-Item -ItemType Directory -Force $sec | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Fill-Json($path, $template, $label, $keys) {
    if (-not (Test-Path $path)) { [IO.File]::WriteAllText($path, $template, $utf8) }
    try {   # 入力済みならとばす（トークンだけ取り直すとき）
        $j = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if (@($keys | Where-Object { -not $j.$_ }).Count -eq 0) { Write-Host "$label は入力済み（とばします）"; return }
    } catch {}
    while ($true) {
        Write-Host ""
        Write-Host "== $label ==" -ForegroundColor Cyan
        Write-Host "メモ帳が開きます。"" "" の間に入力して、上書き保存（Ctrl+S）してからメモ帳を閉じてください。"
        Start-Process notepad.exe $path -Wait
        try {
            $j = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
            $missing = @($keys | Where-Object { -not $j.$_ })
            if ($missing.Count -eq 0) { Write-Host "OK（保存しました）" -ForegroundColor Green; return }
            Write-Host "まだ空の項目があります: $($missing -join ', ')" -ForegroundColor Yellow
        } catch {
            Write-Host "ファイルの形が崩れています（"" や , を消していないか確認してください）" -ForegroundColor Yellow
        }
    }
}

Fill-Json (Join-Path $sec "a8.json") "{`r`n  `"login_id`": `"`",`r`n  `"password`": `"`"`r`n}`r`n" "1/3 A8.net のログインID とパスワード" @("login_id", "password")
Fill-Json (Join-Path $sec "note.json") "{`r`n  `"email`": `"`",`r`n  `"password`": `"`"`r`n}`r`n" "2/3 note のメールアドレス（または note ID）とパスワード" @("email", "password")

Write-Host ""
Write-Host "== 3/3 Claude Code の長期トークン ==" -ForegroundColor Cyan
Write-Host "ブラウザが開いたら「承認」を押してください。最後に sk-ant-oat で始まるトークンが表示されます。"
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) { $claude = Join-Path $env:USERPROFILE ".local\bin\claude.exe" }
& $claude setup-token
Write-Host ""
Write-Host "表示されたトークン（sk-ant-oat...）をコピーして、ここに貼り付けて Enter（画面には表示されません）:"
while ($true) {
    $s = Read-Host -AsSecureString
    $tok = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)).Trim()
    if ($tok -like "sk-ant-*") { break }
    Write-Host "sk-ant- で始まっていません。もう一度貼り付けてください:" -ForegroundColor Yellow
}
$env:CLAUDE_CODE_OAUTH_TOKEN = $tok
$test = & $claude -p "OKとだけ返して" 2>&1 | Out-String
if ($test -notmatch "OK") {
    Write-Host "トークンで Claude Code を動かせませんでした: $($test.Trim())" -ForegroundColor Red
    Read-Host "Enter で閉じます"
    exit 1
}
$issued = Get-Date
$obj = [ordered]@{ token = $tok; issued = $issued.ToString("yyyy-MM-dd"); expires = $issued.AddDays(365).ToString("yyyy-MM-dd"); _note = "claude setup-token で発行（有効期限1年）。期限が近づいたら setup-once.bat の 3/3 だけやり直す" }
[IO.File]::WriteAllText((Join-Path $sec "claude-token.json"), ($obj | ConvertTo-Json), $utf8)
Write-Host "OK（トークンで動くことを確認・保存しました。期限: $($obj.expires)）" -ForegroundColor Green
Write-Host ""
Write-Host "初期設定はすべて終わりました。このウィンドウを閉じて、Claude Code に「終わった」と伝えてください。" -ForegroundColor Green
Read-Host "Enter で閉じます"
