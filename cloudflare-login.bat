@echo off
chcp 65001 >nul
rem X の投稿係を Cloudflare に切り替える（1回だけ）。ブラウザで Cloudflare にログインして「Allow」を押せば、あとは自動で進む
cd /d %~dp0
python scripts\x-cloudflare-setup.py
pause
