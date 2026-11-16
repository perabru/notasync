@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "NS_COMPILER="
for %%F in ("%ProgramFiles(x86)%\NSIS\makensis.exe" "%ProgramFiles%\NSIS\makensis.exe") do if exist "%%~F" set "NS_COMPILER=%%~F"
if not defined NS_COMPILER (
  echo Instale o compilador NSIS 3 oficial para gerar o instalador.
  echo https://nsis.sourceforge.io/Download
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Prepare-Vendor.ps1"
if errorlevel 1 (
  echo Nao foi possivel obter os componentes oficiais do instalador.
  pause
  exit /b 1
)
py -3 "%~dp0bundle_runtime.py"
if errorlevel 1 (
  echo Nao foi possivel preparar o ambiente do aplicativo.
  pause
  exit /b 1
)
"%NS_COMPILER%" /V3 NotaSync.nsi
if errorlevel 1 (
  echo Nao foi possivel gerar o instalador. Confira as mensagens acima.
  pause
  exit /b 1
)
echo Instalador gerado em ..\NotaSync_Setup_2.3.0.exe
pause
