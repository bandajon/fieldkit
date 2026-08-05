# FieldKit one-shot Windows setup. Run from the unzipped FieldKit folder:
#   powershell -ExecutionPolicy Bypass -File setup-windows.ps1
# Idempotent — safe to run again any time. Relaunches itself as Administrator.

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent(
      )).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "Relaunching as Administrator (accept the prompt)..."
  Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`""
  exit
}
Set-Location $PSScriptRoot
Write-Host "== FieldKit Windows setup ==" -ForegroundColor Cyan

# 1) Python + dependencies (offline, from the bundled wheels)
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  Write-Host "Python not found. Install Python 3.12 from python.org and TICK 'Add to PATH', then rerun." -ForegroundColor Red
  Read-Host "Enter to close"; exit 1
}
python -m pip install --quiet --no-index --find-links wheels\win_amd64 -r requirements.txt
Write-Host "[ok] dependencies installed"

# 2) Firewall: let the phone reach the UI, let camera SADP replies in
netsh advfirewall firewall delete rule name="FieldKit web"  | Out-Null
netsh advfirewall firewall delete rule name="FieldKit SADP" | Out-Null
netsh advfirewall firewall add rule name="FieldKit web"  dir=in action=allow protocol=TCP localport=8080 | Out-Null
netsh advfirewall firewall add rule name="FieldKit SADP" dir=in action=allow protocol=UDP localport=37020 | Out-Null
Write-Host "[ok] firewall rules for ports 8080/tcp and 37020/udp"

# 3) Camera-network address: field switches have no DHCP, so the wired adapter
#    self-assigns 169.254.x — that adapter gets 192.168.1.2 added.
# "Already done" means a camera address on a WIRED adapter. An earlier run of this
# script could have parked one on Wi-Fi; that reaches no cameras, so clean it up.
$cam = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -like "192.168.1.*" }
$onWifi = $cam | Where-Object { (Get-NetAdapter -InterfaceIndex $_.InterfaceIndex
                                 -ErrorAction SilentlyContinue).InterfaceDescription -match "Wireless|Wi-?Fi|802\.11" }
foreach ($bad in $onWifi) {
  Remove-NetIPAddress -IPAddress $bad.IPAddress -InterfaceIndex $bad.InterfaceIndex -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "[--] removed $($bad.IPAddress) from wireless '$($bad.InterfaceAlias)' - cameras are on the cable"
}
$have = $cam | Where-Object { $_.InterfaceIndex -notin ($onWifi | ForEach-Object InterfaceIndex) }
if ($have) {
  Write-Host "[ok] already on the camera network as $($have[0].IPAddress) on '$($have[0].InterfaceAlias)'"
} else {
  # WIRED adapters only: an idle Wi-Fi radio also self-assigns 169.254, and putting
  # the camera address on a radio that isn't on the switch reaches nothing.
  $wired = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object {
    $_.Status -eq "Up" -and $_.InterfaceDescription -notmatch "Wireless|Wi-?Fi|802\.11" }
  $ll = $wired | ForEach-Object {
          Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $_.ifIndex -ErrorAction SilentlyContinue } |
        Where-Object { $_.IPAddress -like "169.254.*" } | Select-Object -First 1
  # No self-assigned wired address? Still prefer a connected wired adapter over Wi-Fi.
  if (-not $ll -and $wired) { $ll = [pscustomobject]@{ InterfaceAlias = $wired[0].Name } }
  if ($ll) {
    try {
      New-NetIPAddress -InterfaceAlias $ll.InterfaceAlias -IPAddress 192.168.1.2 -PrefixLength 24 -ErrorAction Stop | Out-Null
      Write-Host "[ok] added 192.168.1.2 on '$($ll.InterfaceAlias)'"
    } catch {
      netsh interface ipv4 add address name="$($ll.InterfaceAlias)" addr=192.168.1.2 mask=255.255.255.0
      Write-Host "[ok] added 192.168.1.2 on '$($ll.InterfaceAlias)' (netsh)"
    }
  } else {
    Write-Host "[!!] no connected wired adapter found - is the Ethernet cable (or USB dongle)" -ForegroundColor Yellow
    Write-Host "     plugged into the camera switch? Check with: Get-NetAdapter" -ForegroundColor Yellow
    Write-Host "     Plug it in, wait 30 seconds, and rerun this script." -ForegroundColor Yellow
  }
}

# 4) Go
Write-Host "`nSetup complete. Starting FieldKit - open http://localhost:8080 (Ctrl+C stops it)." -ForegroundColor Cyan
python app.py
