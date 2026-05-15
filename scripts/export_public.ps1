param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
if (-not $OutputPath) {
    $Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $OutputPath = Join-Path $Root "dist\feishu_bridge_public_$Stamp"
}

$OutputFullPath = [System.IO.Path]::GetFullPath($OutputPath)
$RootFullPath = [System.IO.Path]::GetFullPath($Root)
$skipDirs = @("runtime", "__pycache__", "dist", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv")
$skipNames = @(
    "local_settings.json",
    "local_secrets.json",
    "local_settings.private.json",
    ".env"
)
$skipExtensions = @(".pyc", ".pyo", ".log", ".pid", ".sock", ".png")

New-Item -ItemType Directory -Path $OutputFullPath -Force | Out-Null

Get-ChildItem -LiteralPath $RootFullPath -Recurse -Force | ForEach-Object {
    $full = [System.IO.Path]::GetFullPath($_.FullName)
    if ($full.StartsWith($OutputFullPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return
    }

    $relative = [System.IO.Path]::GetRelativePath($RootFullPath, $full)
    $segments = $relative -split '[\\/]'
    if ($segments | Where-Object { $skipDirs -contains $_ }) {
        return
    }
    if ($skipNames -contains $_.Name) {
        return
    }
    if ((-not $_.PSIsContainer) -and ($skipExtensions -contains $_.Extension.ToLowerInvariant())) {
        return
    }

    $target = Join-Path $OutputFullPath $relative
    if ($_.PSIsContainer) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    } else {
        $targetDir = Split-Path -Parent $target
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
}

Write-Output "exported public package: $OutputFullPath"
