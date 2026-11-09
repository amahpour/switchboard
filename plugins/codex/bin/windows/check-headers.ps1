# Read the helper setting only; never execute it or print config values (DESIGN.md section 49.11, #665).
$ErrorActionPreference = 'Stop'
try {
    $base = if ($env:CODEX_HOME) { $env:CODEX_HOME } elseif ($env:HOME) { Join-Path $env:HOME '.codex' } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex' }
    $config = Join-Path $base 'config.toml'
    if (-not (Test-Path -LiteralPath $config -PathType Leaf)) { exit 0 }
    $stream = [IO.File]::OpenRead($config)
    try {
        $bytes = New-Object byte[] 1048576
        $count = $stream.Read($bytes, 0, $bytes.Length)
        $text = [Text.Encoding]::UTF8.GetString($bytes, 0, $count)
    } finally { $stream.Dispose() }
    $server = $false
    foreach ($line in $text.Split("`n")) {
        if ($line -match '^\s*\[') { $server = $line -match '^\s*\[mcp_servers\.switchboard\]\s*(#.*)?$' }
        if (-not $server -or $line -notmatch '^\s*http_headers_helper\s*=') { continue }
        if ($line -match '^\s*http_headers_helper\s*=\s*''"([^"]+)".*''\s*(#.*)?$' -and [IO.Path]::IsPathRooted($Matches[1])) {
            if (-not (Test-Path -LiteralPath $Matches[1] -PathType Leaf)) {
                [Console]::Error.WriteLine('meeting-host: configured http_headers_helper is missing; run the installed bin/windows/headers-launcher.ps1 --install, set its stable path in config.toml yourself, then restart Codex (see the meetings guide)')
            }
        } else { [Console]::Error.WriteLine('meeting-host: cannot check http_headers_helper command syntax; use the quoted stable launcher from the meetings guide') }
        break
    }
} catch { [Console]::Error.WriteLine('meeting-host: cannot check http_headers_helper; see the meetings guide') }
exit 0
