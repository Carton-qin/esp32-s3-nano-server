#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP32 Nano-Server & 自动化打卡中枢 - 一键部署工具 (Deploy Tool)
支持 ESP32-S3 / ESP32-C3 / 经典 ESP32 全系列开发板（Windows / macOS / Linux）
"""

import os
import sys
import time
import argparse
import subprocess

def print_banner():
    print("""
============================================================
   🚀 ESP32 / ESP32-S3 / ESP32-C3 Nano-Server 一键部署工具
============================================================
""")

def find_serial_ports():
    """自动扫描检测系统中连接的串口"""
    try:
        import serial.tools.list_ports
        ports = list(serial.tools.list_ports.comports())
        esp_ports = []
        for p in ports:
            desc = p.description.lower()
            hwid = p.hwid.lower()
            if any(k in desc or k in hwid for k in ["ch340", "cp210", "usb-serial", "jtag", "espressif", "uart"]):
                esp_ports.append(p.device)
            else:
                esp_ports.append(p.device)
        return esp_ports
    except ImportError:
        return []

def run_cmd(cmd, desc=None, check=True):
    if desc:
        print(f"[*] {desc}...", flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode != 0:
        print(f"[!] 执行失败: {' '.join(cmd)}")
        if res.stderr:
            print(f"[!] 错误输出:\n{res.stderr.strip()}")
        return False
    return True

def detect_chip(port):
    """自动通过 esptool 识别连接的芯片架构 (ESP32-S3 / ESP32-C3 / ESP32)"""
    print(f"[*] 正在自动检测 {port} 连接的芯片型号...", flush=True)
    cmd = [sys.executable, "-m", "esptool", "--port", port, "chip_id"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = (res.stdout + res.stderr).lower()

    if "esp32-s3" in out:
        print("[+] 识别芯片架构: ESP32-S3 (Xtensa 双核)")
        return "esp32s3"
    elif "esp32-c3" in out:
        print("[+] 识别芯片架构: ESP32-C3 (RISC-V 单核)")
        return "esp32c3"
    elif "esp32-c6" in out:
        print("[+] 识别芯片架构: ESP32-C6")
        return "esp32c6"
    elif "esp32-s2" in out:
        print("[+] 识别芯片架构: ESP32-S2")
        return "esp32s2"
    elif "esp32" in out:
        print("[+] 识别芯片架构: ESP32 (经典双核)")
        return "esp32"
    else:
        print("[*] 未能精准识别芯片，将交由 esptool 自动协商")
        return "auto"

def flash_micropython(port, chip_type, bin_path):
    """刷写 MicroPython 固件"""
    if not os.path.exists(bin_path):
        print(f"[!] 找不到固件文件: {bin_path}")
        return False

    print(f"\n[*] 正在准备为 {chip_type.upper()} 烧录 MicroPython 固件: {bin_path}")
    print("[*] 步骤 1/2: 擦除 Flash 闪存...")
    chip_arg = ["--chip", chip_type] if chip_type != "auto" else []
    erase_cmd = [sys.executable, "-m", "esptool"] + chip_arg + ["--port", port, "erase_flash"]
    if not run_cmd(erase_cmd, "擦除芯片闪存"):
        return False

    print("[*] 步骤 2/2: 写入固件...")
    offset = "0" if chip_type in ("esp32s3", "esp32c3", "esp32c6", "esp32s2") else "0x1000"
    write_cmd = [
        sys.executable, "-m", "esptool"
    ] + chip_arg + [
        "--port", port,
        "--baud", "460800",
        "write_flash", "-z", offset, bin_path
    ]
    if not run_cmd(write_cmd, f"写入 MicroPython 固件 (基地址: {offset})"):
        return False

    print("[+] 固件烧录成功！等待芯片冷启动 (3秒)...")
    time.sleep(3)
    return True

def upload_project(port):
    """上传项目源码及静态资源到开发板"""
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "root")
    if not os.path.exists(base_dir):
        print(f"[!] 找不到源码目录: {base_dir}")
        return False

    print(f"\n[*] 正在连接串口 {port} 并准备上传项目代码...")
    
    # 先做一次软复位并暂停执行，确保看门狗不会在上传静态大文件时超时
    print("[*] 复位开发板进入安全 REPL 状态...")
    subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "reset"], capture_output=True)
    time.sleep(1.5)

    # 1. 确保远程文件夹结构就绪
    remote_dirs = [":app", ":microdot", ":static", ":data", ":data/backups"]
    for rd in remote_dirs:
        subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "fs", "mkdir", rd], capture_output=True)

    # 2. 收集本地文件列表并规划上传顺序（main.py 必须在最后上传）
    file_queue = []
    for root, _, files in os.walk(base_dir):
        for f in files:
            if f.endswith(".pyc") or "__pycache__" in root:
                continue
            local_fp = os.path.join(root, f)
            rel = os.path.relpath(local_fp, base_dir).replace("\\", "/")
            remote_fp = f":{rel}"
            file_queue.append((local_fp, remote_fp, rel))

    # 将 main.py 挪至队列最末尾
    file_queue.sort(key=lambda x: (x[2] == "main.py", x[2] == "boot.py", x[2]))

    total = len(file_queue)
    print(f"[*] 共发现 {total} 个核心文件，开始逐项写入 Flash 闪存:")
    for idx, (local_fp, remote_fp, rel) in enumerate(file_queue, 1):
        fsize = os.path.getsize(local_fp)
        size_str = f"{fsize / 1024:.1f} KB" if fsize > 1024 else f"{fsize} B"
        print(f"  [{idx}/{total}] 正在同步 -> {remote_fp} ({size_str}) ...", end="", flush=True)
        t0 = time.time()
        res = subprocess.run(
            [sys.executable, "-m", "mpremote", "connect", port, "fs", "cp", local_fp, remote_fp],
            capture_output=True,
            text=True
        )
        if res.returncode != 0:
            print(" ❌ 失败！")
            print(f"[!] 上传文件 {local_fp} 失败:\n{res.stderr.strip()}")
            return False
        dt = time.time() - t0
        print(f" 耗时 {dt:.1f}s")

    print("\n[*] 全部核心文件同步完成！正在重启开发板...")
    subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "reset"])
    print("[+] 部署圆满完成！开发板已重启并运行新系统。")
    return True

def main():
    print_banner()
    parser = argparse.ArgumentParser(description="ESP32 / ESP32-S3 / ESP32-C3 Nano-Server 一键部署工具")
    parser.add_argument("--port", "-p", help="指定开发板串口号 (例如: COM7, /dev/ttyUSB0)")
    parser.add_argument("--flash", "-f", action="store_true", help="是否从零烧录 MicroPython 固件")
    parser.add_argument("--bin", "-b", help="MicroPython 固件路径 (.bin)")
    args = parser.parse_args()

    # 1. 检查串口
    port = args.port
    if not port:
        ports = find_serial_ports()
        if not ports:
            print("[!] 未自动检测到串口，请通过 --port 指定，例如：python deploy.py --port COM7")
            sys.exit(1)
        elif len(ports) == 1:
            port = ports[0]
            print(f"[*] 自动选择检测到的串口: {port}")
        else:
            print("[*] 检测到多个串口设备，请选择一个:")
            for idx, p in enumerate(ports, 1):
                print(f"  [{idx}] {p}")
            try:
                sel = int(input("请输入序号 (例如 1): ").strip())
                port = ports[sel - 1]
            except Exception:
                print("[!] 输入无效，退出部署")
                sys.exit(1)

    # 2. 自动检测连接的芯片类型
    chip_type = detect_chip(port)

    # 3. 固件烧录（若开启）
    if args.flash:
        bin_file = args.bin
        if not bin_file:
            # 根据侦测到的芯片类型智能匹配
            bins = []
            if chip_type == "esp32c3":
                bins = [f for f in os.listdir(".") if f.endswith(".bin") and "C3" in f.upper()]
            elif chip_type == "esp32s3":
                bins = [f for f in os.listdir(".") if f.endswith(".bin") and "S3" in f.upper()]
            else:
                bins = [f for f in os.listdir(".") if f.endswith(".bin")]

            if bins:
                bin_file = bins[0]
                print(f"[*] 自动匹配到对应芯片的本地 MicroPython 固件: {bin_file}")
            else:
                print(f"[!] 未找到匹配 {chip_type.upper()} 的 .bin 固件包，请使用 --bin 指定固件路径")
                sys.exit(1)

        if not flash_micropython(port, chip_type, bin_file):
            print("[!] 固件刷写失败，请确认开发板处于 Bootloader 模式 (按住 BOOT 键插入 USB 或短按 RST)")
            sys.exit(1)

    # 4. 上传源码
    if not upload_project(port):
        print("[!] 代码部署失败，请检查串口连接或占用情况。")
        sys.exit(1)

    print("""
============================================================
  🎉 部署成功！后续使用说明：
============================================================
  1. 初始网络连接：
     - 若已配置 WiFi，打开浏览器访问: http://esp32.local 或分配的内网 IP
     - 若尚未配网，开发板会自动广播救援热点:
       * SSID: ESP32-Server-Setup
       * 密码: 空 (免密开放)
       * 配网后台: http://192.168.4.1

  2. 初始管理密码：
     - 初始管理员密码为: admin
     - 进入控制台后可在「网络与系统 -> 安全与系统运维」中自行修改

  3. 硬件救援：
     - 若路由器更换导致连不上，可长按板载【BOOT 物理按键 5 秒】
     - RGB 指示灯紫闪后，系统将自动重置 WiFi 并开启救援热点！
============================================================
""")

if __name__ == '__main__':
    main()
