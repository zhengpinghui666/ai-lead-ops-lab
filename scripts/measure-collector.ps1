param(
    [Parameter(Mandatory = $true)][ValidateRange(1, 2147483647)][int]$TaskId,
    [ValidateRange(1, 300)][int]$MaxSeconds = 90,
    [ValidateRange(500, 10000)][int]$IntervalMs = 1000,
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$DataDir = $env:CLUBOPS_DATA_DIR
)

# Read-only sampling of one task and its dedicated Chrome process tree.
# Working sets can double-count shared pages; private commit is not physical RAM.
$projectDir = Split-Path -Parent $PSScriptRoot
if (-not $DataDir) { $DataDir = Join-Path $projectDir 'data' }
$profilePath = [IO.Path]::GetFullPath((Join-Path $DataDir 'browser-profile')).Replace('\', '/').ToLowerInvariant()
$timer = [Diagnostics.Stopwatch]::StartNew()
do {
    try {
        $snapshot = Invoke-RestMethod "http://127.0.0.1:$Port/api/collector" -TimeoutSec 5
        $task = $snapshot.tasks | Where-Object id -eq $TaskId | Select-Object -First 1
        $chrome = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'")
        $ids = [Collections.Generic.HashSet[int]]::new()
        foreach ($item in $chrome) {
            $command = ([string]$item.CommandLine).Replace('\', '/').ToLowerInvariant()
            if ($command.Contains($profilePath) -and $command.Contains('--user-data-dir')) { [void]$ids.Add([int]$item.ProcessId) }
        }
        do {
            $added = $false
            foreach ($item in $chrome) {
                if ($ids.Contains([int]$item.ParentProcessId) -and $ids.Add([int]$item.ProcessId)) { $added = $true }
            }
        } while ($added)
        $processes = @($ids | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        $working = ($processes | Measure-Object WorkingSet64 -Sum).Sum
        $private = ($processes | Measure-Object PrivateMemorySize64 -Sum).Sum
        $system = Get-CimInstance Win32_OperatingSystem
        [ordered]@{
            time = (Get-Date).ToString('o')
            task_id = $TaskId
            status = $(if ($task) { $task.status } else { 'task_not_found' })
            active_pages = $task.active_pages
            peak_pages = $task.peak_pages
            chrome_processes = $processes.Count
            working_set_sum_mib = [math]::Round($working / 1MB, 1)
            private_commit_sum_mib = [math]::Round($private / 1MB, 1)
            system_free_mib = [math]::Round($system.FreePhysicalMemory / 1024, 1)
        } | ConvertTo-Json -Compress
        if ($task -and $task.finished_at -and -not $task.active) { break }
        if ($task.status -in @('needs_login', 'needs_verification', 'needs_interaction')) { break }
    } catch {
        [ordered]@{time=(Get-Date).ToString('o');task_id=$TaskId;error=$_.Exception.GetType().Name} | ConvertTo-Json -Compress
    }
    if ($timer.Elapsed.TotalSeconds -lt $MaxSeconds) { Start-Sleep -Milliseconds $IntervalMs }
} while ($timer.Elapsed.TotalSeconds -lt $MaxSeconds)
