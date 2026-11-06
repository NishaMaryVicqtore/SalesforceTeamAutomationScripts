$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python venv not found at $python"
}
& $python (Join-Path $root "update_in_verification.py")
if ($LASTEXITCODE -ne 0) {
    throw "update_in_verification.py exited with $LASTEXITCODE"
}
