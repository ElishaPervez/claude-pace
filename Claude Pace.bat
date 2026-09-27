@echo off
title Claude Pace
rem Prefer the py launcher: a bare "python" can be the Microsoft Store placeholder.
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 "%~dp0claude_pace.py" %*
) else (
  python "%~dp0claude_pace.py" %*
)
if errorlevel 1 pause
