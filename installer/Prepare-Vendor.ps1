$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try {
    $nsVendor = Join-Path $PSScriptRoot 'vendor'
    $nsManifest = Get-Content -LiteralPath (Join-Path $nsVendor 'SOURCES.json') -Raw | ConvertFrom-Json
    foreach ($nsFile in $nsManifest.files) {
        $nsName = [string]$nsFile.file
        if ([IO.Path]::GetFileName($nsName) -ne $nsName) { throw 'Nome de arquivo invalido no manifesto.' }
        $nsUrl = [Uri]([string]$nsFile.source)
        if ($nsUrl.Scheme -ne 'https' -or $nsUrl.Host -notin @('www.python.org', 'files.pythonhosted.org')) { throw 'Origem de componente invalida.' }
        $nsDestination = Join-Path $nsVendor $nsName
        if ((Test-Path -LiteralPath $nsDestination) -and (Get-FileHash -LiteralPath $nsDestination -Algorithm SHA256).Hash -eq $nsFile.sha256) { continue }
        Write-Host ('Baixando componente oficial: ' + $nsName)
        $nsPartial = $nsDestination + '.download'
        Invoke-WebRequest -Uri $nsUrl.AbsoluteUri -OutFile $nsPartial -UseBasicParsing -TimeoutSec 1200
        if ((Get-FileHash -LiteralPath $nsPartial -Algorithm SHA256).Hash -ne $nsFile.sha256) {
            Remove-Item -LiteralPath $nsPartial -Force
            throw 'Falha de integridade no componente baixado.'
        }
        Move-Item -LiteralPath $nsPartial -Destination $nsDestination -Force
    }
    Write-Host 'Componentes oficiais verificados.'
    exit 0
} catch {
    Write-Host $_.Exception.Message
    exit 1
}
