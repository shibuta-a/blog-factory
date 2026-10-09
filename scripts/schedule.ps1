# 毎日の自動投稿をオン／オフする（Windows タスクスケジューラ）
#
#   powershell -ExecutionPolicy Bypass -File scripts\schedule.ps1 on            … 毎日 9:00 に2本（既定）
#   powershell -ExecutionPolicy Bypass -File scripts\schedule.ps1 on 10:30 3    … 毎日 10:30 に3本
#   powershell -ExecutionPolicy Bypass -File scripts\schedule.ps1 off           … 止める
#   powershell -ExecutionPolicy Bypass -File scripts\schedule.ps1 status        … 状態を見る
#
# ★ オンにするのは、渋田さんが「毎日の自動投稿をオンにして」と言ったときだけ。
# ・PC が起動していて渋田さんがログオン中のときに動く（A8 のログインした Chrome もこの条件で使える）
# ・実行時刻に PC が止まっていたら、次に起動したときに1回だけ遅れて実行する
param([string]$Mode = "status", [string]$Time = "09:00", [int]$Count = 2)

$TaskName = "BlogFactory-Daily"
$Root = "C:\blog-factory"

switch ($Mode) {
  "on" {
    $action   = New-ScheduledTaskAction -Execute "$Root\auto-generate.bat" -Argument "-n $Count" -WorkingDirectory $Root
    $trigger  = New-ScheduledTaskTrigger -Daily -At $Time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
      -Description "ブログ工場 毎日の自動投稿（Claude Code が記事を書いて公開。1日の上限は3本）" -Force | Out-Null
    Write-Output "自動投稿をオンにしました: 毎日 $Time に $Count 本"
  }
  "off" {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "自動投稿をオフにしました"
  }
  default {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) {
      $i = Get-ScheduledTaskInfo -TaskName $TaskName
      Write-Output "オン（次回: $($i.NextRunTime) / 前回: $($i.LastRunTime) 結果 $($i.LastTaskResult)）"
    } else {
      Write-Output "オフ"
    }
  }
}
