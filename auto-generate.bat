@echo off
chcp 65001 >nul
cd /d %~dp0
python scripts\auto-generate.py %* >> logs\auto-generate.log 2>&1
