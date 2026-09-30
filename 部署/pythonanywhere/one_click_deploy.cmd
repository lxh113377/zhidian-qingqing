@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0one_click_deploy.ps1" %*
exit /b %ERRORLEVEL%
