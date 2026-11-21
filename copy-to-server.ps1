[CmdletBinding()]
param([string] $EnvironmentFile = (Join-Path $PSScriptRoot ".env"))

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$package = Join-Path $PSScriptRoot "dist/radio_ad_detector.zip"
if (-not (Test-Path -LiteralPath $package -PathType Leaf)) {
    throw "Package missing. Run python tools/package_integration.py first."
}

$settings = @{}
foreach ($rawLine in Get-Content -LiteralPath $EnvironmentFile) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith("#")) { continue }
    if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        throw "Invalid setting in $EnvironmentFile. Expected KEY=value."
    }
    $key = $Matches[1]
    $value = $Matches[2]
    if ($value -match '^"(.*)"$' -or $value -match "^'(.*)'$") {
        $value = $Matches[1]
    }
    $settings[$key] = $value
}
foreach ($key in "HA_HOST", "HA_USER", "HA_CUSTOM_COMPONENTS_PATH") {
    if (-not $settings[$key]) { throw "Missing $key in $EnvironmentFile." }
}
$serverHost = $settings["HA_HOST"]
$serverUser = $settings["HA_USER"]
$components = $settings["HA_CUSTOM_COMPONENTS_PATH"].TrimEnd("/")
if ($serverHost -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$' -or
    $serverUser -notmatch '^[A-Za-z_][A-Za-z0-9._-]*$') {
    throw "Invalid HA_HOST or HA_USER."
}
if ($components -notmatch '^/(?:[A-Za-z0-9_.-]+/)*custom_components$' -or
    ($components.Split("/") -contains "..")) {
    throw "HA_CUSTOM_COMPONENTS_PATH must be an absolute path ending in /custom_components."
}

$target = "$serverUser@$serverHost"
$deployments = "$components/../.radio-ad-detector-deployments"
$deploymentId = "{0}-{1}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), ([guid]::NewGuid().ToString("N"))
$remoteZip = "$deployments/$deploymentId.zip"
$sshOptions = @("-o", "BatchMode=yes")
Get-Command ssh, scp -ErrorAction Stop | Out-Null
& ssh @sshOptions $target "test -d '$components' && mkdir -p '$deployments'"
if ($LASTEXITCODE -ne 0) { throw "Unable to prepare the remote deployment directory." }

try {
    & scp @sshOptions $package "${target}:$remoteZip"
    if ($LASTEXITCODE -ne 0) { throw "Unable to upload the integration ZIP." }

    # Python accepts both LF and CRLF when reading the installer from stdin.
    Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot "tools/install_on_server.py") |
        & ssh @sshOptions $target "python3 - '$components' '$remoteZip'"
    if ($LASTEXITCODE -ne 0) { throw "Unable to install the integration ZIP." }
    Write-Host "Copy complete. Restart Home Assistant to load the packaged version."
}
finally {
    & ssh @sshOptions $target "rm -f '$remoteZip'"
    if ($LASTEXITCODE -ne 0) { Write-Warning "Unable to remove the uploaded ZIP from the staging directory." }
}
