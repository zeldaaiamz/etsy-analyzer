#!/usr/bin/env python3
"""把本地文件 SFTP 到 ECS 指定路径。用法: put_bundle.py LOCAL REMOTE"""
import os, sys, paramiko
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(os.environ.get("ECS_HOST", "47.98.137.124"), 22, os.environ.get("ECS_USER", "root"),
          key_filename=os.environ.get("ECS_KEY", "/home/box/.ssh/aliyun_user.pem"), timeout=20)
s = c.open_sftp(); s.put(sys.argv[1], sys.argv[2]); print("put", os.path.getsize(sys.argv[1]), "->", sys.argv[2]); s.close(); c.close()
