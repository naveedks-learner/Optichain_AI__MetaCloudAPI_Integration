$ErrorActionPreference = "Stop"

$apiBaseUrl = "http://127.0.0.1:8000"
$openApiUrl = "$apiBaseUrl/openapi.json"
$ngrokCommand = Get-Command ngrok -ErrorAction SilentlyContinue

if ($ngrokCommand) {
    $ngrokPath = $ngrokCommand.Source
} else {
    $installedNgrok = Join-Path $env:LOCALAPPDATA `
        "Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe"
    if (Test-Path -LiteralPath $installedNgrok) {
        $ngrokPath = $installedNgrok
    } else {
        throw "ngrok was not found. Install it with: winget install --id Ngrok.Ngrok"
    }
}

try {
    $openApi = & curl.exe --fail --silent --show-error --max-time 5 $openApiUrl
    if ($LASTEXITCODE -ne 0) {
        throw "FastAPI did not return its OpenAPI document."
    }
    $openApiText = $openApi -join "`n"
    if ($openApiText -notmatch '"/webhook"') {
        throw "The FastAPI process at port 8000 does not expose /webhook. Restart it with the current project code."
    }
} catch {
    throw "Cannot prepare the webhook tunnel: $($_.Exception.Message)"
}

Write-Host "Forwarding public HTTPS traffic to $apiBaseUrl"
Write-Host "When ngrok displays its Forwarding URL, add /webhook to it for Meta."
Write-Host "Example shape: https://<ngrok-host>/webhook"
Write-Host "Keep this tunnel temporary. It also exposes the demo's unauthenticated read-only API routes."

& $ngrokPath http 8000
if ($LASTEXITCODE -ne 0) {
    throw "ngrok exited with code $LASTEXITCODE."
}
