param(
    [string]$Destination = "",
    [switch]$NoExplorer,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$desktop = [Environment]::GetFolderPath("Desktop")

if ([string]::IsNullOrWhiteSpace($Destination)) {
    $baseDestination = Join-Path $desktop "CodingAgent-GitHub"
    $Destination = $baseDestination
    $suffix = 2
    while (Test-Path -LiteralPath $Destination) {
        $Destination = "$baseDestination-$suffix"
        $suffix++
    }
}

$Destination = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $Destination) {
    throw "Destination already exists: $Destination"
}

New-Item -ItemType Directory -Path $Destination | Out-Null

$items = @(
    ".env.example",
    ".gitignore",
    ".python-version",
    "README.md",
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "uv.lock",
    "prepare-github-upload.cmd",
    "electron",
    "src",
    "tests",
    "scripts",
    "packaging",
    "docs"
)

$copied = @()
$missing = @()

foreach ($item in $items) {
    $sourcePath = Join-Path $projectRoot $item
    if (Test-Path -LiteralPath $sourcePath) {
        Copy-Item -LiteralPath $sourcePath -Destination $Destination -Recurse -Force
        $copied += $item
    } else {
        $missing += $item
    }
}

# Clean only generated caches inside the new upload folder.
Get-ChildItem -LiteralPath $Destination -Directory -Recurse -Force -Filter "__pycache__" |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force

Get-ChildItem -LiteralPath $Destination -File -Recurse -Force |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") -or $_.Name -in @(".DS_Store", "Thumbs.db") } |
    Remove-Item -Force

$forbidden = @(".env", ".venv", ".git", ".idea", "node_modules", "data", "workspace", "logs", "dist")
$foundForbidden = @()
foreach ($name in $forbidden) {
    if (Test-Path -LiteralPath (Join-Path $Destination $name)) {
        $foundForbidden += $name
    }
}
if ($foundForbidden.Count -gt 0) {
    throw "Unsafe files were copied: $($foundForbidden -join ', ')"
}

$leakedEnvFiles = Get-ChildItem -LiteralPath $Destination -File -Recurse -Force |
    Where-Object {
        $_.Name -eq ".env" -or
        ($_.Name -like ".env.*" -and $_.Name -ne ".env.example")
    }
if ($leakedEnvFiles) {
    throw "Environment files were copied: $($leakedEnvFiles.FullName -join ', ')"
}

$fileCount = (Get-ChildItem -LiteralPath $Destination -File -Recurse -Force | Measure-Object).Count

Write-Host ""
Write-Host "GitHub upload folder is ready." -ForegroundColor Green
Write-Host "Location: $Destination"
Write-Host "Files: $fileCount"
Write-Host "Copied: $($copied -join ', ')"
if ($missing.Count -gt 0) {
    Write-Host "Missing optional items: $($missing -join ', ')" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Next steps:"
Write-Host "1. Open GitHub and create an empty repository."
Write-Host "2. Choose Add file -> Upload files."
Write-Host "3. Open the folder above and drag its contents, not the folder itself."
Write-Host "4. Commit the uploaded files."
Write-Host ""
Write-Host "The original project was not modified."
Write-Host ".env, databases, workspace, node_modules and build outputs are excluded."

if (-not $NoExplorer) {
    Start-Process explorer.exe $Destination
}

if (-not $NonInteractive) {
    Write-Host ""
    Write-Host "Press Enter to close this window..."
    Read-Host | Out-Null
}
