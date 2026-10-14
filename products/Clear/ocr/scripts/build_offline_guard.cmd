@echo off
setlocal
if "%~1"=="" (
  echo Usage: build_offline_guard.cmd "bundle-directory" ["vcvars64.bat"]
  exit /b 2
)
if not "%~2"=="" call "%~2" >nul
where cl >nul 2>nul
if errorlevel 1 (
  echo Run in an x64 Native Tools Command Prompt or provide vcvars64.bat.
  exit /b 1
)
if not exist "%~1\tools" mkdir "%~1\tools"
cl /nologo /std:c++17 /EHsc /O2 /MT /D_UNICODE /DUNICODE "%~dp0offline_guard.cpp" /Fo"%~1\tools\offline_guard.obj" /Fe"%~1\tools\offline_guard.exe" /link /SUBSYSTEM:CONSOLE
exit /b %errorlevel%
