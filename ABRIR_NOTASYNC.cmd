@echo off
setlocal
chcp 65001 >nul
title NotaSync - 4o bimestre
cd /d "%~dp0"
echo.
echo  NotaSync - Preparando o programa...
echo  Na primeira abertura, os componentes necessarios serao baixados.
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar.ps1"
if errorlevel 1 (
  echo.
  echo  Nao foi possivel iniciar. Confira a mensagem acima.
  echo  O guia LEIA_PRIMEIRO.html explica os requisitos.
  pause
)
endlocal
