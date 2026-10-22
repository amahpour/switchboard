@echo off
rem switchboard's listener for Claude Code on Windows (docs/DESIGN.md section 49.4). Without Git Bash,
rem Claude Code runs hooks in PowerShell, where "./listen" finds this file. PowerShell's default
rem policy refuses scripts, so the script starts with the policy lifted for that run only.
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0windows\listen.ps1"
exit /b %ERRORLEVEL%
