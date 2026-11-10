# Host the meeting bot for one call on this machine, on Windows (docs/DESIGN.md section 54, #623): the PowerShell
# twin of bin/meeting-host, which bin/meeting-host (under Git Bash) and bin/meeting-host.cmd (under PowerShell)
# hand over to. Read bin/meeting-host first: it says what each command does and why; this does the same.
#
#   meeting-host start [-server URL] [-data DIR] [--cpu|--gpu] [--model NAME] [--add-host NAME:IP] [--no-join|--from-room] ROOM
#                                                                                     (the meeting link on stdin)
#   meeting-host stop [FINGERPRINT] | status | plan [--cpu|--gpu] [--model NAME]
#
# Arguments are written as the sh script writes them (--server, --data, ...), so the skill's one command works in
# both. The meeting link is read from stdin and written to a file only the user can read (an access list of the
# user alone, set while the file is still empty); the key is made here, the same way. Neither is ever an
# argument, an environment variable or printed. The credential it registers with is what the plugin's other
# scripts hold: the agent token in the environment, else the listener key bin/headers.cmd made for this Claude Code
# (listener-key-<Claude Code's pid> in the plugin's data folder). Exit codes: 0 done, 1 something is wrong, 2 a
# bad command line.
# It runs under Windows PowerShell 5.1, which ships with Windows, and under PowerShell 7. Keep this file ASCII:
# 5.1 reads a file without a byte-order mark as ANSI.
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch { } # the middle dot of a fingerprint
$Prog = 'meeting-host'
$windows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
$plugin = if ($env:SWITCHBOARD_PLUGIN_ROOT) { $env:SWITCHBOARD_PLUGIN_ROOT } else { Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
$compose = Join-Path (Join-Path $plugin 'meeting') 'compose.yaml'
$pins = Join-Path (Join-Path $plugin 'meeting') 'fixes.pins'
$ProjectPrefix = 'switchboard-call-'
$Models = 'base', 'small', 'distil-large-v3', 'large-v3-turbo'

function Say([string]$text) { [Console]::Out.WriteLine($text) }
function Err([string]$text) { [Console]::Error.WriteLine($text) }
function Die([string]$text) { Err "${Prog}: $text"; exit 1 }
function Usage([string]$text) {
    Err "${Prog}: $text"
    Err "usage: $Prog start [--server URL] [--data DIR] [--cpu|--gpu] [--model NAME] [--add-host NAME:IP] [--no-join|--from-room] ROOM"
    Err '       (the meeting link on stdin)'
    Err "       $Prog stop [FINGERPRINT] | status | plan [--cpu|--gpu] [--model NAME]"
    exit 2
}
function First($a, $b) { if ($a) { $a } else { $b } }
function Sha256Hex([byte[]]$bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $hex = (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
    $sha.Dispose()
    $hex
}
function Native([string]$exe, [string[]]$cmdArgs) { # run a program, return its output lines; $LASTEXITCODE tells how it went
    try { & $exe @cmdArgs 2>$null } catch { $global:LASTEXITCODE = 127; @() }
}
function Has([string]$exe) { [bool](Get-Command $exe -ErrorAction SilentlyContinue) }

# ------------------------------------------------------------------------------------------------ the device
# GPU if there is one for Docker to use, CPU if not (#623): nvidia-smi sees a GPU, and Docker has an nvidia runtime.
function Choose-Device([string]$forced) {
    $detected = 'cpu'
    $detectedWhy = 'no NVIDIA GPU: nvidia-smi is missing or sees none'
    if (-not $windows -and (Has 'uname') -and ((& uname -s) -eq 'Darwin')) {
        $detectedWhy = 'Docker on a Mac cannot use the GPU'
    } elseif (Has 'nvidia-smi') {
        $gpus = @(Native 'nvidia-smi' @('-L') | Where-Object { "$_" -match '^GPU ' })
        if ($gpus.Count -gt 0) {
            $runtimes = (Native 'docker' @('info', '--format', '{{range $name, $_ := .Runtimes}}{{$name}} {{end}}')) -join ' '
            if ($runtimes -match '(^|\s)nvidia(\s|$)') {
                $detected = 'gpu'
                $detectedWhy = 'an NVIDIA GPU that Docker can use'
            } else {
                $detectedWhy = 'Docker has no nvidia runtime: the NVIDIA Container Toolkit is not set up'
            }
        }
    }
    if ($forced) {
        $why = "forced by --$forced"
        if ($forced -ne $detected) { $why = "$why (detected: $detected, because $detectedWhy)" }
        return @{ Device = $forced; Why = $why }
    }
    @{ Device = $detected; Why = $detectedWhy }
}
function Choose-Model([string]$asked, [string]$device) { # (model, what joinly is told: empty is its own)
    $model = $asked
    if (-not $model) { $model = if ($device -eq 'gpu') { 'large-v3-turbo' } else { 'base' } } elseif ($Models -cnotcontains $model) { Usage ('--model is one of: ' + ($Models -join ' ')) }
    $told = $model
    if ($device -eq 'cpu' -and $model -ceq 'base') { $told = '' }
    @{ Model = $model; Told = $told }
}
function Joinly-Image([string]$device) { if ($device -eq 'gpu') { 'ghcr.io/joinly-ai/joinly:v0.5.4-cuda@sha256:cc79e10311f95252d7684c4969c4467ea581c78de9b331096ac27a4a5a98532e' } else { 'ghcr.io/joinly-ai/joinly:v0.5.4@sha256:d19ca294eb24625d69d8069d0168cb5214af3652fbf721eb07480177eb4b9ea0' } }

# ----------------------------------------------------------------------------------------- the data folder
function ParentOf([int]$id) { # the pid of the process that started this one, or 0
    if ($windows) {
        if (-not ('SwitchboardParentOf' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SwitchboardParentOf {
    [StructLayout(LayoutKind.Sequential)]
    struct Basic { public IntPtr Exit; public IntPtr Peb; public IntPtr Affinity; public IntPtr Priority; public IntPtr Id; public IntPtr Parent; }
    [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("ntdll.dll")] static extern int NtQueryInformationProcess(IntPtr handle, int info, ref Basic basic, int size, out int returned);
    public static int Of(int pid) {
        IntPtr handle = OpenProcess(0x1000, false, pid);
        if (handle == IntPtr.Zero) return 0;
        try {
            Basic basic = new Basic();
            int returned;
            return NtQueryInformationProcess(handle, 0, ref basic, Marshal.SizeOf(basic), out returned) == 0 ? basic.Parent.ToInt32() : 0;
        } finally { CloseHandle(handle); }
    }
}
'@
        }
        return [SwitchboardParentOf]::Of($id)
    }
    $got = & ps -o ppid= -p $id 2>$null
    if ($LASTEXITCODE -eq 0 -and "$got".Trim() -cmatch '^[0-9]{1,9}\z') { return [int]"$got".Trim() }
    0
}
function Claude-Pid { # CLAUDE_PID, which a Bash command is given, else the first ancestor that is not a shell
    if ($env:CLAUDE_PID -cmatch '^[0-9]{1,9}\z') { return [int]$env:CLAUDE_PID }
    $shells = 'cmd', 'powershell', 'pwsh', 'bash', 'sh', 'dash', 'zsh', 'conhost'
    $child = [Diagnostics.Process]::GetCurrentProcess()
    for ($hops = 0; $hops -lt 8; $hops++) {
        $parent = ParentOf $child.Id
        if ($parent -le 1) { return 0 }
        try { $found = [Diagnostics.Process]::GetProcessById($parent) } catch { return 0 }
        if ($shells -notcontains $found.ProcessName.ToLowerInvariant()) { return $parent }
        $child = $found
    }
    0
}
function Data-Dir([string]$given) {
    $codex = Test-Path -LiteralPath (Join-Path $plugin '.codex-plugin')
    $d = First $given $env:SWITCHBOARD_PLUGIN_DATA
    if (-not $codex) { $d = First $d $env:CLAUDE_PLUGIN_DATA }
    if (-not $d -and -not $codex) {
        $pid_ = Claude-Pid
        $config = First $env:CLAUDE_CONFIG_DIR $(Join-Path ([Environment]::GetFolderPath('UserProfile')) '.claude')
        $glob = Join-Path (Join-Path $config 'plugins') 'data'
        if ($pid_ -gt 0 -and (Test-Path -LiteralPath $glob)) {
            foreach ($sub in Get-ChildItem -LiteralPath $glob -Directory) {
                if (Test-Path -LiteralPath (Join-Path $sub.FullName "listener-key-$pid_")) { $d = $sub.FullName; break }
            }
        }
    }
    if (-not $d) { $d = Join-Path (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.cache') 'switchboard' }
    $d = "$d".Trim('"')
    if ($windows) { if ($d -cnotmatch '^([A-Za-z]:[\\/]|[\\/]{2})') { Die "no folder to keep this call's files in: pass --data DIR (the plugin's data folder)" } }
    elseif (-not $d.StartsWith('/')) { Die "no folder to keep this call's files in: pass --data DIR (the plugin's data folder)" }
    $d
}
function Read-Text([string]$path) { # a small file's text, shared so a helper may replace it meanwhile; '' when unreadable
    try {
        $fs = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        try {
            $buffer = New-Object byte[] 512
            [Text.Encoding]::ASCII.GetString($buffer, 0, $fs.Read($buffer, 0, 512)).Trim()
        } finally { $fs.Dispose() }
    } catch { '' }
}
function Credential([string]$data) { # the agent token, else the listener key of this Claude Code; '' when there is neither
    if (Test-Path -LiteralPath (Join-Path $plugin '.codex-plugin')) {
        if ($env:SWITCHBOARD_AGENT_TOKEN) { return [string]$env:SWITCHBOARD_AGENT_TOKEN }
        if ($env:CODEX_THREAD_ID -cnotmatch '^[A-Za-z0-9_-]{1,128}\z') { return '' }
        $id = $PID
        for ($hops = 0; $hops -lt 32; $hops++) {
            $id = ParentOf $id
            if ($id -le 1) { return '' }
            $file = Join-Path $data "codex-listener-key-$id"
            if (Test-Path -LiteralPath $file) {
                $k = Read-Text $file
                if ($k -cmatch '^sbl_[0-9a-f]{64}\z') { return $k }
                return ''
            }
        }
        return ''
    }
    $t = First $env:SWITCHBOARD_AGENT_TOKEN $env:CLAUDE_PLUGIN_OPTION_TOKEN
    if ($t) { return $t }
    $id = Claude-Pid
    if ($id -le 0) { return '' }
    $k = Read-Text (Join-Path $data "listener-key-$id")
    if ($k -cmatch '^sbl_[0-9a-f]{64}\z') { $k } else { '' }
}
function Make-Private([string]$path) { # readable by the user only: 0600, or on Windows an access list of the user alone
    if (-not $windows) {
        & chmod 600 $path
        if ($LASTEXITCODE -ne 0) { Die 'chmod failed' }
        return
    }
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $rules = New-Object Security.AccessControl.FileSecurity
    $rules.SetAccessRuleProtection($true, $false)
    $full = [Security.AccessControl.FileSystemRights]::FullControl
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $rules.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule $me, $full, $allow))
    Set-Acl -LiteralPath $path -AclObject $rules
    $now = Get-Acl -LiteralPath $path
    if (-not $now.AreAccessRulesProtected) { Die 'could not make a file private' }
    foreach ($rule in $now.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.IdentityReference.Value -cne $me.Value) { Die 'could not make a file private' }
    }
}
function Make-PrivateDir([string]$path) { # a folder only the user can enter: 0700, or an access list of the user alone
    if (-not $windows) {
        & chmod 700 $path
        if ($LASTEXITCODE -ne 0) { Die 'chmod failed' }
        return
    }
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $rules = New-Object Security.AccessControl.DirectorySecurity
    $rules.SetAccessRuleProtection($true, $false)
    $full = [Security.AccessControl.FileSystemRights]::FullControl
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $none = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $rules.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule $me, $full, $inherit, $none, $allow))
    Set-Acl -LiteralPath $path -AclObject $rules
}
function Write-Private([string]$path, [string]$text) { # the file exists, empty and private, before a byte of the secret is in it
    [IO.File]::WriteAllBytes($path, [byte[]]@())
    Make-Private $path
    [IO.File]::WriteAllBytes($path, [Text.Encoding]::ASCII.GetBytes($text + "`n"))
}

# ------------------------------------------------------------------------------------------------ the fixes
function Ensure-Fixes([string]$data) {
    $fixes = Join-Path $data 'joinly-fixes'
    [void][IO.Directory]::CreateDirectory($fixes)
    if (-not (Test-Path -LiteralPath $pins)) { Die 'the plugin has no meeting/fixes.pins: reinstall the plugin' }
    foreach ($line in Get-Content -LiteralPath $pins) {
        $parts = "$line".Trim() -split '\s+'
        if ($parts.Count -lt 3 -or $parts[0].StartsWith('#')) { continue }
        $name, $sum, $url = $parts[0], $parts[1], $parts[2]
        if ($sum -cnotmatch '^[0-9a-f]{64}\z') { Die "meeting/fixes.pins: $name has no sha256" }
        $file = Join-Path $fixes $name
        $have = ''
        if (Test-Path -LiteralPath $file) { $have = Sha256Hex ([IO.File]::ReadAllBytes($file)) }
        if ($have -cne $sum) {
            $tmp = Join-Path $fixes ('.fetch.' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
            if ($url.StartsWith('plugin:')) {
                # a file this plugin ships: no download, and the same check as for one that was fetched
                $rel = $url.Substring(7)
                if ($rel -match '(^|[\\/])\.\.?([\\/]|$)') { Die "meeting/fixes.pins: $name has a path that leaves the plugin" }
                $src = Join-Path (Join-Path $plugin 'meeting') $rel
                try { Copy-Item -LiteralPath $src -Destination $tmp -Force -ErrorAction Stop } catch {
                    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
                    Die "the plugin has no $src: reinstall the plugin"
                }
            } else {
                try {
                    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
                    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tmp -TimeoutSec 120 | Out-Null
                } catch {
                    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
                    Die "could not fetch $name from the address in meeting/fixes.pins; run again when it can be reached"
                }
            }
            $got = Sha256Hex ([IO.File]::ReadAllBytes($tmp))
            if ($got -cne $sum) {
                Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
                Die "$name is not the file meeting/fixes.pins pins (its sha256 is $got): not using it"
            }
            Move-Item -LiteralPath $tmp -Destination $file -Force
        }
        if (-not $windows) { & chmod 644 $file }
    }
    $fixes
}

# ----------------------------------------------------------------------------------------------- Compose
$script:override = ''
function Compose([hashtable]$vars, [string[]]$composeArgs) { # docker compose with the project's variables (none of them secret)
    $saved = @{}
    foreach ($k in $vars.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, [string]$vars[$k]) }
    try { & docker compose @composeArgs | Out-Host; $global:LASTEXITCODE }
    finally { foreach ($k in $vars.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) } }
}
$unused = @{ SB_CALL_DIR = '/unused'; SB_SERVER = 'https://unused'; SB_FIXES_DIR = '/unused'; SB_RELAY_USER = '0:0'; SB_RELAY_IMAGE = $env:SWITCHBOARD_RELAY_IMAGE }
function Docker-Quiet([string[]]$a) { & docker @a *> $null; $LASTEXITCODE -eq 0 }
function Running([string]$project) { [bool]((& docker ps -q --filter "label=com.docker.compose.project=$project" 2>$null) -join '') }
function Remove-Project([string]$data, [string]$id) {
    $project = "$ProjectPrefix$id"
    $null = Compose $unused @('-f', $compose, '-p', $project, '--profile', 'cpu', '--profile', 'gpu', 'down', '--timeout', '20')
    Remove-Item -LiteralPath (Join-Path (Join-Path $data 'calls') $id) -Recurse -Force -ErrorAction SilentlyContinue
}
function Call-Dirs([string]$data) {
    $calls = Join-Path $data 'calls'
    if (Test-Path -LiteralPath $calls) { Get-ChildItem -LiteralPath $calls -Directory | ForEach-Object { $_.Name } }
}
function Sweep([string]$data) { foreach ($id in Call-Dirs $data) { if (-not (Running "$ProjectPrefix$id")) { Remove-Project $data $id } } }

# ------------------------------------------------------------------------------------------------ commands
function Parse([string[]]$rest, [string[]]$valued, [string[]]$flags) { # {name: value}, '' room/rest in _
    $got = @{ _ = @() }
    for ($i = 0; $i -lt $rest.Count; $i++) {
        $a = $rest[$i]
        if ($valued -contains $a) {
            if ($i + 1 -ge $rest.Count) { Usage "$a needs a value" }
            $got[$a] = $rest[++$i]
        } elseif ($flags -contains $a) { $got[$a] = $true }
        elseif ($a.StartsWith('-')) { Usage "unknown argument $a" }
        elseif ($a -match '://') { Usage 'the meeting link goes on stdin, never on the command line' }
        else { $got['_'] += $a }
    }
    $got
}

function Check-Headers {
    $check = Join-Path $PSScriptRoot 'check-headers.ps1'
    if ((Test-Path -LiteralPath (Join-Path $plugin '.codex-plugin')) -and (Test-Path -LiteralPath $check)) {
        # A child process keeps its exit 0 from ending the host script.
        $shell = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
        & $shell -NoProfile -NonInteractive -File $check
    }
}

function Cmd-Plan([string[]]$rest) {
    $o = Parse $rest @('--model') @('--cpu', '--gpu')
    $forced = if ($o['--cpu']) { 'cpu' } elseif ($o['--gpu']) { 'gpu' } else { '' }
    $d = Choose-Device $forced
    $m = Choose-Model $o['--model'] $d.Device
    Say "device=$($d.Device)"; Say "reason=$($d.Why)"; Say "model=$($m.Model)"; Say "joinly=$(Joinly-Image $d.Device)"
    Check-Headers
}

function Cmd-Status([string[]]$rest) {
    $o = Parse $rest @('--data') @()
    $data = Data-Dir $o['--data']
    Check-Headers
    $any = $false
    foreach ($id in Call-Dirs $data) {
        $any = $true
        Say "--- call $id"
        $info = Join-Path (Join-Path (Join-Path $data 'calls') $id) 'info'
        if (Test-Path -LiteralPath $info) { Get-Content -LiteralPath $info | ForEach-Object { Say $_ } }
        if (Running "$ProjectPrefix$id") {
            Say 'state=running'
            $null = Compose $unused @('-f', $compose, '-p', "$ProjectPrefix$id", '--profile', 'cpu', '--profile', 'gpu', 'logs', '--no-color', '--tail', '6', 'relay')
        } else { Say 'state=stopped' }
    }
    if (-not $any) { Say 'no hosted call on this machine' }
}

function Cmd-Stop([string[]]$rest) {
    $o = Parse $rest @('--data') @()
    $data = Data-Dir $o['--data']
    $want = ''
    if ($o['_'].Count -gt 0) { $want = ((($o['_'][0] -replace '\\u00b7', '') -replace '[^0-9a-f]', '') + '        ').Substring(0, 8).Trim() }
    $any = $false
    $live = $false
    foreach ($id in Call-Dirs $data) {
        if ($want -and $id -cne $want) { continue }
        $any = $true
        if (Running "$ProjectPrefix$id") { $live = $true }
        Remove-Project $data $id
        Say ('stopped the call with key ' + $id.Substring(0, 4) + [char]0x00B7 + $id.Substring(4, 4))
    }
    if (-not $any) { Say 'no hosted call to stop'; return }
    if ($live) { Say 'The relay reports departure so the call bar ends at once; an unreachable server uses the silence timeout.' }
}

function Cmd-Start([string[]]$rest) {
    $o = Parse $rest @('--server', '--data', '--model', '--add-host') @('--cpu', '--gpu', '--no-join', '--from-room')
    if ($o['--from-room'] -and $o['--no-join']) { Usage 'cannot combine --from-room and --no-join' }
    if ($o['_'].Count -gt 1) { Usage 'start: one room' }
    if ($o['_'].Count -lt 1) { Usage 'start: which room?' }
    $room = ([string]$o['_'][0]).TrimStart('#')
    if ($room -cnotmatch '^[A-Za-z0-9._-]{1,64}\z') { Usage 'start: a room is letters, digits, . _ - (as in #uploads)' }
    $server = First $o['--server'] (First $env:SWITCHBOARD_URL $env:CLAUDE_PLUGIN_OPTION_SERVER_URL)
    if (-not $server) { Die 'which server? pass --server URL (the plugin''s server_url)' }
    $server = $server.TrimEnd('/')
    $allowHttp = $env:SWITCHBOARD_RELAY_ALLOW_HTTP -ceq '1'
    if ($server -cmatch '^https://') { }
    elseif ($server -cmatch '^http://(localhost|[^/:]+\.localhost|127\.0\.0\.1)(:[0-9]+)?(/|\z)') { }
    elseif ($server -cmatch '^http://') { if (-not $allowHttp) { Die 'the server address is plain http: the key would cross the network in the clear (SWITCHBOARD_RELAY_ALLOW_HTTP=1 allows it on a network you trust)' } }
    else { Die 'the server address must start with https://' }
    if ($server -cnotmatch '^[A-Za-z0-9._~:/@%+=-]+\z') { Die 'the server address has a character it should not' }
    $addHost = First $o['--add-host'] $env:SWITCHBOARD_RELAY_ADD_HOST
    if ($addHost -and $addHost -cnotmatch '^[A-Za-z0-9.-]+:[A-Za-z0-9.:-]+\z') { Usage '--add-host is NAME:IP (or NAME:host-gateway), such as sb.example.internal:192.0.2.7' }
    $noJoin = [bool]$o['--no-join']

    # ---- everything that can be wrong before a key exists
    if (-not (Has 'docker')) { Die 'Docker is needed: the bot runs in two containers (https://docs.docker.com/get-docker/)' }
    if (-not (Docker-Quiet @('info'))) { Die 'Docker is installed but not running (or you may not use it)' }
    if (-not (Docker-Quiet @('compose', 'version'))) { Die 'Docker Compose is needed: ''docker compose'' did not answer' }
    if (-not (Test-Path -LiteralPath $compose)) { Die 'the plugin has no meeting/compose.yaml: reinstall the plugin' }
    $data = Data-Dir $o['--data']
    [void][IO.Directory]::CreateDirectory($data)
    $cred = Credential $data
    if (-not $cred -and (Test-Path -LiteralPath (Join-Path $plugin '.codex-plugin'))) {
        Die 'no credential: hosting from Codex needs SWITCHBOARD_AGENT_TOKEN or browser sign-in with http_headers_helper configured and a current thread; see the Codex hosting guide'
    }
    if (-not $cred) { Die "no credential: this Claude Code has no agent token in its environment and no listener key in $data (listener-key-<its pid>); sign in to switchboard from Claude Code (/mcp, then Authenticate) or set the plugin's token, then ask again" }
    $forced = if ($o['--cpu']) { 'cpu' } elseif ($o['--gpu']) { 'gpu' } else { '' }
    $d = Choose-Device $forced
    $device = $d.Device
    $m = Choose-Model $o['--model'] $device
    $fixes = Ensure-Fixes $data
    Sweep $data
    $relayImage = $env:SWITCHBOARD_RELAY_IMAGE
    $imgVars = @{ SB_CALL_DIR = '/unused'; SB_SERVER = 'https://unused'; SB_FIXES_DIR = '/unused'; SB_RELAY_USER = '0:0'; SB_RELAY_IMAGE = $relayImage }
    $saved = @{}
    foreach ($k in $imgVars.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, [string]$imgVars[$k]) }
    try { $imgs = @(& docker compose -f $compose --profile $device config --images 2>$null) } finally { foreach ($k in $imgVars.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) } }
    if ($LASTEXITCODE -ne 0 -or $imgs.Count -eq 0) { Die 'docker compose could not read meeting/compose.yaml' }
    $missing = @($imgs | Where-Object { -not (Docker-Quiet @('image', 'inspect', $_)) })

    # ---- the link: stdin only, into a file only its owner reads
    $platform = 'unknown'
    $link = ''
    if (-not $noJoin -and -not $o['--from-room']) {
        $link = [Console]::In.ReadLine()
        if ($null -eq $link) { $link = '' }
        $link = $link.Trim()
        if (-not $link) { Die 'the meeting link is read from stdin, and there was none' }
        if ($link -cmatch '[^\x21-\x7e]' -or $link.Length -gt 2000) { Die 'that is not a meeting link' }
        if ($link -cmatch '^https://([A-Za-z0-9-]+\.)*zoom\.us/') { $platform = 'zoom' }
        elseif ($link -cmatch '^https://meet\.google\.com/') { $platform = 'meet' }
        elseif ($link -cmatch '^https://(teams\.microsoft\.com|teams\.live\.com)/') { $platform = 'teams' }
        else { Die 'that is not an https link to a Zoom, Meet or Teams call' }
    }

    # ---- the key, its files, the registration
    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    $random.GetBytes($bytes)
    $random.Dispose()
    $key = 'sbc_' + (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
    $hash = Sha256Hex ([Text.Encoding]::ASCII.GetBytes($key))
    $fp8 = $hash.Substring(0, 8)
    $project = "$ProjectPrefix$fp8"
    if ($missing.Count -gt 0) {
        Err ('Pulling: ' + ($missing -join ' ') + ' (this can take a few minutes)')
        $code = Compose $imgVars @('-f', $compose, '--profile', $device, 'pull')
        if ($code -ne 0) { Die 'could not pull the images' }
    }
    $calls = Join-Path $data 'calls'
    $cdir = Join-Path $calls $fp8
    [void][IO.Directory]::CreateDirectory($calls)
    Make-PrivateDir $calls
    [void][IO.Directory]::CreateDirectory($cdir)
    Make-PrivateDir $cdir
    Write-Private (Join-Path $cdir 'key') $key
    if ($link) { Write-Private (Join-Path $cdir 'link') $link }
    $link = ''
    $body = '{{"room": "#{0}", "key_hash": "{1}", "platform": "{2}", "model": "{3}"}}' -f $room, $hash, $platform, $m.Model
    if ($o['--from-room']) { $body = $body.Substring(0, $body.Length - 1) + ', "from_room": true}' }
    if ((Test-Path -LiteralPath (Join-Path $plugin '.codex-plugin')) -and $cred.StartsWith('sbl_')) {
        $body = $body.Substring(0, $body.Length - 1) + ', "session": "' + $env:CODEX_THREAD_ID + '"}'
    }
    Add-Type -AssemblyName System.Net.Http
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
    $handler = New-Object Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false # a redirect would carry the credential to wherever it points
    $client = New-Object Net.Http.HttpClient $handler
    $client.Timeout = [TimeSpan]::FromSeconds(30)
    $req = New-Object Net.Http.HttpRequestMessage ([Net.Http.HttpMethod]::Post), "$server/mcp/meeting/host"
    [void]$req.Headers.TryAddWithoutValidation('Authorization', "Bearer $cred")
    [void]$req.Headers.TryAddWithoutValidation('User-Agent', 'switchboard-meeting-host')
    $req.Headers.ExpectContinue = $false
    $req.Content = New-Object Net.Http.StringContent $body, (New-Object Text.UTF8Encoding $false), 'application/json'
    $cred = ''
    $status = 0
    $text = ''
    try {
        $res = $client.SendAsync($req).Result
        $status = [int]$res.StatusCode
        $text = [Text.Encoding]::UTF8.GetString($res.Content.ReadAsByteArrayAsync().Result)
    } catch { $status = 0 }
    function JsonField([string]$name) { $mm = [regex]::Match($text, '"' + $name + '":\s*"([^"]*)"'); if ($mm.Success) { $mm.Groups[1].Value } else { '' } }
    if ($status -ne 200) {
        Remove-Item -LiteralPath $cdir -Recurse -Force -ErrorAction SilentlyContinue
        if ($o['--from-room']) { Die "the server refused the room handoff ($status); ask again in the room" }
        if ($status -eq 0) { Die "could not reach the server at $server" }
        if ($status -eq 401) {
            if (Test-Path -LiteralPath (Join-Path $plugin '.codex-plugin')) {
                Die ('the server did not accept the host credential (' + (JsonField 'error') + '): check SWITCHBOARD_AGENT_TOKEN, then ask again')
            }
            Die ('the server did not accept this Claude Code''s credential (' + (JsonField 'error') + '): sign in to switchboard again from Claude Code (/mcp, then Authenticate), then ask again')
        }
        if ($status -eq 429) { Die ('too many tries: wait a minute (' + (JsonField 'message') + ')') }
        Die ("the server refused ($status, " + (JsonField 'error') + '): ' + (JsonField 'message'))
    }
    $theirs = ((JsonField 'fingerprint') -replace '\\u00b7', '') -replace '[^0-9a-f]', ''
    $deviceName = JsonField 'host'
    if ($theirs -cne $fp8) {
        Remove-Item -LiteralPath $cdir -Recurse -Force -ErrorAction SilentlyContinue
        Die 'the server''s fingerprint for this key is not the one made here: not starting. Tell the person; do not retry'
    }
    if ($o['--from-room']) {
        $encoded = JsonField 'link_b64'
        $platform = JsonField 'platform'
        $usable = $encoded -and $encoded.Length -le 2700 -and $platform -cin @('zoom', 'meet', 'teams')
        if ($usable) {
            try {
                $decoded = [Convert]::FromBase64String($encoded)
                $link = (New-Object Text.UTF8Encoding $false, $true).GetString($decoded)
                if (-not $link) { $usable = $false }
            } catch { $usable = $false }
        }
        $encoded = ''; $text = ''
        if (-not $usable) {
            Remove-Item -LiteralPath $cdir -Recurse -Force -ErrorAction SilentlyContinue
            Die 'no usable room handoff returned; update the server first, then ask again in the room'
        }
        Write-Private (Join-Path $cdir 'link') $link
        $link = ''; $decoded = $null
    }
    $override = ''
    if ($addHost) {
        $override = Join-Path $cdir 'override.yaml'
        [IO.File]::WriteAllText($override, "services:`n  relay:`n    extra_hosts: [`"$addHost`"]`n")
    }
    $started = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    [IO.File]::WriteAllText((Join-Path $cdir 'info'), "room=#$room`nplatform=$platform`nfingerprint=$fp8`ndevice=$device`nmodel=$($m.Model)`nproject=$project`nserver=$server`nstarted=$started`n")

    # ---- start it and return
    $user = if ($windows) { '1000:1000' } else { "$(& id -u):$(& id -g)" }
    $vars = @{
        SB_CALL_DIR = $cdir; SB_SERVER = $server; SB_MODEL = $m.Told; SB_FIXES_DIR = $fixes; SB_RELAY_USER = $user
        SB_NO_JOIN = $(if ($noJoin) { '1' } else { '' }); SB_ALLOW_HTTP = $(if ($allowHttp) { '1' } else { '' }); SB_RELAY_IMAGE = $relayImage
    }
    $files = @('-f', $compose)
    if ($override) { $files += @('-f', $override) }
    $code = Compose $vars ($files + @('-p', $project, '--profile', $device, 'up', '-d'))
    if ($code -ne 0) {
        $null = Compose $vars ($files + @('-p', $project, '--profile', $device, 'down', '--timeout', '5'))
        Remove-Item -LiteralPath $cdir -Recurse -Force -ErrorAction SilentlyContinue
        Die 'the containers did not start. The call is registered in the room and ends by itself in ten minutes, or sooner with End call'
    }
    $label = switch ($platform) { 'zoom' { 'Zoom' } 'meet' { 'Meet' } 'teams' { 'Teams' } default { 'call' } }
    $where = First $deviceName 'this machine'
    Say "Hosting #$room ($label) from $where."
    Say ('Key fingerprint: ' + $fp8.Substring(0, 4) + [char]0x00B7 + $fp8.Substring(4, 4) + '. The room''s call bar shows the same; if it does not, stop and tell the person.')
    Say ("Speech: $($device.ToUpperInvariant()), model $($m.Model) ($($d.Why)).")
    Say "It runs in two containers (project $project). To stop it: $Prog stop"
}

if ($args.Count -eq 0) { Usage 'which one: start, stop, status or plan?' }
$rest = @($args | Select-Object -Skip 1)
switch ($args[0]) {
    'start' { Cmd-Start $rest }
    'stop' { Cmd-Stop $rest }
    'status' { Cmd-Status $rest }
    'plan' { Cmd-Plan $rest }
    default { Usage 'which one: start, stop, status or plan?' }
}
exit 0
