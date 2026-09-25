#define MyAppName "ApplicantScout Companion"
#define MyAppUserModelID "Antrakt.ApplicantScout.Companion"
#define EnvVersion GetEnv("APSCOUT_INNO_VERSION")
#define EnvSourceDir GetEnv("APSCOUT_INNO_SOURCE_DIR")
#define EnvIcon GetEnv("APSCOUT_INNO_ICON")
#if Ver != EncodeVer(6, 7, 1, 0)
#error "ApplicantScout release installers require Inno Setup 6.7.1 exactly."
#endif
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
AppVerName={#MyAppName} {#MyAppVersion}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoDescription=ApplicantScout Companion Setup
VersionInfoProductName={#MyAppName}
VersionInfoCompany=Antrakt
AppPublisher=Antrakt
DefaultDirName={localappdata}\Programs\ApplicantScout Companion
DefaultGroupName=ApplicantScout Companion
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; WHY: custom install locations must survive updates. Without this, a silent
; update reinstalls into the default directory and the custom install is
; orphaned while the updater keeps launching /DIR at the old path.
UsePreviousAppDir=yes
UninstallDisplayIcon={app}\current\ApplicantScout.exe
SetupIconFile={#EnvIcon}
OutputDir=..\..\dist
OutputBaseFilename=ApplicantScoutCompanionSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
; WHY: the staged current/next/backup transaction briefly needs ~2-3x the
; payload on disk. Reserve 1 GiB up front; PrepareToInstall re-checks the real
; measured sizes fail-closed before any process is stopped.
ExtraDiskSpaceRequired=1073741824
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
RedirectionGuard=yes
SetupMutex=Antrakt.ApplicantScout.Companion.Setup

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Copy the complete candidate before touching the working payload. The code
; below promotes this directory only after extraction and validation finish.
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}\.apscout-next"; Excludes: ".apscout-payload-version"; Flags: ignoreversion recursesubdirs createallsubdirs
; This marker is deliberately the final file. Promotion therefore happens
; inside the Files phase, before Inno creates shortcuts or finalizes install
; metadata. The scripted destination treats early enumeration as read-only;
; its actual Files-phase transaction catches ordinary skippable errors,
; performs its own rollback, and raises EAbort to force Inno rollback.
Source: "{#MyAppSourceDir}\.apscout-payload-version"; DestDir: "{app}\.apscout-next"; Flags: ignoreversion
Source: "{#MyAppSourceDir}\.apscout-payload-version"; DestDir: "{code:CommitPayloadSwapForInstall}"; Flags: ignoreversion onlyifdoesntexist

[UninstallDelete]
Type: filesandordirs; Name: "{app}\current"; Check: UninstallPayloadDeletionAllowed
Type: filesandordirs; Name: "{app}\.apscout-next"; Check: UninstallPayloadDeletionAllowed
Type: filesandordirs; Name: "{app}\.apscout-backup"; Check: UninstallPayloadDeletionAllowed
Type: files; Name: "{app}\.apscout-promotion-pending"; Check: UninstallPayloadDeletionAllowed
; Remove the per-user logon shortcut even though companion user data itself is
; preserved. The StartupApproved registry value is removed in code below.
Type: files; Name: "{userstartup}\ApplicantScout Companion.lnk"
; Remove payloads from builds that predate the staged current/next layout.
Type: filesandordirs; Name: "{app}\_internal"; Check: UninstallPayloadDeletionAllowed
Type: filesandordirs; Name: "{app}\licenses"; Check: UninstallPayloadDeletionAllowed
Type: files; Name: "{app}\ApplicantScout.exe"; Check: UninstallPayloadDeletionAllowed
Type: files; Name: "{app}\LICENSE"; Check: UninstallPayloadDeletionAllowed
Type: files; Name: "{app}\THIRD-PARTY-NOTICES.md"; Check: UninstallPayloadDeletionAllowed
Type: files; Name: "{app}\RELEASE_NOTES.md"; Check: UninstallPayloadDeletionAllowed

[Icons]
Name: "{group}\ApplicantScout Companion"; Filename: "{app}\current\ApplicantScout.exe"; IconFilename: "{app}\current\ApplicantScout.exe"; AppUserModelID: {#MyAppUserModelID}
Name: "{autodesktop}\ApplicantScout Companion"; Filename: "{app}\current\ApplicantScout.exe"; IconFilename: "{app}\current\ApplicantScout.exe"; AppUserModelID: {#MyAppUserModelID}; Tasks: desktopicon

[Run]
Filename: "{app}\current\ApplicantScout.exe"; Parameters: "--show-settings"; Description: "Launch ApplicantScout Companion"; Flags: nowait postinstall skipifsilent
Filename: "{app}\current\ApplicantScout.exe"; Parameters: "--show-settings"; Flags: nowait skipifnotsilent; Check: ShouldRelaunchAfterInstall

[Code]
const
  ProcessProbeFailed = -1;
  ProcessAbsent = 0;
  ProcessRunning = 1;
  ProcessExitPollAttempts = 10;
  ProcessExitPollMilliseconds = 500;
  PayloadRenameRetryAttempts = 20;
  PayloadRenameRetryMilliseconds = 500;
  FileAttributeDirectory = $00000010;
  FileAttributeReparsePoint = $00000400;
  InvalidFileAttributes = $FFFFFFFF;
  ErrorFileNotFound = 2;
  ErrorPathNotFound = 3;
  ErrorNoMoreFiles = 18;

var
  CompanionWasRunning: Boolean;
  SelfUpdateWasRequested: Boolean;
  ShutdownWasConfirmed: Boolean;
  PayloadPreparationStarted: Boolean;
  PayloadPromoted: Boolean;
  PayloadHadPrevious: Boolean;
  PayloadSwapCommitted: Boolean;
  PayloadRenameFailureInjected: Boolean;

function GetFileAttributesW(FileName: String): LongWord;
  external 'GetFileAttributesW@kernel32.dll stdcall';
function GetLastError(): LongWord;
  external 'GetLastError@kernel32.dll stdcall';

function PowerShellSingleQuoted(Value: String): String;
begin
  Result := Value;
  StringChangeEx(Result, '''', '''''', True);
  Result := '''' + Result + '''';
end;

function CompanionProcessScript(Terminate: Boolean): String;
var
  BackupTarget: String;
  CompanionBackupTarget: String;
  CompanionCurrentTarget: String;
  CompanionLegacyTarget: String;
  CompanionNextTarget: String;
  CurrentTarget: String;
  LegacyTarget: String;
  NextTarget: String;
begin
  BackupTarget := PowerShellSingleQuoted(ExpandConstant('{app}\.apscout-backup\ApplicantScout.exe'));
  CurrentTarget := PowerShellSingleQuoted(ExpandConstant('{app}\current\ApplicantScout.exe'));
  LegacyTarget := PowerShellSingleQuoted(ExpandConstant('{app}\ApplicantScout.exe'));
  NextTarget := PowerShellSingleQuoted(ExpandConstant('{app}\.apscout-next\ApplicantScout.exe'));
  { WHY: the portable ZIP ships ApplicantScoutCompanion.exe while installed
    payloads keep ApplicantScout.exe. Both names must be closed, otherwise a
    portable copy keeps files locked and the installed update promotes a
    mixed-version payload. Portable copies may live outside {app}, so the
    renamed process name also matches by itself for same-user processes. }
  CompanionBackupTarget := PowerShellSingleQuoted(ExpandConstant('{app}\.apscout-backup\ApplicantScoutCompanion.exe'));
  CompanionCurrentTarget := PowerShellSingleQuoted(ExpandConstant('{app}\current\ApplicantScoutCompanion.exe'));
  CompanionLegacyTarget := PowerShellSingleQuoted(ExpandConstant('{app}\ApplicantScoutCompanion.exe'));
  CompanionNextTarget := PowerShellSingleQuoted(ExpandConstant('{app}\.apscout-next\ApplicantScoutCompanion.exe'));
  Result :=
    '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference = ''Stop''; try { ' +
    '$targets = @(' + CurrentTarget + ', ' + LegacyTarget + ', ' + BackupTarget + ', ' + NextTarget + ', ' +
    CompanionCurrentTarget + ', ' + CompanionLegacyTarget + ', ' + CompanionBackupTarget + ', ' + CompanionNextTarget + ') | ForEach-Object { ' +
    '[System.IO.Path]::GetFullPath($_) }; ' +
    '$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; ' +
    '$currentSessionId = [System.Diagnostics.Process]::GetCurrentProcess().SessionId; ' +
    '$candidates = @(Get-CimInstance Win32_Process -OperationTimeoutSec 5 | Where-Object { ' +
    '$_.Name -ieq ''ApplicantScout.exe'' -or $_.Name -ieq ''ApplicantScoutCompanion.exe'' }); ' +
    '$owned = @(); foreach ($candidate in $candidates) { ' +
    'try { $owner = Invoke-CimMethod -InputObject $candidate -MethodName GetOwnerSid -OperationTimeoutSec 5 } ' +
    'catch { $owner = $null }; ' +
    'if (-not $owner -or $owner.ReturnValue -ne 0 -or -not $owner.Sid) { ' +
    'if ($candidate.SessionId -eq $currentSessionId -or ($candidate.ExecutablePath -and ($targets -icontains ' +
    '[System.IO.Path]::GetFullPath($candidate.ExecutablePath)))) { exit 2 }; continue }; ' +
    'if ($owner.Sid -eq $currentSid) { $owned += $candidate } }; ' +
    'if ($owned | Where-Object { -not $_.ExecutablePath }) { exit 2 }; ' +
    '$procs = @($owned | Where-Object { $targets -icontains ' +
    '[System.IO.Path]::GetFullPath($_.ExecutablePath) -or $_.Name -ieq ''ApplicantScoutCompanion.exe'' }); ';
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

function ProbeCompanionProcess(): Integer; forward;

function CurrentPayloadDir(): String;
begin
  Result := ExpandConstant('{app}\current');
end;

function NextPayloadDir(): String;
begin
  Result := ExpandConstant('{app}\.apscout-next');
end;

function BackupPayloadDir(): String;
begin
  Result := ExpandConstant('{app}\.apscout-backup');
end;

function PendingPromotionMarker(): String;
begin
  Result := ExpandConstant('{app}\.apscout-promotion-pending');
end;

function RedirectionGuard(Path: String): Boolean;
var
  Attributes: LongWord;
  Cursor: String;
  ErrorCode: LongWord;
  Parent: String;
begin
  Result := False;
  Cursor := RemoveBackslashUnlessRoot(ExpandFileName(Path));
  if Cursor = '' then begin
    Log('RedirectionGuard rejected an empty path.');
    Exit;
  end;

  while True do begin
    Attributes := GetFileAttributesW(Cursor);
    if Attributes = InvalidFileAttributes then begin
      ErrorCode := GetLastError();
      if (ErrorCode <> ErrorFileNotFound) and (ErrorCode <> ErrorPathNotFound) then begin
        Log(Format('RedirectionGuard could not inspect %s (Windows error %d).', [Cursor, ErrorCode]));
        Exit;
      end;
    end else if (Attributes and FileAttributeReparsePoint) <> 0 then begin
      Log('RedirectionGuard rejected reparse-point ancestry at ' + Cursor + '.');
      Exit;
    end;

    Parent := RemoveBackslashUnlessRoot(ExtractFileDir(Cursor));
    if (Parent = '') or PathSame(Parent, Cursor) then begin
      Break;
    end;
    Cursor := Parent;
  end;
  Result := True;
end;

function DirectoryTreeIsRedirectionFree(Path: String): Boolean;
var
  ChildPath: String;
  ErrorCode: LongWord;
  FindRec: TFindRec;
  MoreFiles: Boolean;
begin
  Result := RedirectionGuard(Path);
  if not Result or not DirExists(Path) then begin
    Exit;
  end;

  if FindFirst(AddBackslash(Path) + '*', FindRec) then begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then begin
          ChildPath := AddBackslash(Path) + FindRec.Name;
          if (FindRec.Attributes and FileAttributeReparsePoint) <> 0 then begin
            Log('RedirectionGuard rejected a reparse point inside payload at ' + ChildPath + '.');
            Result := False;
            Exit;
          end;
          if ((FindRec.Attributes and FileAttributeDirectory) <> 0) and
             not DirectoryTreeIsRedirectionFree(ChildPath) then begin
            Result := False;
             Exit;
          end;
        end;
        MoreFiles := FindNext(FindRec);
      until not MoreFiles;
      ErrorCode := GetLastError();
      if (ErrorCode <> ErrorFileNotFound) and
         (ErrorCode <> ErrorPathNotFound) and
         (ErrorCode <> ErrorNoMoreFiles) then begin
        Log(Format('RedirectionGuard could not finish enumerating %s (Windows error %d).', [Path, ErrorCode]));
        Result := False;
      end;
    finally
      FindClose(FindRec);
    end;
  end else begin
    ErrorCode := GetLastError();
    if (ErrorCode <> ErrorFileNotFound) and
       (ErrorCode <> ErrorPathNotFound) and
       (ErrorCode <> ErrorNoMoreFiles) then begin
      Log(Format('RedirectionGuard could not enumerate %s (Windows error %d).', [Path, ErrorCode]));
      Result := False;
    end;
  end;
end;

function PayloadMutationGuard(): Boolean;
begin
  Result :=
    RedirectionGuard(ExpandConstant('{app}')) and
    RedirectionGuard(PendingPromotionMarker()) and
    RedirectionGuard(ExpandConstant('{app}\ApplicantScout.exe')) and
    DirectoryTreeIsRedirectionFree(CurrentPayloadDir()) and
    DirectoryTreeIsRedirectionFree(NextPayloadDir()) and
    DirectoryTreeIsRedirectionFree(BackupPayloadDir()) and
    DirectoryTreeIsRedirectionFree(ExpandConstant('{app}\_internal')) and
    DirectoryTreeIsRedirectionFree(ExpandConstant('{app}\licenses'));
end;

function UninstallPayloadDeletionAllowed(): Boolean;
begin
  Result := PayloadMutationGuard();
  if not Result then
    Log('RedirectionGuard blocked an unsafe uninstall deletion.');
end;

function IsCompletePayload(Path: String; ExpectedVersion: String): Boolean; forward;

function IsDirectoryEmpty(Path: String): Boolean;
var
  FindRec: TFindRec;
begin
  Result := True;
  if not DirExists(Path) then begin
    Exit;
  end;
  if FindFirst(AddBackslash(Path) + '*', FindRec) then begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then begin
          Result := False;
          Exit;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

function IsOwnedAppDir(): Boolean;
var
  AppDir: String;
begin
  AppDir := ExpandFileName(ExpandConstant('{app}'));
  if PathSame(
    AppDir,
    ExpandFileName(ExpandConstant('{localappdata}\Programs\ApplicantScout Companion'))
  ) then begin
    Result := True;
    Exit;
  end;
  { A fresh custom target is safe only while it does not exist or is empty.
    Nonempty custom directories still require installer/payload ownership proof. }
  if not DirExists(AppDir) or IsDirectoryEmpty(AppDir) then begin
    Result := True;
    Exit;
  end;
  { Preserve upgrades from an explicitly selected legacy/custom install only
    when its own uninstaller and a recognizable companion payload prove scope. }
  Result := FileExists(AddBackslash(AppDir) + 'unins000.exe') and (
    FileExists(AddBackslash(AppDir) + 'ApplicantScout.exe') or
    IsCompletePayload(CurrentPayloadDir(), '') or
    IsCompletePayload(BackupPayloadDir(), '')
  );
end;

function IsCompletePayload(Path: String; ExpectedVersion: String): Boolean;
var
  Marker: AnsiString;
begin
  Result := False;
  if not FileExists(AddBackslash(Path) + 'ApplicantScout.exe') then begin
    Exit;
  end;
  if not DirExists(AddBackslash(Path) + '_internal') then begin
    Exit;
  end;
  if not LoadStringFromFile(AddBackslash(Path) + '.apscout-payload-version', Marker) then begin
    Exit;
  end;
  if Trim(Marker) = '' then begin
    Exit;
  end;
  Result := (ExpectedVersion = '') or (CompareText(Trim(Marker), ExpectedVersion) = 0);
end;

function RemovePayloadDir(Path: String): Boolean;
begin
  Result := DirectoryTreeIsRedirectionFree(Path) and
    ((not DirExists(Path)) or DelTree(Path, True, True, True));
end;

function ReadPendingPromotionState(var State: String): Boolean;
var
  RawState: AnsiString;
begin
  Result := False;
  State := '';
  if not LoadStringFromFile(PendingPromotionMarker(), RawState) then begin
    Exit;
  end;
  State := Trim(RawState);
  Result := (State = 'fresh') or (State = 'upgrade');
end;

function RemovePendingPromotionMarker(): Boolean;
begin
  Result :=
    RedirectionGuard(PendingPromotionMarker()) and
    ((not FileExists(PendingPromotionMarker())) or DeleteFile(PendingPromotionMarker()));
end;

function ShouldInjectFirstPayloadRenameFailure(): Boolean;
begin
  Result :=
    (not PayloadRenameFailureInjected) and
    (GetEnv('GITHUB_ACTIONS') = 'true') and
    (ExpandConstant('{param:APSCOUT_TEST_FAIL_FIRST_RENAME|0}') = '1');
end;

function RenamePayloadDirWithRetry(SourcePath: String; DestPath: String): Boolean;
var
  Attempt: Integer;
begin
  Result := False;
  for Attempt := 1 to PayloadRenameRetryAttempts do begin
    if ShouldInjectFirstPayloadRenameFailure() then begin
      PayloadRenameFailureInjected := True;
      Log('Injected one transient payload directory rename failure for upgrade smoke.');
    end else if RenameFile(SourcePath, DestPath) then begin
      Result := True;
      Exit;
    end;
    if Attempt < PayloadRenameRetryAttempts then begin
      { Windows can briefly retain a directory handle after the staged native
        probe exits. Keep the transaction fail-closed, but allow that bounded
        transient contention to clear before rollback. }
      Sleep(PayloadRenameRetryMilliseconds);
    end;
  end;
  Log(
    'Payload directory rename failed after ' +
    IntToStr(PayloadRenameRetryAttempts) + ' attempts: ' +
    SourcePath + ' -> ' + DestPath + '.'
  );
end;

function RecoverInterruptedPayloadSwap(): Boolean;
var
  CurrentDir: String;
  NextDir: String;
  BackupDir: String;
  PendingState: String;
begin
  Result := False;
  if not PayloadMutationGuard() then begin
    Exit;
  end;
  CurrentDir := CurrentPayloadDir();
  NextDir := NextPayloadDir();
  BackupDir := BackupPayloadDir();

  if FileExists(PendingPromotionMarker()) then begin
    if not ReadPendingPromotionState(PendingState) then begin
      Exit;
    end;
    if PendingState = 'upgrade' then begin
      if DirExists(BackupDir) then begin
        if not IsCompletePayload(BackupDir, '') or
           not RemovePayloadDir(CurrentDir) or
           not RenamePayloadDirWithRetry(BackupDir, CurrentDir) then begin
          Exit;
        end;
      end else if not IsCompletePayload(CurrentDir, '') then begin
        { An upgrade marker without either a complete current or backup cannot
          prove which payload owns the directory, so leave it untouched. }
        Exit;
      end;
    end else begin
      if DirExists(BackupDir) or not RemovePayloadDir(CurrentDir) then begin
        Exit;
      end;
    end;
    if not RemovePayloadDir(NextDir) or not RemovePendingPromotionMarker() then begin
      Exit;
    end;
    Result := True;
    Exit;
  end;

  if DirExists(BackupDir) then begin
    if IsCompletePayload(CurrentDir, '') then begin
      if not RemovePayloadDir(BackupDir) then begin
        Exit;
      end;
    end else begin
      if not RemovePayloadDir(CurrentDir) then begin
        Exit;
      end;
      if not RenamePayloadDirWithRetry(BackupDir, CurrentDir) then begin
        Exit;
      end;
    end;
  end;
  if not RemovePayloadDir(NextDir) then begin
    Exit;
  end;
  if DirExists(CurrentDir) and not IsCompletePayload(CurrentDir, '') then begin
    Exit;
  end;
  Result := True;
end;

procedure RestorePayloadBackup();
var
  CurrentDir: String;
  BackupDir: String;
begin
  CurrentDir := CurrentPayloadDir();
  BackupDir := BackupPayloadDir();
  if not DirExists(BackupDir) then begin
    Exit;
  end;
  RemovePayloadDir(CurrentDir);
  if not RenamePayloadDirWithRetry(BackupDir, CurrentDir) then begin
    Log('WARNING: could not restore the previous companion payload.');
  end;
end;

procedure RollbackPendingPayloadSwap();
var
  RollbackSucceeded: Boolean;
begin
  if not PayloadMutationGuard() then begin
    Log('WARNING: RedirectionGuard prevented unsafe payload rollback.');
    Exit;
  end;
  RollbackSucceeded := RemovePayloadDir(NextPayloadDir());
  if DirExists(BackupPayloadDir()) then begin
    RestorePayloadBackup();
    RollbackSucceeded := RollbackSucceeded and
      (not DirExists(BackupPayloadDir())) and
      IsCompletePayload(CurrentPayloadDir(), '');
  end else if PayloadPromoted and not PayloadHadPrevious then begin
    RollbackSucceeded := RollbackSucceeded and RemovePayloadDir(CurrentPayloadDir());
  end;
  if RollbackSucceeded and not RemovePendingPromotionMarker() then begin
    Log('WARNING: could not remove the pending payload promotion marker.');
  end else if not RollbackSucceeded then begin
    Log('WARNING: payload rollback was incomplete; preserving its recovery marker.');
  end;
end;

procedure RemoveLegacyRootPayload();
begin
  if not DeleteFile(ExpandConstant('{app}\ApplicantScout.exe')) and
     FileExists(ExpandConstant('{app}\ApplicantScout.exe')) then
    Log('WARNING: could not remove the legacy root executable.');
  if not RemovePayloadDir(ExpandConstant('{app}\_internal')) then
    Log('WARNING: could not remove the legacy root runtime.');
  if not RemovePayloadDir(ExpandConstant('{app}\licenses')) then
    Log('WARNING: could not remove legacy dependency notices.');
  DeleteFile(ExpandConstant('{app}\LICENSE'));
  DeleteFile(ExpandConstant('{app}\THIRD-PARTY-NOTICES.md'));
  DeleteFile(ExpandConstant('{app}\RELEASE_NOTES.md'));
end;

function RunStagedPayloadProbe(): Boolean;
var
  Parameters: String;
  ResultCode: Integer;
  Target: String;
begin
  if not PayloadMutationGuard() then begin
    Result := False;
    Exit;
  end;
  Target := PowerShellSingleQuoted(AddBackslash(NextPayloadDir()) + 'ApplicantScout.exe');
  Parameters :=
    '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference = ''Stop''; try { ' +
    '$probe = Start-Process -FilePath ' + Target +
    ' -ArgumentList ''--startup-import-probe'' -PassThru -WindowStyle Hidden; ' +
    'if (-not $probe.WaitForExit(15000)) { Stop-Process -Id $probe.Id -Force -ErrorAction SilentlyContinue; exit 2 }; ' +
    'exit $probe.ExitCode } catch { exit 2 }"';
  Result := Exec(
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Parameters,
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) and (ResultCode = 0);
end;

function ShouldInjectPayloadPromotionFailure(): Boolean;
begin
  Result :=
    (GetEnv('GITHUB_ACTIONS') = 'true') and
    (ExpandConstant('{param:APSCOUT_TEST_FAIL_PROMOTION|0}') = '1');
end;

function ShouldInjectPostPromotionFailure(): Boolean;
begin
  Result :=
    (GetEnv('GITHUB_ACTIONS') = 'true') and
    (ExpandConstant('{param:APSCOUT_TEST_FAIL_POST_PROMOTION|0}') = '1');
end;

function ShouldInjectFinalizationFailure(): Boolean;
begin
  Result :=
    (GetEnv('GITHUB_ACTIONS') = 'true') and
    (ExpandConstant('{param:APSCOUT_TEST_FAIL_FINALIZATION|0}') = '1');
end;

procedure CommitPayloadSwap();
var
  CurrentDir: String;
  NextDir: String;
  BackupDir: String;
  PendingState: String;
begin
  CurrentDir := CurrentPayloadDir();
  NextDir := NextPayloadDir();
  BackupDir := BackupPayloadDir();
  if not PayloadMutationGuard() then begin
    RaiseException('The install path failed its redirection safety check.');
  end;
  if not ShutdownWasConfirmed then begin
    RaiseException('Companion shutdown was not confirmed before payload promotion.');
  end;
  if ProbeCompanionProcess() <> ProcessAbsent then begin
    RaiseException('ApplicantScout Companion restarted during the update.');
  end;
  if not IsCompletePayload(NextDir, '{#MyAppVersion}') then begin
    RaiseException('The staged companion payload is incomplete.');
  end;
  if not RunStagedPayloadProbe() then begin
    RaiseException('The staged companion payload failed its native startup probe.');
  end;
  if DirExists(BackupDir) then begin
    RaiseException('A previous companion payload backup was not recovered.');
  end;
  PayloadHadPrevious := DirExists(CurrentDir);
  if PayloadHadPrevious then begin
    PendingState := 'upgrade';
  end else begin
    PendingState := 'fresh';
  end;
  if not SaveStringToFile(
    PendingPromotionMarker(),
    PendingState,
    False
  ) then begin
    RaiseException('Could not create the pending payload promotion marker.');
  end;
  if DirExists(CurrentDir) and
     not RenamePayloadDirWithRetry(CurrentDir, BackupDir) then begin
    RaiseException('Could not preserve the previous companion payload.');
  end;
  if ProbeCompanionProcess() <> ProcessAbsent then begin
    RestorePayloadBackup();
    RaiseException('ApplicantScout Companion restarted during payload promotion.');
  end;
  if DirExists(BackupDir) and ShouldInjectPayloadPromotionFailure() then begin
    RaiseException('Injected payload promotion failure for upgrade smoke.');
  end;
  if not RenamePayloadDirWithRetry(NextDir, CurrentDir) then begin
    RestorePayloadBackup();
    RaiseException('Could not promote the staged companion payload.');
  end;
  PayloadPromoted := True;
  if ShouldInjectPostPromotionFailure() then begin
    RaiseException('Injected post-promotion failure for upgrade smoke.');
  end;
end;

procedure FinalizePayloadSwap();
begin
  if PayloadSwapCommitted then begin
    Exit;
  end;
  if not PayloadPromoted then begin
    RaiseException('The companion payload was not promoted before install finalization.');
  end;
  if not PayloadMutationGuard() then begin
    RaiseException('The install path failed its final redirection safety check.');
  end;
  if ShouldInjectFinalizationFailure() then begin
    RaiseException('Injected payload finalization failure for upgrade smoke.');
  end;
  { Removing the durable marker commits recovery to the promoted payload. If
    setup dies before this step, the next installer restores the backup. }
  if not RemovePendingPromotionMarker() then begin
    RaiseException('Could not finalize the companion payload promotion.');
  end;
  PayloadSwapCommitted := True;
  if not RemovePayloadDir(BackupPayloadDir()) then
    Log('WARNING: could not remove the previous companion payload backup.');
  RemoveLegacyRootPayload();
end;

function CommitPayloadSwapForInstall(Param: String): String;
var
  Failure: String;
begin
  { Inno expands [Files] code constants during its early pending-operation
    enumeration as well as during PerformInstall. The early pass happens before
    PrepareToInstall and must be side-effect free; the actual Files pass repeats
    this expansion after staging and confirmed shutdown. }
  if not PayloadPreparationStarted or not ShutdownWasConfirmed then begin
    Result := NextPayloadDir();
    Exit;
  end;
  if not PayloadSwapCommitted then begin
    try
      CommitPayloadSwap();
      FinalizePayloadSwap();
    except
      Failure := GetExceptionMessage();
      Log('ERROR: payload transaction failed: ' + Failure);
      { Ordinary [Files] exceptions offer Retry/Skip. Roll back here and raise
        EAbort so ProcessFileEntry cannot convert a failed transaction into a
        skippable file error or successful mixed-version install. }
      try
        RollbackPendingPayloadSwap();
      except
        Log('ERROR: immediate payload rollback also failed: ' +
          GetExceptionMessage());
      end;
      Abort;
    end;
  end;
  Result := CurrentPayloadDir();
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
  CompanionCurrentExe: String;
  CompanionLegacyExe: String;
  CurrentExe: String;
  LegacyExe: String;
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
  CurrentExe := ExpandConstant('{app}\current\ApplicantScout.exe');
  CompanionCurrentExe := ExpandConstant('{app}\current\ApplicantScoutCompanion.exe');
  LegacyExe := ExpandConstant('{app}\ApplicantScout.exe');
  CompanionLegacyExe := ExpandConstant('{app}\ApplicantScoutCompanion.exe');
  if FileExists(CurrentExe) then begin
    { WARNING: Do not wait here. Older builds treat the shutdown flag as a
      normal app launch and would block the installer until fallback runs. }
    Exec(
      CurrentExe,
      '--shutdown-running-instance',
      '',
      SW_HIDE,
      ewNoWait,
      ResultCode
    );
  end;
  if FileExists(CompanionCurrentExe) then begin
    Exec(
      CompanionCurrentExe,
      '--shutdown-running-instance',
      '',
      SW_HIDE,
      ewNoWait,
      ResultCode
    );
  end;
  if FileExists(LegacyExe) then begin
    Exec(
      LegacyExe,
      '--shutdown-running-instance',
      '',
      SW_HIDE,
      ewNoWait,
      ResultCode
    );
  end;
  if FileExists(CompanionLegacyExe) then begin
    Exec(
      CompanionLegacyExe,
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
var
  CommonDesktopLink: String;
  CommonProgramsDir: String;
  CommonProgramsLink: String;
begin
  { WHY: Builds before the per-user installer could create common shortcuts
    pointing at Program Files. A non-admin updater cannot guarantee deletion of
    protected files, but deleting writable legacy shortcuts prevents most stale
    launcher confusion after migrating to the per-user app directory. Deletion
    failures are logged with the exact manual cleanup path instead of failing
    silently, since only an administrator can remove protected shortcuts. }
  CommonDesktopLink := ExpandConstant('{commondesktop}\ApplicantScout Companion.lnk');
  CommonProgramsLink := ExpandConstant('{commonprograms}\ApplicantScout Companion\ApplicantScout Companion.lnk');
  CommonProgramsDir := ExpandConstant('{commonprograms}\ApplicantScout Companion');
  if FileExists(CommonDesktopLink) and not DeleteFile(CommonDesktopLink) then begin
    Log('WARNING: could not remove the legacy per-machine desktop shortcut: ' +
      CommonDesktopLink + '. An administrator can remove it manually.');
  end;
  if FileExists(CommonProgramsLink) and not DeleteFile(CommonProgramsLink) then begin
    Log('WARNING: could not remove the legacy per-machine Start Menu shortcut: ' +
      CommonProgramsLink + '. An administrator can remove it manually.');
  end;
  if DirExists(CommonProgramsDir) and not RemoveDir(CommonProgramsDir) then begin
    Log('WARNING: could not remove the legacy per-machine Start Menu folder: ' +
      CommonProgramsDir + '. An administrator can remove it manually.');
  end;
end;

function ShouldRelaunchAfterInstall(): Boolean;
begin
  Result := PayloadSwapCommitted and
    (CompanionWasRunning or SelfUpdateWasRequested);
end;

function PendingPathTouchesPayloadRoot(Path: String; Root: String): Boolean;
var
  RootWithSlash: String;
begin
  Result := False;
  if Path = '' then begin
    Exit;
  end;
  if Path[1] = '!' then begin
    Delete(Path, 1, 1);
  end else if (Length(Path) >= 2) and (Path[1] = '*') and
              ((Path[2] = '1') or (Path[2] = '2')) then begin
    Delete(Path, 1, 2);
  end;
  if CompareText(Copy(Path, 1, 4), '\??\') = 0 then begin
    Delete(Path, 1, 4);
  end else if CompareText(Copy(Path, 1, 4), '\\?\') = 0 then begin
    Delete(Path, 1, 4);
  end;
  StringChange(Path, '/', '\');
  while (Length(Root) > 3) and (Root[Length(Root)] = '\') do begin
    Delete(Root, Length(Root), 1);
  end;
  RootWithSlash := Root + '\';
  Result := (CompareText(Path, Root) = 0) or
    ((Length(Path) > Length(RootWithSlash)) and
     (CompareText(Copy(Path, 1, Length(RootWithSlash)), RootWithSlash) = 0));
end;

function PendingRenameListTouchesPayloadRoots(Raw: String): Boolean;
var
  Item: String;
  ItemEnd: Integer;
  Offset: Integer;
begin
  Result := False;
  Offset := 1;
  while Offset <= Length(Raw) do begin
    ItemEnd := Offset;
    while (ItemEnd <= Length(Raw)) and (Raw[ItemEnd] <> #0) do begin
      ItemEnd := ItemEnd + 1;
    end;
    Item := Copy(Raw, Offset, ItemEnd - Offset);
    if PendingPathTouchesPayloadRoot(Item, CurrentPayloadDir()) or
       PendingPathTouchesPayloadRoot(Item, NextPayloadDir()) or
       PendingPathTouchesPayloadRoot(Item, BackupPayloadDir()) or
       PendingPathTouchesPayloadRoot(Item, ExpandConstant('{app}\ApplicantScout.exe')) or
       PendingPathTouchesPayloadRoot(Item, ExpandConstant('{app}\_internal')) or
       PendingPathTouchesPayloadRoot(Item, ExpandConstant('{app}\licenses')) then begin
      Log('Blocking install because a pending reboot operation touches payload path: ' + Item);
      Result := True;
      Exit;
    end;
    Offset := ItemEnd + 1;
  end;
end;

function PendingRenameValueTouchesPayload(ValueName: String): Boolean;
var
  Raw: String;
begin
  Result := False;
  if not RegValueExists(
    HKLM,
    'SYSTEM\CurrentControlSet\Control\Session Manager',
    ValueName
  ) then begin
    Exit;
  end;
  if not RegQueryMultiStringValue(
    HKLM,
    'SYSTEM\CurrentControlSet\Control\Session Manager',
    ValueName,
    Raw
  ) then begin
    Log('Blocking install because pending reboot operations could not be read.');
    Result := True;
    Exit;
  end;
  Result := PendingRenameListTouchesPayloadRoots(Raw);
end;

function PendingRenameTouchesPayload(): Boolean;
begin
  Result :=
    PendingRenameValueTouchesPayload('PendingFileRenameOperations') or
    PendingRenameValueTouchesPayload('PendingFileRenameOperations2');
end;

function DowngradeExplicitlyAllowed(): Boolean;
begin
  Result := CompareText(ExpandConstant('{param:ALLOWDOWNGRADE|0}'), '1') = 0;
end;

function ParseDottedVersion(const Value: String; var Major, Minor, Patch: Integer): Boolean;
var
  Dot: Integer;
  Remainder: String;
begin
  Result := False;
  Major := -1;
  Minor := -1;
  Patch := -1;
  Remainder := Trim(Value);
  Dot := Pos('.', Remainder);
  if Dot <= 1 then begin
    Exit;
  end;
  Major := StrToIntDef(Copy(Remainder, 1, Dot - 1), -1);
  Delete(Remainder, 1, Dot);
  Dot := Pos('.', Remainder);
  if Dot <= 1 then begin
    Exit;
  end;
  Minor := StrToIntDef(Copy(Remainder, 1, Dot - 1), -1);
  Delete(Remainder, 1, Dot);
  if (Remainder = '') or (Pos('.', Remainder) <> 0) then begin
    Exit;
  end;
  Patch := StrToIntDef(Remainder, -1);
  Result := (Major >= 0) and (Minor >= 0) and (Patch >= 0);
end;

function CheckDowngradeBlocked(): String;
var
  InstalledMajor, InstalledMinor, InstalledPatch: Integer;
  InstalledVersion: String;
  Marker: AnsiString;
  NewMajor, NewMinor, NewPatch: Integer;
begin
  { WHY: a silent downgrade over a newer payload can strand the updater and
    user data on an older schema. Fail closed unless the operator explicitly
    opts in with /ALLOWDOWNGRADE=1. }
  Result := '';
  if not DirExists(CurrentPayloadDir()) then begin
    Exit;
  end;
  if not LoadStringFromFile(
    AddBackslash(CurrentPayloadDir()) + '.apscout-payload-version',
    Marker
  ) then begin
    if not DowngradeExplicitlyAllowed() then begin
      Result := 'Could not determine the installed companion version from ' +
        AddBackslash(CurrentPayloadDir()) + '.apscout-payload-version. ' +
        'Rerun with /ALLOWDOWNGRADE=1 only to accept the unverified reinstall.';
    end;
    Exit;
  end;
  InstalledVersion := Trim(Marker);
  if not ParseDottedVersion(InstalledVersion, InstalledMajor, InstalledMinor, InstalledPatch) or
     not ParseDottedVersion('{#MyAppVersion}', NewMajor, NewMinor, NewPatch) then begin
    if not DowngradeExplicitlyAllowed() then begin
      Result := 'Could not compare the installed companion version (' +
        InstalledVersion + ') with {#MyAppVersion}. ' +
        'Rerun with /ALLOWDOWNGRADE=1 only to accept the unverified reinstall.';
    end;
    Exit;
  end;
  if (InstalledMajor > NewMajor) or
     ((InstalledMajor = NewMajor) and (InstalledMinor > NewMinor)) or
     ((InstalledMajor = NewMajor) and (InstalledMinor = NewMinor) and (InstalledPatch > NewPatch)) then begin
    if not DowngradeExplicitlyAllowed() then begin
      Result := 'The installed companion version ' + InstalledVersion +
        ' is newer than {#MyAppVersion}. Downgrades are blocked; ' +
        'rerun with /ALLOWDOWNGRADE=1 only to accept the downgrade.';
    end;
  end;
end;

function GetPayloadDirSizeBytes(Path: String; var Size: Int64): Boolean;
var
  Child: String;
  FindRec: TFindRec;
  SubSize: Int64;
begin
  Result := False;
  Size := 0;
  if not DirExists(Path) then begin
    Result := True;
    Exit;
  end;
  if not FindFirst(AddBackslash(Path) + '*', FindRec) then begin
    Exit;
  end;
  try
    repeat
      if (FindRec.Name <> '.') and (FindRec.Name <> '..') then begin
        Child := AddBackslash(Path) + FindRec.Name;
        if (FindRec.Attributes and FileAttributeDirectory) <> 0 then begin
          if not GetPayloadDirSizeBytes(Child, SubSize) then begin
            Exit;
          end;
          Size := Size + SubSize;
        end else begin
          Size := Size + FindRec.Size;
        end;
      end;
    until not FindNext(FindRec);
    Result := True;
  finally
    FindClose(FindRec);
  end;
end;

function CheckDiskSpaceBlocked(): String;
var
  AppDir: String;
  BackupSize: Int64;
  CurrentSize: Int64;
  FreeSpace: Int64;
  NextProbe: String;
  ProbeDir: String;
  Required: Int64;
  TotalSpace: Int64;
begin
  { WHY: the staged current/next/backup transaction briefly needs ~2-3x the
    payload on disk. Fail closed here, before any running companion is
    stopped, instead of dying mid-promotion with a half-moved payload. }
  Result := '';
  if not GetPayloadDirSizeBytes(CurrentPayloadDir(), CurrentSize) then begin
    Result := 'Could not measure the current companion payload for a disk-space check. ' +
      'Free additional disk space and try again.';
    Exit;
  end;
  if not GetPayloadDirSizeBytes(BackupPayloadDir(), BackupSize) then begin
    Result := 'Could not measure the companion payload backup for a disk-space check. ' +
      'Free additional disk space and try again.';
    Exit;
  end;
  Required := (CurrentSize + BackupSize) * 3;
  if Required < 536870912 then begin
    Required := 536870912;
  end;
  AppDir := ExpandConstant('{app}');
  ProbeDir := ExpandFileName(AppDir);
  while not DirExists(ProbeDir) do begin
    NextProbe := RemoveBackslashUnlessRoot(ExtractFileDir(ProbeDir));
    if (NextProbe = '') or PathSame(NextProbe, ProbeDir) then begin
      Break;
    end;
    ProbeDir := NextProbe;
  end;
  if not GetSpaceOnDisk(ProbeDir, FreeSpace, TotalSpace) then begin
    Result := 'Could not verify free disk space for the companion install path. ' +
      'Free additional disk space and try again.';
    Exit;
  end;
  if FreeSpace < Required then begin
    Result := 'Not enough free disk space for the companion update: ' +
      IntToStr(FreeSpace div 1048576) + ' MB free, but about ' +
      IntToStr(Required div 1048576) + ' MB is required. ' +
      'Free additional disk space and try again.';
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  CompanionWasRunning := False;
  ShutdownWasConfirmed := False;
  PayloadPreparationStarted := False;
  PayloadPromoted := False;
  PayloadHadPrevious := False;
  PayloadSwapCommitted := False;
  PayloadRenameFailureInjected := False;
  SelfUpdateWasRequested := SelfUpdateRequested();
  if not PayloadMutationGuard() then begin
    Result := 'The selected directory contains an unsafe filesystem redirection.';
    Exit;
  end;
  if not IsOwnedAppDir() then begin
    Result := 'The selected directory is not a recognized ApplicantScout Companion installation. ' +
      'Select the previous install location, an empty folder, or a folder that contains its own unins000.exe.';
    Exit;
  end;
  Result := CheckDowngradeBlocked();
  if Result <> '' then begin
    Exit;
  end;
  Result := CheckDiskSpaceBlocked();
  if Result <> '' then begin
    Exit;
  end;
  if PendingRenameTouchesPayload() then begin
    NeedsRestart := True;
    Result := 'Windows has a pending reboot operation for ApplicantScout Companion files. Restart Windows before updating.';
    Exit;
  end;
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
  if not RecoverInterruptedPayloadSwap() then begin
    Result := 'Could not recover or clean the previous companion update staging area.';
    Exit;
  end;
  PayloadPreparationStarted := True;
  ShutdownWasConfirmed := True;
end;

procedure DeinitializeSetup();
begin
  if PayloadPreparationStarted and not PayloadSwapCommitted then begin
    RollbackPendingPayloadSwap();
  end;
end;
function InitializeUninstall(): Boolean;
begin
  CompanionWasRunning := False;
  Result := PayloadMutationGuard();
  if not Result then
  begin
    SuppressibleMsgBox(
      'ApplicantScout Companion uninstall stopped because the install path contains an unsafe redirection.',
      mbCriticalError,
      MB_OK,
      IDOK
    );
    Exit;
  end;
  Result := CloseRunningCompanion();
end;

procedure RemoveWowSyncStartupEntries();
var
  ApprovalKey: String;
  ApprovalValue: String;
  StartupShortcut: String;
begin
  { WHY: first-run and WoW sync create a per-user logon shortcut plus a Windows
    StartupApproved approval value. Leaving either behind silently relaunches a
    removed install location at logon, so uninstall removes both. }
  ApprovalKey := 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder';
  ApprovalValue := 'ApplicantScout Companion.lnk';
  StartupShortcut := ExpandConstant('{userstartup}\ApplicantScout Companion.lnk');
  if FileExists(StartupShortcut) and not DeleteFile(StartupShortcut) then begin
    Log('WARNING: could not remove the Startup shortcut: ' + StartupShortcut +
      '. Remove ApplicantScout Companion.lnk from the Startup folder manually.');
  end;
  if RegValueExists(HKCU, ApprovalKey, ApprovalValue) and
     not RegDeleteValue(HKCU, ApprovalKey, ApprovalValue) then begin
    Log('WARNING: could not remove the Startup approval value: ' + ApprovalKey +
      '\' + ApprovalValue + '. Disable the leftover entry in Windows Settings > Apps > Startup.');
  end;
end;

procedure CurUninstallStepChanged(CurStep: TUninstallStep);
var
  Leftovers: String;
begin
  if CurStep = usUninstall then begin
    RemoveWowSyncStartupEntries();
    Exit;
  end;
  if CurStep <> usPostUninstall then begin
    Exit;
  end;
  { WHY: UninstallPayloadDeletionAllowed=False makes [UninstallDelete] skip
    payload entries silently. Never leave that silent: report every leftover
    payload path so the user can remove it manually. }
  Leftovers := '';
  if DirExists(CurrentPayloadDir()) then begin
    Leftovers := Leftovers + #13#10 + '  ' + CurrentPayloadDir();
  end;
  if DirExists(NextPayloadDir()) then begin
    Leftovers := Leftovers + #13#10 + '  ' + NextPayloadDir();
  end;
  if DirExists(BackupPayloadDir()) then begin
    Leftovers := Leftovers + #13#10 + '  ' + BackupPayloadDir();
  end;
  if FileExists(PendingPromotionMarker()) then begin
    Leftovers := Leftovers + #13#10 + '  ' + PendingPromotionMarker();
  end;
  if FileExists(ExpandConstant('{app}\ApplicantScout.exe')) then begin
    Leftovers := Leftovers + #13#10 + '  ' + ExpandConstant('{app}\ApplicantScout.exe');
  end;
  if Leftovers <> '' then begin
    SuppressibleMsgBox(
      'ApplicantScout Companion uninstall left payload files behind:' + Leftovers +
        #13#10#13#10 +
        'The install path failed a safety check, so these were kept instead of deleted. ' +
        'Remove them manually once the path is trusted again.',
      mbInformation,
      MB_OK,
      IDOK
    );
  end;
end;
