@echo off
setlocal
chcp 65001 >nul
title Gerar executavel NotaSync
cd /d "%~dp0"
set "NS_BUILD_PY=%LOCALAPPDATA%\NotaSync\venv\Scripts\python.exe"
if not exist "%NS_BUILD_PY%" (
  echo Abra ABRIR_NOTASYNC.cmd uma vez para instalar os componentes.
  pause
  exit /b 1
)
"%NS_BUILD_PY%" -m pip install --disable-pip-version-check "pyinstaller>=6.10,<7"
if errorlevel 1 goto failed
"%NS_BUILD_PY%" -m PyInstaller --clean --noconfirm --onedir --name NotaSync --add-data "notasync\static;notasync\static" --collect-all playwright --collect-submodules notasync --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on --paths . iniciar_exe.py
if errorlevel 1 goto failed
copy /y "LEIA_PRIMEIRO.html" "dist\NotaSync\LEIA_PRIMEIRO.html" >nul
copy /y "PREPARAR_NAVEGADOR.cmd" "dist\NotaSync\PREPARAR_NAVEGADOR.cmd" >nul
echo.
echo Executavel gerado em dist\NotaSync\NotaSync.exe.
echo Entregue a pasta NotaSync inteira. O navegador precisa estar instalado no computador de destino.
echo Use PREPARAR_NAVEGADOR.cmd no destino ou abra a versao Python uma vez.
pause
exit /b 0
:failed
echo Nao foi possivel gerar o executavel. Confira as mensagens acima.
pause
exit /b 1
