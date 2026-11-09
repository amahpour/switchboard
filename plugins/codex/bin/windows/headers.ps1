# Codex HTTP header helper (docs/DESIGN.md section 49.11, #637).
# A local private key, its hash on the authenticated MCP connection, no credential-store reads.
# The inherited-rule-free access list is installed before writing a secret, as in Claude's helper.
# Keep ASCII and Windows PowerShell 5.1 compatibility.
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
function CodexPid { # the first ancestor that is not a shell, or 0
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
if (-not $dir) { $dir = $env:SWITCHBOARD_PLUGIN_DATA }
$home_dir = [Environment]::GetFolderPath('UserProfile')
if (-not $dir -and $home_dir) { $dir = Join-Path (Join-Path $home_dir '.cache') 'switchboard' }
if (-not $dir) { None }
$dir = "$dir".Trim('"')
# a full path only: a relative one would land wherever Codex happens to be
if ($windows) { if ($dir -cnotmatch '^([A-Za-z]:[\\/]|[\\/]{2})') { None } } elseif (-not $dir.StartsWith('/')) { None }
$codex = CodexPid
if ($codex -le 1) { None }
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
Move-Item -LiteralPath $tmp -Destination (Join-Path $dir "codex-listener-key-$codex") -Force
$tmp = ''
# the keys of Codexs that have gone, so the folder holds only live ones
foreach ($file in [IO.Directory]::GetFiles($dir, 'codex-listener-key-*')) {
    $owner = [IO.Path]::GetFileName($file).Substring('codex-listener-key-'.Length)
    if ($owner -cmatch '^[0-9]{1,9}\z' -and [int]$owner -ne $codex -and -not (Alive ([int]$owner))) {
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }
}
Show ('{"X-Switchboard-Listener": "' + $hash + '"}')
exit 0
