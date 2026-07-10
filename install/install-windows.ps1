param(
  [switch]$NoStart,
  [switch]$BackendOnly,
  [string]$InstallDir,
  [ValidateSet("auto", "local", "ngrok", "cloudflare")]
  [string]$TunnelProvider = "auto"
)

$ErrorActionPreference = "Stop"

$BackendRepoUrl = if ($env:AGENCY_BACKEND_REPO_URL) { $env:AGENCY_BACKEND_REPO_URL } else { "https://github.com/knivore/open-agency.git" }
$FrontendRepoUrl = if ($env:AGENCY_FRONTEND_REPO_URL) { $env:AGENCY_FRONTEND_REPO_URL } else { "https://github.com/knivore/open-agency-fe.git" }
$InstallRoot = if ($env:AGENCY_INSTALL_DIR) { $env:AGENCY_INSTALL_DIR } else { Join-Path $HOME "OpenAgency" }
$StartAfterInstall = $true
$CloneFrontend = $true

if ($PSBoundParameters.ContainsKey("InstallDir")) {
  $InstallRoot = $InstallDir
}
if ($NoStart) {
  $StartAfterInstall = $false
}
if ($BackendOnly) {
  $CloneFrontend = $false
}

$BackendDir = Join-Path $InstallRoot "open-agency"
$FrontendDir = Join-Path $InstallRoot "open-agency-fe"

function Require-Command {
  param([string]$Name, [string]$InstallHint)

  if (Get-Command $Name -ErrorAction SilentlyContinue) {
    return
  }
  throw "Missing required command '$Name'. $InstallHint"
}

function Get-DockerDesktopPath {
  $candidates = @(
    (Join-Path $Env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
    (Join-Path $Env:LocalAppData "Programs\Docker\Docker\Docker Desktop.exe")
  )

  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
      return $candidate
    }
  }
  return $null
}

function Wait-ForDocker {
  for ($attempt = 0; $attempt -lt 30; $attempt += 1) {
    try {
      docker info *> $null
      return $true
    } catch {
      Start-Sleep -Seconds 2
    }
  }
  return $false
}

function Ensure-DockerReady {
  Require-Command docker "Install Docker Desktop and retry."

  try {
    docker info *> $null
    return
  } catch {
    $dockerDesktop = Get-DockerDesktopPath
    if ($dockerDesktop) {
      Write-Host "Starting Docker Desktop..."
      Start-Process -FilePath $dockerDesktop | Out-Null
      if (Wait-ForDocker) {
        return
      }
    }

    throw "Docker Desktop is installed but not ready. Open Docker Desktop, wait until Docker is running, then retry."
  }
}

function Sync-Repo {
  param(
    [string]$Url,
    [string]$Dir
  )

  if (Test-Path (Join-Path $Dir ".git")) {
    Write-Host "Updating $Dir..."
    git -C $Dir pull --ff-only
    return
  }

  $parent = Split-Path -Parent $Dir
  if (-not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent | Out-Null
  }
  Write-Host "Cloning $Url into $Dir..."
  git clone $Url $Dir
}

function Get-StartArguments {
  $args = @("start")
  switch ($TunnelProvider) {
    "local" { $args += "-local" }
    "ngrok" { $args += "-ngrok" }
    "cloudflare" { $args += "-cloudflare" }
  }
  return $args
}

Write-Host "Installing Open Agency into $InstallRoot"

Require-Command git "Install Git for Windows or run 'winget install --id Git.Git -e' and retry."
Require-Command py "Install Python 3.12+ from python.org or run 'winget install --id Python.Python.3.12 -e' and retry."
Require-Command node "Install Node.js or run 'winget install --id OpenJS.NodeJS.LTS -e' and retry."
Require-Command npm "Install Node.js/npm and retry."
Ensure-DockerReady

Sync-Repo -Url $BackendRepoUrl -Dir $BackendDir

if ($CloneFrontend) {
  Sync-Repo -Url $FrontendRepoUrl -Dir $FrontendDir
} else {
  Write-Host "Skipping open-agency-fe clone because -BackendOnly was selected."
}

Set-Location $BackendDir

if (-not $StartAfterInstall) {
  $nextArgs = (Get-StartArguments) -join " "
  Write-Host ""
  Write-Host "Install complete."
  Write-Host "Next step: Set-Location $BackendDir ; .\run-windows.cmd $nextArgs"
  exit 0
}

Write-Host "Starting Open Agency..."
$startArgs = Get-StartArguments
if ($CloneFrontend) {
  & .\run-windows.cmd @startArgs
} else {
  $env:AGENCY_FRONTEND_ENABLED = "false"
  & .\run-windows.cmd @startArgs
}
