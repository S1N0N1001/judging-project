param([string]$Python = '.build-venv\Scripts\python.exe')
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
    & $Python -m PyInstaller --noconfirm Judging.spec
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
    Write-Host 'Build: dist\Judging\Judging.exe. Distribute the entire Judging directory.'
} finally { Pop-Location }
