; ============================================================================
; FrameGuard Desktop v0.5.5 -- Inno Setup Installer Script (Clean Rebuild)
;
; DESIGN PRINCIPLE: Use a single recursive Source entry for the dist folder.
; This guarantees ALL files PyInstaller outputs are included, regardless of
; how future PyInstaller versions restructure their output.
;
; Requires: Inno Setup 6.2+
; Build:    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; ============================================================================

[Setup]
AppId={{B7E2F4A1-9C3D-4E5F-8A1B-6D7C8E9F0A2B}
AppName=FrameGuard Desktop
AppVersion=0.5.5
AppVerName=FrameGuard Desktop v0.5.5
AppPublisher=Meta Platforms - TPG XR Studio
AppPublisherURL=https://about.meta.com
AppSupportURL=https://about.meta.com
AppUpdatesURL=https://about.meta.com
DefaultDirName={autopf}\FrameGuard Desktop
DefaultGroupName=FrameGuard Desktop
AllowNoIcons=yes
OutputDir=Output
OutputBaseFilename=FrameGuardInstaller
Compression=lzma2/ultra64
SolidCompression=yes
SetupIconFile=assets\frameguard.ico
UninstallDisplayIcon={app}\FrameGuard.exe
UninstallDisplayName=FrameGuard Desktop v0.5.5
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=auto
DisableWelcomePage=no
DisableDirPage=no
DisableReadyPage=no
DisableFinishedPage=no
ShowLanguageDialog=no
CloseApplications=force
CloseApplicationsFilter=FrameGuard.exe
RestartApplications=no
SetupMutex=FrameGuardDesktopSetup
AppMutex=FrameGuardDesktopApp
MinVersion=10.0
VersionInfoVersion=0.5.4.0
VersionInfoCompany=Meta Platforms - TPG XR Studio
VersionInfoDescription=FrameGuard Desktop Installer
VersionInfoCopyright=Copyright (c) 2026 Meta Platforms, Inc.
VersionInfoProductName=FrameGuard Desktop
VersionInfoProductVersion=0.5.4.0
InfoBeforeFile=installer_info.txt
LicenseFile=installer_license.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel1=Welcome to FrameGuard Desktop
WelcomeLabel2=This will install FrameGuard Desktop v0.5.4 on your computer.%n%nFrameGuard checks video deliverables against your delivery specification automatically - resolution, frame rate, codec, bitrate, audio, black frames and silence - and produces a PDF or CSV report you can hand off.%n%nAll processing happens on this computer. No video data is uploaded or shared.%n%nIt is recommended that you close all other applications before continuing.
FinishedHeadingLabel=FrameGuard Desktop is ready
FinishedLabel=FrameGuard Desktop v0.5.4 has been installed successfully.%n%nReports are saved to:%n      Documents\FrameGuard%n%nTo get started, drag a video file or folder onto the window and press Start Scan.%n%nDeveloped by Jace Aleria - TPG XR Studio
ConfirmUninstall=Are you sure you want to completely remove FrameGuard Desktop and all of its components?%n%nYour exported reports and custom presets will be kept unless you choose otherwise.
SelectDirLabel3=Setup will install FrameGuard Desktop into the following folder. Everything it needs, including the video analysis tools, is installed here.
SelectDirBrowseLabel=To continue, click Next. If you would like to select a different folder, click Browse.
SelectTasksLabel2=Select the additional tasks you would like Setup to perform, then click Next.
ReadyLabel1=Setup is ready to install FrameGuard Desktop on your computer.
ReadyLabel2a=Click Install to continue, or click Back if you want to review or change any settings.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startmenuicon"; Description: "Create a &Start Menu shortcut"; GroupDescription: "Shortcuts:"
Name: "documentsdir"; Description: "Create a Documents\FrameGuard folder for exported reports"; GroupDescription: "Reports:"

[Dirs]
Name: "{userdocs}\FrameGuard"; Tasks: documentsdir; Flags: uninsneveruninstall

[Files]
; ==========================================================================
; CORE APPLICATION - Single recursive entry captures EVERYTHING PyInstaller
; outputs, including dist\FrameGuard\tools\. This is the bulletproof approach:
; no matter how PyInstaller structures _internal, DLLs, or plugins, they all
; get included.
;
; NOTE: do NOT re-list the tools here with the `external` flag. `external`
; makes Inno read the source at INSTALL time from a path that only exists on
; the build machine, so those entries silently do nothing.
; ==========================================================================
Source: "dist\FrameGuard\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Icon at app root level (for shortcuts and the .fg association - PyInstaller
; puts its own copy in _internal/assets/, which is not a stable public path)
Source: "assets\frameguard.ico"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{autodesktop}\FrameGuard Desktop"; Filename: "{app}\FrameGuard.exe"; IconFilename: "{app}\frameguard.ico"; Tasks: desktopicon; Comment: "Check video deliverables against your delivery spec"
Name: "{group}\FrameGuard Desktop"; Filename: "{app}\FrameGuard.exe"; IconFilename: "{app}\frameguard.ico"; Tasks: startmenuicon; Comment: "Check video deliverables against your delivery spec"
Name: "{group}\FrameGuard Reports"; Filename: "{userdocs}\FrameGuard"; Tasks: startmenuicon documentsdir; Comment: "Open the folder containing your exported reports"
Name: "{group}\Uninstall FrameGuard Desktop"; Filename: "{uninstallexe}"

[Run]
; Launch app after install - WorkingDir ensures exe finds _internal/
Filename: "{app}\FrameGuard.exe"; WorkingDir: "{app}"; Description: "Launch FrameGuard Desktop now"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKCU; Subkey: "Software\FrameGuard Desktop"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\FrameGuard Desktop"; ValueType: string; ValueName: "Version"; ValueData: "0.5.4"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\FrameGuard Desktop"; ValueType: string; ValueName: "ExportPath"; ValueData: "{userdocs}\FrameGuard"; Flags: uninsdeletekey

; --- .fg preset file association (double-click a preset to import it) ---
; DefaultIcon includes the ",0" index: Windows splits on the last comma, so an
; indexless path under "C:\Program Files\..." can be mis-parsed.
Root: HKCU; Subkey: "Software\Classes\.fg"; ValueType: string; ValueName: ""; ValueData: "FrameGuard.Preset"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.fg"; ValueType: string; ValueName: "PerceivedType"; ValueData: "text"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.fg\OpenWithProgids"; ValueType: string; ValueName: "FrameGuard.Preset"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\FrameGuard.Preset"; ValueType: string; ValueName: ""; ValueData: "FrameGuard Scan Preset"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\FrameGuard.Preset"; ValueType: string; ValueName: "FriendlyTypeName"; ValueData: "FrameGuard Scan Preset"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\FrameGuard.Preset\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\frameguard.ico,0"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\FrameGuard.Preset\shell\open"; ValueType: string; ValueName: ""; ValueData: "Import into FrameGuard"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\FrameGuard.Preset\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\FrameGuard.exe"" ""%1"""; Flags: uninsdeletekey

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// Tells the shell that file associations changed, so .fg files pick up the
// FrameGuard icon immediately instead of after a sign-out or reboot.
const
  SHCNE_ASSOCCHANGED = $08000000;
  SHCNF_IDLIST       = $00000000;

procedure SHChangeNotify(wEventId: Integer; uFlags: Cardinal; dwItem1, dwItem2: Cardinal);
  external 'SHChangeNotify@shell32.dll stdcall';

function InitializeSetup(): Boolean;
var
  WinVersion: TWindowsVersion;
  InstalledVersion: string;
  UninstallKey: string;
begin
  GetWindowsVersionEx(WinVersion);
  if WinVersion.Major < 10 then
  begin
    MsgBox('FrameGuard Desktop requires Windows 10 or later.' + #13#10 + #13#10 +
           'This computer is running an earlier version of Windows, so Setup cannot continue.',
           mbError, MB_OK);
    Result := False;
    Exit;
  end;

  UninstallKey := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#SetupSetting("AppId")}_is1';
  if RegQueryStringValue(HKEY_CURRENT_USER, UninstallKey, 'DisplayVersion', InstalledVersion) then
  begin
    if InstalledVersion = '{#SetupSetting("AppVersion")}' then
    begin
      if MsgBox('FrameGuard Desktop v' + InstalledVersion + ' is already installed.' + #13#10 + #13#10 +
                'Would you like to repair this installation by reinstalling it?' + #13#10 +
                'Your settings, presets and saved reports will not be affected.',
                mbConfirmation, MB_YESNO) = IDNO then
      begin
        Result := False;
        Exit;
      end;
    end
    else
    begin
      MsgBox('FrameGuard Desktop will be updated from v' + InstalledVersion +
             ' to v{#SetupSetting("AppVersion")}.' + #13#10 + #13#10 +
             'Your settings, custom presets and saved reports will be preserved.',
             mbInformation, MB_OK);
    end;
  end;

  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  FFmpegPath: string;
begin
  if CurStep = ssPostInstall then
  begin
    // Refresh the shell so .fg files show the FrameGuard icon right away.
    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, 0, 0);

    FFmpegPath := ExpandConstant('{app}\tools\ffmpeg.exe');
    if not FileExists(FFmpegPath) then
    begin
      MsgBox('FFmpeg was not included in this installer package.' + #13#10 + #13#10 +
             'FrameGuard cannot scan video without it. To finish setting up, install ' +
             'FFmpeg and make sure ffmpeg.exe and ffprobe.exe are on your system PATH, ' +
             'or place them in:' + #13#10 + #13#10 +
             ExpandConstant('{app}\tools\') + #13#10 + #13#10 +
             'Then restart FrameGuard.',
             mbInformation, MB_OK);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ExportPath: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    ExportPath := ExpandConstant('{userdocs}\FrameGuard');
    if DirExists(ExportPath) then
    begin
      if MsgBox('Would you like to delete your saved FrameGuard reports?' + #13#10 + #13#10 +
                ExportPath + #13#10 + #13#10 +
                'Choose No to keep your exported PDF and CSV reports on this computer.',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(ExportPath, True, True, True);
    end;

    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\FrameGuard Desktop');
    // Custom presets in %USERPROFILE%\.frameguard\presets are intentionally
    // left in place so a reinstall keeps the team's shared delivery specs.
    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, 0, 0);
  end;
end;
