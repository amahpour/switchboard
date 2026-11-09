# Stable Codex HTTP helper (DESIGN.md section 49.11, #665). No runtime environment executable paths.
# Windows PowerShell 5.1, ASCII. --install copies this and its cmd launcher beside the versioned cache.
$ErrorActionPreference = 'Stop'
$windows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
function Fail([string]$message) { [Console]::Error.WriteLine("codex-headers: $message"); exit 1 }
function None { [Console]::Error.WriteLine('codex-headers: no checked switchboard helper in the plugin cache; update the plugin'); [Console]::Out.WriteLine('{}'); exit 0 }
function Safe([string]$path) {
    try {
        $item = Get-Item -LiteralPath $path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        if (-not $windows) {
            $info = & stat -c '%u %a' $path 2>$null
            if ($LASTEXITCODE -ne 0) { $info = & stat -f '%u %Lp' $path 2>$null }
            $parts = "$info".Split(' ')
            return ($parts[0] -eq (& id -u)) -and (([Convert]::ToInt32($parts[1], 8) -band 18) -eq 0)
        }
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $acl = Get-Acl -LiteralPath $path
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $me) { return $false }
        $trusted = @($me, 'S-1-5-18', 'S-1-5-32-544') # the account, SYSTEM and Administrators
        $write = [Security.AccessControl.FileSystemRights]'Write, Delete, ChangePermissions, TakeOwnership'
        foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and ($rule.FileSystemRights -band $write) -and $trusted -notcontains $rule.IdentityReference.Value) { return $false }
        }
        return $true
    } catch { return $false }
}
try {
    $install = $args.Count -gt 0 -and $args[0] -ceq '--install'
    $dir = $PSScriptRoot
    $cache = if ($install) { Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $dir))) } else { $dir }
    $normalized = $cache.Replace('\', '/')
    if (-not $normalized.EndsWith('/.codex/plugins/cache/switchboard')) { Fail 'expected the personal .codex plugin cache' }
    $homeDir = $cache.Substring(0, $cache.Length - '/.codex/plugins/cache/switchboard'.Length)
    $p = $dir
    while ($true) {
        if (-not (Safe $p)) { Fail 'the plugin cache must be owned by you, without links or shared write access' }
        if ($p -eq $homeDir) { break }
        $p = Split-Path -Parent $p
        if (-not $p) { Fail 'invalid cache path' }
    }
    if (-not (Safe $PSCommandPath)) { Fail 'unsafe launcher' }
    if ($install) {
        $pairs = @(@($PSCommandPath, (Join-Path $cache 'codex-headers.ps1')), @((Join-Path (Split-Path -Parent $dir) 'headers-launcher.cmd'), (Join-Path $cache 'codex-headers.cmd')))
        foreach ($pair in $pairs) {
            if (-not (Safe $pair[0])) { Fail 'unsafe launcher source' }
            if ((Test-Path -LiteralPath $pair[1]) -and -not (Safe $pair[1])) { Fail 'unsafe launcher destination' }
        }
        foreach ($pair in $pairs) { Copy-Item -LiteralPath $pair[0] -Destination $pair[1] -Force }
        [Console]::Out.WriteLine('Installed stable helper: ' + (Join-Path $cache 'codex-headers.cmd'))
        [Console]::Out.WriteLine('Add its quoted absolute path to http_headers_helper yourself; restart Codex.')
        exit 0
    }
    $plugins = Join-Path $cache 'switchboard'
    if (-not (Safe $plugins)) { None }
    $versions = Get-ChildItem -LiteralPath $plugins -Directory | Where-Object { $_.Name -cmatch '^[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}\z' } | Sort-Object { [version]$_.Name } -Descending
    foreach ($entry in $versions) {
        $root = $entry.FullName
        $meta = Join-Path $root '.codex-plugin'
        $manifest = Join-Path $meta 'plugin.json'
        $bin = Join-Path $root 'bin'
        $win = Join-Path $bin 'windows'
        $helper = Join-Path $win 'headers.ps1'
        $checked = $true
        foreach ($path in @($root, $meta, $manifest, $bin, $win, $helper)) { if (-not (Safe $path)) { $checked = $false; break } }
        if (-not $checked -or (Get-Item -LiteralPath $manifest).Length -gt 16384) { continue }
        try { $data = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json } catch { continue }
        if ($data.name -cne 'switchboard' -or $data.version -cne $entry.Name) { continue }
        & $helper @args
        exit 0
    }
    None
} catch { if ($install) { Fail 'could not install launcher' }; None }
