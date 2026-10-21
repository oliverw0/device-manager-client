# Install pending Windows updates, streaming progress for the dashboard's
# "Update packages" button (run over SSH as an administrator).
#
# The Windows Update API refuses to download/install from a remote (SSH/WinRM)
# logon, so this registers a one-off scheduled task that runs the -Worker half
# as SYSTEM, then tails its log. If the SSH session drops, the install carries
# on. Never reboots; it reports when a reboot is needed.
#
# Output protocol (parsed by the host):
#   PROGRESS:<0-100>:<stage text>     NOTE:<text shown when done>     RESULT:<exit code>
param([switch]$Worker)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$DataDir = Join-Path $env:ProgramData 'DeviceManagerClient'
$Log = Join-Path $DataDir 'windows-update.log'
$TaskName = 'DeviceManagerClient-WindowsUpdate'

function Out-Log($line) {
    # The launcher may be reading the file at this instant; retry briefly.
    for ($try = 0; $try -lt 10; $try++) {
        try { Add-Content -Path $Log -Value $line -Encoding ASCII; return } catch { Start-Sleep -Milliseconds 200 }
    }
}

if ($Worker) {
    try {
        $session = New-Object -ComObject Microsoft.Update.Session
        $session.ClientApplicationID = 'DeviceManager'
        Out-Log 'PROGRESS:2:Searching for updates...'
        $found = $session.CreateUpdateSearcher().Search("IsInstalled=0 and IsHidden=0 and Type='Software'").Updates
        $todo = @()
        foreach ($u in $found) {
            if (-not $u.EulaAccepted) { $u.AcceptEula() }
            $todo += $u
        }
        if ($todo.Count -eq 0) {
            Out-Log 'NOTE:No updates available'
            Out-Log 'RESULT:0'
            exit 0
        }
        Out-Log "PROGRESS:5:Found $($todo.Count) update(s)"

        # One at a time, so progress is real rather than one long silent call.
        # Download fills 5-40%, install 40-99%.
        $n = $todo.Count
        for ($i = 0; $i -lt $n; $i++) {
            $u = $todo[$i]
            if ($u.IsDownloaded) { continue }
            Out-Log ("PROGRESS:{0}:Downloading {1}/{2}: {3}" -f [int](5 + 35 * $i / $n), ($i + 1), $n, $u.Title)
            $coll = New-Object -ComObject Microsoft.Update.UpdateColl
            [void]$coll.Add($u)
            $dl = $session.CreateUpdateDownloader()
            $dl.Updates = $coll
            [void]$dl.Download()
        }

        $failed = @()
        $reboot = $false
        for ($i = 0; $i -lt $n; $i++) {
            $u = $todo[$i]
            Out-Log ("PROGRESS:{0}:Installing {1}/{2}: {3}" -f [int](40 + 59 * $i / $n), ($i + 1), $n, $u.Title)
            if (-not $u.IsDownloaded) { $failed += "$($u.Title) (download failed)"; continue }
            $coll = New-Object -ComObject Microsoft.Update.UpdateColl
            [void]$coll.Add($u)
            $inst = $session.CreateUpdateInstaller()
            $inst.Updates = $coll
            $r = $inst.Install()
            # ResultCode: 2 succeeded, 3 succeeded with errors, 4 failed, 5 aborted
            if ($r.ResultCode -ne 2 -and $r.ResultCode -ne 3) {
                $failed += ("{0} (0x{1:X8})" -f $u.Title, $r.HResult)
            }
            if ($r.RebootRequired) { $reboot = $true }
        }

        $ok = $n - $failed.Count
        $note = "Installed $ok of $n update(s)"
        if ($reboot) { $note += ' - reboot required to finish' }
        Out-Log "NOTE:$note"
        foreach ($f in $failed) { Out-Log "Failed: $f" }
        if ($failed.Count -gt 0) { Out-Log 'RESULT:1'; exit 1 }
        Out-Log 'RESULT:0'
        exit 0
    } catch {
        Out-Log "Error: $($_.Exception.Message)"
        Out-Log 'RESULT:1'
        exit 1
    }
}

# --- launcher: run the worker as SYSTEM and stream its log --------------------
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq 'Running') {
    Write-Output 'PROGRESS:1:Attaching to an update already in progress...'
} else {
    Set-Content -Path $Log -Value '' -Encoding ASCII
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Worker"
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
}

$printed = 0
$result = $null
$started = Get-Date
$stoppedSeen = $false
while ((Get-Date) -lt $started.AddHours(4)) {
    Start-Sleep -Seconds 1
    $lines = @()
    try { $lines = @(Get-Content $Log -ErrorAction Stop | Where-Object { $_ -ne '' }) } catch { continue }  # mid-write; retry
    for ($i = $printed; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^RESULT:(\d+)') { $result = [int]$Matches[1] } else { Write-Output $lines[$i] }
    }
    $printed = $lines.Count
    if ($null -ne $result) { break }
    $state = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State
    if ($state -eq 'Running' -or $state -eq 'Queued') { continue }
    if ($printed -eq 0 -and ((Get-Date) - $started).TotalSeconds -lt 30) { continue }  # not started yet
    # Stopped: read the log once more (it may have finished just after our read).
    if ($stoppedSeen) {
        Write-Output 'Error: the update task stopped without reporting a result.'
        $result = 1
        break
    }
    $stoppedSeen = $true
}
if ($null -eq $result) { Write-Output 'Error: timed out waiting for Windows Update.'; $result = 1 }
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
exit $result
