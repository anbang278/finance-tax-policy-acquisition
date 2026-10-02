$ftrRoot = Split-Path -Parent $PSScriptRoot
$ftrPython = Join-Path $ftrRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $ftrPython)) { throw '运行环境不存在，请先执行 scripts/setup.ps1' }
$ftrConfig = if ($env:FTR_CONFIG) { $env:FTR_CONFIG } else { Join-Path $ftrRoot 'ftr.local.yaml' }
$ftrPreviousPythonHome = $env:PYTHONHOME
$ftrPreviousPythonPath = $env:PYTHONPATH
try {
    Remove-Item Env:PYTHONHOME, Env:PYTHONPATH -ErrorAction SilentlyContinue
    & $ftrPython -m ftr.cli --config $ftrConfig @args
    $ftrExitCode = $LASTEXITCODE
} finally { $env:PYTHONHOME = $ftrPreviousPythonHome; $env:PYTHONPATH = $ftrPreviousPythonPath }
exit $ftrExitCode
