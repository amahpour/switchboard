@echo off
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0codex-headers.ps1" %*
exit /b %ERRORLEVEL%
