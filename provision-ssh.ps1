# Authorize the DeviceManager dashboard's SSH key on THIS Windows machine, so
# the in-browser PowerShell console, container controls and remote updates
# work. Safe to re-run (won't duplicate the key).
#
# Installs/enables the built-in OpenSSH Server if needed and opens port 22.
#
# Usage (elevated PowerShell):
#   powershell -ExecutionPolicy Bypass -File .\provision-ssh.ps1                      # interactive
#   powershell -ExecutionPolicy Bypass -File .\provision-ssh.ps1 -HostUrl http://host:8000 -User alice
param(
    [string]$HostUrl = $env:HOST_URL,
    [string]$User = $env:SSH_USER
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail 'Run this from an elevated PowerShell (Run as Administrator).'
}

# Fall back to the host URL an installed client already uses.
$EnvFile = Join-Path $env:ProgramData 'DeviceManagerClient\client.env'
if (-not $HostUrl -and (Test-Path $EnvFile)) {
    foreach ($line in Get-Content $EnvFile) { if ($line -match '^HOST_URL=(.*)$') { $HostUrl = $Matches[1].Trim() } }
}
if (-not $HostUrl) { $HostUrl = Read-Host 'DeviceManager host URL (e.g. http://192.168.50.225:8000)' }
$HostUrl = $HostUrl.Trim().TrimEnd('/')
if ($HostUrl -notmatch '^https?://') { $HostUrl = "http://$HostUrl" }

if (-not $User) {
    $User = Read-Host "Local account to authorize [$env:USERNAME]"
    if (-not $User) { $User = $env:USERNAME }
}

# --- resolve the account ------------------------------------------------------
$sid = $null
try { $sid = (New-Object Security.Principal.NTAccount($User)).Translate([Security.Principal.SecurityIdentifier]).Value } catch {}
if (-not $sid) { Fail "Account '$User' does not exist on this machine." }
$shortName = $User.Split([char]92)[-1].Split('@')[0]

# Members of the local Administrators group (by SID: the name is localized).
$adminGroup = (New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-544').Translate([Security.Principal.NTAccount]).Value.Split([char]92)[-1]
$isAdmin = $false
try {
    $members = ([ADSI]"WinNT://$env:COMPUTERNAME/$adminGroup,group").psbase.Invoke('Members') |
        ForEach-Object { $_.GetType().InvokeMember('Name', 'GetProperty', $null, $_, $null) }
    $isAdmin = @($members | Where-Object { $_ -eq $shortName }).Count -gt 0
} catch {}

# --- OpenSSH Server -----------------------------------------------------------
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
    Write-Host 'Installing the OpenSSH Server Windows feature (this can take a few minutes) ...'
    try {
        $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
        if (-not $cap) { throw 'capability not offered by this Windows build' }
        Add-WindowsCapability -Online -Name $cap.Name | Out-Null
    } catch {
        Fail ("Couldn't install OpenSSH Server ($($_.Exception.Message)). " +
              'Install it via Settings > Optional features (or Server Manager), or the MSI from ' +
              'https://github.com/PowerShell/Win32-OpenSSH/releases, then re-run this script.')
    }
}
Set-Service sshd -StartupType Automatic
Start-Service sshd

$rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if ($rule) {
    $rule | Enable-NetFirewallRule
} elseif (-not (Get-NetFirewallRule -Name 'DeviceManager-sshd' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'DeviceManager-sshd' -DisplayName 'OpenSSH Server (sshd) - DeviceManager' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
Write-Host 'OpenSSH Server is running and allowed through the firewall.'

# --- fetch and install the key -----------------------------------------------
Write-Host "Fetching the dashboard's public key from $HostUrl/api/v1/ssh-pubkey ..."
try {
    $pubkey = "$(Invoke-RestMethod -UseBasicParsing -Uri "$HostUrl/api/v1/ssh-pubkey")".Trim()
} catch {
    Fail "Could not fetch the host public key from $HostUrl ($($_.Exception.Message)). Check the URL is reachable from this machine."
}
if ($pubkey -notmatch '^ssh-') { Fail "Unexpected response from $HostUrl/api/v1/ssh-pubkey" }

# Windows OpenSSH's default sshd_config reads admins' keys ONLY from
# administrators_authorized_keys (the "Match Group administrators" block), and
# refuses key files other accounts can write to.
$sshdConfig = Join-Path $env:ProgramData 'ssh\sshd_config'
$adminFileInUse = $isAdmin -and (Test-Path $sshdConfig) -and
    ((Get-Content $sshdConfig) -match '^\s*Match\s+Group\s+administrators')

if ($adminFileInUse) {
    $keyFile = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
    $acl = @('*S-1-5-32-544:F', '*S-1-5-18:F')
} else {
    $profilePath = (Get-CimInstance Win32_UserProfile -Filter "SID='$sid'" -ErrorAction SilentlyContinue).LocalPath
    if (-not $profilePath) {
        $profilePath = Join-Path (Split-Path $env:PUBLIC) $shortName
        Write-Warning "No profile for '$User' yet (never logged in?); using $profilePath."
    }
    $sshDir = Join-Path $profilePath '.ssh'
    New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
    $keyFile = Join-Path $sshDir 'authorized_keys'
    $acl = @("*${sid}:F", '*S-1-5-32-544:F', '*S-1-5-18:F')
}

$existing = if (Test-Path $keyFile) { Get-Content $keyFile } else { @() }
if (@($existing | Where-Object { $_.Trim() -eq $pubkey }).Count -gt 0) {
    Write-Host "Key already authorized in $keyFile"
} else {
    # ASCII, not PowerShell 5.1's default UTF-16 (which sshd can't read).
    $prefix = ''
    if ((Test-Path $keyFile) -and (Get-Item $keyFile).Length -gt 0) {
        $raw = [IO.File]::ReadAllText($keyFile)
        if (-not $raw.EndsWith("`n")) { $prefix = "`r`n" }
    }
    [IO.File]::AppendAllText($keyFile, "$prefix$pubkey`r`n", [Text.Encoding]::ASCII)
    Write-Host "Added dashboard key to $keyFile"
}
& icacls $keyFile /inheritance:r /grant:r @acl | Out-Null

Write-Host ''
Write-Host "Done. '$User' on this machine now authorizes the DeviceManager host."
if (-not $isAdmin) {
    Write-Warning "'$User' is not a local administrator: the console works, but package and client updates from the dashboard need an admin account."
}
Write-Host 'Enable SSH for this device on its dashboard page, then click the terminal icon.'
exit 0
