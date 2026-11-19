$ErrorActionPreference = "Stop"

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

$secureToken = Read-Host "Paste your ngrok authtoken (input is hidden)" -AsSecureString
$plainToken = [System.Net.NetworkCredential]::new("", $secureToken).Password
try {
    & $ngrokPath config add-authtoken $plainToken
    if ($LASTEXITCODE -ne 0) {
        throw "ngrok rejected the authtoken."
    }
} finally {
    Remove-Variable plainToken, secureToken -ErrorAction SilentlyContinue
}
