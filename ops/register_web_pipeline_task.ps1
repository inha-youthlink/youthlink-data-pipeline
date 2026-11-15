[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "YouthLink-Web-Pipeline",
    [datetime]$DailyAt = [datetime]::Today.AddHours(4).AddMinutes(30),
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runnerPath = Join-Path $repoRoot "ops\run_web_pipeline.ps1"

if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Scheduled runner does not exist: $runnerPath"
}

function Test-PythonRuntime {
    param([string]$Candidate)

    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Candidate -c "import psycopg, dotenv, requests, openai, lxml" *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $candidates = @(
        (Join-Path $repoRoot ".venv\Scripts\python.exe"),
        (Get-Command python.exe -ErrorAction Stop).Source
    ) | Select-Object -Unique
    $PythonPath = $candidates |
        Where-Object { Test-PythonRuntime $_ } |
        Select-Object -First 1
}

if ([string]::IsNullOrWhiteSpace($PythonPath) -or
    -not (Test-PythonRuntime $PythonPath)) {
    throw (
        "No Python runtime with required packages was found. " +
        "Run: python -m pip install -r requirements.txt"
    )
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$powerShellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$taskArguments = (
    '-NoProfile -ExecutionPolicy Bypass -File "{0}" -PythonPath "{1}"' -f
    $runnerPath,
    $PythonPath
)

$action = New-ScheduledTaskAction `
    -Execute $powerShellPath `
    -Argument $taskArguments `
    -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -MultipleInstances IgnoreNew
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, "register daily YouthLink web pipeline")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "YouthLink bounded crawling, AI evidence review, web embedding, and RAG publish" `
        -Force | Out-Null

    $task = Get-ScheduledTask -TaskName $TaskName
    $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "task=$($task.TaskName)"
    Write-Host "state=$($task.State)"
    Write-Host "next_run=$($taskInfo.NextRunTime)"
    Write-Host "user=$currentUser"
}
