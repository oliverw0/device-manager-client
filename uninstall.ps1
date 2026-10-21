# Removes the DeviceManager client from this Windows machine (scheduled task +
# program files). Config in ProgramData is kept unless -RemoveConfig is given.
# Leaves OpenSSH Server and the authorized key in place.
#
# Usage (elevated): powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 [-RemoveConfig]
param([switch]$RemoveConfig)

$ErrorActionPreference = 'Stop'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'ERROR: run this from an elevated PowerShell.' -ForegroundColor Red
    exit 1
}

$InstallDir = Join-Path $env:ProgramFiles 'DeviceManagerClient'
$DataDir = Join-Path $env:ProgramData 'DeviceManagerClient'

foreach ($task in 'DeviceManagerClient', 'DeviceManagerClient-WindowsUpdate') {
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $task -Confirm:$false
        Write-Host "Removed scheduled task $task"
    }
}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*client.main*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if (Test-Path $InstallDir) {
    # Leave the shell's working directory first if we're running from inside it.
    Set-Location $env:SystemRoot
    Remove-Item $InstallDir -Recurse -Force
    Write-Host "Removed $InstallDir"
}
if ($RemoveConfig -and (Test-Path $DataDir)) {
    Remove-Item $DataDir -Recurse -Force
    Write-Host "Removed $DataDir"
} elseif (Test-Path $DataDir) {
    Write-Host "Kept config and logs in $DataDir (pass -RemoveConfig to delete)."
}
Write-Host 'Done. (OpenSSH Server and the dashboard key were left in place; remove them manually if desired.)'
