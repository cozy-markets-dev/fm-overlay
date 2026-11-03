$ErrorActionPreference = 'Stop'

try {
    $project = Split-Path -Parent $PSScriptRoot
    $python = Join-Path $project '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $python = 'C:\Python312\pythonw.exe'
    }
    if (-not (Test-Path -LiteralPath $python)) {
        $console = & py.exe -3.12 -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -ne 0) {
            throw 'Install Python 3.12 before starting MaplePrice from source.'
        }
        $python = Join-Path (Split-Path -Parent $console) 'pythonw.exe'
    }
    if (-not (Test-Path -LiteralPath $python)) {
        throw 'Could not find pythonw.exe for Python 3.12.'
    }

    $console = Join-Path (Split-Path -Parent $python) 'python.exe'
    $dependencyCheck = "import importlib.util, sys; modules = ['tkinter', 'rapidocr_onnxruntime', 'cv2', 'numpy', 'mss', 'PIL', 'rapidfuzz', 'requests', 'psutil', 'cryptography']; missing = [name for name in modules if importlib.util.find_spec(name) is None]; print('Missing dependencies: ' + ', '.join(missing)) if missing else None; sys.exit(bool(missing))"
    & $console -c $dependencyCheck
    if ($LASTEXITCODE -ne 0) {
        $requirements = Join-Path $project 'requirements.txt'
        throw "Install the source dependencies first: `"$console`" -m pip install -r `"$requirements`""
    }

    $entry = Join-Path $project 'run.py'
    # Full paths work from Explorer and elevated launches, including WSL shares.
    # Packet Monitor needs elevation; pythonw keeps the diagnostic console closed.
    Start-Process -FilePath $python -ArgumentList ('"' + $entry + '"') -Verb RunAs -WorkingDirectory $env:TEMP
    exit 0
}
catch {
    Write-Host "Could not start MaplePrice: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
