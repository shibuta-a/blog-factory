# Windows Update が朝の自動実行（3:00〜8:00）に更新・再起動しないようにする（管理者で1回だけ。guard-setup.py が UAC 付きで呼ぶ）
#   アクティブ時間 3:00〜21:00（Windows 10 の上限 18 時間）: この間は更新のための自動再起動をしない
#   「アクティブ時間の自動調整」はオフ（勝手に時間帯を変えない）
#   ログオン中は自動再起動しない（自動ログオンで朝の実行中はずっとログオン中）
#   「できるだけ早く再起動する」（IsExpedited）はオフ
#   更新のインストールは、power-guard のシャットダウン（shutdown /s）のときに行われる（朝の実行の後）
# 結果は state/winupdate.json（scripts/guard.py check が読む）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "state\winupdate.json"
$result = @{ at = (Get-Date -Format "yyyy-MM-dd HH:mm"); ok = $false; error = "" }
try {
    $ux = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
    if (-not (Test-Path $ux)) { New-Item -Path $ux -Force | Out-Null }
    Set-ItemProperty -Path $ux -Name ActiveHoursStart -Value 3 -Type DWord
    Set-ItemProperty -Path $ux -Name ActiveHoursEnd -Value 21 -Type DWord
    Set-ItemProperty -Path $ux -Name SmartActiveHoursState -Value 0 -Type DWord
    Set-ItemProperty -Path $ux -Name IsExpedited -Value 0 -Type DWord

    $pol = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
    $au = Join-Path $pol "AU"
    foreach ($p in @($pol, $au)) { if (-not (Test-Path $p)) { New-Item -Path $p -Force | Out-Null } }
    Set-ItemProperty -Path $pol -Name SetActiveHours -Value 1 -Type DWord
    Set-ItemProperty -Path $pol -Name ActiveHoursStart -Value 3 -Type DWord
    Set-ItemProperty -Path $pol -Name ActiveHoursEnd -Value 21 -Type DWord
    Set-ItemProperty -Path $au -Name NoAutoRebootWithLoggedOnUsers -Value 1 -Type DWord

    $v = Get-ItemProperty -Path $ux
    $result.ok = ($v.ActiveHoursStart -eq 3 -and $v.ActiveHoursEnd -eq 21)
    $result.active_hours = "$($v.ActiveHoursStart):00-$($v.ActiveHoursEnd):00"
    $result.no_reboot_logged_on = (Get-ItemProperty -Path $au).NoAutoRebootWithLoggedOnUsers
} catch {
    $result.error = "$_"
}
New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
$result | ConvertTo-Json | Out-File -FilePath $out -Encoding utf8
