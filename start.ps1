param(
    [string]$PythonPath = $env:CLUBOPS_PYTHON,
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$DataDir,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $PythonPath) {
    $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pythonCommand) { throw '未找到 Python；安装 Python 3.10+ 或传入 -PythonPath。' }
    $PythonPath = $pythonCommand.Source
}
& $PythonPath -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'
if ($LASTEXITCODE -ne 0) { throw '需要 Python 3.10 或更高版本。' }
$previousPort = $env:LEADOPS_PORT
$previousData = $env:CLUBOPS_DATA_DIR
try {
    if ($PSBoundParameters.ContainsKey('Port') -or -not $env:LEADOPS_PORT) { $env:LEADOPS_PORT = [string]$Port }
    if ($DataDir) { $env:CLUBOPS_DATA_DIR = [System.IO.Path]::GetFullPath($DataDir) }
    if ($Check) { & $PythonPath (Join-Path $projectDir 'manage.py') doctor }
    else { & $PythonPath (Join-Path $projectDir 'server.py') }
    $resultCode = $LASTEXITCODE
} finally {
    $env:LEADOPS_PORT = $previousPort
    $env:CLUBOPS_DATA_DIR = $previousData
}
exit $resultCode
