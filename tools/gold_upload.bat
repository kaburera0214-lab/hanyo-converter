@echo off
rem 楽天GOLDアップローダ: アプリでDLしたzip(またはHTML)をこのbatにドラッグ&ドロップする
cd /d "%~dp0"
chcp 65001 >nul
python gold_upload_local.py %1 %2
echo.
pause
