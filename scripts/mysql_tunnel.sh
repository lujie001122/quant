#!/bin/bash
# MySQL SSH 隧道 - 自动重连
# 映射本地3307 → 远程3306
# 使用SSH密钥认证（无需密码）
AUTOSSH_PIDFILE=/tmp/autossh_mysql.pid \
autossh -M 0 \
  -o "ServerAliveInterval=15" \
  -o "ServerAliveCountMax=3" \
  -o "StrictHostKeyChecking=no" \
  -o "ExitOnForwardFailure=yes" \
  -L 3307:127.0.0.1:3306 \
  -N \
  root@110.40.168.227 &>/tmp/autossh_mysql.log &
echo "autossh PID: $!"
echo "隧道 127.0.0.1:3307 → 远程 3306"