Coding Agent Windows 便携版
===========================

启动方式
--------
双击 “Coding Agent.exe”。

首次启动
--------
首次启动会自动打开设置窗口。请填写：

1. LLM API Key（默认使用 DeepSeek）
2. Embedding API Key（默认使用 DashScope 文本向量模型）
3. 工作区目录（可使用默认目录）

API Key 不会写入安装包，只保存在当前用户的应用程序数据目录中。

数据位置
--------
Electron 用户数据目录，通常位于：

%APPDATA%\Coding Agent

其中包含：

- .env              配置和 API Key
- data\              SQLite 数据库
- workspace\         默认代码工作区
- logs\backend.log   后端运行日志

桌面快捷方式
------------
右键 “Install-Coding-Agent.ps1”，选择“使用 PowerShell 运行”，
即可在桌面创建 Coding Agent 快捷方式。

注意事项
--------
- Windows SmartScreen 可能提示“未知发布者”，这是因为便携版没有购买代码签名证书。
- 本应用是便携版，请保留整个文件夹，不要只移动 exe 文件。
