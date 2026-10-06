#!/usr/bin/env python3
"""Zeabur 入口：运行币安 API 测试，然后保持容器存活。"""
import subprocess
import sys
import time

if __name__ == "__main__":
    print("=" * 60)
    print("Zeabur 容器启动，运行币安 API 测试...")
    print("=" * 60)
    print()

    # 运行测试脚本
    result = subprocess.run([sys.executable, "test_binance.py"], capture_output=False)

    print()
    print(f"测试退出码: {result.returncode}")
    print("保持容器存活 10 分钟供查看日志...")
    time.sleep(600)