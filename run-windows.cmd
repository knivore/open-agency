@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "GIT_BASH=%ProgramFiles%\Git\bin\bash.exe"

if not exist "%GIT_BASH%" (
  set "GIT_BASH=%ProgramFiles%\Git\usr\bin\bash.exe"
)

if not exist "%GIT_BASH%" (
  echo Git Bash was not found.
  echo Install Git for Windows or run: bash ./run-windows.sh start
  pause
  exit /b 1
)

pushd "%SCRIPT_DIR%" >nul
set "RUN_LOG=%TEMP%\open-agency-run-windows.log"
"%GIT_BASH%" "%SCRIPT_DIR%run-windows.sh" %* > "%RUN_LOG%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul

type "%RUN_LOG%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo run-windows.sh exited with code %EXIT_CODE%.
  echo Full log: %RUN_LOG%
  pause
)

exit /b %EXIT_CODE%
