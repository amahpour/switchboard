@echo off
rem switchboard's headersHelper for Claude Code on Windows (docs/DESIGN.md section 49.11). There is no listener key
rem on native Windows yet, so this sends no headers: the scripts there send a typed token only. Without Git
rem Bash, Claude Code runs hooks and this helper in PowerShell, where "./headers" finds this file.
echo {}
exit /b 0
