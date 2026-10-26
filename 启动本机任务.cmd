@echo off
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0scripts\start_local.ps1"
if errorlevel 1 pause
