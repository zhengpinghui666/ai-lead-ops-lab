param(
    [ValidateRange(1024,65535)][int]$Port = 11434,
    [string]$Executable = '',
    [string]$ModelDirectory = ''
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskExe = if ($Executable) { $Executable } else { Join-Path $taskRoot '.tools/ollama-v0.34.0/ollama.exe' }
if (-not (Test-Path -LiteralPath $taskExe)) { throw 'The verified local Ollama runtime is not installed.' }
$env:OLLAMA_HOST = "127.0.0.1:$Port"
$env:OLLAMA_MODELS = if ($ModelDirectory) { $ModelDirectory } else { Join-Path $taskRoot 'data/private/local-models' }
$env:OLLAMA_NO_CLOUD = '1'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:OLLAMA_MAX_LOADED_MODELS = '1'
$env:OLLAMA_CONTEXT_LENGTH = '4096'
$env:OLLAMA_KEEP_ALIVE = '10m'
$env:OLLAMA_GPU_OVERHEAD = '536870912'
$taskLogs = Join-Path $taskRoot 'data/private/local-model-runtime'
New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
Set-Location -LiteralPath $taskRoot
# Windows PowerShell 5 treats native stderr as an error record under Stop.
# Redirect at the process boundary so ordinary runtime logs cannot stop serving.
$taskProcess = Start-Process -FilePath $taskExe -ArgumentList 'serve' -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput (Join-Path $taskLogs 'stdout.log') -RedirectStandardError (Join-Path $taskLogs 'stderr.log')
exit $taskProcess.ExitCode
