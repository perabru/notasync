; -*- coding: utf-8 -*-
Unicode True
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!ifndef OUTPUT_FILE
  !define OUTPUT_FILE "../NotaSync_Setup_2.3.0.exe"
!endif
Name "NotaSync"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\NotaSync"
InstallDirRegKey HKCU "Software\NotaSync" "InstallDir"
RequestExecutionLevel user
CRCCheck force
SetCompressor /SOLID lzma
SetCompressorDictSize 64
ShowInstDetails show
ShowUninstDetails show
BrandingText "NotaSync  |  Coordenação escolar"
VIProductVersion "2.3.0.0"
VIAddVersionKey /LANG=1046 "ProductName" "NotaSync"
VIAddVersionKey /LANG=1046 "ProductVersion" "2.3.0"
VIAddVersionKey /LANG=1046 "FileVersion" "2.3.0"
VIAddVersionKey /LANG=1046 "CompanyName" "NotaSync"
VIAddVersionKey /LANG=1046 "FileDescription" "Instalador NotaSync para Windows"
VIAddVersionKey /LANG=1046 "LegalCopyright" "NotaSync"
!define MUI_ICON "assets/notasync.ico"
!define MUI_UNICON "assets/notasync.ico"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "assets/header.bmp"
!define MUI_WELCOMEFINISHPAGE_BITMAP "assets/wizard.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "assets/wizard.bmp"
!define MUI_BGCOLOR "FFFFFF"
!define MUI_TEXTCOLOR "25304A"
!define MUI_INSTFILESPAGE_COLORS "51407C FFFFFF"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Suas notas em dia.$\r$\nSem retrabalho."
!define MUI_WELCOMEPAGE_TEXT "O NotaSync automatiza o lançamento das avaliações dos quatro bimestres.$\r$\n$\r$\nPython e bibliotecas já estão incluídos. Este assistente prepara o programa sem comandos manuais.$\r$\n$\r$\nSe houver um Edge utilizável no computador, ele será aproveitado. Caso contrário, um navegador de automação será baixado automaticamente.$\r$\n$\r$\nWindows 10/11 de 64 bits • Instalação no seu usuário"
!define MUI_FINISHPAGE_TITLE "Pronto para começar."
!define MUI_FINISHPAGE_TEXT "A instalação foi conferida e o NotaSync está pronto.$\r$\n$\r$\nAbra pelo atalho, selecione a planilha e as avaliações e escolha entre pular, testar resumido ou testar completo.$\r$\n$\r$\nUse Encerrar programa antes de instalar uma atualização."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Abrir NotaSync agora"
!define MUI_FINISHPAGE_RUN_FUNCTION "LaunchNotaSync"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "PortugueseBR"
Var StageResult
Var StageName
Var InstallerMutex
Var InstallSucceeded
!macro CHECK_APP_STOPPED PREFIX
Function ${PREFIX}CheckAppStopped
  IfFileExists "$LOCALAPPDATA\NotaSync\instancia.lock" 0 check_done
  System::Call 'kernel32::CreateFileW(w "$LOCALAPPDATA\NotaSync\instancia.lock", i 0xC0000000, i 3, p 0, i 3, i 0x80, p 0) p .r0'
  ${If} $0 != -1
    System::Call 'kernel32::LockFile(p r0, i 0, i 0, i 1, i 0) i .r1'
    ${If} $1 = 0
      System::Call 'kernel32::CloseHandle(p r0)'
      MessageBox MB_OK|MB_ICONINFORMATION "O NotaSync está aberto. Use Encerrar programa no painel e execute este instalador novamente. Um lançamento em andamento não será interrompido."
      Abort
    ${Else}
      System::Call 'kernel32::UnlockFile(p r0, i 0, i 0, i 1, i 0)'
      System::Call 'kernel32::CloseHandle(p r0)'
    ${EndIf}
  ${Else}
    MessageBox MB_OK|MB_ICONINFORMATION "Não foi possível conferir se o NotaSync está aberto. Feche o programa e tente novamente."
    Abort
  ${EndIf}
  check_done:
FunctionEnd
!macroend
!insertmacro CHECK_APP_STOPPED ""
!insertmacro CHECK_APP_STOPPED "un."
Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "O NotaSync precisa de Windows de 64 bits."
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_OK|MB_ICONSTOP "O NotaSync precisa de Windows 10 ou Windows 11."
    Abort
  ${EndIf}
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "NotaSyncInstaller") p .r0 ?e'
  Pop $1
  ${If} $1 = 183
    MessageBox MB_OK|MB_ICONINFORMATION "Outra instalação do NotaSync está em andamento. Aguarde."
    Abort
  ${EndIf}
  StrCpy $InstallerMutex $0
  StrCpy $InstallSucceeded "0"
  SetShellVarContext current
  Call CheckAppStopped
FunctionEnd
Function InstallFailed
  DetailPrint "Falha na etapa: $StageName (código $StageResult)."
  MessageBox MB_OK|MB_ICONSTOP "A instalação parou na etapa: $StageName.$\r$\nCódigo: $StageResult.$\r$\n$\r$\nO programa não foi marcado como pronto.$\r$\nRelatório detalhado:$\r$\n$LOCALAPPDATA\NotaSync\instalacao.log"
  SetErrorLevel 1
  Abort
FunctionEnd
Section "Programa e componentes incluídos" SEC_APP
  SectionIn RO
  StrCpy $StageName "Extrair programa e componentes"
  Delete "$INSTDIR\install\ready.txt"
  DetailPrint "1 de 4 • Extraindo o programa e os componentes incluídos..."
  SetOutPath "$INSTDIR\app"
  ClearErrors
  File /r /x "__pycache__" /x "*.pyc" "../notasync"
  File "../requirements.txt"
  File "../requirements-windows.lock"
  File "../LEIA_PRIMEIRO.html"
  File "abrir.pyw"
  IfErrors 0 +3
    StrCpy $StageResult "Extração do programa"
    Call InstallFailed
  SetOutPath "$INSTDIR\install"
  File "verify_install.py"
  File "assets/notasync.ico"
  File "vendor/runtime_manifest.json"
  ; Ambiente e bibliotecas verificados antes da compilação. Sem pip ou script de shell.
  SetOutPath "$INSTDIR\runtime"
  ClearErrors
  File /r /x "__pycache__" /x "*.pyc" "vendor/runtime/*"
  IfErrors 0 +3
    StrCpy $StageResult "Extração do ambiente"
    Call InstallFailed
  WriteUninstaller "$INSTDIR\Desinstalar.exe"
  WriteRegStr HKCU "Software\NotaSync" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NotaSync" "DisplayName" "NotaSync (preparando instalação)"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NotaSync" "DisplayVersion" "2.3.0"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NotaSync" "Publisher" "NotaSync"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NotaSync" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NotaSync" "DisplayIcon" "$INSTDIR\install\notasync.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NotaSync" "UninstallString" '$\"$INSTDIR\Desinstalar.exe$\"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NotaSync" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NotaSync" "NoRepair" 1
  AddSize 700000
SectionEnd
Section "Conferir ambiente" SEC_RUNTIME
  SectionIn RO
  StrCpy $StageName "Conferir Python e bibliotecas"
  DetailPrint "2 de 4 • Conferindo o ambiente incluído..."
  nsExec::ExecToLog '"$INSTDIR\runtime\python.exe" "$INSTDIR\install\verify_install.py" --stage runtime'
  Pop $StageResult
  ${If} $StageResult != 0
    Call InstallFailed
  ${EndIf}
SectionEnd
Section "Preparar navegador" SEC_BROWSER
  SectionIn RO
  StrCpy $StageName "Preparar navegador de automação"
  DetailPrint "3 de 4 • Conferindo o navegador disponível..."
  DetailPrint "Um download será feito somente se não houver navegador utilizável."
  nsExec::ExecToLog '"$INSTDIR\runtime\python.exe" "$INSTDIR\install\verify_install.py" --stage browser --install'
  Pop $StageResult
  ${If} $StageResult != 0
    Call InstallFailed
  ${EndIf}
SectionEnd
Section "Finalizar" SEC_FINISH
  SectionIn RO
  StrCpy $StageName "Verificação final"
  DetailPrint "4 de 4 • Conferindo a instalação..."
  nsExec::ExecToLog '"$INSTDIR\runtime\python.exe" "$INSTDIR\install\verify_install.py" --stage verify'
  Pop $StageResult
  ${If} $StageResult != 0
    Call InstallFailed
  ${EndIf}
  SetOutPath "$INSTDIR\app"
  CreateDirectory "$SMPROGRAMS\NotaSync"
  CreateShortcut "$SMPROGRAMS\NotaSync\NotaSync.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\abrir.pyw"' "$INSTDIR\install\notasync.ico" 0 SW_SHOWNORMAL "" "Automatizar lançamento de notas"
  CreateShortcut "$DESKTOP\NotaSync.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\abrir.pyw"' "$INSTDIR\install\notasync.ico" 0 SW_SHOWNORMAL "" "Automatizar lançamento de notas"
  CreateShortcut "$SMPROGRAMS\NotaSync\Guia de uso.lnk" "$INSTDIR\app\LEIA_PRIMEIRO.html"
  CreateShortcut "$SMPROGRAMS\NotaSync\Desinstalar NotaSync.lnk" "$INSTDIR\Desinstalar.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NotaSync" "DisplayName" "NotaSync"
  FileOpen $0 "$INSTDIR\install\ready.txt" w
  FileWrite $0 "NotaSync 2.3.0 - instalado e conferido"
  FileClose $0
  StrCpy $InstallSucceeded "1"
  DetailPrint "NotaSync instalado e conferido."
SectionEnd
Function LaunchNotaSync
  ${If} $InstallSucceeded = 1
    SetOutPath "$INSTDIR\app"
    Exec '"$INSTDIR\runtime\pythonw.exe" "$INSTDIR\app\abrir.pyw"'
  ${EndIf}
FunctionEnd
Function un.onInit
  SetShellVarContext current
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "NotaSyncInstaller") p .r0 ?e'
  Pop $1
  ${If} $1 = 183
    MessageBox MB_OK|MB_ICONINFORMATION "Aguarde a outra instalação do NotaSync terminar."
    Abort
  ${EndIf}
  StrCpy $InstallerMutex $0
  Call un.CheckAppStopped
FunctionEnd
Section "Uninstall"
  Delete "$DESKTOP\NotaSync.lnk"
  Delete "$SMPROGRAMS\NotaSync\NotaSync.lnk"
  Delete "$SMPROGRAMS\NotaSync\Guia de uso.lnk"
  Delete "$SMPROGRAMS\NotaSync\Desinstalar NotaSync.lnk"
  RMDir "$SMPROGRAMS\NotaSync"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\runtime"
  RMDir /r "$INSTDIR\install"
  RMDir /r "$LOCALAPPDATA\NotaSync\browsers"
  Delete "$INSTDIR\Desinstalar.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NotaSync"
  DeleteRegKey HKCU "Software\NotaSync"
  DetailPrint "NotaSync removido. Histórico e configurações preservados."
SectionEnd
