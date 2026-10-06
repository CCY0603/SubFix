@echo off
rem 以管理员身份运行可部署到全用户目录；普通运行部署到当前用户。
powershell -ExecutionPolicy Bypass -File "%~dp0install_subfix.ps1" %*
pause
