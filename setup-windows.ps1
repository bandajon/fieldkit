# FieldKit one-shot Windows setup. Launch via setup-windows.bat (double-click)
# or: powershell -NoProfile -ExecutionPolicy Bypass -File setup-windows.ps1
# Idempotent - safe to run again any time. Relaunches itself elevated.

# --- self-elevate -------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent(
           )).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Write-Host "A second window will ask for Administrator permission - click Yes."
  Write-Host "Setup continues in the NEW window; this one can be closed."
  try {
    # -NoExit: keep the elevated console open so errors stay readable.
    # Policy + profile flags do NOT carry across elevation; re-pass them.
    Start-Process powershell -Verb RunAs -ErrorAction Stop -ArgumentList `
      '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-File',('"{0}"' -f $PSCommandPath)
  } catch {
    Write-Host "Setup needs Administrator rights. Run it again and click Yes on the prompt." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
  }
  exit
}

# Elevated processes start in System32; every relative path depends on this line.
Set-Location -LiteralPath (Split-Path -Parent $PSCommandPath)
Write-Host "== FieldKit Windows setup ==" -ForegroundColor Cyan

function Fail([string]$msg) {
  Write-Host $msg -ForegroundColor Red
  Read-Host "Press Enter to close"
  exit 1
}

# Clear Mark-of-the-Web on everything we shipped (Explorer-extracted zips carry it).
Get-ChildItem -LiteralPath $PSScriptRoot -Recurse -File -Force -ErrorAction SilentlyContinue |
  Unblock-File -ErrorAction SilentlyContinue

# --- python -------------------------------------------------------------------
function Resolve-Python {
  # The Microsoft Store ships a FAKE python.exe under WindowsApps that opens the
  # Store; reject it by path, then trust only an interpreter that actually runs.
  $c = Get-Command python -ErrorAction SilentlyContinue
  if ($c -and $c.Source -notmatch 'WindowsApps') {
    & $c.Source --version *> $null
    if ($LASTEXITCODE -eq 0) { return $c.Source }
  }
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in '-3.12', '-3') {
      $p = & py $v -c "import sys;print(sys.executable)" 2>$null
      if ($LASTEXITCODE -eq 0 -and $p) { return "$p".Trim() }
    }
  }
  return $null
}

$pyExe = Resolve-Python
if (-not $pyExe) {
  Fail ("Python not found. Install Python 3.12 from python.org - tick BOTH " +
        "'Add python.exe to PATH' and 'Install for all users' - then rerun setup. (user: $env:USERNAME)")
}
$pyVer = & $pyExe -c "import sys;print('%d.%d' % sys.version_info[:2])"
if ($pyVer -notin '3.12', '3.13', '3.14') {
  Fail "Python $pyVer found, but the offline bundle carries wheels for Python 3.12-3.14 only. Install one of those from python.org, then rerun setup."
}

# venv: immune to user-site/admin-account mixups, works for whichever account runs the app.
$venvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
  & $pyExe -m venv (Join-Path $PSScriptRoot '.venv')
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail "Could not create the Python environment (.venv) - see errors above." }
}
$wheels = Join-Path $PSScriptRoot 'wheels\win_amd64'
if (-not (Test-Path $wheels)) { Fail "Bundled wheels not found at $wheels - was the whole zip extracted?" }
& $venvPy -m pip install --no-index --find-links "$wheels" -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed - see the pip errors above." }
Write-Host "[ok] dependencies installed into .venv (python $pyVer)"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Host "[!!] ffmpeg is not on PATH - the UI works, but RECORDING will not. See INSTALL.txt." -ForegroundColor Yellow
}

# --- firewall (profile=any: a gatewayless field LAN counts as 'Public') --------
netsh advfirewall firewall delete rule name="FieldKit web"  *> $null
netsh advfirewall firewall delete rule name="FieldKit SADP" *> $null
netsh advfirewall firewall add rule name="FieldKit web"  dir=in action=allow protocol=TCP localport=8080 profile=any | Out-Null
netsh advfirewall firewall add rule name="FieldKit SADP" dir=in action=allow protocol=UDP localport=37020 profile=any | Out-Null
Write-Host "[ok] firewall: 8080/tcp (phone UI) and 37020/udp (camera discovery), all profiles"

# --- camera network -------------------------------------------------------------
$CAM_IP = '192.168.1.2'

function Test-Wireless($adapter) {
  ($adapter.PhysicalMediaType -match '802\.11|Wireless|BlueTooth') -or
  ($adapter.InterfaceDescription -match 'Wireless|Wi-?Fi|WLAN|802\.11|Bluetooth')
}

function Select-FieldAdapter {
  # Physical drops Hyper-V/VPN virtuals; then exclude radios and known fakes.
  # PhysicalMediaType alone is unreliable for USB dongles (some report Unspecified),
  # so wired = physical + Up + not-wireless, never require 802.3.
  $cands = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object {
    $_.Status -eq 'Up' -and -not (Test-Wireless $_) -and
    $_.InterfaceDescription -notmatch 'TAP|VPN|Loopback|VirtualBox|VMware|Hyper-V' })
  if (-not $cands) { return $null }
  # The adapter self-assigned 169.254.x IS the one on the DHCP-less field switch.
  $apipa = @($cands | Where-Object {
    @(Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object { $_.IPAddress -like '169.254.*' }).Count -gt 0 })
  if ($apipa) { return $apipa[0] }
  return $cands[0]
}

# A previous run may have parked the camera address on Wi-Fi; that reaches nothing.
foreach ($addr in @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $CAM_IP -ErrorAction SilentlyContinue)) {
  $ad = Get-NetAdapter -InterfaceIndex $addr.InterfaceIndex -ErrorAction SilentlyContinue
  if ($ad -and (Test-Wireless $ad)) {
    Remove-NetIPAddress -IPAddress $CAM_IP -InterfaceIndex $addr.InterfaceIndex -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[--] removed $CAM_IP from wireless '$($ad.Name)' - cameras are on the cable"
  }
}

$onWired = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $CAM_IP -ErrorAction SilentlyContinue)
if ($onWired) {
  $alias = (Get-NetAdapter -InterfaceIndex $onWired[0].InterfaceIndex -ErrorAction SilentlyContinue).Name
  Write-Host "[ok] already on the camera network as $CAM_IP on '$alias'"
} else {
  $nic = Select-FieldAdapter
  if (-not $nic) {
    Write-Host "[!!] no connected wired adapter found. Is the Ethernet cable (or USB dongle)" -ForegroundColor Yellow
    Write-Host "     plugged into the camera switch? Plug it in, wait 30 seconds, rerun setup." -ForegroundColor Yellow
  } else {
    Write-Host "     using wired adapter '$($nic.Name)' ($($nic.InterfaceDescription))"
    Set-NetIPInterface -InterfaceIndex $nic.ifIndex -Dhcp Disabled -ErrorAction SilentlyContinue
    try {
      New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress $CAM_IP -PrefixLength 24 -ErrorAction Stop | Out-Null
    } catch {
      # set address (not add): atomically switches a DHCP interface to static.
      netsh interface ipv4 set address name="$($nic.Name)" source=static address=$CAM_IP mask=255.255.255.0
    }
    # If Wi-Fi is on a 192.168.1.x home router too, make camera traffic prefer the cable.
    Set-NetIPInterface -InterfaceIndex $nic.ifIndex -InterfaceMetric 5 -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    if (@(Get-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress $CAM_IP -ErrorAction SilentlyContinue)) {
      Write-Host "[ok] added $CAM_IP on '$($nic.Name)' (persists across reboots; same USB port please)"
    } else {
      Write-Host "[!!] setting $CAM_IP on '$($nic.Name)' did not stick - photograph this window for support." -ForegroundColor Yellow
    }
  }
}

# --- go ------------------------------------------------------------------------
Write-Host ""
Write-Host "Setup complete. Starting FieldKit - open http://localhost:8080 (Ctrl+C stops it)." -ForegroundColor Cyan
& $venvPy (Join-Path $PSScriptRoot 'app.py')
Write-Host "FieldKit stopped (exit code $LASTEXITCODE)."
Read-Host "Press Enter to close"
