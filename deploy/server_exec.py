#!/usr/bin/env python3
"""在阿里云 ECS 上执行 stdin 中的 shell 脚本（box 上没有 openssh，用 paramiko）。
环境变量：ECS_HOST（默认 47.98.137.124）、ECS_USER（root）、ECS_KEY（/home/box/.ssh/aliyun_user.pem）。"""
import os, sys
import paramiko
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(os.environ.get("ECS_HOST", "47.98.137.124"), int(os.environ.get("ECS_PORT", "22")),
          os.environ.get("ECS_USER", "root"), key_filename=os.environ.get("ECS_KEY", "/home/box/.ssh/aliyun_user.pem"), timeout=20)
_, o, e = c.exec_command(sys.stdin.read(), timeout=600)
sys.stdout.write(o.read().decode(errors="replace")); sys.stderr.write(e.read().decode(errors="replace"))
sys.exit(o.channel.recv_exit_status())
