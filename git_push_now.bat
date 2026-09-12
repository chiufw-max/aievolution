@echo off
cd /d "%~dp0"
set GIT="C:\Program Files\Git\cmd\git.exe"
if not exist %GIT% set GIT=git

REM 讀取 token（存放於 .token 檔案，不進 git）
set /p TOKEN=<.token

REM 清除舊的 git 歷史（避免 GitHub Push Protection 阻擋）
if exist ".git" rmdir /s /q ".git"

%GIT% init
%GIT% branch -M main
%GIT% remote add origin https://chiufw-max:%TOKEN%@github.com/chiufw-max/aievolution.git
%GIT% config user.email "chiufw@gmail.com"
%GIT% config user.name "chiufw-max"
%GIT% add index.html weekly.html education.html medical_news_data.json update_medical_news.py vercel.json robots.txt sitemap.xml git_push_now.bat .gitignore logo.jpg logo2.jpg logo3.jpg og_facebook.jpg education/painless-endoscopy.html education/masld.html
%GIT% commit -m "refactor: 衛教頁改為正式版，移除即將上線，改為文章卡片列表"
%GIT% push origin main --force
if %ERRORLEVEL% == 0 (
    echo Push OK! https://github.com/chiufw-max/aievolution
    timeout /t 3 > nul
) else (
    echo Push FAILED - please screenshot this window
    pause
)
