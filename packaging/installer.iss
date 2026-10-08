; Inno Setup 6 脚本。
; 编译：ISCC.exe packaging\installer.iss
; 前置：先跑过 PyInstaller，dist\ReadingHelper\ 里有东西。
;
; 想换成中文安装界面：把官方翻译包里的 ChineseSimplified.isl 放到本目录，
; 再把下面 [Languages] 那两行的注释去掉。默认不带，免得没放文件时编译不过。

#define AppName "纸质书阅读助手"
#define AppNameEn "NiceReadingLens"
#define AppVersion "0.1.0"
#define ExeName "NiceReadingLens.exe"

[Setup]
AppId={{8F3A2C41-7B5E-4D62-9A18-5C7D2E0B4F31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Reading Helper
DefaultDirName={autopf}\{#AppNameEn}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename={#AppNameEn}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#ExeName}
; 由 make_icon.py 在打包前生成，构建脚本保证它存在
SetupIconFile=build\icon.ico

; [Languages]
; Name: "chinese"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "firewall"; Description: "添加防火墙规则，让手机能连上（建议勾选）"; GroupDescription: "附加任务："

[Files]
; 整个目录铺过去，包括 Python 运行时、OCR 模型和 web 页面
Source: "..\dist\NiceReadingLens\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Run]
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""NiceReadingLens"" dir=in action=allow program=""{app}\{#ExeName}"" enable=yes profile=private"; Flags: runhidden; Tasks: firewall
Filename: "{app}\{#ExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""NiceReadingLens"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"

[Code]
function OllamaInstalled(): Boolean;
begin
  // Ollama 的 Windows 版默认装在这儿，注册表不一定有记录，直接看文件
  Result := FileExists(ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe'));
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (not OllamaInstalled()) then
    MsgBox('没有检测到 Ollama，翻译功能暂时用不了。' + #13#10 + #13#10 +
           '到 https://ollama.com/download 装好之后，在命令行执行一次：' + #13#10 +
           '    ollama pull qwen3:8b' + #13#10 + #13#10 +
           '识别和排版不受影响，装好后再启动本程序即可。',
           mbInformation, MB_OK);
end;
