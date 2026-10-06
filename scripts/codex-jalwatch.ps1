# Launch Codex with the deployed JalWatch MCP server and the ignored local .env.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot '.env'
$key = ''
if (Test-Path -LiteralPath $envFile) {
    $keyLine = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^JALWATCH_API_KEY=' } | Select-Object -Last 1
    if ($keyLine) {
        $key = ($keyLine -replace '^JALWATCH_API_KEY=', '').Trim().Trim('"', "'")
    }
}

$mcpUrl = 'mcp_servers.jalwatch.url=https://jalwatch-mcp.onrender.com/mcp/'
$mcpTimeout = 'mcp_servers.jalwatch.startup_timeout_sec=45'
$mcpToolTimeout = 'mcp_servers.jalwatch.tool_timeout_sec=60'
$configArgs = @('-c', $mcpUrl, '-c', $mcpTimeout, '-c', $mcpToolTimeout)
if ($key) {
    $configArgs += @('-c', 'mcp_servers.jalwatch.bearer_token_env_var=JALWATCH_API_KEY')
}
$previousKey = $env:JALWATCH_API_KEY
Push-Location $projectRoot
try {
    if ($key) {
        $env:JALWATCH_API_KEY = $key
    }
    codex @configArgs @args
    exit $LASTEXITCODE
}
finally {
    $env:JALWATCH_API_KEY = $previousKey
    Pop-Location
}
