@echo off
rem switchboard's headersHelper for Claude Code on Windows (docs/DESIGN.md section 49.11). Claude Code runs a headersHelper
rem with cmd.exe on Windows, whatever shell its hooks use, so "bin/headers" with no extension finds this file.
rem PowerShell's default policy refuses scripts, so the script starts with the policy lifted for that run only.
rem The plugin's data folder is the one argument.
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0windows\headers.ps1" "%~1"
exit /b %ERRORLEVEL%
