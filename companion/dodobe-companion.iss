; The dodobe companion's Windows installer (Inno Setup 6). Built by
; .github/workflows/companion-windows.yml; see ..\README.md.
;
; A per-user install (no admin prompt): the tray app into %LOCALAPPDATA%\Programs, a Start menu
; entry, the Run entry that starts it at every sign-in, and it starts now. Uninstalling stops it,
; disconnects the drives it made, removes the saved network login and the Run entry; the settings
; and log in %LOCALAPPDATA%\dodobe-companion are left unless the user chooses otherwise.

#define AppVersion GetEnv("COMPANION_APP_VERSION")
#if AppVersion == ""
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{0669BB8B-FD02-450F-AA02-9DB3B2A6AFE3}
AppName=dodobe companion
AppVersion={#AppVersion}
AppPublisher=dodobe
DefaultDirName={localappdata}\Programs\dodobe companion
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=dodobe-companion-setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\dodobe-companion.exe
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=force

[Files]
Source: "dist\dodobe-companion\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\dodobe companion"; Filename: "{app}\dodobe-companion.exe"

[Registry]
; Start at every sign-in.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "dodobeCompanion"; ValueData: """{app}\dodobe-companion.exe"""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\dodobe-companion.exe"; Description: "Start the dodobe companion now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM dodobe-companion.exe"; Flags: runhidden; RunOnceId: "stop"
Filename: "{app}\dodobe-companion.exe"; Parameters: "--forget"; Flags: runhidden waituntilterminated; RunOnceId: "forget"

[Code]
function DataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\dodobe-companion');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  // An update: stop the running copy so its files can be replaced (the installer starts it again).
  if CurStep = ssInstall then
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM dodobe-companion.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DirExists(DataDir()) then
    // A silent uninstall keeps them (the answer with /SUPPRESSMSGBOXES is No).
    if SuppressibleMsgBox('Also delete the companion''s settings and log in ' + DataDir() + '?',
                          mbConfirmation, MB_YESNO, IDNO) = IDYES then
      DelTree(DataDir(), True, True, True);
end;
