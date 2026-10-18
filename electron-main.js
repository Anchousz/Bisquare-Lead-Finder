'use strict';
// Electron-обёртка: поднимает локальный Python-бэкенд (server.py) и открывает его
// в отдельном окне-приложении. Никаких вкладок/адресной строки. Бэкенд гасится при выходе.
const { app, BrowserWindow, Menu, shell } = require('electron');
const { spawn } = require('child_process');
const net = require('net');
const http = require('http');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

// Одноразовый секрет на запуск: бэкенд принимает /api/* только с ним.
// Без этого любая открытая в браузере страница могла бы слать POST на 127.0.0.1
// и, например, стереть базу (прочитать ответ она бы не смогла, но испортить — да).
const TOKEN = crypto.randomBytes(24).toString('hex');

let pyProc = null;
let win = null;
let PORT = 0;

// В установленном приложении бэкенд и встроенный Python лежат в resources/,
// в режиме разработки — рядом с этим файлом.
const PACKAGED = app.isPackaged;
const PY_SRC = PACKAGED ? path.join(process.resourcesPath, 'pysrc') : __dirname;
const BUNDLED_PY = PACKAGED ? path.join(process.resourcesPath, 'python', 'python.exe') : null;

/* свободный порт: сначала пробуем 8770, иначе любой свободный */
function pickPort(preferred) {
  return new Promise(resolve => {
    const tryAny = () => {
      const s = net.createServer();
      s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
    };
    const s = net.createServer();
    s.once('error', tryAny);
    s.listen(preferred, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

/* Кандидаты Python, без привязки к конкретной машине:
   переменная окружения -> встроенный в установщик -> venv рядом с проектом -> системный. */
function pythonCandidates() {
  const list = [];
  if (process.env.BISQUARE_PYTHON) list.push(process.env.BISQUARE_PYTHON);
  if (BUNDLED_PY && fs.existsSync(BUNDLED_PY)) list.push(BUNDLED_PY);
  for (const rel of [['.venv', 'Scripts', 'python.exe'], ['venv', 'Scripts', 'python.exe'],
                     ['.venv', 'bin', 'python3'], ['venv', 'bin', 'python3']]) {
    const p = path.join(PY_SRC, ...rel);
    if (fs.existsSync(p)) list.push(p);
  }
  if (process.platform === 'win32') list.push('py', 'python', 'python3');
  else list.push('python3', 'python');
  return list;
}

function spawnPython(cands, args, onReady, onFail) {
  if (!cands.length) return onFail(new Error('Python не найден'));
  const cmd = cands[0];
  let proc;
  const env = Object.assign({}, process.env, {
    PYTHONIOENCODING: 'utf-8',
    PYTHONDONTWRITEBYTECODE: '1',     // в Program Files писать .pyc всё равно нельзя
    BISQUARE_TOKEN: TOKEN,
    // версия сборки — сервер может не пустить к поиску устаревшее приложение
    BISQUARE_APP_VERSION: app.getVersion(),
  });
  // В установленном приложении рядом с кодом писать нельзя (Program Files), поэтому база и
  // настройки уезжают в пользовательскую папку. В разработке данные остаются в проекте.
  if (PACKAGED) env.BISQUARE_DATA_DIR = app.getPath('userData');
  try { proc = spawn(cmd, args, { cwd: PY_SRC, env }); }
  catch (e) { return spawnPython(cands.slice(1), args, onReady, onFail); }
  let failed = false;
  proc.on('error', () => { if (!failed) { failed = true; spawnPython(cands.slice(1), args, onReady, onFail); } });
  if (proc.stdout) proc.stdout.on('data', d => process.stdout.write('[py] ' + d));
  if (proc.stderr) proc.stderr.on('data', d => process.stderr.write('[py] ' + d));
  setTimeout(() => { if (!failed) onReady(proc, cmd); }, 500);
}

function waitReady(port, cb, tries) {
  const req = http.get({ host: '127.0.0.1', port, path: '/api/state', timeout: 800 }, r => { r.resume(); cb(null); });
  req.on('error', () => { if (tries <= 0) cb(new Error('timeout')); else setTimeout(() => waitReady(port, cb, tries - 1), 300); });
  req.on('timeout', () => req.destroy());
}

function screen(msg) {
  // charset=utf-8 обязателен: без него браузер читает русские буквы как Latin-1
  // и вместо «запуск…» показывает «Ð·Ð°Ð¿ÑƒÑÐº…». <meta> внутри — на случай,
  // если тип из data-URL где-то потеряется.
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(
    '<meta charset="utf-8">' +
    '<body style="margin:0;background:#0a0b0d;color:#8b919e;font:14px \'Segoe UI\',sans-serif;' +
    'display:grid;place-items:center;height:100vh;letter-spacing:.04em">' +
    '<div style="text-align:center"><div style="font-size:12px;letter-spacing:.2em;color:#e2e4e8">BISQUARE · LEAD FINDER</div>' +
    '<div style="margin-top:14px">' + msg + '</div></div></body>');
}

function createWindow() {
  win = new BrowserWindow({
    width: 1180, height: 780, minWidth: 940, minHeight: 600,
    backgroundColor: '#0a0b0d', title: 'Bisquare · Lead Finder',
    // autoHideMenuBar только ПРЯЧЕТ меню, но оно всплывает по Alt — из-за этого
    // Alt+Shift (переключение языка) открывал панель окна. Меню нам не нужно
    // совсем, поэтому убираем его целиком: см. Menu.setApplicationMenu(null) ниже.
    autoHideMenuBar: true, show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  win.loadURL(screen('запуск…'));
  win.once('ready-to-show', () => win.show());
  // Календарь и CRM — разделы внутри окна. Наружу уходят только внешние ссылки
  // (сайты лидов) — в системный браузер.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.on('closed', () => { win = null; });
}

async function boot() {
  // Полностью снимаем меню окна. Без этого Alt и Alt+Shift (обычное переключение
  // раскладки) открывали системную панель поверх интерфейса.
  Menu.setApplicationMenu(null);
  createWindow();
  PORT = await pickPort(8770);
  spawnPython(pythonCandidates(),
    [path.join(PY_SRC, 'server.py'), '--no-open', '--port', String(PORT)],
    proc => {
      pyProc = proc;
      waitReady(PORT, err => {
        if (!win) return;
        const url = 'http://127.0.0.1:' + PORT + '/?t=' + TOKEN;
        if (!err) console.log('[bisquare] UI:', url);
        win.loadURL(err ? screen('не удалось поднять сервер — проверь Python 3') : url);
      }, 80);
    },
    () => { if (win) win.loadURL(screen('Python 3 не найден — установи и перезапусти')); }
  );
}

function killPy() {
  if (pyProc && !pyProc.killed) {
    try {
      if (process.platform === 'win32') spawn('taskkill', ['/pid', String(pyProc.pid), '/f', '/t']);
      else pyProc.kill('SIGTERM');
    } catch (e) {}
    pyProc = null;
  }
}

app.whenReady().then(boot);
app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) boot(); });
app.on('window-all-closed', () => { killPy(); if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', killPy);
process.on('exit', killPy);
