; =====================================================================
;  MP4 HintBox Pro 설치 프로그램 (Inno Setup 6)
;  - 일반판 / Tiny 판을 같은 스크립트로 빌드:
;      ISCC.exe /DAppVer=2.0.9 mp4hintbox.iss            (일반판)
;      ISCC.exe /DAppVer=2.0.9 /DTiny mp4hintbox.iss     (Tiny 판)
;  - 입력: PyInstaller --onedir 결과 (build_out\dist\<폴더>)
;  - 사용자 설정/로그는 %LOCALAPPDATA% 에 저장되므로 설치 폴더는 읽기 전용이어도 됨
; =====================================================================

#ifndef AppVer
  #define AppVer "2.0.9"
#endif

#ifdef Tiny
  #define AppName      "MP4 HintBox Pro Tiny"
  #define AppIdGuid    "{{52D3D295-A65B-4A13-ADC5-8612703787C7}"
  #define DistDir      "MP4_HintBox_Pro_Tiny"
  #define AppExe       "MP4_HintBox_Pro_Tiny.exe"
  #define AumId        "EV7lab.MP4HintBoxProTiny"
  #define SetupBase    "MP4_HintBox_Pro_Tiny_Setup_v" + AppVer
  #define VerbKey      "MP4HintBoxProTiny.FastStart"
  #define VerbLabel    "FastStart 처리하기 (Tiny)"
#else
  #define AppName      "MP4 HintBox Pro"
  #define AppIdGuid    "{{E0225071-5428-4542-94D9-140E3078160C}"
  #define DistDir      "MP4_HintBox_Pro"
  #define AppExe       "MP4_HintBox_Pro.exe"
  #define AumId        "EV7lab.MP4HintBoxPro"
  #define SetupBase    "MP4_HintBox_Pro_Setup_v" + AppVer
  #define VerbKey      "MP4HintBoxPro.FastStart"
  #define VerbLabel    "FastStart 처리하기"
#endif

#define AppPublisher "EV7lab"
#define AppURL       "https://www.mp4hintbox.co.kr"

[Setup]
AppId={#AppIdGuid}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVer}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
VersionInfoVersion={#AppVer}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 관리자 권한이 없으면 현재 사용자용으로 설치할 수 있게 선택 창 제공
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\build_out\installer
OutputBaseFilename={#SetupBase}
SetupIconFile=..\app_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVer}
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
; 업데이트 설치 시 실행 중인 앱을 닫고 다시 실행
CloseApplications=yes
RestartApplications=no
MinVersion=10.0
; 우클릭 메뉴 등록 후 탐색기에 바로 반영
ChangesAssociations=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "contextmenu"; Description: "영상 파일 우클릭 메뉴에 ""{#VerbLabel}"" 추가 (mp4 / m4v / mov)"; GroupDescription: "추가 기능:"

[Files]
Source: "..\build_out\dist\{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID 를 앱 코드(APP_USER_MODEL_ID)와 맞춰 작업표시줄 묶음/알림이 정상 동작하도록 함
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AumId}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AumId}"; Tasks: desktopicon

[Registry]
; 탐색기 우클릭 "FastStart 처리하기" (HKA = 모든 사용자 설치면 HKLM, 현재 사용자 설치면 HKCU)
; 여러 파일을 선택하면 파일마다 실행되지만, 앱이 첫 미니 창 하나로 모아서 차례로 처리함
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mp4\shell\{#VerbKey}"; ValueType: string; ValueName: "MUIVerb"; ValueData: "{#VerbLabel}"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mp4\shell\{#VerbKey}"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExe}"",0"; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mp4\shell\{#VerbKey}\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" --faststart ""%1"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.m4v\shell\{#VerbKey}"; ValueType: string; ValueName: "MUIVerb"; ValueData: "{#VerbLabel}"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.m4v\shell\{#VerbKey}"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExe}"",0"; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.m4v\shell\{#VerbKey}\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" --faststart ""%1"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mov\shell\{#VerbKey}"; ValueType: string; ValueName: "MUIVerb"; ValueData: "{#VerbLabel}"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mov\shell\{#VerbKey}"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExe}"",0"; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mov\shell\{#VerbKey}\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" --faststart ""%1"""; Tasks: contextmenu

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller 가 실행 중 만들 수 있는 캐시 정리 (사용자 설정/로그는 보존)
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
