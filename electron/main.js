const { app, BrowserWindow, ipcMain, dialog, Menu } = require('electron');
const { spawn, execFile } = require('child_process');
const fs = require('fs');
const net = require('net');
const path = require('path');

app.setName('Coding Agent');
app.setAppUserModelId('com.codingagent.desktop');

let mainWindow;
let pythonProcess;
let backendPort;
let backendLogPath;
let backendLogStream;
let shuttingDown = false;

const gotSingleInstanceLock =
  process.env.CODING_AGENT_SMOKE_TEST === '1' || app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
}

function getRuntimePaths() {
  if (app.isPackaged) {
    const resources = process.resourcesPath;
    return {
      python: path.join(resources, 'runtime', 'python.exe'),
      backendRoot: path.join(resources, 'backend'),
      sourceRoot: path.join(resources, 'backend', 'src'),
      sitePackages: path.join(resources, 'runtime', 'site-packages'),
      appHome: process.env.CODING_AGENT_HOME || app.getPath('userData')
    };
  }

  const projectRoot = path.join(__dirname, '..');
  return {
    python: path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
    backendRoot: projectRoot,
    sourceRoot: path.join(projectRoot, 'src'),
    sitePackages: path.join(projectRoot, '.venv', 'Lib', 'site-packages'),
    appHome: projectRoot
  };
}

function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = address.port;
      server.close(() => resolve(port));
    });
  });
}

function appendBackendLog(chunk, prefix = '') {
  const text = chunk.toString();
  if (prefix) console[prefix](text.trimEnd());
  if (backendLogStream) backendLogStream.write(text);
}

function stopPythonProcess() {
  if (backendLogStream) {
    backendLogStream.end();
    backendLogStream = null;
  }
  if (!pythonProcess || pythonProcess.killed) return;

  const pid = pythonProcess.pid;
  pythonProcess = null;
  execFile('taskkill', ['/PID', String(pid), '/T', '/F'], () => {});
}

async function startBackend() {
  const paths = getRuntimePaths();
  fs.mkdirSync(paths.appHome, { recursive: true });
  fs.mkdirSync(path.join(paths.appHome, 'logs'), { recursive: true });

  backendLogPath = path.join(paths.appHome, 'logs', 'backend.log');
  backendLogStream = fs.createWriteStream(backendLogPath, { flags: 'a' });
  backendLogStream.write(`\n\n=== ${new Date().toISOString()} ===\n`);

  backendPort = await findFreePort();
  const env = {
    ...process.env,
    CODING_AGENT_HOME: paths.appHome,
    CODING_AGENT_PACKAGED: app.isPackaged ? '1' : '0',
    PYTHONPATH: [paths.sourceRoot, paths.sitePackages].filter(Boolean).join(path.delimiter),
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    PYTHONDONTWRITEBYTECODE: '1'
  };

  pythonProcess = spawn(
    paths.python,
    [
      '-m', 'uvicorn',
      'app.api.main:app',
      '--host', '127.0.0.1',
      '--port', String(backendPort)
    ],
    {
      cwd: paths.backendRoot,
      env,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe']
    }
  );

  pythonProcess.stdout.on('data', data => appendBackendLog(data, 'log'));
  pythonProcess.stderr.on('data', data => appendBackendLog(data, 'error'));
  pythonProcess.on('error', error => appendBackendLog(`Failed to start backend: ${error}\n`, 'error'));
  pythonProcess.on('exit', (code, signal) => {
    appendBackendLog(`Backend exited: code=${code}, signal=${signal}\n`, 'log');
    pythonProcess = null;
    if (!shuttingDown && mainWindow) {
      dialog.showErrorBox(
        '后端已停止',
        `Coding Agent 后端意外退出。\n日志：${backendLogPath}`
      );
    }
  });

  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (!pythonProcess) {
      throw new Error(`后端启动失败，请查看日志：${backendLogPath}`);
    }
    try {
      const response = await fetch(`http://127.0.0.1:${backendPort}/api/health`);
      if (response.ok) return;
    } catch (_) {
      // 后端仍在初始化
    }
    await new Promise(resolve => setTimeout(resolve, 400));
  }

  throw new Error(`等待后端启动超时，请查看日志：${backendLogPath}`);
}

async function createWindow(showWindow = true) {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 900,
    minHeight: 640,
    title: 'Coding Agent',
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true
    }
  });

  if (showWindow) {
    mainWindow.once('ready-to-show', () => mainWindow.show());
  }
  mainWindow.on('closed', () => { mainWindow = null; });
  await mainWindow.loadURL(`http://127.0.0.1:${backendPort}`);
}

app.on('second-instance', () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
});

app.whenReady().then(async () => {
  Menu.setApplicationMenu(null);
  try {
    await startBackend();
    if (process.env.CODING_AGENT_SMOKE_TEST === '1') {
      const paths = getRuntimePaths();
      const healthResponse = await fetch(`http://127.0.0.1:${backendPort}/api/health`);
      await createWindow(false);
      const pageTitle = await mainWindow.webContents.executeJavaScript('document.title');
      const pageText = await mainWindow.webContents.executeJavaScript('document.body.innerText');
      if (!healthResponse.ok || pageTitle !== 'Coding Agent' || !pageText.includes('New Chat')) {
        throw new Error('打包应用健康检查失败。');
      }
      fs.writeFileSync(
        path.join(paths.appHome, 'smoke-test-ok.txt'),
        JSON.stringify({
          health: await healthResponse.json(),
          pageTitle,
          pageTextBytes: pageText.length,
          timestamp: new Date().toISOString()
        }, null, 2),
        'utf8'
      );
      app.quit();
      return;
    }
    await createWindow();
  } catch (error) {
    dialog.showErrorBox('Coding Agent 启动失败', String(error));
    app.quit();
  }

  ipcMain.handle('select-directory', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: '选择工作区目录',
      properties: ['openDirectory', 'createDirectory']
    });
    if (result.canceled) return null;
    return result.filePaths[0];
  });
});

app.on('before-quit', () => {
  shuttingDown = true;
  stopPythonProcess();
});

app.on('window-all-closed', () => {
  app.quit();
});
