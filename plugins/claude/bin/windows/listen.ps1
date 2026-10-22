# switchboard's listener for Claude Code on Windows (docs/DESIGN.md section 49.4): the PowerShell
# twin of bin/listen, which bin/listen and bin/listen.cmd hand over to there. Same job, same
# rules: Claude Code runs it at Stop and at SessionStart for startup/resume, with asyncRewake; it
# sends only this session's id and waits at the switchboard server, round after round, so the
# session can be woken however long it stays idle. A message is written to stderr and the exit
# code is 2, which makes Claude Code start a turn with it. Exit 0 wakes nothing: when another
# listener or a hook of the session takes over, the session is busy or gone, or Claude Code quits.
# Any other reason the listener stops (a refused token, an unexpected answer) goes to listen.log,
# and the session is woken once with it, as it is when someone else ended it (a kick, a closed
# room). A round that runs out starts the next; a server that can't be reached is tried again, a
# little later each time. The token goes to the server in a request header built here, never on a
# command line, where other processes could read it.
# It runs under Windows PowerShell 5.1, which ships with Windows, and under PowerShell 7. Keep this
# file ASCII: 5.1 reads a file without a byte-order mark as ANSI.
$ErrorActionPreference = 'Stop'
function First($a, $b) { if ($a) { $a } else { $b } }
$sid = ''
# the plugin's own data folder, or the user's; never a shared one like %TEMP%, where another
# account could plant the name first
$profile_dir = [Environment]::GetFolderPath('UserProfile')
$dir = First $env:CLAUDE_PLUGIN_DATA $(if ($profile_dir) { Join-Path (Join-Path $profile_dir '.cache') 'switchboard' })
$utf8 = New-Object Text.UTF8Encoding $false
$stderr = [Console]::OpenStandardError()
$rounds = 0

function Say([byte[]]$bytes) { $stderr.Write($bytes, 0, $bytes.Length); $stderr.Flush() }
function SayText([string]$text) { Say $utf8.GetBytes($text) }
function Flat([string]$text) { # what the server said, on one line and cut short
    $one = $text -replace '[\r\n]', ' '
    $one.Substring(0, [Math]::Min(300, $one.Length))
}
function LogLine($status, $said) { # one line in listen.log: when, the session, the round, the status, what the server said
    if (-not $dir) { return }
    try {
        [void][IO.Directory]::CreateDirectory($dir)
        $log = Join-Path $dir 'listen.log'
        if ((Test-Path $log) -and (Get-Item $log).Length -gt 65536) { # a log that stays small
            [IO.File]::WriteAllLines($log, [string[]]@([IO.File]::ReadAllLines($log) | Select-Object -Last 100), $utf8)
        }
        $when = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", [Globalization.CultureInfo]::InvariantCulture)
        [IO.File]::AppendAllText($log, "$when session=$sid round=$rounds status=$status $said`n", $utf8)
    } catch { }
}
function Remembers($name, [string]$text) { # whether the marker in the data folder holds exactly this
    try { ([IO.File]::ReadAllText((Join-Path $dir $name), $utf8)).TrimEnd("`n") -ceq $text.TrimEnd("`n") } catch { $false }
}
function Remember($name, [byte[]]$bytes) { # false when there is nowhere to write: no marker, no wake, or it could loop the session awake
    if (-not $dir) { return $false }
    try {
        [void][IO.Directory]::CreateDirectory($dir)
        [IO.File]::WriteAllBytes((Join-Path $dir $name), $bytes)
        $true
    } catch { $false }
}
function Alive { # Claude Code's own pid: a background hook outlives a Claude Code that quits
    try { -not [Diagnostics.Process]::GetProcessById($owner).HasExited } catch { $false }
}

# whatever goes wrong, the listener ends with exit 0, never a code Claude Code would read as a wake, and says why in the log
trap { LogLine 'error' (Flat "$($_.Exception.Message)"); exit 0 }

$stdin = New-Object IO.MemoryStream
[Console]::OpenStandardInput().CopyTo($stdin)
$input_text = [Text.Encoding]::UTF8.GetString($stdin.ToArray())
function Field([string]$name) {
    $m = [regex]::Match($input_text, '"' + $name + '":\s*"([^"]*)"')
    if ($m.Success) { $m.Groups[1].Value } else { '' }
}

$event = Field 'hook_event_name'
# A /clear or compaction can happen mid-turn; its next Stop starts the listener. Starting one
# here would report that busy session idle and could deliver a wake into the running turn.
if ($event -cne 'Stop' -and -not ($event -ceq 'SessionStart' -and (Field 'source') -cin 'startup', 'resume')) { exit 0 }
$sid = Field 'session_id'
if ($sid -cnotmatch '^[A-Za-z0-9_-]+\z') { exit 0 }
$url = First $env:CLAUDE_PLUGIN_OPTION_SERVER_URL $env:SWITCHBOARD_URL
if ($url -and $url.EndsWith('/')) { $url = $url.Substring(0, $url.Length - 1) }
$token = First $env:CLAUDE_PLUGIN_OPTION_TOKEN $env:SWITCHBOARD_AGENT_TOKEN
if (-not $url -or -not $token) { exit 0 }
$round = 900
if ($env:SWITCHBOARD_LISTEN_S -cmatch '^[0-9]{1,9}\z') { $round = [Math]::Max(10, [int]$env:SWITCHBOARD_LISTEN_S) }
$owner = 0
if ($env:CLAUDE_PID -cmatch '^[0-9]{1,9}\z') { $owner = [int]$env:CLAUDE_PID }
if ($owner -le 0) { LogLine 'no-owner' 'CLAUDE_PID is not set, so the listener can not tell when Claude Code quits'; exit 0 }

Add-Type -AssemblyName System.Net.Http
# an older .NET Framework offers TLS 1.0 by default, which servers no longer take; a newer one lets Windows choose
try { if ([int][Net.ServicePointManager]::SecurityProtocol -ne 0) { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } } catch { }
$handler = New-Object Net.Http.HttpClientHandler
$handler.AllowAutoRedirect = $false # a redirect would carry the token to wherever it points; curl doesn't follow one either
$client = New-Object Net.Http.HttpClient $handler
$client.Timeout = [TimeSpan]::FromSeconds($round + 30)

function Round($again) { # one request: the status ('' when nothing answered) and the body; $null when Claude Code is gone
    $req = New-Object Net.Http.HttpRequestMessage ([Net.Http.HttpMethod]::Post), "$url/mcp/listen"
    foreach ($h in @(@('Authorization', "Bearer $token"), @('X-Switchboard-Session', $sid), @('X-Switchboard-Wait', "$round"),
            @('X-Switchboard-Again', "$again"), @('User-Agent', 'switchboard-claude-plugin'))) {
        [void]$req.Headers.TryAddWithoutValidation($h[0], $h[1])
    }
    $req.Headers.ExpectContinue = $false
    $req.Content = New-Object Net.Http.ByteArrayContent (, [byte[]]@())
    $task = $client.SendAsync($req)
    try {
        while (-not $task.Wait(2000)) { if (-not (Alive)) { return $null } }
        [pscustomobject]@{ Status = [string][int]$task.Result.StatusCode; Body = $task.Result.Content.ReadAsByteArrayAsync().Result }
    } catch {
        [pscustomobject]@{ Status = ''; Body = [byte[]]@() } # refused, reset, timed out
    }
}

$again = 0
$backoff = 1
while (Alive) {
    $rounds++
    $began = Get-Date
    $answer = Round $again
    if ($null -eq $answer -or -not (Alive)) { exit 0 }
    $again = 1
    $status = $answer.Status
    $body = [byte[]]$answer.Body
    $said = Flat $utf8.GetString($body)
    switch -CaseSensitive ($status) {
        '200' {
            if ($dir) { Remove-Item -ErrorAction SilentlyContinue (Join-Path $dir "stopped-$sid") }
            Say $body
            exit 2
        }
        '410' { # someone else ended the session: say so once, or it goes deaf without a word
            LogLine 410 $said
            if (Remembers "ended-$sid" $utf8.GetString($body)) { exit 0 }
            if (-not (Remember "ended-$sid" $body)) { exit 0 }
            Say $body
            exit 2
        }
        '408' { $backoff = 1; Start-Sleep -Seconds 1 }
        { $_ -in '', '429', '500', '502', '503', '504' } {
            if (((Get-Date) - $began).TotalSeconds -ge 10) {
                $backoff = 1 # a long wait that a proxy cut: wait again at once
            } else {
                Start-Sleep -Seconds $backoff
                if ($backoff -lt 60) { $backoff = $backoff * 2 }
            }
        }
        '409' { # busy (a turn is running: its next Stop listens) or taken over by a newer listener or
            # another hook of the session, which is every turn's ordinary end: only a refusal is logged
            if ($said.Contains('"not_idle"')) { LogLine 409 $said }
            exit 0
        }
        default { # a reason nobody expects, such as a token that was refused: log it, and wake the session once
            # with it, because nothing else would tell it the listener is gone. The marker keeps the same
            # reason from waking it again at every later Stop; a wake clears it.
            LogLine $status $said
            # only switchboard's own error word goes to the model, never the rest of the body: whatever
            # stood in the way (a proxy's page) is in the log
            $m = [regex]::Match($said, '"error":\s*"([A-Za-z0-9_-]*)"')
            $what = if ($m.Success) { $m.Groups[1].Value } else { '' }
            $reason = "$status $what"
            if (Remembers "stopped-$sid" $reason) { exit 0 }
            if (-not (Remember "stopped-$sid" $utf8.GetBytes($reason))) { exit 0 }
            $named = if ($what) { " ($what)" } else { '' }
            SayText ("[switchboard] The listener for this session stopped, so room messages can no longer wake it while it is idle: the server answered $status$named. Check the plugin settings, the server address and the agent token. The listener starts again at the end of this session's next turn. Details: " + (Join-Path $dir 'listen.log') + "`n")
            exit 2
        }
    }
}
exit 0
