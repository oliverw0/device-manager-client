# win_power_install.ps1: set up Windows power tracking (run by the dashboard's
# Power switch over SSH, as an administrator; downloaded fresh each time so the
# command it sends stays short: cmd.exe caps command lines at 8191 characters).
#
#   1. bare metal only (a VM can't measure the wall)
#   2. LibreHardwareMonitor's library + the signed PawnIO driver (CPU power)
#   3. win_power.ps1 as the SYSTEM scheduled task DeviceManagerPower, with the
#      chosen PSU curve in %ProgramData%\DeviceManagerClient\power-config.json
param([string]$Psu = 'generic-gold')

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 'Tls12'
$cs = Get-CimInstance Win32_ComputerSystem
if ("$($cs.Manufacturer) $($cs.Model)" -match 'Virtual|VMware|VirtualBox|QEMU|KVM|Xen|Parallels') {
  Write-Output "This is a virtual machine ($($cs.Model)), not bare metal, so it can't measure wall power. Turn power tracking on for the physical host instead."
  exit 3
}
$dir = Join-Path $env:ProgramFiles 'DeviceManagerClient\power'
$data = Join-Path $env:ProgramData 'DeviceManagerClient'
New-Item -ItemType Directory -Force -Path $dir, $data | Out-Null
$tmp = Join-Path $env:TEMP ('dm-power-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  if (-not (Test-Path "$dir\lhm\LibreHardwareMonitorLib.dll")) {
    Write-Output 'Downloading LibreHardwareMonitor ...'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip' -OutFile "$tmp\lhm.zip"
    Expand-Archive -Path "$tmp\lhm.zip" -DestinationPath "$dir\lhm" -Force
  }
  if (-not (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO' -ErrorAction SilentlyContinue)) {
    Write-Output 'Installing the PawnIO driver ...'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/namazso/PawnIO.Setup/releases/download/2.2.0/PawnIO_setup.exe' -OutFile "$tmp\PawnIO_setup.exe"
    $p = Start-Process -FilePath "$tmp\PawnIO_setup.exe" -ArgumentList '-install', '-silent' -Wait -PassThru
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { Write-Output "The PawnIO driver didn't install (exit $($p.ExitCode))."; exit 1 }
  }
  Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/oliverw0/device-manager-client/main/power/win_power.ps1' -OutFile "$dir\win_power.ps1"
} finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
@{ psu = $Psu } | ConvertTo-Json | Set-Content -Path "$data\power-config.json" -Encoding ASCII
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$dir\win_power.ps1`""
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Stop-ScheduledTask -TaskName 'DeviceManagerPower' -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName 'DeviceManagerPower' -Description 'DeviceManager power estimate' -Action $action `
  -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $principal -Settings $settings -Force | Out-Null
$out = "$data\power.json"
Remove-Item $out -Force -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName 'DeviceManagerPower'
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Seconds 1
  if (Test-Path $out) {
    $j = Get-Content $out -Raw | ConvertFrom-Json
    $note = if (-not $j.cpu_measured) { ' (CPU power not readable yet; a restart may be needed to load the PawnIO driver)' } else { '' }
    Write-Output "Power tracking is running: $($j.wall_w) W$note"
    exit 0
  }
}
Write-Output 'Installed and started, but no reading yet.'
exit 1
