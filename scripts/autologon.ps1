# 自動ログオンを設定する（Owner はパスワードなしのため、パスワードは保存しない）
#   powershell -ExecutionPolicy Bypass -File scripts\autologon.ps1 on|off   … 管理者権限で実行（UACの確認が出る）
param([string]$Mode = "on")
$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
if ($Mode -eq "on") {
  Set-ItemProperty $k AutoAdminLogon "1"
  Set-ItemProperty $k DefaultUserName "Owner"
  Set-ItemProperty $k DefaultDomainName $env:COMPUTERNAME
  Set-ItemProperty $k DefaultPassword ""
  Remove-ItemProperty $k AutoLogonCount -ErrorAction SilentlyContinue
} else {
  Set-ItemProperty $k AutoAdminLogon "0"
  Remove-ItemProperty $k DefaultPassword -ErrorAction SilentlyContinue
}
Get-ItemProperty $k | Select AutoAdminLogon, DefaultUserName, DefaultDomainName | Out-File "C:\blog-factory\logs\autologon.txt" -Encoding utf8
