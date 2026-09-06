import asyncio
import gc
from app.config import config_mgr
from app.wifi import WiFiManager
from app.llm import LLMClient
from app.notify import Notifier
from app.executor import TaskExecutor
from app.cron import TaskScheduler
from app.server import create_app
from app.led import RGBManager
from app.button import BootButtonWatcher
import machine

async def main():
    gc.collect()
    print("========================================")
    print("  ESP32-S3 Nano-Server & AI Platform   ")
    print("========================================")
    
    # 0. Hardware Reset Cause Diagnostics
    rc_map = {
        1: "正常冷启动上电 (Power-on Reset)",
        2: "外部硬件按键/引脚复位 (Hard Reset)",
        3: "⚠️ 硬件看门狗复位 (WDT Timeout Reset)",
        4: "深度睡眠唤醒 (Deep Sleep Wakeup)",
        5: "软件指令重启/热重载 (Soft Reset)"
    }
    raw_rc = machine.reset_cause()
    rc_str = rc_map.get(raw_rc, "复位代码: {}".format(raw_rc))
    config_mgr.last_reset_cause = rc_str
    print("[System] Boot reset cause:", rc_str)
    config_mgr.add_log(
        "system",
        "开机自检",
        "info" if raw_rc in [1, 5] else "warning",
        "系统启动完成，检测到开机原因: {}".format(rc_str),
        category="system"
    )

    # 0.1 Apply Power Management / DFS Initial Frequency
    config_mgr.apply_power_freq(busy=False)

    # 0.2 Hardware RGB LED
    rgb_mgr = RGBManager(config_mgr)
    
    # 1. Connect WiFi (STA or fallback to AP)
    wifi_mgr = WiFiManager(config_mgr)
    wifi_mgr.auto_connect()
    if wifi_mgr.is_connected:
        rgb_mgr.set_state("idle")
    else:
        rgb_mgr.set_state("warning")
    
    # 2. Instantiate Core Engines
    llm_client = LLMClient(config_mgr)
    notifier = Notifier(config_mgr)
    executor = TaskExecutor(config_mgr, llm_client, notifier, rgb_manager=rgb_mgr)
    scheduler = TaskScheduler(config_mgr, executor)
    
    # 3. Create Microdot App
    app = create_app(config_mgr, wifi_mgr, llm_client, notifier, executor, rgb_manager=rgb_mgr, scheduler=scheduler)
    
    # 4. Hardware Watchdog (WDT) & Heartbeat
    wdt = None
    if config_mgr.config.get("watchdog", True):
        try:
            wdt = machine.WDT(timeout=60000)
            print("[Watchdog] Hardware WDT enabled (timeout: 60s)")
        except Exception as e:
            print("[Watchdog] Hardware WDT init error:", e)

    async def heartbeat_loop():
        while True:
            await asyncio.sleep(5)
            if wdt:
                wdt.feed()

    # 5. Launch Background Coroutines
    asyncio.create_task(scheduler.start())
    asyncio.create_task(wifi_mgr.keepalive_loop(rgb_mgr))
    asyncio.create_task(heartbeat_loop())
    if not wifi_mgr.is_connected:
        asyncio.create_task(wifi_mgr.dns_server.run())
    
    btn_watcher = BootButtonWatcher(wifi_mgr, config_mgr, rgb_manager=rgb_mgr)
    asyncio.create_task(btn_watcher.start())
    
    # 6. Start Web Server
    print("[Server] Listening on http://0.0.0.0:80")
    if wifi_mgr.is_connected:
        print("[Server] Access URL: http://" + wifi_mgr.current_ip + " or http://esp32.local")
    else:
        print("[Server] AP Mode Setup URL: http://192.168.4.1")

    await app.start_server(host='0.0.0.0', port=80)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Server halted.")
    except Exception as e:
        print("Fatal error in main loop:", e)
