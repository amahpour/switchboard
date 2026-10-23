# switchboard's headersHelper for Claude Code on Windows (docs/DESIGN.md section 49.11, #499): the PowerShell
# twin of bin/headers, which bin/headers.cmd starts (Claude Code runs a headersHelper with cmd.exe on Windows,
# whatever shell its hooks use). Claude Code runs it each time it connects the plugin's MCP server and sends what
# it prints as headers of that connection. It makes a listener key for this Claude Code and prints only the
# key's sha256, as X-Switchboard-Listener. The key goes to a file in the plugin's data folder
# (listener-key-<Claude Code's pid>) that only the user can read: its access list is the user alone, with no
# inherited rule, set while the file is still empty. listen.ps1 and session.ps1 read it there, and present it
# in place of a token when the agent signed in through the browser (Claude Code keeps that token to itself).
# The server records the hash when a request carries it together with the sign-in's token, so only the
# signed-in connection can make the key count; the key itself never leaves this machine except in those
# scripts' requests to the server, and it opens nothing but their two routes.
# Whatever goes wrong, it prints {} and exits 0: the connection then has no key and the scripts do nothing,
# exactly as before. The data folder is its one argument (Claude Code fills it in on the command line; the
# helper's environment has none of the plugin's settings).
# Claude Code's pid is found from this script's ancestors, since the helper's own CLAUDE_PID is whoever
# started Claude Code's: the first one that is not a shell (seen: claude.exe, then cmd.exe, then this script),
# which is the CLAUDE_PID the hooks get.
# It runs under Windows PowerShell 5.1, which ships with Windows, and under PowerShell 7; elsewhere (the tests
# run it under pwsh on Linux and macOS) the file mode stands in for the access list. Keep this file ASCII: 5.1
# reads a file without a byte-order mark as ANSI.
$ErrorActionPreference = 'Stop'
$tmp = ''
$windows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
function Show([string]$text) {
    $bytes = [Text.Encoding]::ASCII.GetBytes($text + "`n")
    $stdout = [Console]::OpenStandardOutput()
    $stdout.Write($bytes, 0, $bytes.Length)
    $stdout.Flush()
}
function None { if ($tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }; Show '{}'; exit 0 }
trap { None }

function ParentOf([int]$id) { # the pid of the process that started this one, or 0
    if ($windows) {
        if (-not ('SwitchboardParent' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SwitchboardParent {
    [StructLayout(LayoutKind.Sequential)]
    struct Basic { public IntPtr Exit; public IntPtr Peb; public IntPtr Affinity; public IntPtr Priority; public IntPtr Id; public IntPtr Parent; }
    [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("ntdll.dll")] static extern int NtQueryInformationProcess(IntPtr handle, int info, ref Basic basic, int size, out int returned);
    public static int Of(int pid) {
        IntPtr handle = OpenProcess(0x1000, false, pid); // PROCESS_QUERY_LIMITED_INFORMATION
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
        return [SwitchboardParent]::Of($id)
    }
    $got = & ps -o ppid= -p $id 2>$null
    if ($LASTEXITCODE -eq 0 -and "$got".Trim() -cmatch '^[0-9]{1,9}\z') { return [int]"$got".Trim() }
    0
}
function Started($process) { try { $process.StartTime } catch { $null } }
function ClaudePid { # the first ancestor that is not a shell, or 0
    $shells = 'cmd', 'powershell', 'pwsh', 'bash', 'sh', 'dash', 'zsh', 'conhost'
    $child = [Diagnostics.Process]::GetCurrentProcess()
    for ($hops = 0; $hops -lt 8; $hops++) {
        $parent = ParentOf $child.Id
        if ($parent -le 1) { return 0 }
        $found = [Diagnostics.Process]::GetProcessById($parent) # gone: the pid is not the parent's any more
        $born = Started $found
        $mine = Started $child
        if ($born -and $mine -and $born -gt $mine) { return 0 } # a pid that was reused since: not our parent
        if ($shells -notcontains $found.ProcessName.ToLowerInvariant()) { return $parent }
        $child = $found
    }
    0
}
function Alive([int]$id) { try { -not [Diagnostics.Process]::GetProcessById($id).HasExited } catch { $false } }
function MakePrivate([string]$path) { # readable by the user only: 0600, or on Windows an access list of the user alone
    if (-not $windows) {
        & chmod 600 $path
        if ($LASTEXITCODE -ne 0) { throw 'chmod failed' }
        return
    }
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $rules = New-Object Security.AccessControl.FileSecurity
    $rules.SetAccessRuleProtection($true, $false) # no rule is inherited from the folder
    $full = [Security.AccessControl.FileSystemRights]::FullControl
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $rules.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule $me, $full, $allow))
    Set-Acl -LiteralPath $path -AclObject $rules
    # check what is there now, not what was asked for: the user alone, nothing inherited
    $now = Get-Acl -LiteralPath $path
    if (-not $now.AreAccessRulesProtected) { throw 'inherited rules' }
    foreach ($rule in $now.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.IdentityReference.Value -cne $me.Value) { throw 'another account has access' }
    }
}

$dir = $args[0]
if (-not $dir) { $dir = $env:CLAUDE_PLUGIN_DATA }
$home_dir = [Environment]::GetFolderPath('UserProfile')
if (-not $dir -and $home_dir) { $dir = Join-Path (Join-Path $home_dir '.cache') 'switchboard' }
if (-not $dir) { None }
$dir = "$dir".Trim('"')
# a full path only: a relative one would land wherever Claude Code happens to be
if ($windows) { if ($dir -cnotmatch '^([A-Za-z]:[\\/]|[\\/]{2})') { None } } elseif (-not $dir.StartsWith('/')) { None }
$claude = ClaudePid
if ($claude -le 1) { None }
$bytes = New-Object byte[] 32
$random = [Security.Cryptography.RandomNumberGenerator]::Create()
$random.GetBytes($bytes)
$random.Dispose()
$key = 'sbl_' + (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
if ($key.Length -ne 68) { None }
$sha = [Security.Cryptography.SHA256]::Create()
$hash = (($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($key)) | ForEach-Object { $_.ToString('x2') }) -join '')
$sha.Dispose()
if ($hash -cnotmatch '^[0-9a-f]{64}\z') { None }
[void][IO.Directory]::CreateDirectory($dir)
$tmp = Join-Path $dir ('.listener-key.' + [Guid]::NewGuid().ToString('N').Substring(0, 12))
[IO.File]::WriteAllBytes($tmp, [byte[]]@()) # empty until only the user can read it
MakePrivate $tmp
[IO.File]::WriteAllBytes($tmp, [Text.Encoding]::ASCII.GetBytes($key + "`n"))
# a rename, so a reader never sees half a key, and a link planted at the name is replaced, never followed
Move-Item -LiteralPath $tmp -Destination (Join-Path $dir "listener-key-$claude") -Force
$tmp = ''
# the keys of Claude Codes that have gone, so the folder holds only live ones
foreach ($file in [IO.Directory]::GetFiles($dir, 'listener-key-*')) {
    $owner = [IO.Path]::GetFileName($file).Substring('listener-key-'.Length)
    if ($owner -cmatch '^[0-9]{1,9}\z' -and [int]$owner -ne $claude -and -not (Alive ([int]$owner))) {
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }
}
Show ('{"X-Switchboard-Listener": "' + $hash + '"}')
exit 0
