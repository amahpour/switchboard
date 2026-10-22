# switchboard's SessionStart and SessionEnd for Claude Code on Windows (docs/DESIGN.md section 49.4):
# the PowerShell twin of bin/session, which bin/session and bin/session.cmd hand over to there.
# Claude Code runs no mcp_tool hook at these two events (it has no MCP connection then), so this
# command hook posts them to the server instead: the event, the session id, and the event's own
# short fields, nothing else. A SessionEnd takes the session offline; a SessionStart after /clear or
# a compaction may get context back, which it prints for Claude Code. It always exits 0, and
# quickly (within 1 s for a SessionEnd): a server that can't be reached changes nothing here. The
# token goes to the server in a request header built here, never on a command line, where other
# processes could read it.
# It runs under Windows PowerShell 5.1, which ships with Windows, and under PowerShell 7. Keep this
# file ASCII: 5.1 reads a file without a byte-order mark as ANSI.
$ErrorActionPreference = 'Stop'
trap { exit 0 }

$stdin = New-Object IO.MemoryStream
[Console]::OpenStandardInput().CopyTo($stdin)
$input_text = [Text.Encoding]::UTF8.GetString($stdin.ToArray())
function Plain([string]$name) { # only letters, digits, - and _: nothing that needs quoting in JSON
    $m = [regex]::Match($input_text, '"' + $name + '":\s*"([^"]*)"')
    if ($m.Success -and $m.Groups[1].Value -cmatch '^[A-Za-z0-9_-]*\z') { $m.Groups[1].Value } else { '' }
}
function First($a, $b) { if ($a) { $a } else { $b } }

$event = Plain 'hook_event_name'
$sid = Plain 'session_id'
if ($event -cne 'SessionStart' -and $event -cne 'SessionEnd') { exit 0 }
if (-not $sid) { exit 0 }
$url = First $env:CLAUDE_PLUGIN_OPTION_SERVER_URL $env:SWITCHBOARD_URL
if ($url -and $url.EndsWith('/')) { $url = $url.Substring(0, $url.Length - 1) }
$token = First $env:CLAUDE_PLUGIN_OPTION_TOKEN $env:SWITCHBOARD_AGENT_TOKEN
if (-not $url -or -not $token) { exit 0 }
$body = '{{"event": "{0}", "session_id": "{1}", "source": "{2}", "reason": "{3}", "permission_mode": "{4}"}}' -f
    $event, $sid, (Plain 'source'), (Plain 'reason'), (Plain 'permission_mode')
# Claude Code gives SessionEnd hooks as little as 1.5 s (2.1.299), and starting PowerShell takes a third of
# that before this line; the request itself takes tens of milliseconds. A SessionEnd gets a short limit all the
# same, so a server that doesn't answer doesn't hold up Claude Code's exit.
$limit = 5
if ($event -ceq 'SessionEnd') { $limit = 2 }

Add-Type -AssemblyName System.Net.Http
# an older .NET Framework offers TLS 1.0 by default, which servers no longer take; a newer one lets Windows choose
try { if ([int][Net.ServicePointManager]::SecurityProtocol -ne 0) { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } } catch { }
$handler = New-Object Net.Http.HttpClientHandler
$handler.AllowAutoRedirect = $false # a redirect would carry the token to wherever it points; curl doesn't follow one either
$client = New-Object Net.Http.HttpClient $handler
$client.Timeout = [TimeSpan]::FromSeconds($limit)
$req = New-Object Net.Http.HttpRequestMessage ([Net.Http.HttpMethod]::Post), "$url/mcp/session"
[void]$req.Headers.TryAddWithoutValidation('Authorization', "Bearer $token")
[void]$req.Headers.TryAddWithoutValidation('User-Agent', 'switchboard-claude-plugin')
$req.Headers.ExpectContinue = $false
$req.Content = New-Object Net.Http.StringContent $body, (New-Object Text.UTF8Encoding $false), 'application/json'
try {
    $res = $client.SendAsync($req).Result
    if (-not $res.IsSuccessStatusCode) { exit 0 }
    $out = $res.Content.ReadAsByteArrayAsync().Result
} catch { exit 0 }
$text = [Text.Encoding]::UTF8.GetString($out).Trim()
if ($event -ceq 'SessionStart' -and $text -and $text -cne '{}') {
    $stdout = [Console]::OpenStandardOutput()
    $bytes = [Text.Encoding]::UTF8.GetBytes($text + "`n")
    $stdout.Write($bytes, 0, $bytes.Length)
    $stdout.Flush()
}
exit 0
