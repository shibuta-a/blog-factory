@echo off
rem one-time setup for full automation
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-once.ps1"
