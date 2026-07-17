<#
.SYNOPSIS
  radio-server one-command installer for Windows (ADR 0053).

.DESCRIPTION
  Gets you from nothing to the control panel — pointed at the demo server, so you can explore right
  away. Installs uv (which brings its own Python), fetches the pieces, builds the web page, and
  writes a starter config.

  On Windows, radio-server runs fully in PRACTICE mode (no radio), and the browser Mumble client
  works. Over-the-air DTMF login now decodes in-process (no external helper), so a real radio works
  on Windows too — this installer sets up the practice/browser experience; see docs/install.md for
  the real-hardware notes.

  Run it either way:
    irm https://raw.githubusercontent.com/kbennett2000/radio-server/master/scripts/install.ps1 | iex
    .\scripts\install.ps1              # from inside a checkout

.PARAMETER ForceWeb
  Rebuild the web page even if it is already built.

.PARAMETER Run
  Start the server when the install finishes.

.NOTES
  Safe to run again any time. It never overwrites an existing radio.toml or radio-secrets.toml, so a
  re-run can't clobber your callsign, password, or login secret. If a step fails, fix what it printed
  and run the same line again — it picks up where it left off.
#>
[CmdletBinding()]
param(
  [switch]$ForceWeb,
  [switch]$Run
)

$ErrorActionPreference = 'Stop'
$RepoUrl     = 'https://github.com/kbennett2000/radio-server.git'
$RepoTarball = 'https://github.com/kbennett2000/radio-server/archive/refs/heads/master.zip'
$Port        = 8000

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Info($m) { Write-Host "    $m" }
function Ok($m)   { Write-Host "    ok $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    ! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "install stopped: $m" -ForegroundColor Yellow; exit 1 }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }

Step "Windows note"
Info "Practice mode and the browser Mumble voice link work natively here — no WSL2 needed."
Info "Only a REAL radio needs WSL2 (the DTMF decoder has no Windows build) — see docs/install.md."

# --- 1. find or fetch the repo --------------------------------------------------------------------
Step "Finding the radio-server files"
function Find-Root {
  $d = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
  while ($d -and (Test-Path $d)) {
    $pp = Join-Path $d 'pyproject.toml'
    if ((Test-Path $pp) -and (Select-String -Path $pp -Pattern 'name = "radio-server"' -Quiet)) { return $d }
    $parent = Split-Path $d -Parent
    if ($parent -eq $d) { break }
    $d = $parent
  }
  $pp = Join-Path (Get-Location).Path 'pyproject.toml'
  if ((Test-Path $pp) -and (Select-String -Path $pp -Pattern 'name = "radio-server"' -Quiet)) { return (Get-Location).Path }
  return $null
}

$Root = Find-Root
if ($Root) {
  Ok "using $Root"
} else {
  Info "no checkout here — downloading a fresh copy into .\radio-server"
  if (Test-Path 'radio-server') { Die "a .\radio-server already exists but isn't a valid checkout; move it aside and re-run" }
  if (Have git) {
    git clone --depth 1 $RepoUrl radio-server
  } else {
    $zip = Join-Path $env:TEMP 'radio-server.zip'
    Invoke-WebRequest -Uri $RepoTarball -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath . -Force
    Rename-Item 'radio-server-master' 'radio-server'
    Remove-Item $zip -ErrorAction SilentlyContinue
  }
  $Root = (Resolve-Path 'radio-server').Path
  Ok "downloaded to $Root"
}
Set-Location $Root

# --- 2. uv (brings its own Python) ----------------------------------------------------------------
Step "Checking for uv (the helper that gathers everything else)"
if (-not (Have uv)) {
  Info "installing uv from astral.sh ..."
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  $uvbin = Join-Path $env:USERPROFILE '.local\bin'
  if (Test-Path $uvbin) { $env:Path = "$uvbin;$env:Path" }
}
if (-not (Have uv)) { Die "uv still isn't on PATH. Open a new terminal and run this script again." }
Ok (uv --version)

# --- 3. Node (for the web page) -------------------------------------------------------------------
Step "Checking for Node.js (used only to build the control panel)"
if (-not (Have npm)) {
  Warn "Node.js isn't installed."
  Info "Install the LTS version from https://nodejs.org/ then run this script again —"
  Info "it picks up right here where it left off."
  exit 2
}
Ok (node --version)

# --- 4. Python deps (practice mode + the browser voice link; a real radio needs WSL2) --------------
Step "Gathering radio-server's pieces (uv sync)"
# The Mumble voice link is the headline feature and needs no radio — it ships by default (ADR 0057).
# libopus rides along in the extra (a bundled-wheel carrier), so there's nothing to install by hand.
uv sync --extra mumble
Info "practice-mode install, with the browser voice link. A real radio needs WSL2 — see docs/install.md."
Ok "dependencies ready"

# --- 5. build the web page ------------------------------------------------------------------------
Step "Building the control panel"
if ((Test-Path 'web\dist\index.html') -and (-not $ForceWeb)) {
  Ok "already built (use -ForceWeb to rebuild)"
} else {
  Push-Location web
  npm install
  npm run build
  Pop-Location
  Ok "control panel built"
}

# --- 6. first-run config (never overwrites what's already there) ----------------------------------
Step "Setting up your configuration"
if (Test-Path 'radio.toml') {
  Ok "radio.toml already exists — leaving it untouched"
} else {
  Copy-Item 'radio.toml.example' 'radio.toml'
  Info "wrote radio.toml (starts on the practice radio, already pointed at the demo server)"
  $callsign = Read-Host "  Your FCC callsign (needed before transmitting; press Enter to skip for now)"
  if ($callsign) {
    $up = $callsign.ToUpper()
    $lines = Get-Content 'radio.toml'
    if ($lines -match '^# callsign = ') {
      $lines = $lines -replace '^# callsign = .*', "callsign = `"$up`""
      Set-Content 'radio.toml' $lines
    } elseif (-not ($lines -match '^callsign = ')) {
      Add-Content 'radio.toml' "`n[station]`ncallsign = `"$up`""
    }
    Ok "callsign set to $up"
  } else {
    Info "no callsign yet — set it in radio.toml before you transmit (looking around is fine without)."
  }
}

Step "Control-panel password"
$tokenSet = (Test-Path 'radio-secrets.toml') -and (Select-String -Path 'radio-secrets.toml' -Pattern '^api_token' -Quiet)
if ($tokenSet) {
  Ok "already set (in radio-secrets.toml) — leaving it untouched"
} else {
  $token = uv run python -c "from radio_server.config.secrets import rotate; print(rotate('radio-secrets.toml','api_token'))"
  Info "This is the password you'll type in the browser (saved in radio-secrets.toml):"
  Write-Host ""
  Write-Host "      $token" -ForegroundColor Cyan
  Write-Host ""
}

Step "Over-the-air login (optional — only needed to log in from a radio)"
Info "Callers log in with a rolling 6-digit code from a phone authenticator app (Google"
Info "Authenticator, Authy, any TOTP app) — install one first if you want to set this up now."
Info "The easiest way is in the browser later: Settings -> Secrets -> Set up login code."
$ans = Read-Host "  Set it up here on the command line instead? [y/N]"
if ($ans -match '^(y|yes)$') {
  try { uv run python -m radio_server.enroll } catch { Warn "enrollment skipped (run later: uv run python -m radio_server.enroll)" }
} else {
  Info "skipped — do it in the browser (Settings -> Secrets), or run"
  Info "'uv run python -m radio_server.enroll' any time. Walkthrough: docs/install.md."
}

# --- 7. earn the banner: verify the headline browser voice link actually loads --------------------
# Runs the exact opus shim that used to be silently broken. A native non-zero exit doesn't throw under
# $ErrorActionPreference='Stop', so branch on $LASTEXITCODE rather than try/catch.
Step "Checking the Mumble voice link"
uv run python -c "import sys; from radio_server.link._opus import check_mumble_importable as c; ok, m = c(); print('    ' + m); sys.exit(0 if ok else 1)"
$MumbleOk = ($LASTEXITCODE -eq 0)
if ($MumbleOk) { Ok "voice link ready — the browser Connect button will work" }
else { Warn "the Mumble voice link isn't ready (see the message just above)" }

# --- 8. done --------------------------------------------------------------------------------------
Step "All set."
Write-Host ""
Write-Host "Start radio-server with:"
Write-Host "    uv run python -m radio_server" -ForegroundColor Cyan
Write-Host ""
Write-Host "then open http://127.0.0.1:$Port in your browser and enter the password above."
Write-Host ""
Write-Host "  - First time here?           docs/getting-started.md"
Write-Host "  - Connecting a real radio?    docs/install.md   (WSL2 on Windows)"
Write-Host "  - Running your own server?    docs/mumble-server/"
if (-not $MumbleOk) {
  Write-Host ""
  Warn "heads up: the browser 'Connect' (voice link) won't work until the message above is fixed —"
  Warn "fix it and re-run this installer. Everything else (practice mode) still works."
}

if ($Run) {
  Step "Starting radio-server (Ctrl+C to stop) ..."
  uv run python -m radio_server
}
