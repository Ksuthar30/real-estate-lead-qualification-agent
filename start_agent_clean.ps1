$ErrorActionPreference = 'Stop'

$env:HTTP_PROXY = ''
$env:HTTPS_PROXY = ''
$env:ALL_PROXY = ''
$env:http_proxy = ''
$env:https_proxy = ''
$env:all_proxy = ''
$env:NO_PROXY = 'localhost,127.0.0.1,::1'
$env:no_proxy = 'localhost,127.0.0.1,::1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'

Set-Location $PSScriptRoot

$tmpDir = if ($env:AGENT_TMP_DIR) { $env:AGENT_TMP_DIR } else { Join-Path $PSScriptRoot 'agent_tmp' }
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
$env:TEMP = $tmpDir
$env:TMP = $tmpDir
$env:TMPDIR = $tmpDir

$outLog = Join-Path $PSScriptRoot 'agent-live-current.out'
$errLog = Join-Path $PSScriptRoot 'agent-live-current.err'

# LiveKit writes normal INFO startup lines to stderr. Keep strict mode for the
# setup above, but do not let native stderr records terminate the worker.
$ErrorActionPreference = 'Continue'
if (Get-Variable PSNativeCommandUseErrorActionPreference -Scope Global -ErrorAction SilentlyContinue) {
    $global:PSNativeCommandUseErrorActionPreference = $false
}

$python = $null
if (Test-Path (Join-Path $PSScriptRoot 'venv\Scripts\python.exe')) {
    $python = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
    & $python 'agent.py' 'start' 1>> $outLog 2>> $errLog
    exit $LASTEXITCODE
}

try {
    & py -3.13 'agent.py' 'start' 1>> $outLog 2>> $errLog
    exit $LASTEXITCODE
} catch {
    & py -3 'agent.py' 'start' 1>> $outLog 2>> $errLog
    exit $LASTEXITCODE
}
