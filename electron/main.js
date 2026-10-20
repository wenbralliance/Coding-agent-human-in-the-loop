const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const { spawn, execSync } = require('child_process');
const path = require('path');

let mainWindow;
let pythonProcess;

function killPort8000() {
    try {
        // 查找占用 8000 端口的进程
        const output = execSync('netstat -ano | findstr :8000').toString();
        const lines = output.split('\n');
        for (const line of lines) {
            const parts = line.trim().split(/\s+/);
            if (parts.length > 4 && parts[1].endsWith(':8000')) {
                const pid = parts[4];
                // 杀掉它
                execSync(`taskkill /PID ${pid} /F`);
                console.log(`Killed existing process on port 8000 (PID: ${pid})`);
            }
        }
    } catch (e) {
        // 没找到占用端口的进程，忽略错误
    }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    title: "Coding Agent",
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true
    }
  });
  mainWindow.loadURL('http://127.0.0.1:8000');
}

app.whenReady().then(() => {
  // 启动前先清理端口
  killPort8000();

  // 给系统一点时间释放端口
  setTimeout(() => {
      pythonProcess = spawn('uv', ['run', 'uvicorn', 'app.api.main:app', '--host', '127.0.0.1', '--port', '8000'], {
        cwd: path.join(__dirname, '..'),
        shell: true,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }
      });

      pythonProcess.stdout.on('data', (data) => console.log(`[Python] ${data}`));
      pythonProcess.stderr.on('data', (data) => console.error(`[Python Error] ${data}`));

      // 等待启动后加载网页
      setTimeout(createWindow, 2500);
  }, 500);

  ipcMain.handle('select-directory', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: '选择工作区目录',
      properties: ['openDirectory', 'createDirectory']
    });
    if (result.canceled) return null;
    return result.filePaths[0];
  });
});

app.on('window-all-closed', () => {
  if (pythonProcess) {
    // 在 Windows 下，直接 kill 可能杀不掉子进程，用 taskkill
    try {
        execSync(`taskkill /PID ${pythonProcess.pid} /T /F`);
    } catch (e) {}
  }
  app.quit();
});