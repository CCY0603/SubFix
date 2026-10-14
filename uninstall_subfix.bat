@echo off
rem 双击卸载 SubFix；检测到系统级安装时会自动弹出 UAC 请求提权。
rem 透传所有参数（如 -DryRun / -NeedsAdmin / -AllUsers）。
powershell -ExecutionPolicy Bypass -File "%~dp0uninstall_subfix.ps1" %*
pause
