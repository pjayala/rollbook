@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
rem Windows entry point, same as ./rollbook: state in .\data, prefers the .venv Python,
rem else Python 3.15, then 3.14, through the py launcher (bin\rollbook refuses < 3.14).
rem   rollbook.cmd app        dashboard in its own window
setlocal
set "ROLLBOOK_HOME=%~dp0data"
set "PY=python"
where py >nul 2>nul && (
  py -3.15 -c "" >nul 2>nul && set "PY=py -3.15" || (py -3.14 -c "" >nul 2>nul && set "PY=py -3.14" || set "PY=py -3")
)
if exist "%~dp0.venv\Scripts\python.exe" set "PY="%~dp0.venv\Scripts\python.exe""
%PY% "%~dp0bin\rollbook" %*
