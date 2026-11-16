$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$nsRoot = $PSScriptRoot
$nsData = Join-Path $env:LOCALAPPDATA 'NotaSync'
$nsVenv = Join-Path $nsData 'venv'
$nsVenvPython = Join-Path $nsVenv 'Scripts\python.exe'

function Find-NotaPython {
    $nsCandidates = New-Object System.Collections.Generic.List[string]
    $nsPyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($nsPyLauncher) {
        try {
            $nsResolved = & $nsPyLauncher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $nsResolved) { $nsCandidates.Add(($nsResolved | Select-Object -Last 1).Trim()) }
        } catch { }
    }
    $nsPythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($nsPythonCommand -and $nsPythonCommand.Source -notlike '*\Microsoft\WindowsApps\*') {
        $nsCandidates.Add($nsPythonCommand.Source)
    }
    foreach ($nsVersion in @('314', '313', '312', '311')) {
        $nsCandidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python$nsVersion\python.exe"))
        if ($env:ProgramFiles) { $nsCandidates.Add((Join-Path $env:ProgramFiles "Python$nsVersion\python.exe")) }
    }
    foreach ($nsRegistryRoot in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore')) {
        if (Test-Path $nsRegistryRoot) {
            foreach ($nsKey in Get-ChildItem $nsRegistryRoot -ErrorAction SilentlyContinue) {
                $nsInstallKey = Join-Path $nsKey.PSPath 'InstallPath'
                if (Test-Path $nsInstallKey) {
                    $nsInstallPath = (Get-Item $nsInstallKey).GetValue('')
                    if ($nsInstallPath) { $nsCandidates.Add((Join-Path $nsInstallPath 'python.exe')) }
                }
            }
        }
    }
    foreach ($nsCandidate in ($nsCandidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $nsCandidate -PathType Leaf)) { continue }
        try {
            & $nsCandidate -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return $nsCandidate }
        } catch { }
    }
    return $null
}

try {
    if (-not $env:LOCALAPPDATA) { throw 'A pasta de dados do usuario nao esta disponivel.' }
    New-Item -ItemType Directory -Force -Path $nsData | Out-Null
    $nsPython = Find-NotaPython
    if (-not $nsPython) {
        $nsWinget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $nsWinget) {
            Write-Host 'E necessario Python 3.11 ou superior.' -ForegroundColor Yellow
            Write-Host 'Instale pelo site oficial https://www.python.org/downloads/windows/ e abra este arquivo novamente.'
            exit 1
        }
        Write-Host 'Instalando Python oficial para este usuario...' -ForegroundColor Cyan
        & $nsWinget.Source install --id Python.Python.3.12 --exact --source winget --scope user --silent --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) { throw 'A instalacao do Python nao foi concluida. Instale pelo site oficial indicado no guia.' }
        $nsPython = Find-NotaPython
        if (-not $nsPython) { throw 'O Python foi instalado, mas nao foi localizado. Feche esta janela e abra o programa novamente.' }
    }
    if (-not (Test-Path -LiteralPath $nsVenvPython)) {
        Write-Host 'Criando o ambiente do NotaSync...' -ForegroundColor Cyan
        & $nsPython -m venv $nsVenv
        if ($LASTEXITCODE -ne 0) { throw 'Nao foi possivel criar o ambiente Python.' }
    }
    $nsRequirements = Join-Path $nsRoot 'requirements.txt'
    $nsRequirementsHash = (Get-FileHash -LiteralPath $nsRequirements -Algorithm SHA256).Hash
    $nsMarker = Join-Path $nsData 'requirements.sha256'
    $nsPreviousHash = if (Test-Path -LiteralPath $nsMarker) { (Get-Content -LiteralPath $nsMarker -Raw).Trim() } else { '' }
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $nsData 'browsers'
    if ($nsRequirementsHash -ne $nsPreviousHash) {
        Write-Host 'Instalando componentes. Aguarde a conclusao...' -ForegroundColor Cyan
        & $nsVenvPython -m pip install --disable-pip-version-check -r $nsRequirements
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao baixar componentes. Verifique a internet e tente novamente.' }
        & $nsVenvPython -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar o navegador de automacao. Verifique a internet e tente novamente.' }
        Set-Content -LiteralPath $nsMarker -Value $nsRequirementsHash -Encoding ASCII
    }
    Set-Location -LiteralPath $nsRoot
    Write-Host 'Abrindo NotaSync. Mantenha esta janela aberta enquanto usa o programa.' -ForegroundColor Green
    & $nsVenvPython -m notasync
    if ($LASTEXITCODE -ne 0) { throw 'O programa foi encerrado com erro. Consulte a mensagem acima.' }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
