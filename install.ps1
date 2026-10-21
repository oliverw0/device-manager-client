# DeviceManager client - Windows install (desktop and Server, PowerShell 5.1+).
#
# Installs Python if needed, a venv in "C:\Program Files\DeviceManagerClient",
# config in "C:\ProgramData\DeviceManagerClient\client.env", and a scheduled
# task that runs the client as SYSTEM at boot (and restarts it if it dies).
# Optionally authorizes the dashboard's SSH key for the in-browser console.
#
# Run in an elevated PowerShell. From a checkout:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -HostUrl http://host:8000 -ApiKey xxx
# Or straight from GitHub (the dashboard's device page has this prefilled):
#   $env:HOST_URL='http://host:8000'; $env:API_KEY='xxx'
#   irm https://raw.githubusercontent.com/oliverw0/device-manager-client/main/install.ps1 | iex
#
# Non-interactive extras: -EnableSsh y -SshUser alice  (or ENABLE_SSH / SSH_USER env vars)
# -Update: refresh code + dependencies, keep config, restart. The dashboard's
#          "Update client" button runs this over SSH.
param(
    [string]$HostUrl = $env:HOST_URL,
    [string]$ApiKey = $env:API_KEY,
    [string]$EnableSsh = $env:ENABLE_SSH,
    [string]$SshUser = $env:SSH_USER,
    [int]$ReportInterval = 60,
    [string]$Source = $env:DM_SOURCE,
    [switch]$Update
)

function Install-DeviceManagerClient {
    param($HostUrl, $ApiKey, $EnableSsh, $SshUser, $ReportInterval, $Source, $Update, $ScriptRoot)

    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is 10x slower with the progress bar
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $RepoUrl = if ($env:DM_REPO) { $env:DM_REPO.TrimEnd('/') } else { 'https://github.com/oliverw0/device-manager-client' }
    $Branch = if ($env:DM_BRANCH) { $env:DM_BRANCH } else { 'main' }
    $PythonVersion = '3.12.10'

    $InstallDir = Join-Path $env:ProgramFiles 'DeviceManagerClient'
    $DataDir = Join-Path $env:ProgramData 'DeviceManagerClient'
    $EnvFile = Join-Path $DataDir 'client.env'
    $SourceFile = Join-Path $DataDir 'source.txt'
    $LogFile = Join-Path $DataDir 'logs\client.log'
    $TaskName = 'DeviceManagerClient'
    $VenvPy = Join-Path $InstallDir 'venv\Scripts\python.exe'

    # The dashboard parses STAGE: lines from -Update for its progress bar.
    function Stage($name) { if ($Update) { Write-Output "STAGE:$name" } }
    function Info($msg) { Write-Host $msg }
    # Run a native tool, showing its output. With ErrorActionPreference=Stop,
    # PowerShell 5.1 can turn a tool's stderr chatter (git's "From github.com",
    # pip warnings) into a terminating error when output is redirected, as it is
    # over SSH; success is judged by $LASTEXITCODE instead.
    function Invoke-Native([scriptblock]$cmd) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $cmd 2>&1 | ForEach-Object { Info "$_" } } finally { $ErrorActionPreference = $prev }
    }

    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this from an elevated PowerShell (Run as Administrator).'
    }

    New-Item -ItemType Directory -Force -Path $DataDir, (Split-Path $LogFile) | Out-Null

    # --- 1. get the code -----------------------------------------------------
    Stage 'git'
    $tmp = $null
    $src = $null
    if ($Source) {
        $src = $Source
    } elseif ($ScriptRoot -and (Test-Path (Join-Path $ScriptRoot 'client\main.py')) -and
              ((Resolve-Path $ScriptRoot).Path -ne (Resolve-Path $InstallDir -ErrorAction SilentlyContinue).Path)) {
        $src = $ScriptRoot   # running from a checkout
    } elseif (Test-Path $SourceFile) {
        $recorded = "$(Get-Content $SourceFile -Raw)".Trim()
        if ($recorded -and (Test-Path (Join-Path $recorded 'client\main.py'))) { $src = $recorded }
    }

    # -Update runs unattended (the dashboard starts it over SSH), so a recorded
    # checkout is only used if git can pull it without prompting; anything else
    # (no git, plain folder copy, pull failed) falls back to the GitHub zip so an
    # update never just re-copies the same old code.
    if ($src -and $Update -and -not $Source) {
        $pulled = $false
        if ((Test-Path (Join-Path $src '.git')) -and (Get-Command git -ErrorAction SilentlyContinue)) {
            Info "Pulling latest code in $src ..."
            $env:GIT_TERMINAL_PROMPT = '0'                    # no username/password prompt
            $env:GIT_SSH_COMMAND = 'ssh -o BatchMode=yes'     # no passphrase / host-key prompt
            # safe.directory: the checkout is usually owned by a different account than the one updating.
            Invoke-Native { git -c "safe.directory=*" -C $src pull --ff-only }
            $pulled = $LASTEXITCODE -eq 0
        }
        if (-not $pulled) {
            Info "Couldn't update $src in place; downloading the latest release instead."
            $src = $null
        }
    }

    if (-not $src) {
        $zipUrl = "$RepoUrl/archive/refs/heads/$Branch.zip"
        Info "Downloading $zipUrl ..."
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ("dmclient-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $tmp | Out-Null
        $zip = Join-Path $tmp 'src.zip'
        Invoke-WebRequest -UseBasicParsing -Uri $zipUrl -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $tmp
        $src = (Get-ChildItem $tmp -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'client\main.py') } | Select-Object -First 1).FullName
        if (-not $src) { throw "Downloaded archive doesn't contain the client." }
        Set-Content -Path $SourceFile -Value '' -Encoding ASCII
    } else {
        Set-Content -Path $SourceFile -Value (Resolve-Path $src).Path -Encoding ASCII
    }

    # --- 2. Python -----------------------------------------------------------
    function Get-PyVersion($exe) {
        try {
            $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) { return [version]$v.Trim() }
        } catch {}
        return $null
    }

    # All-users installs only: the task runs as SYSTEM, which can't use a
    # per-user or Microsoft Store Python.
    function Find-Python {
        $candidates = @(Join-Path $env:ProgramFiles 'Python312\python.exe')
        foreach ($root in 'HKLM:\SOFTWARE\Python\PythonCore', 'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore') {
            foreach ($key in (Get-ChildItem $root -ErrorAction SilentlyContinue)) {
                $ip = Get-ItemProperty (Join-Path $key.PSPath 'InstallPath') -ErrorAction SilentlyContinue
                if (-not $ip) { continue }
                if ($ip.ExecutablePath) { $candidates += $ip.ExecutablePath }
                elseif ($ip.'(default)') { $candidates += (Join-Path $ip.'(default)' 'python.exe') }
            }
        }
        foreach ($exe in $candidates) {
            if (-not (Test-Path $exe)) { continue }
            $v = Get-PyVersion $exe
            if ($v -and $v -ge [version]'3.10') { return $exe }
        }
        return $null
    }

    $python = Find-Python
    if (-not $python) {
        $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
        $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-$arch.exe"
        $exe = Join-Path ([IO.Path]::GetTempPath()) "python-$PythonVersion-$arch.exe"
        Info "No suitable Python found. Downloading Python $PythonVersion ..."
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $exe
        $sig = Get-AuthenticodeSignature $exe
        if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
            Remove-Item $exe -Force
            throw "Python installer signature check failed ($($sig.Status)). Not running it."
        }
        Info 'Installing Python (all users) ...'
        $target = Join-Path $env:ProgramFiles 'Python312'
        $p = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList @(
            '/quiet', 'InstallAllUsers=1', 'PrependPath=0', 'Include_launcher=0',
            'Include_test=0', 'Include_doc=0', 'Include_tcltk=0', "TargetDir=`"$target`"")
        Remove-Item $exe -Force -ErrorAction SilentlyContinue
        if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw "Python installer failed (exit $($p.ExitCode))." }
        $python = Find-Python
        if (-not $python) { throw 'Python installed but could not be found afterwards.' }
    }
    Info "Using Python: $python"

    # --- 3. stop a running client before touching its files ------------------
    Stage 'deps'
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    # Make sure no client python.exe survives: it holds DLLs pip needs to replace.
    # (A venv's python.exe is a launcher for the base interpreter, so match on
    # the command line rather than the executable path.)
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*client.main*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    # --- 4. copy code + venv -------------------------------------------------
    Info "Installing to $InstallDir ..."
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    if (Test-Path (Join-Path $InstallDir 'client')) { Remove-Item (Join-Path $InstallDir 'client') -Recurse -Force }
    Copy-Item (Join-Path $src 'client') $InstallDir -Recurse
    foreach ($f in 'requirements.txt', 'install.ps1', 'provision-ssh.ps1', 'uninstall.ps1', 'windows-update.ps1') {
        if (Test-Path (Join-Path $src $f)) { Copy-Item (Join-Path $src $f) $InstallDir -Force }
    }
    if ($tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }

    # Rebuild the venv if it's missing or was made from a Python that's gone.
    if (-not (Test-Path $VenvPy) -or -not (Get-PyVersion $VenvPy)) {
        if (Test-Path (Join-Path $InstallDir 'venv')) { Remove-Item (Join-Path $InstallDir 'venv') -Recurse -Force }
        Invoke-Native { & $python -m venv (Join-Path $InstallDir 'venv') }
        if ($LASTEXITCODE -ne 0) { throw 'Creating the virtualenv failed.' }
    }
    Invoke-Native { & $VenvPy -m pip install --disable-pip-version-check --no-cache-dir --quiet --upgrade pip }
    Invoke-Native { & $VenvPy -m pip install --disable-pip-version-check --no-cache-dir --quiet -r (Join-Path $InstallDir 'requirements.txt') }
    if ($LASTEXITCODE -ne 0) { throw 'Installing Python dependencies failed.' }

    # --- 5. config -----------------------------------------------------------
    function Set-EnvValue($key, $value) {
        $lines = @()
        if (Test-Path $EnvFile) { $lines = @(Get-Content $EnvFile | Where-Object { $_ -notmatch "^$key=" }) }
        $lines += "$key=$value"
        [IO.File]::WriteAllText($EnvFile, (($lines -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding $false))
    }

    if ((Test-Path $EnvFile) -and -not $HostUrl -and -not $ApiKey) {
        Info "$EnvFile already exists - keeping it (delete it to reconfigure)."
        foreach ($line in Get-Content $EnvFile) {
            if ($line -match '^HOST_URL=(.*)$') { $HostUrl = $Matches[1].Trim() }
            if ($line -match '^API_KEY=(.*)$') { $ApiKey = $Matches[1].Trim() }
        }
    } elseif ($Update) {
        throw "No config at $EnvFile; run install.ps1 without -Update first."
    } else {
        if (-not $HostUrl) {
            Info 'Enter the DeviceManager host URL, including the port. e.g. http://192.168.50.225:8000'
            $HostUrl = Read-Host 'Host URL'
        }
        if (-not $ApiKey) { $ApiKey = Read-Host 'API key for this device (from the dashboard)' }
        if (-not $HostUrl -or -not $ApiKey) { throw 'HOST_URL and API_KEY are both required.' }
        $HostUrl = $HostUrl.Trim().TrimEnd('/')
        if ($HostUrl -notmatch '^https?://') { $HostUrl = "http://$HostUrl" }

        $config = @(
            "HOST_URL=$HostUrl", "API_KEY=$($ApiKey.Trim())", "REPORT_INTERVAL=$ReportInterval",
            'SSH_LOG_WINDOW_MINUTES=15', 'REQUEST_TIMEOUT_SECONDS=10', "LOG_FILE=$LogFile"
        )
        [IO.File]::WriteAllText($EnvFile, (($config -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding $false))
        Info "Wrote $EnvFile"
    }
    # The API key is a secret: Administrators + SYSTEM only.
    & icacls $EnvFile /inheritance:r /grant:r '*S-1-5-32-544:F' '*S-1-5-18:F' | Out-Null

    # --- 6. optional SSH console --------------------------------------------
    if (-not $Update) {
        if (-not $EnableSsh) {
            $EnableSsh = Read-Host "Authorize the dashboard's SSH key on this machine (enables the in-browser PowerShell console and remote updates)? [y/N]"
        }
        if ($EnableSsh -match '^[Yy]') {
            if (-not $SshUser) {
                $SshUser = Read-Host "Local account to authorize [$env:USERNAME]"
                if (-not $SshUser) { $SshUser = $env:USERNAME }
            }
            # Child process with Bypass: under `irm | iex` the machine's execution
            # policy (Restricted on desktops) would refuse to run a .ps1 file.
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'provision-ssh.ps1') -HostUrl $HostUrl -User $SshUser
            if ($LASTEXITCODE -eq 0) {
                Set-EnvValue 'SSH_USER' $SshUser   # reported to the host as the account for updates
            } else {
                Write-Warning "SSH provisioning failed; re-run later: powershell -ExecutionPolicy Bypass -File '$InstallDir\provision-ssh.ps1'"
            }
        }
    }

    # --- 7. connectivity test (fresh installs only; -Update is unattended) ---
    if (-not $Update) {
        Info ''
        Info "Running a one-shot connectivity test to $HostUrl ..."
        Push-Location $InstallDir
        try { & $VenvPy -m client.main --env-file $EnvFile --once } finally { Pop-Location }
        if ($LASTEXITCODE -eq 0) {
            Info '  connectivity test PASSED'
        } else {
            Write-Warning "Connectivity test FAILED (see above). The task is installed anyway and will keep retrying. Fix $EnvFile, then: Restart-ScheduledTask $TaskName"
        }
    }

    # --- 8. scheduled task ---------------------------------------------------
    Stage 'restart'
    $action = New-ScheduledTaskAction -Execute $VenvPy -Argument "-m client.main --env-file `"$EnvFile`"" -WorkingDirectory $InstallDir
    # Boot trigger, plus a 5-minute repeat as a watchdog: if the client is
    # running the repeat is ignored (IgnoreNew); if it died, it's restarted.
    $triggers = @(
        (New-ScheduledTaskTrigger -AtStartup),
        (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::FromDays(3650)))
    )
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Description 'DeviceManager monitoring client' `
        -Action $action -Trigger $triggers -Principal $taskPrincipal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Stage 'done'

    Info ''
    Info "Installed and started (scheduled task '$TaskName', runs as SYSTEM)."
    Info "  Status:  Get-ScheduledTask $TaskName | Select-Object State"
    Info "  Logs:    Get-Content '$LogFile' -Tail 50 -Wait"
    Info "  Restart: Stop-ScheduledTask $TaskName; Start-ScheduledTask $TaskName"
    Info "  Re-test: cd '$InstallDir'; .\venv\Scripts\python.exe -m client.main --env-file '$EnvFile' --once"
}

try {
    Install-DeviceManagerClient -HostUrl $HostUrl -ApiKey $ApiKey -EnableSsh $EnableSsh -SshUser $SshUser `
        -ReportInterval $ReportInterval -Source $Source -Update $Update.IsPresent -ScriptRoot $PSScriptRoot
    if ($PSCommandPath) { exit 0 }
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    # Only exit when run as a file: under `irm | iex` exit would close the user's window.
    if ($PSCommandPath) { exit 1 }
}
