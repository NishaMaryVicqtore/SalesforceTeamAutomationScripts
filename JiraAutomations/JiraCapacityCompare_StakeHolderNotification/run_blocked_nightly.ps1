$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python venv not found at $python"
}
& $python (Join-Path $root "update_blocked.py")
if ($LASTEXITCODE -ne 0) {
    throw "update_blocked.py exited with $LASTEXITCODE"
}
