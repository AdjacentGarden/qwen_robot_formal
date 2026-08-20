@echo off
setlocal
set "ADB=C:\Users\Administrator\Documents\Codex\android-sdk\platform-tools\adb.exe"
set "APK=%~dp0release\ideal-robot-debug.apk"
if not exist "%ADB%" (
  echo [ERROR] adb not found: %ADB%
  exit /b 2
)
if not exist "%APK%" (
  echo [ERROR] APK not found: %APK%
  exit /b 3
)
"%ADB%" start-server >nul
for /f "skip=1 tokens=2" %%A in ('"%ADB%" devices') do if "%%A"=="device" set DEVICE_READY=1
if not defined DEVICE_READY (
  echo [ERROR] No authorized Android phone detected. Enable USB debugging and reconnect it.
  exit /b 4
)
"%ADB%" install -r "%APK%"
if errorlevel 1 exit /b %errorlevel%
echo App installed successfully.
