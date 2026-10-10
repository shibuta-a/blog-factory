@echo off
rem 猶予中の休止を取り消す（今回起きている間は休止しない）
cd /d %~dp0
python scripts\power-guard.py --cancel
pause
