#define MyAppName "ApplicantScout Companion"
#define MyAppUserModelID "Antrakt.ApplicantScout.Companion"
#define EnvVersion GetEnv("APSCOUT_INNO_VERSION")
#define EnvSourceDir GetEnv("APSCOUT_INNO_SOURCE_DIR")
#define EnvIcon GetEnv("APSCOUT_INNO_ICON")
#if EnvVersion == ""
#error "Missing APSCOUT_INNO_VERSION. Run scripts\\build-windows.ps1 instead of invoking iscc directly."
#endif
#if EnvSourceDir == ""
#error "Missing APSCOUT_INNO_SOURCE_DIR. Run scripts\\build-windows.ps1 instead of invoking iscc directly."
#endif
#if EnvIcon == ""
#error "Missing APSCOUT_INNO_ICON. Run scripts\\build-windows.ps1 instead of invoking iscc directly."
#endif
#define MyAppVersion EnvVersion
#define MyAppSourceDir EnvSourceDir

[Setup]
AppId={{9A68DF9E-3784-42A2-9B9B-F99024F1C37F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Antrakt
DefaultDirName={localappdata}\Programs\ApplicantScout Companion
DefaultGroupName=ApplicantScout Companion
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UsePreviousAppDir=no
UninstallDisplayIcon={app}\ApplicantScout.exe
SetupIconFile={#EnvIcon}
OutputDir=..\..\dist
OutputBaseFilename=ApplicantScoutCompanionSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
SetupMutex=Antrakt.ApplicantScout.Companion.Setup

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\ApplicantScout Companion"; Filename: "{app}\ApplicantScout.exe"; IconFilename: "{app}\ApplicantScout.exe"; AppUserModelID: {#MyAppUserModelID}
Name: "{autodesktop}\ApplicantScout Companion"; Filename: "{app}\ApplicantScout.exe"; IconFilename: "{app}\ApplicantScout.exe"; AppUserModelID: {#MyAppUserModelID}; Tasks: desktopicon

[Run]
Filename: "{app}\ApplicantScout.exe"; Parameters: "--show-settings"; Description: "Launch ApplicantScout Companion"; Flags: nowait postinstall skipifsilent
Filename: "{app}\ApplicantScout.exe"; Flags: nowait skipifnotsilent; Check: WasCompanionRunningBeforeInstall

[Code]
var
  CompanionWasRunning: Boolean;

function PowerShellSingleQuoted(Value: String): String;
begin
  Result := Value;
  StringChangeEx(Result, '''', '''''', True);
  Result := '''' + Result + '''';
end;

function CompanionProcessScript(Terminate: Boolean): String;
var
  Target: String;
begin
  Target := PowerShellSingleQuoted(ExpandConstant('{app}\ApplicantScout.exe'));
  Result :=
    '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$target = ' + Target + '; ' +
    '$procs = Get-CimInstance Win32_Process | Where-Object { ' +
    '$_.Name -ieq ''ApplicantScout.exe'' -and $_.ExecutablePath -and ' +
    '([System.IO.Path]::GetFullPath($_.ExecutablePath) -ieq [System.IO.Path]::GetFullPath($target)) ' +
    '}; ';
  if Terminate then begin
    Result := Result +
      'foreach ($p in $procs) { Invoke-CimMethod -InputObject $p -MethodName Terminate | Out-Null }; exit 0"';
  end else begin
    Result := Result + 'if ($procs) { exit 0 } else { exit 1 }"';
  end;
end;

function IsCompanionRunning(): Boolean;
var
  ResultCode: Integer;
begin
  Exec(
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    CompanionProcessScript(False),
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  Result := ResultCode = 0;
end;

procedure CloseRunningCompanion();
var
  ResultCode: Integer;
begin
  { WHY: The tray app may keep ApplicantScout.exe running with no visible window;
     Inno Restart Manager then shows a confusing manual-close prompt. }
  if FileExists(ExpandConstant('{app}\ApplicantScout.exe')) then begin
    { WARNING: Do not wait here. Older builds treat the shutdown flag as a
      normal app launch and would block the installer until taskkill runs. }
    Exec(
      ExpandConstant('{app}\ApplicantScout.exe'),
      '--shutdown-running-instance',
      '',
      SW_HIDE,
      ewNoWait,
      ResultCode
    );
    Sleep(1500);
  end;

  if not IsCompanionRunning() then begin
    Exit;
  end;

  Exec(
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    CompanionProcessScript(True),
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  Sleep(500);
end;

procedure RemoveLegacyPerMachineShortcuts();
begin
  { WHY: Builds before the per-user installer could create common shortcuts
    pointing at Program Files. A non-admin updater cannot guarantee deletion of
    protected files, but deleting writable legacy shortcuts prevents most stale
    launcher confusion after migrating to the per-user app directory. }
  DeleteFile(ExpandConstant('{commondesktop}\ApplicantScout Companion.lnk'));
  DeleteFile(ExpandConstant('{commonprograms}\ApplicantScout Companion\ApplicantScout Companion.lnk'));
  RemoveDir(ExpandConstant('{commonprograms}\ApplicantScout Companion'));
end;

function WasCompanionRunningBeforeInstall(): Boolean;
begin
  Result := CompanionWasRunning;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CompanionWasRunning := IsCompanionRunning();
  if CompanionWasRunning then begin
    CloseRunningCompanion();
  end;
  RemoveLegacyPerMachineShortcuts();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  CloseRunningCompanion();
  Result := True;
end;
