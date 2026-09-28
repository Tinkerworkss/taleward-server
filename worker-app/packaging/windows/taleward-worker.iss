; Taleward Worker – Windows-Installer (Inno Setup 6). Baut der GitHub-Ablauf worker-app.yml:
;   iscc /DVersion=0.1.0 packaging\windows\taleward-worker.iss
; Installation nur für den eigenen Benutzer (keine Administratorrechte nötig).

#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{5B7C2E61-8F3A-4C8E-9C1B-7A0D4E2F9B31}
AppName=Taleward Worker
AppVersion={#Version}
AppPublisher=Taleward
AppPublisherURL=https://taleward.org
AppSupportURL=https://github.com/Tinkerworkss/taleward-server
DefaultDirName={localappdata}\Programs\Taleward Worker
DefaultGroupName=Taleward
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=TalewardWorker-Setup
SetupIconFile=taleward-worker.ico
UninstallDisplayIcon={app}\TalewardWorker.exe
WizardStyle=modern
WizardImageFile=installer-seite.bmp
WizardSmallImageFile=installer-klein.bmp
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
MinVersion=10.0.17763
LicenseFile=..\..\..\LICENSE

[Languages]
Name: "de"; MessagesFile: "compiler:Languages\German.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
de.Autostart=Mit Windows starten (empfohlen) – der Worker läuft dann unsichtbar im Hintergrund
en.Autostart=Start with Windows (recommended) – the worker then runs invisibly in the background
de.Starten=Taleward Worker jetzt starten
en.Starten=Start Taleward Worker now
de.DatenLoeschen=Auch das KI-Paket, die Sprachmodelle und die Einstellungen löschen (etwa 10 GB)?%n%nBei „Nein“ bleiben sie für eine spätere Neuinstallation erhalten.
en.DatenLoeschen=Also delete the AI package, speech models and settings (about 10 GB)?%n%nIf you choose “No”, they are kept for a later reinstall.

[Tasks]
Name: "autostart"; Description: "{cm:Autostart}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\dist\TalewardWorker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Taleward Worker"; Filename: "{app}\TalewardWorker.exe"
Name: "{userdesktop}\Taleward Worker"; Filename: "{app}\TalewardWorker.exe"; Tasks: desktopicon

[Registry]
; Gleicher Eintrag, den die App unter Einstellungen → „Mit dem Computer starten“ setzt oder entfernt
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Taleward Worker"; \
  ValueData: """{app}\TalewardWorker.exe"" --hintergrund"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\TalewardWorker.exe"; Description: "{cm:Starten}"; Flags: nowait postinstall skipifsilent
; Update aus der App (still): danach die App im Hintergrund wieder starten
Filename: "{app}\TalewardWorker.exe"; Parameters: "--hintergrund"; Flags: nowait runasoriginaluser; Check: WizardSilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM TalewardWorker.exe /F"; Flags: runhidden; RunOnceId: "Beenden"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Taleward Worker');
    if (not UninstallSilent) and
       (MsgBox(CustomMessage('DatenLoeschen'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DelTree(ExpandConstant('{localappdata}\Taleward Worker'), True, True, True);
  end;
end;
