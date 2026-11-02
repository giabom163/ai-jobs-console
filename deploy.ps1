param(
    [switch]$Publish,
    [string]$SitePath = ''
)
$ErrorActionPreference = 'Stop'
$site = if ([string]::IsNullOrWhiteSpace($SitePath)) { Join-Path $PSScriptRoot 'site' } else { (Resolve-Path -LiteralPath $SitePath).Path }
$manifestPath = Join-Path $site 'build-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'Build site first: python tools/build_site.py --limit 3' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($entry in $manifest.files.PSObject.Properties) {
    if ([IO.Path]::GetFileName($entry.Name) -ne $entry.Name) { throw 'Invalid manifest path' }
    $path = Join-Path $site $entry.Name
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) {
        throw ('Artifact differs from manifest: ' + $entry.Name)
    }
}
$allowed = @($manifest.files.PSObject.Properties.Name) + 'build-manifest.json'
foreach ($file in Get-ChildItem -LiteralPath $site -Force) {
    if ($file.PSIsContainer -or $file.Name -notin $allowed) { throw ('Unlisted publish artifact: ' + $file.Name) }
}
if (-not $Publish) {
    Write-Output ('Prepared only: ' + $site + '; jobs=' + $manifest.jobs)
    Write-Output 'No deployment performed. After approval, run: .\deploy.ps1 -Publish'
    exit 0
}
$wrangler = Get-Command wrangler.cmd -ErrorAction Stop
$node = 'C:\Users\Lenovo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe'
$cli = Join-Path (Split-Path $wrangler.Source) 'node_modules\wrangler\bin\wrangler.js'
if (-not (Test-Path -LiteralPath $cli)) { throw ('Missing Wrangler CLI: ' + $cli) }
Push-Location $PSScriptRoot
try {
    & $node $cli pages deploy $site --project-name giabom-blog --branch main
    if ($LASTEXITCODE -ne 0) { throw ('Wrangler exit code: ' + $LASTEXITCODE) }
} finally {
    Pop-Location
}
Write-Output 'Upload finished. Production page and dataset still require verification.'
