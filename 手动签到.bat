@echo off
chcp 65001 >nul
cd /d "%~dp0"
python run_once.py %*
echo.
echo 退出码：%ERRORLEVEL%
pause
