# 毎朝の自動起動→記事作成→シャットダウン（省エネ運転）をオン／オフ・時刻変更する
#
#   powershell -ExecutionPolicy Bypass -File scripts\schedule.ps1 on            … 毎朝 4:00 起動の設定でオン（既定）
#   powershell -ExecutionPolicy Bypass -File scripts\schedule.ps1 on 05:30      … 起動時刻を 5:30 に変える
#   powershell -ExecutionPolicy Bypass -File scripts\schedule.ps1 off           … 止める（シャットダウンもしない）
#   powershell -ExecutionPolicy Bypass -File scripts\schedule.ps1 status        … 状態を見る
#
# 流れ: BIOS の RTC Alarm で毎朝起動 → 自動ログオン → BlogFactory-Boot（ログオン1分後）→ scripts\boot-run.py が1〜3本書く
#       → publish.py が各記事に公開予定時刻（24時間以内のランダム）を付けて GitHub へ → 無人ならシャットダウン
#       → Cloudflare の門番（functions/_middleware.js）がアクセスのたびに判定し、時刻が来た瞬間から表示する
# ★ 起動時刻を変えたら、BIOS の RTC Alarm の時刻も同じに変えること（ずれていると安全のためシャットダウンしない）
param([string]$Mode = "status", [string]$Time = "")

$TaskName = "BlogFactory-Boot"
$Root   = "C:\blog-factory"
$Config = "$Root\data\power.json"
$Python = "C:\Users\Owner\AppData\Local\Programs\Python\Python310\python.exe"

function Set-Config([hashtable]$kv) {
  $j = Get-Content $Config -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($k in $kv.Keys) { $j.shutdown.$k = $kv[$k] }
  [IO.File]::WriteAllText($Config, ($j | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))
}

switch ($Mode) {
  "on" {
    $cfg = (Get-Content $Config -Raw -Encoding UTF8 | ConvertFrom-Json).shutdown
    if (-not $Time) { $Time = $cfg.bios_wake_time }
    $at = [datetime]::ParseExact($Time, "HH:mm", $null)
    Set-Config @{ bios_wake_time = $at.ToString("HH:mm"); enabled = $true }
    $action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Root\scripts\boot-run.py`"" -WorkingDirectory $Root
    $t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; $t1.Delay = "PT1M"
    $t2 = New-ScheduledTaskTrigger -Daily -At $at.AddMinutes(5)   # その時刻に PC がすでに点いていた日の保険
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $t1, $t2 -Settings $settings `
      -Description "ブログ工場 起動したら記事を書いて予約公開→無人ならシャットダウン（1日1回）" -Force | Out-Null
    $ab = New-ScheduledTaskAction -Execute ($Python -replace 'python.exe$', 'pythonw.exe') -Argument "`"$Root\scripts\idle-beacon.pyw`"" -WorkingDirectory $Root
    $sb = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "IdleBeacon" -Action $ab -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings $sb `
      -Description "ブログ工場 操作の記録係（人が触っていたら落とさないための判定用）" -Force | Out-Null
    if ((Get-ScheduledTask IdleBeacon).State -ne "Running") { Start-ScheduledTask -TaskName "IdleBeacon" }
    Write-Output "オンにしました: 毎朝 $($at.ToString('HH:mm')) に起動（BIOS の RTC Alarm も $($at.ToString('HH:mm')) にしてください）"
  }
  "off" {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Set-Config @{ enabled = $false }
    Write-Output "オフにしました（記事は書かず、シャットダウンもしません。BIOS の RTC Alarm を止めるときは Disabled に）"
  }
  default {
    $cfg = (Get-Content $Config -Raw -Encoding UTF8 | ConvertFrom-Json).shutdown
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
      $i = Get-ScheduledTaskInfo -TaskName $TaskName
      Write-Output "オン: 毎朝 $($cfg.bios_wake_time) 起動 / シャットダウン $($cfg.enabled) / 前回 $($i.LastRunTime) 結果 $($i.LastTaskResult)"
    } else { Write-Output "オフ" }
    & $Python "$Root\scripts\publish_schedule.py" --list
  }
}
