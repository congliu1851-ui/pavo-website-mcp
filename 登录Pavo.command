#!/bin/zsh
cd -- "${0:A:h}" || exit 1
.venv/bin/python -m pavo_website_mcp.login
login_result=$?
if (( login_result == 0 )); then
  print '小主，登录已完成，可以回到 Codex 继续联调。'
else
  print '小主，登录尚未完成，可以重新打开此文件重试。'
fi
read -r 'pavo_exit?按回车关闭此窗口。'
