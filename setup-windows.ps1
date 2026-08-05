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
$have = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -like "192.168.1.*" }
if ($have) {
  Write-Host "[ok] already on the camera network as $($have[0].IPAddress)"
} else {
  $ll = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -like "169.254.*" } | Select-Object -First 1
  if ($ll) {
    try {
      New-NetIPAddress -InterfaceAlias $ll.InterfaceAlias -IPAddress 192.168.1.2 -PrefixLength 24 -ErrorAction Stop | Out-Null
      Write-Host "[ok] added 192.168.1.2 on '$($ll.InterfaceAlias)'"
    } catch {
      netsh interface ipv4 add address name="$($ll.InterfaceAlias)" addr=192.168.1.2 mask=255.255.255.0
      Write-Host "[ok] added 192.168.1.2 on '$($ll.InterfaceAlias)' (netsh)"
    }
  } else {
    Write-Host "[!!] no adapter has a 169.254.x address - is the Ethernet cable plugged into the camera switch?" -ForegroundColor Yellow
    Write-Host "     Plug it in, wait 30 seconds, and rerun this script." -ForegroundColor Yellow
  }
}

# 4) Go
Write-Host "`nSetup complete. Starting FieldKit - open http://localhost:8080 (Ctrl+C stops it)." -ForegroundColor Cyan
python app.py
