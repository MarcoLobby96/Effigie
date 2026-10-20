; Installer di Effigie (Inno Setup 6). Si compila da scripts/build_dist.py, che passa
; AppVersion, SourceDir (cartella portatile dist/Effigie) e OutDir.

#ifndef AppVersion
  #define AppVersion "1.0"
#endif

[Setup]
AppId={{6E0F3A52-9C1B-4D8E-A0B7-EF1601E9E3D2}
AppName=Effigie
AppVersion={#AppVersion}
AppVerName=Effigie {#AppVersion}
AppPublisher=Marco Lo Bartolo
AppComments=Dalle foto alla stampa 3D
DefaultDirName={localappdata}\Programs\Effigie
DefaultGroupName=Effigie
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=Effigie-Setup-{#AppVersion}
SetupIconFile={#SourceDir}\assets\icona.ico
UninstallDisplayIcon={app}\assets\icona.ico
WizardStyle=modern
DisableWelcomePage=no
DisableDirPage=no
WizardImageFile={#OutDir}\wizard_100.bmp,{#OutDir}\wizard_125.bmp,{#OutDir}\wizard_150.bmp,{#OutDir}\wizard_200.bmp
WizardSmallImageFile={#OutDir}\wizard_small_100.bmp,{#OutDir}\wizard_small_125.bmp,{#OutDir}\wizard_small_150.bmp,{#OutDir}\wizard_small_200.bmp
WizardImageStretch=no
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=8
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "it"; MessagesFile: "compiler:Languages\Italian.isl"

[Messages]
it.WelcomeLabel1=Benvenuto in Effigie
it.WelcomeLabel2=Effigie trasforma le foto o un video di un oggetto in un modello 3D pronto da stampare: busti, statuine, ritratti.%n%nVerranno installati Effigie {#AppVersion} e tutto quello che serve (Python, COLMAP, OpenMVS, ffmpeg) con un esempio già pronto. Non servono diritti di amministratore.%n%nSpazio richiesto: circa 1,2 GB.
it.FinishedHeadingLabel=Effigie è pronto
it.FinishedLabel=Effigie è stato installato.%n%nPer iniziare apri l'esempio del Buddha dal menu File, oppure trascina nella finestra le foto del tuo soggetto.

[Tasks]
Name: "desktopicon"; Description: "Crea un'icona sul desktop"; GroupDescription: "Icone:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Effigie"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\src\app.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\icona.ico"; Comment: "Dalle foto alla stampa 3D"; AppUserModelID: "MarcoLoBartolo.Effigie"
Name: "{group}\Esempi di Effigie"; Filename: "{app}\Esempi"
Name: "{group}\Disinstalla Effigie"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Effigie"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\src\app.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\icona.ico"; AppUserModelID: "MarcoLoBartolo.Effigie"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\src\app.py"""; WorkingDir: "{app}"; Description: "Avvia Effigie"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\src\__pycache__"
Type: filesandordirs; Name: "{app}\python"

[Code]
var
  ProjectsPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  ProjectsPage := CreateInputDirPage(wpSelectDir,
    'Dove salvare i modelli', 'Ogni modello creato ha la sua cartella qui dentro.',
    'Scegli la cartella dei progetti, poi premi Avanti.', False, '');
  ProjectsPage.Add('');
  ProjectsPage.Values[0] := ExpandConstant('{userdocs}\Effigie\progetti');
end;

function JsonPath(const S: String): String;
begin
  Result := S;
  StringChangeEx(Result, '\', '\\', True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Cfg: String;
begin
  if CurStep = ssPostInstall then
  begin
    { impostazioni iniziali: cartella dei progetti scelta (non sovrascrive quelle esistenti) }
    Cfg := ExpandConstant('{userdocs}\Effigie\config.json');
    ForceDirectories(ProjectsPage.Values[0]);
    if not FileExists(Cfg) then
      SaveStringToFile(Cfg, '{"projects_dir": "' + JsonPath(ProjectsPage.Values[0]) +
        '", "cpu": 80, "versione": 3}', False);
  end;
end;
