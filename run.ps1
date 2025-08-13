param([switch]$QuickTunnel)

$proj   = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "$proj\.venv\Scripts\python.exe"

Start-Process powershell -WorkingDirectory $proj -ArgumentList "-NoExit","-Command","& `"$python`" web\src\main.py" -WindowStyle Minimized
Start-Sleep -Seconds 2

if ($QuickTunnel) {
  Start-Process powershell -WorkingDirectory $proj -ArgumentList "-NoExit","-Command","& .\cloudflared-windows-amd64.exe tunnel --url http://127.0.0.1:8000" -WindowStyle Minimized
} else {
  Start-Process powershell -WorkingDirectory $proj -ArgumentList "-NoExit","-Command","& .\cloudflared-windows-amd64.exe tunnel run botzaim" -WindowStyle Minimized
}

Start-Sleep -Seconds 2
Start-Process powershell -WorkingDirectory $proj -ArgumentList "-NoExit","-Command","& `"$python`" bot\bot.py" -WindowStyle Minimized
