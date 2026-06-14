# Start the Friday tool server, wait until it is listening, then launch the voice app.
$ErrorActionPreference = "Stop"

Write-Host "Starting Friday tool server..."
$server = Start-Process -FilePath "uv" `
    -ArgumentList "run", "python", "-m", "friday.mcp_server" `
    -PassThru -NoNewWindow

try {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 300
        $up = Test-NetConnection 127.0.0.1 -Port 8765 -InformationLevel Quiet -WarningAction SilentlyContinue
    } until ($up -or (Get-Date) -gt $deadline)

    if (-not $up) { throw "Tool server did not open port 8765 within 30s." }

    Write-Host "Tool server is up. Launching voice app (hold SPACE to talk)..."
    uv run python -m friday.main
}
finally {
    Write-Host "Shutting down tool server..."
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force
    }
}
