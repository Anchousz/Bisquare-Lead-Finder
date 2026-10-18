'use strict';
/**
 * Готовит содержимое установщика:
 *   build/python/  — встроенный Python (embeddable), чтобы приложение работало
 *                    на компьютере без установленного Python;
 *   build/pysrc/   — исходники бэкенда + интерфейс.
 *
 * Ядро сбора написано на чистом stdlib, поэтому embeddable-сборки достаточно:
 * pip-зависимости не нужны. Playwright (анализ сайтов) в установщик не входит.
 *
 * Запуск:  node tools/prepare-build.js
 */
const fs = require('fs');
const path = require('path');
const https = require('https');
const { execFileSync } = require('child_process');

const PY_VER = '3.12.10';
const ROOT = path.join(__dirname, '..');
const BUILD = path.join(ROOT, 'build');
const PY_DIR = path.join(BUILD, 'python');
const SRC_DIR = path.join(BUILD, 'pysrc');
const PY_ZIP = path.join(BUILD, 'python-embed.zip');
const PY_URL = `https://www.python.org/ftp/python/${PY_VER}/python-${PY_VER}-embed-amd64.zip`;

const BACKEND = ['server.py', 'cloud.py', 'leadfinder.py', 'sources.py', 'store.py',
                 'antibot.py', 'cleanup.py'];

function download(url, dest) {
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(dest);
    https.get(url, res => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        file.close(); fs.rmSync(dest, { force: true });
        return download(res.headers.location, dest).then(resolve, reject);
      }
      if (res.statusCode !== 200) { file.close(); return reject(new Error('HTTP ' + res.statusCode)); }
      res.pipe(file);
      file.on('finish', () => file.close(resolve));
    }).on('error', err => { file.close(); fs.rmSync(dest, { force: true }); reject(err); });
  });
}

async function main() {
  fs.mkdirSync(BUILD, { recursive: true });

  if (!fs.existsSync(PY_ZIP)) {
    console.log(`· скачиваю Python ${PY_VER} (embeddable)…`);
    await download(PY_URL, PY_ZIP);
  }
  if (!fs.existsSync(path.join(PY_DIR, 'python.exe'))) {
    console.log('· распаковываю Python…');
    fs.rmSync(PY_DIR, { recursive: true, force: true });
    execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-Command',
      `Expand-Archive -LiteralPath "${PY_ZIP}" -DestinationPath "${PY_DIR}" -Force`], { stdio: 'inherit' });
  }

  // ._pth задаёт весь sys.path embeddable-сборки: каталог скрипта туда НЕ попадает,
  // поэтому явно добавляем соседнюю папку с исходниками.
  const pthName = fs.readdirSync(PY_DIR).find(f => f.endsWith('._pth'));
  if (!pthName) throw new Error('не найден ._pth в embeddable-сборке Python');
  const pthPath = path.join(PY_DIR, pthName);
  let pth = fs.readFileSync(pthPath, 'utf8');
  if (!pth.includes('../pysrc')) {
    pth = pth.replace(/^#\s*import site\s*$/m, 'import site');
    if (!/^import site\s*$/m.test(pth)) pth += '\nimport site';
    pth = pth.trimEnd() + '\n../pysrc\n';
    fs.writeFileSync(pthPath, pth, 'utf8');
    console.log(`· ${pthName} пропатчен (добавлен ../pysrc)`);
  }

  // исходники бэкенда + интерфейс
  fs.rmSync(SRC_DIR, { recursive: true, force: true });
  fs.mkdirSync(SRC_DIR, { recursive: true });
  for (const f of BACKEND) fs.copyFileSync(path.join(ROOT, f), path.join(SRC_DIR, f));
  fs.cpSync(path.join(ROOT, 'web'), path.join(SRC_DIR, 'web'), { recursive: true });

  console.log('· готово: build/python + build/pysrc');
}

main().catch(e => { console.error('ОШИБКА:', e.message); process.exit(1); });
