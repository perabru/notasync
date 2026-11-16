@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\NotaSync\browsers"
if not exist "NotaSync.exe" (
  echo Abra ABRIR_NOTASYNC.cmd para preparar e executar o programa.
  pause
  exit /b 1
)
"%~dp0NotaSync.exe" --preparar-navegador
if errorlevel 1 (
  echo Nao foi possivel preparar o navegador. Verifique sua conexao.
) else (
  echo Navegador preparado. Abra NotaSync.exe.
)
pause
