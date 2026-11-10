; Installeur de Pulse Monitor (Inno Setup 6) - compile par "Creer l'exe.bat"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName "Pulse Monitor"
#define MyAppExe "Pulse Monitor.exe"

[Setup]
AppId={{6E4B7C2A-3F1D-4A8E-9C5B-2D7F1E0A9B43}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=leclodo
AppPublisherURL=https://github.com/leclodo/Pulse-Monitor
AppSupportURL=https://github.com/leclodo/Pulse-Monitor
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; pas besoin des droits administrateur : installe pour l'utilisateur (ou pour tous si on choisit)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
WizardStyle=modern
Compression=lzma2/ultra
SolidCompression=yes
CloseApplications=yes
OutputDir=release
OutputBaseFilename=Pulse-Monitor-v{#MyAppVersion}-Setup

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\Pulse Monitor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Registry]
; a la desinstallation : retire le lancement avec Windows (s'il avait ete active dans l'appli)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "PulseMonitor"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM ""{#MyAppExe}"" /F"; Flags: runhidden; RunOnceId: "StopPulse"
