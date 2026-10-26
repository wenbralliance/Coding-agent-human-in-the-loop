$ErrorActionPreference = "Stop"

$appExe = Join-Path $PSScriptRoot "Coding Agent.exe"
if (-not (Test-Path -LiteralPath $appExe)) {
    throw "未找到 Coding Agent.exe，请保持目录完整。"
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Coding Agent.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $appExe
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = "Coding Agent"
$shortcut.Save()

Write-Host "已创建桌面快捷方式：$shortcutPath" -ForegroundColor Green
