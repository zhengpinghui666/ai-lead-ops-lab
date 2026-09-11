param(
    [string]$PythonPath = $env:CLUBOPS_PYTHON,
    [ValidateRange(1,65535)][int]$Port = 8765,
    [string]$DataDir,
    [string]$ReceiptDir
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$baseUrl = "http://127.0.0.1:$Port"
$instance = Invoke-RestMethod -Uri "$baseUrl/api/service" -TimeoutSec 8
if ($instance.service -ne 'ClubOps' -or $instance.graceful_shutdown -ne $true) { throw '当前实例不支持正常关闭；未终止任何进程' }
if (-not $PythonPath) { $PythonPath = $instance.python }
if (-not $DataDir) { $DataDir = $instance.data_directory }
$DataDir = [System.IO.Path]::GetFullPath($DataDir)
if ($DataDir -ne $instance.data_directory) { throw '重启必须保留当前实例的数据目录；未停止服务' }
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'Python 路径无效；未停止服务' }
$receipt = if ($ReceiptDir) { [System.IO.Path]::GetFullPath($ReceiptDir) } else { Join-Path $projectDir ('artifacts\service-restart-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')) }
New-Item -ItemType Directory -Path $receipt -ErrorAction Stop | Out-Null
& $PythonPath (Join-Path $projectDir 'manage.py') backup --data-dir $DataDir --to (Join-Path $receipt 'database-backup') | Out-Null
if ($LASTEXITCODE -ne 0) { throw '备份未完成，服务未停止' }
$state = Invoke-RestMethod -Uri "$baseUrl/api/state?mode=live" -TimeoutSec 10
$headers = @{'X-ClubOps-Token'=$state.csrf;Origin=$baseUrl}
$body = @{instance_id=$instance.instance_id} | ConvertTo-Json -Compress
$stopped = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/service-stop?mode=live" -ContentType 'application/json' -Headers $headers -Body $body -TimeoutSec 10
if ($stopped.status -ne 'stopping') { throw '服务未接受关闭请求' }
$deadline = [DateTime]::UtcNow.AddSeconds(25)
do {
    $old = Get-Process -Id $instance.pid -ErrorAction SilentlyContinue
    if (-not $old) { break }
    Start-Sleep -Milliseconds 200
} while ([DateTime]::UtcNow -lt $deadline)
if ($old) { throw '旧实例尚未退出；未强制终止，也未启动第二份服务' }
$previous = @{}
$settings = @{LEADOPS_PORT=[string]$Port;CLUBOPS_DATA_DIR=$DataDir;PYTHONIOENCODING='utf-8'}
try {
    foreach ($key in $settings.Keys) { $previous[$key]=[Environment]::GetEnvironmentVariable($key,'Process'); [Environment]::SetEnvironmentVariable($key,$settings[$key],'Process') }
    $new = Start-Process -FilePath $PythonPath -ArgumentList @('-u','server.py') -WorkingDirectory $projectDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $receipt 'stdout.log') -RedirectStandardError (Join-Path $receipt 'stderr.log') -PassThru
} finally {
    foreach ($key in $previous.Keys) { [Environment]::SetEnvironmentVariable($key,$previous[$key],'Process') }
}
$deadline = [DateTime]::UtcNow.AddSeconds(20)
$ready = $null
do {
    try { $ready=Invoke-RestMethod -Uri "$baseUrl/api/service" -TimeoutSec 2 } catch { $ready=$null }
    if ($ready -and $ready.pid -eq $new.Id -and $ready.instance_id -ne $instance.instance_id) { break }
    if ($new.HasExited) { throw "新服务启动失败，请检查 $receipt\stderr.log" }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $deadline)
if (-not $ready -or $ready.pid -ne $new.Id) { throw "未确认新版服务就绪，请检查 $receipt" }
$result = @{old_pid=$instance.pid;new_pid=$new.Id;url=$baseUrl;status='restarted';receipt=$receipt}
$result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $receipt 'result.json') -Encoding utf8
$result | ConvertTo-Json
