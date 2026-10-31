# win_power.ps1: estimate this Windows PC's wall (AC) power draw.
#
# The Windows counterpart of pve_power.py, installed by the dashboard's
# "Power tracking" switch (runs as SYSTEM from a scheduled task). Every few
# seconds it writes the same JSON shape pve_power serves to
#   %ProgramData%\DeviceManagerClient\power.json
# which the client reports with each update.
#
# Model
#   CPU     measured: package power from LibreHardwareMonitor (needs the PawnIO driver), plus VRM loss
#   GPU     measured: board power from nvidia-smi (NVIDIA), else LibreHardwareMonitor's GPU power
#   RAM     per-DIMM idle/active watts, scaled by CPU load
#   Drives  per-drive idle watts by type (NVMe / SATA SSD / HDD)
#   Fixed   motherboard/chipset, fans, USB
#   PSU     AC input interpolated from the chosen PSU's 230V test curve (power-config.json "psu")
param([int]$IntervalSeconds = 5)

$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot
$DataDir = Join-Path $env:ProgramData 'DeviceManagerClient'
$OutFile = Join-Path $DataDir 'power.json'
$StateFile = Join-Path $DataDir 'power-state.json'
$ConfigFile = Join-Path $DataDir 'power-config.json'

# (DC out W, AC in W) at 230V, from each PSU's Cybenetics report.
$PsuCurves = @{
    'corsair-rm750e' = @(  # RM750e (ATX v3.0), CP-9020262, Cybenetics CR75002089
        @(19.989, 26.118), @(39.989, 51.142), @(59.988, 72.023), @(79.936, 92.331), @(149.925, 165.194),
        @(224.923, 243.796), @(300.002, 323.384), @(374.383, 403.482), @(449.213, 484.983),
        @(524.184, 567.759), @(599.286, 651.61), @(674.349, 736.241), @(749.557, 823.334))
    'corsair-rm650e' = @(  # RM650e (ATX 3.1), Cybenetics CR65002628
        @(20.0, 28.05), @(40.0, 48.57), @(60.0, 69.25), @(80.0, 89.99), @(130.0, 142.76), @(195.0, 211.08),
        @(260.0, 280.19), @(325.1, 350.24), @(389.3, 420.23), @(454.8, 492.31), @(519.6, 564.73), @(585.0, 638.50))
    # Generic 80 PLUS curves for a ~650 W unit at 230V (approximate)
    'generic-bronze'   = @(@(20, 29.0), @(65, 80.2), @(130, 151.2), @(325, 371.4), @(650, 755.8))
    'generic-gold'     = @(@(20, 27.0), @(65, 76.5), @(130, 145.3), @(325, 352.4), @(650, 714.3))
    'generic-platinum' = @(@(20, 25.6), @(65, 73.9), @(130, 141.3), @(325, 345.4), @(650, 699.0))
}

$Config = @{ psu = 'generic-gold' }
if (Test-Path $ConfigFile) {
    try { (Get-Content $ConfigFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $Config[$_.Name] = $_.Value } } catch {}
}
$Curve = $PsuCurves[$Config.psu]
if (-not $Curve) { $Config.psu = 'generic-gold'; $Curve = $PsuCurves['generic-gold'] }

$VrmEfficiency = 0.88
$DimmIdleW = 1.2; $DimmActiveW = 3.0          # DDR5 per DIMM
$DriveW = @{ NVMe = 1.5; SSD = 0.6; HDD = 5.0 }
$Fixed = [ordered]@{ motherboard = 10.0; fans = 4.5; usb_extras = 2.0 }

function PsuAc([double]$dc) {
    $p = $Curve
    if ($dc -le $p[0][0]) { return $dc + ($p[0][1] - $p[0][0]) }
    for ($i = 1; $i -lt $p.Count; $i++) {
        if ($dc -le $p[$i][0]) {
            $l0 = $p[$i - 1][1] - $p[$i - 1][0]; $l1 = $p[$i][1] - $p[$i][0]
            return $dc + $l0 + ($l1 - $l0) * ($dc - $p[$i - 1][0]) / ($p[$i][0] - $p[$i - 1][0])
        }
    }
    $last = $p[$p.Count - 1]
    return $dc * $last[1] / $last[0]
}

# --- hardware access ----------------------------------------------------------
Add-Type -Path (Join-Path $Here 'lhm\LibreHardwareMonitorLib.dll')
$Computer = New-Object LibreHardwareMonitor.Hardware.Computer
$Computer.IsCpuEnabled = $true
$Computer.IsGpuEnabled = $true
$Computer.Open()

$NvidiaSmi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
if (-not $NvidiaSmi -and (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe")) { $NvidiaSmi = "$env:SystemRoot\System32\nvidia-smi.exe" }

$Dimms = @(Get-CimInstance Win32_PhysicalMemory).Count
$Drives = foreach ($d in (Get-PhysicalDisk)) {
    $kind = if ("$($d.BusType)" -eq 'NVMe') { 'NVMe' } elseif ("$($d.MediaType)" -eq 'HDD') { 'HDD' } else { 'SSD' }
    [pscustomobject]@{ name = $d.FriendlyName; kind = $kind; w = $DriveW[$kind] }
}

function Read-Sensors {
    $cpu = $null; $gpu = $null; $load = $null
    foreach ($hw in $Computer.Hardware) {
        $hw.Update()
        foreach ($s in $hw.Sensors) {
            if ($null -eq $s.Value) { continue }
            $t = "$($hw.HardwareType)"
            if ($t -eq 'Cpu' -and "$($s.SensorType)" -eq 'Power' -and $s.Name -match 'Package') { $cpu = [double]$s.Value }
            if ($t -eq 'Cpu' -and "$($s.SensorType)" -eq 'Load' -and $s.Name -eq 'CPU Total') { $load = [double]$s.Value / 100 }
            if ($t -match '^Gpu' -and "$($s.SensorType)" -eq 'Power' -and $s.Name -match 'Package|Total|Board') {
                if ($null -eq $gpu -or [double]$s.Value -gt $gpu) { $gpu = [double]$s.Value }
            }
        }
    }
    if ($NvidiaSmi) {  # NVIDIA's own board power reading beats LHM's when available
        try {
            $nv = (& $NvidiaSmi --query-gpu=power.draw --format=csv,noheader,nounits 2>$null) -as [string[]]
            $sum = ($nv | Where-Object { $_ -match '^\s*[\d.]+\s*$' } | ForEach-Object { [double]$_ } | Measure-Object -Sum).Sum
            if ($sum -gt 0) { $gpu = $sum }
        } catch {}
    }
    [pscustomobject]@{ cpu = $cpu; gpu = $gpu; load = $(if ($null -ne $load) { $load } else { 0.1 }) }
}

# --- energy total survives restarts -------------------------------------------
$EnergyWh = 0.0
if (Test-Path $StateFile) { try { $EnergyWh = [double](Get-Content $StateFile -Raw | ConvertFrom-Json).energy_wh } catch {} }
$LastSave = Get-Date
$Last = Get-Date

while ($true) {
    Start-Sleep -Seconds $IntervalSeconds
    try {
        $now = Get-Date
        $dt = ($now - $Last).TotalSeconds; $Last = $now
        $r = Read-Sensors

        $comps = [ordered]@{}
        $cpuPkg = if ($null -ne $r.cpu) { $r.cpu } else { 15.0 }   # no reading (PawnIO missing): rough idle guess
        $comps.cpu_package = $cpuPkg
        $comps.vrm_loss = $cpuPkg / $VrmEfficiency - $cpuPkg
        if ($null -ne $r.gpu) { $comps.gpu = $r.gpu }
        $comps.ram = $Dimms * ($DimmIdleW + ($DimmActiveW - $DimmIdleW) * $r.load)
        foreach ($d in $Drives) { $comps["drive_$($d.kind.ToLower())_$($d.name -replace '\W', '')"] = $d.w }
        foreach ($k in $Fixed.Keys) { $comps[$k] = $Fixed[$k] }

        $dc = ($comps.Values | Measure-Object -Sum).Sum
        $ac = PsuAc $dc
        $comps.psu_loss = $ac - $dc
        $EnergyWh += $ac * $dt / 3600
        if (($now - $LastSave).TotalSeconds -ge 60) {
            @{ energy_wh = $EnergyWh } | ConvertTo-Json | Set-Content -Path $StateFile -Encoding ASCII
            $LastSave = $now
        }

        $round = [ordered]@{}
        foreach ($k in $comps.Keys) { $round[$k] = [math]::Round($comps[$k], 2) }
        $result = [ordered]@{
            timestamp      = $now.ToString('o')
            wall_w         = [math]::Round($ac, 1)
            dc_load_w      = [math]::Round($dc, 1)
            psu_efficiency = [math]::Round($dc / $ac, 3)
            psu            = $Config.psu
            cpu_measured   = ($null -ne $r.cpu)
            cpu_util       = [math]::Round($r.load, 3)
            energy_kwh     = [math]::Round($EnergyWh / 1000, 4)
            components     = $round
            guests         = @()
        }
        $tmp = "$OutFile.tmp"
        $result | ConvertTo-Json -Depth 4 -Compress | Set-Content -Path $tmp -Encoding UTF8
        Move-Item -Path $tmp -Destination $OutFile -Force
    } catch {
        Write-Warning "sample failed: $($_.Exception.Message)"
    }
}
