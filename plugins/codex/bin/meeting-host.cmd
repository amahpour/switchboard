@echo off
rem switchboard's meeting host script for Claude Code on Windows (docs/DESIGN.md section 54). Without Git Bash, Claude Code's
rem shell is PowerShell, where "meeting-host" finds this file. PowerShell's default policy refuses scripts, so the script
rem starts with the policy lifted for that run only. The meeting link is read from stdin, which this passes through.
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0windows\meeting-host.ps1" %*
exit /b %ERRORLEVEL%
