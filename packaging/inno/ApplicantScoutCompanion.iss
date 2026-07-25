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
Filename: "{app}\ApplicantScout.exe"; Parameters: "--show-settings"; Flags: nowait skipifnotsilent; Check: ShouldRelaunchAfterInstall

[Code]
const
  ProcessProbeFailed = -1;
  ProcessAbsent = 0;
  ProcessRunning = 1;
  ProcessExitPollAttempts = 10;
  ProcessExitPollMilliseconds = 500;

var
  CompanionWasRunning: Boolean;
  SelfUpdateWasRequested: Boolean;
  ShutdownWasConfirmed: Boolean;

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
    '$ErrorActionPreference = ''Stop''; try { ' +
    '$target = ' + Target + '; ' +
    '$fullTarget = [System.IO.Path]::GetFullPath($target); ' +
    '$candidates = @(Get-CimInstance Win32_Process -OperationTimeoutSec 5 | Where-Object { ' +
    '$_.Name -ieq ''ApplicantScout.exe'' }); ' +
    'if ($candidates | Where-Object { -not $_.ExecutablePath }) { exit 2 }; ' +
    '$procs = @($candidates | Where-Object { ' +
    '([System.IO.Path]::GetFullPath($_.ExecutablePath) -ieq $fullTarget) ' +
    '}); ';
  if Terminate then begin
    Result := Result +
      'foreach ($p in $procs) { ' +
      '$terminateResult = Invoke-CimMethod -InputObject $p -MethodName Terminate -OperationTimeoutSec 5; ' +
      'if ($terminateResult.ReturnValue -ne 0) { exit 3 } }; ' +
      'exit 0 } catch { exit 2 }"';
  end else begin
    Result := Result +
      'if ($procs) { exit 0 } else { exit 1 } } catch { exit 2 }"';
  end;
end;

function SelfUpdateRequested(): Boolean;
begin
  Result := ExpandConstant('{param:APSCOUT_SELFUPDATE|0}') = '1';
end;

function SelfUpdateSourcePid(): Integer;
begin
  Result := StrToIntDef(ExpandConstant('{param:APSCOUT_SOURCE_PID|0}'), 0);
end;

function SelfUpdateSourcePath(): String;
begin
  Result := ExpandConstant('{param:APSCOUT_SOURCE_PATH|}');
end;

function SelfUpdateProcessScript(Terminate: Boolean): String;
var
  SourcePath: String;
  SourcePid: String;
begin
  SourcePath := PowerShellSingleQuoted(SelfUpdateSourcePath());
  SourcePid := IntToStr(SelfUpdateSourcePid());
  Result :=
    '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference = ''Stop''; try { ' +
    '$target = ' + SourcePath + '; ' +
    '$sourcePid = ' + SourcePid + '; ' +
    'if ($sourcePid -le 0 -or [string]::IsNullOrWhiteSpace($target)) { exit 2 }; ' +
    '$fullTarget = [System.IO.Path]::GetFullPath($target); ' +
    '$candidates = @(Get-CimInstance Win32_Process -OperationTimeoutSec 5 | Where-Object { ' +
    '$_.ProcessId -eq $sourcePid }); ' +
    'if ($candidates | Where-Object { -not $_.ExecutablePath }) { exit 2 }; ' +
    '$procs = @($candidates | Where-Object { ' +
    '([System.IO.Path]::GetFullPath($_.ExecutablePath) -ieq $fullTarget) ' +
    '}); ';
  if Terminate then begin
    Result := Result +
      'foreach ($p in $procs) { ' +
      '$terminateResult = Invoke-CimMethod -InputObject $p -MethodName Terminate -OperationTimeoutSec 5; ' +
      'if ($terminateResult.ReturnValue -ne 0) { exit 3 } }; ' +
      'exit 0 } catch { exit 2 }"';
  end else begin
    Result := Result +
      'if ($procs) { exit 0 } else { exit 1 } } catch { exit 2 }"';
  end;
end;

function ExecuteProcessProbe(Parameters: String): Integer;
var
  ResultCode: Integer;
begin
  if not Exec(
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Parameters,
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then begin
    Result := ProcessProbeFailed;
    Exit;
  end;
  if ResultCode = 0 then begin
    Result := ProcessRunning;
  end else if ResultCode = 1 then begin
    Result := ProcessAbsent;
  end else begin
    Result := ProcessProbeFailed;
  end;
end;

function ExecuteProcessTermination(Parameters: String): Boolean;
var
  ResultCode: Integer;
begin
  if not Exec(
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Parameters,
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then begin
    Result := False;
    Exit;
  end;
  Result := ResultCode = 0;
end;

function ProbeCompanionProcess(): Integer;
begin
  Result := ExecuteProcessProbe(CompanionProcessScript(False));
end;

function ProbeSelfUpdateProcess(): Integer;
begin
  Result := ExecuteProcessProbe(SelfUpdateProcessScript(False));
end;

function WaitForCompanionExit(): Integer;
var
  Attempt: Integer;
begin
  for Attempt := 1 to ProcessExitPollAttempts do begin
    Sleep(ProcessExitPollMilliseconds);
    Result := ProbeCompanionProcess();
    if Result <> ProcessRunning then begin
      Exit;
    end;
  end;
  Result := ProcessRunning;
end;

function WaitForSelfUpdateSourceExit(): Integer;
var
  Attempt: Integer;
begin
  for Attempt := 1 to ProcessExitPollAttempts do begin
    Sleep(ProcessExitPollMilliseconds);
    Result := ProbeSelfUpdateProcess();
    if Result <> ProcessRunning then begin
      Exit;
    end;
  end;
  Result := ProcessRunning;
end;

function CloseSelfUpdateSource(): Boolean;
var
  ResultCode: Integer;
  State: Integer;
begin
  Result := False;
  if not SelfUpdateRequested() then begin
    Result := True;
    Exit;
  end;
  if SelfUpdateSourcePid() <= 0 then begin
    Exit;
  end;
  if SelfUpdateSourcePath() = '' then begin
    Exit;
  end;

  State := ProbeSelfUpdateProcess();
  if State = ProcessProbeFailed then begin
    Exit;
  end;
  if State = ProcessAbsent then begin
    Result := True;
    Exit;
  end;

  { WHY: Self-update may come from a portable or legacy path. Ask that exact
     source process to quit, then poll the original PID/path before fallback. }
  if FileExists(SelfUpdateSourcePath()) then begin
    Exec(
      SelfUpdateSourcePath(),
      '--shutdown-running-instance',
      '',
      SW_HIDE,
      ewNoWait,
      ResultCode
    );
  end;

  State := WaitForSelfUpdateSourceExit();
  if State = ProcessAbsent then begin
    Result := True;
    Exit;
  end;
  if State = ProcessProbeFailed then begin
    Exit;
  end;
  if not ExecuteProcessTermination(SelfUpdateProcessScript(True)) then begin
    Exit;
  end;
  Result := WaitForSelfUpdateSourceExit() = ProcessAbsent;
end;

function CloseRunningCompanion(): Boolean;
var
  ResultCode: Integer;
  State: Integer;
begin
  Result := False;
  State := ProbeCompanionProcess();
  if State = ProcessProbeFailed then begin
    Exit;
  end;
  if State = ProcessAbsent then begin
    Result := True;
    Exit;
  end;
  CompanionWasRunning := True;

  { WHY: The tray app may keep ApplicantScout.exe running with no visible window;
     Inno Restart Manager then shows a confusing manual-close prompt. }
  if FileExists(ExpandConstant('{app}\ApplicantScout.exe')) then begin
    { WARNING: Do not wait here. Older builds treat the shutdown flag as a
      normal app launch and would block the installer until fallback runs. }
    Exec(
      ExpandConstant('{app}\ApplicantScout.exe'),
      '--shutdown-running-instance',
      '',
      SW_HIDE,
      ewNoWait,
      ResultCode
    );
  end;

  State := WaitForCompanionExit();
  if State = ProcessAbsent then begin
    Result := True;
    Exit;
  end;
  if State = ProcessProbeFailed then begin
    Exit;
  end;
  if not ExecuteProcessTermination(CompanionProcessScript(True)) then begin
    Exit;
  end;
  Result := WaitForCompanionExit() = ProcessAbsent;
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

function ShouldRelaunchAfterInstall(): Boolean;
begin
  Result := ShutdownWasConfirmed and (CompanionWasRunning or SelfUpdateWasRequested);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  CompanionWasRunning := False;
  ShutdownWasConfirmed := False;
  SelfUpdateWasRequested := SelfUpdateRequested();
  if SelfUpdateWasRequested then begin
    if not CloseSelfUpdateSource() then begin
      Result := 'Could not verify that the self-update source stopped. Close ApplicantScout Companion and try again.';
      Exit;
    end;
  end;
  if not CloseRunningCompanion() then begin
    Result := 'Could not verify that the installed ApplicantScout Companion stopped. Close it and try again.';
    Exit;
  end;
  RemoveLegacyPerMachineShortcuts();
  ShutdownWasConfirmed := True;
end;

function InitializeUninstall(): Boolean;
begin
  CompanionWasRunning := False;
  Result := CloseRunningCompanion();
end;
