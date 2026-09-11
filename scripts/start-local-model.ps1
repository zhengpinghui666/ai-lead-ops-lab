param(
    [ValidateRange(1024,65535)][int]$Port = 11435,
    [string]$Executable = '',
    [string]$ModelDirectory = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Executable) { $Executable = Join-Path $projectRoot '.tools\ollama-v0.33.3\ollama.exe' }
if (-not $ModelDirectory) { $ModelDirectory = Join-Path $projectRoot '.tools\ollama-models' }
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw 'Ollama executable missing. See LOCAL_MODEL.md for the portable runtime setup.'
}
$Executable = (Resolve-Path -LiteralPath $Executable).Path
$ModelDirectory = [System.IO.Path]::GetFullPath($ModelDirectory)
$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
try { $listener.Start() } finally { $listener.Stop() }
New-Item -ItemType Directory -Path $ModelDirectory -Force | Out-Null
$modelEnvironment = @{
    OLLAMA_HOST = "127.0.0.1:$Port"
    OLLAMA_MODELS = $ModelDirectory
    OLLAMA_NO_CLOUD = '1'
    OLLAMA_NUM_PARALLEL = '1'
    OLLAMA_MAX_LOADED_MODELS = '1'
    OLLAMA_MAX_QUEUE = '1'
    OLLAMA_KEEP_ALIVE = '0'
}
$previousEnvironment = @{}
foreach ($name in $modelEnvironment.Keys) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $modelEnvironment[$name], 'Process')
}
try {
    Write-Host "Local model runtime: http://127.0.0.1:$Port ; cloud disabled. Ctrl+C stops this foreground service."
    & $Executable serve
    if ($LASTEXITCODE -ne 0) { throw "Ollama exited with code $LASTEXITCODE" }
} finally {
    foreach ($name in $modelEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
}
