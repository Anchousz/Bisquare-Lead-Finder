@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Bisquare Lead Finder

rem Запуск из исходников. Готовый установщик собирается через: npm run dist
if exist "node_modules\electron\dist\electron.exe" (
  "node_modules\electron\dist\electron.exe" .
  goto :eof
)
echo Ставлю зависимости (нужен интернет)...
call npm install
call npm start
