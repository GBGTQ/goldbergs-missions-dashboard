@echo off
setlocal
echo ============================================================
echo   Goldberg's Springshot Dashboard — Manual Refresh
echo ============================================================
echo.

REM ── Find Python ─────────────────────────────────────────────────────────────
set PYTHON_EXE=
where python >nul 2>&1 && set PYTHON_EXE=python
if not defined PYTHON_EXE (
  where py >nul 2>&1 && set PYTHON_EXE=py
)
if not defined PYTHON_EXE (
  echo [error] Python not found. Run setup.bat first.
  pause & exit /b 1
)

REM ── Run the refresh script ───────────────────────────────────────────────────
set SCRIPT="%~dp0springshot_full_refresh.py"
echo [run] Starting refresh at %DATE% %TIME%
echo.
%PYTHON_EXE% %SCRIPT%
if errorlevel 1 (
  echo.
  echo [error] Refresh failed — see automation\refresh.log for details.
) else (
  echo.
  echo [done] Dashboard updated successfully.
)
echo.
pause
endlocal
