param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$uv = Get-Command "uv" -ErrorAction Stop
$startupFolder = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupFolder "Jarvis Persistent Agent.lnk"

if ($Remove) {
    Push-Location $repoRoot
    try {
        & $uv.Source run python -m friday.services.agent_supervisor --stop
        if ($LASTEXITCODE -ne 0) {
            throw "Could not send a stop request to the Jarvis supervisor."
        }
    }
    finally {
        Pop-Location
    }
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath
        Write-Host "Stopped the supervisor and removed Jarvis Persistent Agent from Windows Startup."
    }
    else {
        Write-Host "Jarvis Persistent Agent was not installed in Windows Startup."
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $startupFolder -PathType Container)) {
    throw "Windows Startup folder is unavailable: $startupFolder"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $uv.Source
$shortcut.Arguments = "run python -m friday.services.agent_supervisor"
$shortcut.WorkingDirectory = $repoRoot
$shortcut.Description = "Start and monitor the Jarvis persistent channel agent at sign-in."
$shortcut.WindowStyle = 7
$shortcut.Save()

Write-Host "Installed Jarvis Persistent Agent in Windows Startup."
Write-Host "It will start minimized at your next sign-in and restart the host if it exits."
Write-Host "Remove it and request a stop with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Remove"
