#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP32 Nano-Server & 自动化打卡中枢 - 一键部署工具 (Deploy Tool)
支持 ESP32-S3 / ESP32-C3 / 经典 ESP32 全系列开发板（Windows / macOS / Linux）
"""

import os
import sys
import time
import json
import argparse
import subprocess
import hashlib

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def print_banner():
    print("""
============================================================
       [*] ESP32-S3 Nano-Server 一键部署工具 (Deploy Tool)
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
    """自动通过 esptool 识别连接的芯片架构 (严格限定 ESP32-S3)"""
    print(f"[*] 正在自动检测 {port} 连接的芯片型号...", flush=True)
    cmd = [sys.executable, "-m", "esptool", "--port", port, "chip_id"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = (res.stdout + res.stderr).lower()

    if "esp32-s3" in out:
        print("[+] 识别芯片架构: ESP32-S3 (Xtensa 双核 240MHz + PSRAM，完全支持)")
        return "esp32s3"
    else:
        if "esp32-c3" in out:
            chip_name = "ESP32-C3 (单核 RISC-V)"
        elif "esp32" in out:
            chip_name = "经典 ESP32 (无足够 PSRAM)"
        else:
            chip_name = "非 ESP32-S3 芯片"
        print(f"[!] 检测到芯片型号为: {chip_name}。")
        print("[!] ⚠️ 硬件架构不兼容：本项目专为 ESP32-S3 深度定制开发！")
        print("[!] ⚠️ 本项目包含 49 个全套 RESTful API、Gzip 微前端控制台、Cron 调度、大模型诊断等复杂系统。")
        print("[!] ⚠️ 经典 ESP32 与 ESP32-C3 开发板因缺乏外部 PSRAM（可用内存不足 120KB），无法稳定支撑网络缓冲与并发，极易发生 OOM 或连接超时。")
        print("[!] ⚠️ 请更换使用 ESP32-S3 开发板（推荐具备 8MB PSRAM 的 N8R8 / N16R8 型号）！")
        sys.exit(1)

def flash_micropython(port, chip_type, bin_path):
    """刷写 MicroPython 固件 (ESP32-S3)"""
    if not os.path.exists(bin_path):
        print(f"[!] 找不到固件文件: {bin_path}")
        return False

    print(f"\n[*] 正在准备为 ESP32-S3 烧录 MicroPython 固件: {bin_path}")
    print("[*] 步骤 1/2: 擦除 Flash 闪存...")
    erase_cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port, "erase-flash"]
    if not run_cmd(erase_cmd, "擦除芯片闪存"):
        # 兼容旧版本 esptool
        erase_cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port, "erase_flash"]
        if not run_cmd(erase_cmd, "擦除芯片闪存 (重试)"):
            return False

    print("[*] 步骤 2/2: 写入固件...")
    write_cmd = [
        sys.executable, "-m", "esptool",
        "--chip", "esp32s3",
        "--port", port,
        "--baud", "460800",
        "write_flash", "-z", "0", bin_path
    ]
    if not run_cmd(write_cmd, "写入 MicroPython 固件 (基地址: 0)"):
        return False

    print("[+] 固件烧录成功！等待芯片冷启动 (3秒)...")
    time.sleep(3)
    return True

def ensure_mpy_compiled(base_dir):
    """如果安装了 mpy-cross，自动编译 app 和 microdot 目录下的 .py 文件为 .mpy，极大节省板载 RAM 内存"""
    try:
        res = subprocess.run([sys.executable, "-m", "mpy_cross", "--version"], capture_output=True, text=True)
        has_mpy = (res.returncode == 0)
    except Exception:
        has_mpy = False

    if not has_mpy:
        return

    print("[*] 检测到 mpy-cross 编译器，正在检查并预编译 Python 字节码以优化内存...")
    for subdir in ["app", "microdot"]:
        target_path = os.path.join(base_dir, subdir)
        if not os.path.exists(target_path):
            continue
        for root, _, files in os.walk(target_path):
            for f in files:
                if f.endswith(".py") and f != "__init__.py":
                    py_path = os.path.join(root, f)
                    mpy_path = os.path.splitext(py_path)[0] + ".mpy"
                    if not os.path.exists(mpy_path) or os.path.getmtime(py_path) > os.path.getmtime(mpy_path):
                        subprocess.run([sys.executable, "-m", "mpy_cross", py_path], capture_output=True)

def calc_file_hash(filepath):
    """计算文件的 SHA256 哈希值"""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""

def load_deploy_cache(cache_file):
    """加载本地部署记录缓存"""
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_deploy_cache(cache_file, cache_data):
    """保存本地部署记录缓存"""
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

def upload_project(port, force=False, only=None):
    """上传项目源码及静态资源到开发板（支持智能增量同步与单模块指定）"""
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    has_root = os.path.exists(os.path.join(cur_dir, "root"))
    base_dir = os.path.join(cur_dir, "root") if has_root else cur_dir
    cache_file = os.path.join(cur_dir, ".deploy_cache.json")

    # 尝试自动预编译字节码
    ensure_mpy_compiled(base_dir)

    print(f"\n[*] 正在连接串口 {port} 并准备上传项目代码...")

    # 1. 收集本地文件列表（若存在 .mpy，则优先使用 .mpy 并跳过同名 .py 以节省内存）
    ignore_files = {
        "deploy.py", "requirements.txt", "README.md", "LICENSE", ".gitignore", ".deploy_cache.json"
    }
    ignore_dirs = {".git", ".github", "docs", "scratch", ".system_generated"}
    all_files = []
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
        for f in files:
            if f.endswith((".pyc", ".bin", ".tmp", ".log")) or "__pycache__" in root:
                continue
            if not has_root and (f in ignore_files or f.startswith(".")):
                continue
            all_files.append((root, f))

    mpy_stems = set()
    for root, f in all_files:
        if f.endswith(".mpy"):
            mpy_stems.add(os.path.join(root, os.path.splitext(f)[0]))

    candidate_queue = []
    for root, f in all_files:
        local_fp = os.path.join(root, f)
        stem_path = os.path.splitext(local_fp)[0]
        # 跳过已被 .mpy 替代的 .py 文件（保留 boot.py, main.py 与 __init__.py）
        if f.endswith(".py") and f not in ("main.py", "boot.py", "__init__.py") and stem_path in mpy_stems:
            continue
        # 若存在同名 .gz 压缩包（例如 index.html.gz），跳过庞大的未压缩原文件以节省串口传输时间与 Flash 空间
        if f.endswith(".html") and os.path.exists(local_fp + ".gz"):
            continue
        rel = os.path.relpath(local_fp, base_dir).replace("\\", "/")
        remote_fp = f":{rel}"
        candidate_queue.append((local_fp, remote_fp, rel))

    # 读取部署缓存并比对增量
    cache = load_deploy_cache(cache_file)
    cached_files = cache.get("files", {})

    current_hashes = {}
    changed_queue = []
    for local_fp, remote_fp, rel in candidate_queue:
        f_hash = calc_file_hash(local_fp)
        current_hashes[rel] = f_hash

        if only:
            only_norm = only.replace("\\", "/").strip("/")
            if only_norm in rel:
                changed_queue.append((local_fp, remote_fp, rel))
        elif force:
            changed_queue.append((local_fp, remote_fp, rel))
        else:
            if rel not in cached_files or cached_files[rel] != f_hash:
                changed_queue.append((local_fp, remote_fp, rel))

    if not changed_queue:
        print("[+] ✨ 增量检测：所有核心文件与上次部署完全一致，无需重复上传！")
        print("[*] 💡 提示：如需强制全量重新覆盖，请使用参数 --all (或 -a)")
        print("[*] 正在重启开发板确保就绪...")
        subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "reset"])
        return True

    if only:
        print(f"[*] 🎯 定向同步：匹配到 {len(changed_queue)} 个指定文件进行上传 (过滤条件: '{only}'):")
    elif force:
        print(f"[*] 🚀 强制全量同步：共 {len(changed_queue)} 个文件开始逐项写入 Flash 闪存:")
    else:
        print(f"[*] ⚡ 智能增量更新：检测到 {len(changed_queue)} / {len(candidate_queue)} 个文件发生变动/新增，开始同步:")

    # 先做一次软复位并暂停执行，确保看门狗不会在上传时超时
    print("[*] 复位开发板进入安全 REPL 状态...")
    subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "reset"], capture_output=True)
    time.sleep(1.5)

    # 确保远程文件夹结构就绪
    remote_dirs = [":app", ":microdot", ":static", ":data", ":data/backups"]
    for rd in remote_dirs:
        subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "fs", "mkdir", rd], capture_output=True)

    # 将 main.py 挪至队列最末尾
    changed_queue.sort(key=lambda x: (x[2] == "main.py", x[2] == "boot.py", x[2]))

    total = len(changed_queue)
    for idx, (local_fp, remote_fp, rel) in enumerate(changed_queue, 1):
        fsize = os.path.getsize(local_fp)
        size_str = f"{fsize / 1024:.1f} KB" if fsize > 1024 else f"{fsize} B"
        print(f"  [{idx}/{total}] 正在同步 -> {remote_fp} ({size_str}) ...", end="", flush=True)
        t0 = time.time()
        success_upload = False
        last_err = ""
        for attempt in range(2):
            res = subprocess.run(
                [sys.executable, "-m", "mpremote", "connect", port, "fs", "cp", local_fp, remote_fp],
                capture_output=True,
                text=True
            )
            if res.returncode == 0:
                success_upload = True
                break
            else:
                last_err = res.stderr.strip()
                time.sleep(1)

        if not success_upload:
            print(" [X] 失败！")
            print(f"[!] 上传文件 {local_fp} 失败:\n{last_err}")
            return False
        dt = time.time() - t0
        print(f" 耗时 {dt:.1f}s")
        # 成功上传后实时更新并保存缓存，保证意外中断后下次能够断点续传
        cached_files[rel] = current_hashes.get(rel, "")
        cache["files"] = cached_files
        save_deploy_cache(cache_file, cache)

    # 清理远程设备上已被 .mpy 取代的旧 .py 文件以彻底释放闪存与运行时内存
    clean_script = (
        "import os\n"
        "for d in ['app', 'microdot']:\n"
        "    try:\n"
        "        files = os.listdir(d)\n"
        "        for f in files:\n"
        "            if f.endswith('.py') and f != '__init__.py':\n"
        "                if (f[:-3] + '.mpy') in files:\n"
        "                    os.remove(d + '/' + f)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "exec", clean_script], capture_output=True)

    print(f"\n[*] 全部文件同步完成（已同步 {total} 个文件）！正在重启开发板...")
    subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "reset"])
    print("[+] 部署圆满完成！开发板已重启并运行新系统。")
    return True

def configure_wifi_via_serial(port, ssid, password):
    """直接通过 USB 串口写入 WiFi 配置"""
    print(f"[*] 正在通过串口 {port} 为开发板写入 WiFi 配置 (SSID: '{ssid}') ...", flush=True)
    py_code = f"""
import json
p = '/data/config.json'
try:
    with open(p, 'r') as f:
        c = json.load(f)
except Exception:
    c = {{}}
c.setdefault('wifi', {{}})['ssid'] = {json.dumps(ssid)}
c['wifi']['password'] = {json.dumps(password)}
c['wifi']['use_static_ip'] = False
with open(p, 'w') as f:
    json.dump(c, f)
print('WIFI_CONFIG_SAVED')
"""
    cmd = [sys.executable, "-m", "mpremote", "connect", port, "exec", py_code]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if "WIFI_CONFIG_SAVED" in res.stdout:
        print("[+] WiFi 配置写入成功！正在重启开发板使配置生效...")
        subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "reset"])
        print("[+] 重启完成！开发板将在 2~5 秒内自动连接到您的 WiFi。")
        print("[+] 您可以直接在电脑浏览器中访问: http://esp32.local")
        return True
    else:
        print(f"[!] 写入配置失败:\n{res.stderr.strip()}")
        return False

def main():
    print_banner()
    parser = argparse.ArgumentParser(description="ESP32-S3 Nano-Server 一键部署工具")
    parser.add_argument("--port", "-p", help="指定开发板串口号 (例如: COM7, /dev/ttyUSB0)")
    parser.add_argument("--flash", "-f", action="store_true", help="是否从零烧录 MicroPython 固件")
    parser.add_argument("--bin", "-b", help="MicroPython 固件路径 (.bin)")
    parser.add_argument("--wifi", nargs=2, metavar=("SSID", "PASSWORD"), help="直接通过 USB 串口写入 WiFi 配置 (例如: --wifi MyWifi 12345678)")
    parser.add_argument("--all", "-a", action="store_true", help="强制全量重新上传所有核心文件（忽略增量缓存）")
    parser.add_argument("--only", help="仅上传指定文件或匹配路径的文件 (例如: --only app/server.mpy)")
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

    # 2. 如果仅传递了 --wifi，直接通过串口写入 WiFi 并重启，无需重新部署全量代码
    if args.wifi and not args.flash:
        configure_wifi_via_serial(port, args.wifi[0], args.wifi[1])
        return

    # 3. 自动检测连接的芯片类型
    chip_type = detect_chip(port)

    # 3. 固件烧录（若开启）
    if args.flash:
        bin_file = args.bin
        if not bin_file:
            cur_dir = os.path.dirname(os.path.abspath(__file__))
            bins = [os.path.join(cur_dir, f) for f in os.listdir(cur_dir) if f.endswith(".bin") and "S3" in f.upper()]
            if bins:
                bin_file = bins[0]
                print(f"[*] 自动匹配到 ESP32-S3 本地 MicroPython 固件: {bin_file}")
            else:
                print("[!] 未在当前目录下找到 ESP32-S3 的 .bin 固件包，请使用 --bin 指定固件路径")
                sys.exit(1)

        if not flash_micropython(port, chip_type, bin_file):
            print("[!] 固件刷写失败，请确认开发板处于 Bootloader 模式 (按住 BOOT 键插入 USB 或短按 RST)")
            sys.exit(1)

    # 4. 上传源码（若重新刷写固件或指定了 --all，则强制全量上传）
    force_upload = args.all or args.flash
    if not upload_project(port, force=force_upload, only=args.only):
        print("[!] 代码部署失败，请检查串口连接或占用情况。")
        sys.exit(1)

    # 5. 若携带了 --wifi，在部署完成后立即自动写入并联网
    if args.wifi:
        configure_wifi_via_serial(port, args.wifi[0], args.wifi[1])
        return

    print("""
============================================================
  [+] 部署成功！开发板已就绪，进入标准 AP 网页配网流程：
============================================================
  1. 手机/电脑搜索并连接开发板热点：
     - 热点名称 (SSID): ESP32-Server-Setup
     - 热点密码: 空 (免密开放)

  2. 打开配网后台：
     - 手机通常会自动弹出配网页面；
     - 或在手机/电脑浏览器打开: http://192.168.4.1
     - 初始管理员密码: admin

  3. 网页一键配网：
     - 进入控制台「网络与系统 -> 重新配置 WiFi 连接」
     - 点击「📡 重新扫描」，选择您的 2.4G WiFi 并输入密码，点击保存连接
     - 连接成功后，手机切回同一 WiFi，即可通过 http://esp32.local 直达！

  4. 硬件应急救援：
     - 若更换路由器或网络异常，长按板载【BOOT 键 5 秒】即可随时复位并秒启救援热点！
============================================================
""")

if __name__ == '__main__':
    main()
