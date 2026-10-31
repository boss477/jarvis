param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$startupFolder = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupFolder "Jarvis Persistent Agent.lnk"

if ($Remove) {
    $stopRequested = $false
    $uv = Get-Command "uv" -ErrorAction SilentlyContinue
    if ($uv) {
        try {
            Push-Location $repoRoot
            try {
                & $uv.Source run python -m friday.services.agent_supervisor --stop
                if ($LASTEXITCODE -eq 0) {
                    $stopRequested = $true
                }
                else {
                    Write-Warning "The supervisor stop request failed (exit code $LASTEXITCODE)."
                }
            }
            finally {
                Pop-Location
            }
        }
        catch {
            Write-Warning "Could not request a supervisor stop: $($_.Exception.Message)"
        }
    }
    else {
        Write-Warning "uv was not found; could not request a supervisor stop."
    }

    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath
        if ($stopRequested) {
            Write-Host "Removed Jarvis Persistent Agent from Windows Startup and requested a supervisor stop."
        }
        else {
            Write-Host "Removed Jarvis Persistent Agent from Windows Startup; supervisor stop was not confirmed."
        }
    }
    else {
        Write-Host "Jarvis Persistent Agent was not installed in Windows Startup."
    }
    exit 0
}
$uv = Get-Command "uv" -ErrorAction Stop

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
