param([ValidateSet('mof','chinatax','workbench','all')][string]$Capability='all')
$ErrorActionPreference = 'Stop'
$ftrRoot = Split-Path -Parent $PSScriptRoot
$ftrCommand = Get-Command uv -ErrorAction SilentlyContinue
$ftrExisting = Join-Path $env:LOCALAPPDATA 'FTR\bootstrap\bin\uv.exe'
if ($ftrCommand) { $ftrUv = $ftrCommand.Source } elseif (Test-Path $ftrExisting) { $ftrUv = $ftrExisting } else {
    $ftrArch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    if ($ftrArch -eq 'Arm64') {
        $ftrTarget = 'aarch64-pc-windows-msvc'
        $ftrHash = 'dbb3a5bd06d20c9ab8bb9a79c7c4fb5832ca1c7ba5f231a020bc92e5a3c6dcf4'
    } elseif ($ftrArch -eq 'X64') {
        $ftrTarget = 'x86_64-pc-windows-msvc'
        $ftrHash = '5049375aa2a5162f132b2c1cb992e25d42d47d934cab8c174dbe6f60973dcc12'
    } else { throw '不支持的 Windows 架构' }
    $ftrBin = Join-Path $env:LOCALAPPDATA 'FTR\bootstrap\bin'
    $ftrTemp = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
    New-Item -ItemType Directory -Force $ftrTemp, $ftrBin | Out-Null
    try {
        $ftrArchive = Join-Path $ftrTemp 'uv.zip'
        Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/0.8.22/uv-$ftrTarget.zip" -OutFile $ftrArchive
        if ((Get-FileHash $ftrArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ftrHash) { throw 'uv 校验失败，禁止执行' }
        Expand-Archive $ftrArchive -DestinationPath (Join-Path $ftrTemp 'unpacked')
        $ftrBinary = Get-ChildItem (Join-Path $ftrTemp 'unpacked') -Recurse -Filter uv.exe | Select-Object -First 1
        if (-not $ftrBinary) { throw '归档缺 uv.exe' }
        Copy-Item $ftrBinary.FullName (Join-Path $ftrBin 'uv.exe')
        $ftrUv = Join-Path $ftrBin 'uv.exe'
    } finally { Remove-Item -Recurse -Force $ftrTemp }
}
$ftrPreviousEnvironment = $env:UV_PROJECT_ENVIRONMENT
Push-Location $ftrRoot
try {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $ftrRoot '.venv'

    $ftrArgs = @('sync','--frozen','--no-dev','--python','3.13')
    if ($Capability -in @('workbench','all')) { $ftrArgs += @('--extra','web') }
    & $ftrUv @ftrArgs
    if ($LASTEXITCODE -ne 0) { throw '项目环境安装失败，保留现有配置，请检查网络和依赖' }
    & (Join-Path $ftrRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'setup_runtime.py') $Capability
    if ($LASTEXITCODE -ne 0) { throw '环境验证未通过，读取上方 JSON 诊断' }
} finally { $env:UV_PROJECT_ENVIRONMENT = $ftrPreviousEnvironment; Pop-Location }
