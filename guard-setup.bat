@echo off
rem one-time setup: guard / mail / backup / windows update
cd /d %~dp0
python scripts\guard-setup.py
pause
