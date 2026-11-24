@echo off
rem  Version Keeper launcher
rem  Double-click to start in tray; the web UI opens automatically.
rem  The repo location is read from repo.txt (or .\repo by default).
setlocal
cd /d "%~dp0"

set "RUN="
for %%P in (pythonw.exe) do if not defined RUN set "RUN=%%~$PATH:P"
if not defined RUN for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%~fD\pythonw.exe" if not defined RUN set "RUN=%%~fD\pythonw.exe"
)
if not defined RUN if exist "C:\Program Files\python\pythonw.exe" set "RUN=C:\Program Files\python\pythonw.exe"
if not defined RUN for %%P in (python.exe) do if not defined RUN set "RUN=%%~$PATH:P"

if defined RUN goto run

echo.
echo   [Version Keeper] Python not found on this computer.
echo.
echo   Install Python 3.10+ from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during setup, then try again.
echo.
pause
exit /b 1

:run
start "" "%RUN%" "%~dp0server.py" --tray
exit /b 0
