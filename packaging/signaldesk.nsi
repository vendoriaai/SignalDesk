; NSIS installer for SignalDesk (Windows). Build: makensis /DVERSION=1.0.0 packaging/signaldesk.nsi
; Expects the PyInstaller onedir bundle at dist\signaldesk\.
!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!define NAME    "SignalDesk"
!define EXE     "signaldesk.exe"
!define PUBLISHER "SignalDesk contributors"
!define URL     "https://github.com/signaldesk/signaldesk"

Unicode true
Name "${NAME}"
OutFile "dist\SignalDesk-${VERSION}-windows-amd64.exe"
InstallDir "$LOCALAPPDATA\Programs\${NAME}"
RequestExecutionLevel user ; no admin needed (per-user install)
!include "MUI2.nsh"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetOutPath "$INSTDIR"
  File /r "dist\signaldesk\*.*"
  CreateShortcut "$SMPROGRAMS\${NAME}.lnk" "$INSTDIR\${EXE}" "desktop"
  CreateShortcut "$DESKTOP\${NAME}.lnk" "$INSTDIR\${EXE}" "desktop"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${NAME}" "DisplayName" "${NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${NAME}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${NAME}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${NAME}" "URLInfoAbout" "${URL}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${NAME}" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR"
  Delete "$SMPROGRAMS\${NAME}.lnk"
  Delete "$DESKTOP\${NAME}.lnk"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${NAME}"
SectionEnd
