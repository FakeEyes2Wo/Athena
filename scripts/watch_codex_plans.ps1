param(
    [int]$IntervalSeconds = 300,
    [string]$LogPath = ".athena/plan-monitor.jsonl"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$docsRoot = Join-Path $repoRoot "codex_docs"
$resolvedLog = Join-Path $repoRoot $LogPath
$logDirectory = Split-Path -Parent $resolvedLog
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

function Get-PlanSnapshot {
    $plans = Get-ChildItem -LiteralPath $docsRoot -File |
        Where-Object { $_.Name -match "(?i)plan.*\.md$" } |
        Sort-Object Name

    @($plans | ForEach-Object {
        [ordered]@{
            path = $_.FullName.Substring($repoRoot.Length + 1).Replace("\", "/")
            length = $_.Length
            modified_utc = $_.LastWriteTimeUtc.ToString("o")
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
        }
    })
}

function Write-MonitorEvent([string]$Kind, [object[]]$Snapshot) {
    $event = [ordered]@{
        observed_at = [DateTimeOffset]::Now.ToString("o")
        kind = $Kind
        interval_seconds = $IntervalSeconds
        plans = $Snapshot
    }
    Add-Content -LiteralPath $resolvedLog -Value ($event | ConvertTo-Json -Compress -Depth 5)
}

$previous = @(Get-PlanSnapshot)
Write-MonitorEvent -Kind "baseline" -Snapshot $previous

while ($true) {
    Start-Sleep -Seconds $IntervalSeconds
    $current = @(Get-PlanSnapshot)
    $before = $previous | ConvertTo-Json -Compress -Depth 5
    $after = $current | ConvertTo-Json -Compress -Depth 5
    if ($before -ne $after) {
        Write-MonitorEvent -Kind "changed" -Snapshot $current
    } else {
        Write-MonitorEvent -Kind "unchanged" -Snapshot $current
    }
    $previous = $current
}
