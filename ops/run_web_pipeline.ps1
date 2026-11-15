[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [ValidateRange(1, 365)]
    [int]$LogRetentionDays = 30,
    [switch]$Plan
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

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

$logDirectory = Join-Path $repoRoot "etl\logs\web-pipeline"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $logDirectory "web-pipeline-$timestamp.log"

function Invoke-LoggedPython {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$Heading,
        [switch]$Append
    )

    $header = "[$(Get-Date -Format o)] $Heading"
    if ($Append) {
        $header | Out-File -LiteralPath $logPath -Encoding utf8 -Append
    }
    else {
        $header | Out-File -LiteralPath $logPath -Encoding utf8
    }
    Write-Host $header

    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $lines = & $PythonPath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($null -ne $lines) {
        $lines | Out-File -LiteralPath $logPath -Encoding utf8 -Append
        foreach ($line in $lines) {
            Write-Host $line
        }
    }
    "exit_code=$exitCode" | Out-File -LiteralPath $logPath -Encoding utf8 -Append
    return [int]$exitCode
}

Push-Location $repoRoot
try {
    $pipelineArguments = @("etl/run_web_pipeline.py")
    if ($Plan) {
        $pipelineArguments += "--plan"
    }
    else {
        $pipelineArguments += "--resume-latest-failed"
    }
    $pipelineExitCode = Invoke-LoggedPython `
        -Heading "web pipeline" `
        -Arguments $pipelineArguments

    if ($Plan) {
        $healthExitCode = 0
    }
    else {
        $healthExitCode = Invoke-LoggedPython `
            -Heading "pipeline health" `
            -Arguments @("etl/check_pipeline_health.py", "--json") `
            -Append
    }
}
finally {
    Pop-Location
}

$cutoff = (Get-Date).AddDays(-$LogRetentionDays)
Get-ChildItem -LiteralPath $logDirectory -Filter "*.log" -File |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    Remove-Item -Force

Write-Host "log=$logPath"
if ($pipelineExitCode -ne 0) {
    exit $pipelineExitCode
}
exit $healthExitCode
