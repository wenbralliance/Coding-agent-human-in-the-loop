param(
    [string]$PackageName = "CodingAgent-Windows-x64"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$distRoot = Join-Path $projectRoot "dist"
$packageDir = Join-Path $distRoot $PackageName
$zipPath = Join-Path $distRoot "$PackageName.zip"
$electronDist = Join-Path $projectRoot "node_modules\electron\dist"
$venvConfig = Join-Path $projectRoot ".venv\pyvenv.cfg"
$sitePackages = Join-Path $projectRoot ".venv\Lib\site-packages"

if (-not (Test-Path -LiteralPath (Join-Path $electronDist "electron.exe"))) {
    throw "未找到 Electron 运行时，请先执行 npm install。"
}
if (-not (Test-Path -LiteralPath $venvConfig)) {
    throw "未找到 .venv\pyvenv.cfg。"
}

$homeLine = Get-Content -LiteralPath $venvConfig -Encoding utf8 |
    Where-Object { $_ -match '^\s*home\s*=\s*(.+)$' } |
    Select-Object -First 1
if (-not $homeLine -or $homeLine -notmatch '^\s*home\s*=\s*(.+)$') {
    throw "无法从 pyvenv.cfg 读取基础 Python 路径。"
}
$basePython = $Matches[1].Trim()
if (-not (Test-Path -LiteralPath (Join-Path $basePython "python.exe"))) {
    throw "基础 Python 不存在：$basePython"
}

New-Item -ItemType Directory -Force -Path $distRoot | Out-Null
if (Test-Path -LiteralPath $packageDir) {
    $resolvedPackage = (Resolve-Path -LiteralPath $packageDir).Path
    $resolvedDist = (Resolve-Path -LiteralPath $distRoot).Path
    if (-not $resolvedPackage.StartsWith($resolvedDist, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理 dist 目录以外的路径：$resolvedPackage"
    }
    Remove-Item -LiteralPath $resolvedPackage -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $packageDir | Out-Null

Write-Host "1/6 复制 Electron..."
Copy-Item -Path (Join-Path $electronDist "*") -Destination $packageDir -Recurse -Force
$electronExe = Join-Path $packageDir "electron.exe"
$appExe = Join-Path $packageDir "Coding Agent.exe"
Rename-Item -LiteralPath $electronExe -NewName "Coding Agent.exe"

$resourcesDir = Join-Path $packageDir "resources"
$defaultApp = Join-Path $resourcesDir "default_app.asar"
if (Test-Path -LiteralPath $defaultApp) {
    Remove-Item -LiteralPath $defaultApp -Force
}

Write-Host "2/6 复制 Electron 应用..."
$appDir = Join-Path $resourcesDir "app"
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot "package.json") -Destination $appDir
Copy-Item -LiteralPath (Join-Path $projectRoot "electron") -Destination $appDir -Recurse

Write-Host "3/6 复制 Python 后端..."
$backendDir = Join-Path $resourcesDir "backend"
$backendSourceDir = Join-Path $backendDir "src"
New-Item -ItemType Directory -Force -Path $backendSourceDir | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot "src\app") -Destination $backendSourceDir -Recurse

Write-Host "4/6 复制内置 Python 与依赖..."
$runtimeDir = Join-Path $resourcesDir "runtime"
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
Copy-Item -Path (Join-Path $basePython "*") -Destination $runtimeDir -Recurse -Force
$runtimeSitePackages = Join-Path $runtimeDir "site-packages"
Copy-Item -LiteralPath $sitePackages -Destination $runtimeSitePackages -Recurse

# 原虚拟环境的 .pth 会指向开发机路径，运行时通过 PYTHONPATH 指定内置目录。
Get-ChildItem -LiteralPath $runtimeSitePackages -File -Filter "*.pth" |
    Remove-Item -Force

Write-Host "5/6 添加快捷方式和说明..."
Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\README.txt") -Destination $packageDir
Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\Install-Coding-Agent.ps1") -Destination $packageDir

Get-ChildItem -LiteralPath $packageDir -Directory -Recurse -Filter "__pycache__" |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $packageDir -File -Recurse -Include "*.pyc", "*.pyo" |
    Remove-Item -Force

if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

Write-Host "6/6 生成压缩包..."
Compress-Archive -LiteralPath $packageDir -DestinationPath $zipPath -CompressionLevel Optimal

Write-Host ""
Write-Host "构建完成：$appExe" -ForegroundColor Green
Write-Host "压缩包：$zipPath" -ForegroundColor Green
