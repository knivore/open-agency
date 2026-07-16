@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "RUN_LOG=%TEMP%\agency-run-windows.log"
set "AGENCY_RUN_LOG=%RUN_LOG%"
if not defined AGENCY_FE_DIR set "AGENCY_FE_DIR=%SCRIPT_DIR%..\open-agency-fe"
if not defined AGENCY_FRONTEND_HOST_WORKSPACE set "AGENCY_FRONTEND_HOST_WORKSPACE=%AGENCY_FE_DIR%"
set "GIT_BASH=%ProgramFiles%\Git\bin\bash.exe"

if not exist "%GIT_BASH%" (
  set "GIT_BASH=%ProgramFiles%\Git\usr\bin\bash.exe"
)

if not exist "%GIT_BASH%" (
  echo Git Bash was not found.
  echo Install Git for Windows, then run this command again.
  pause
  exit /b 1
)

pushd "%SCRIPT_DIR%" >nul
"%GIT_BASH%" "%SCRIPT_DIR%scripts/launcher/run-windows.sh" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul

if not "%EXIT_CODE%"=="0" (
  echo.
  echo run-windows.sh exited with code %EXIT_CODE%.
  echo Full log: %RUN_LOG%
)

exit /b %EXIT_CODE%
