@echo off
setlocal
cd /d "%~dp0"
call npm install || exit /b 1
call npx cap sync android || exit /b 1
call android\gradlew.bat -p android assembleDebug lintDebug --no-daemon || exit /b 1
copy /y android\app\build\outputs\apk\debug\app-debug.apk release\ideal-robot-debug.apk >nul
echo APK ready: %~dp0release\ideal-robot-debug.apk
