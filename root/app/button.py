import machine
import time
try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

class BootButtonWatcher:
    def __init__(self, wifi_mgr, config_mgr, rgb_manager=None, pin_num=None):
        self.wifi_mgr = wifi_mgr
        self.config_mgr = config_mgr
        self.rgb_manager = rgb_manager
        
        if pin_num is None:
            # ESP32-C3 出厂物理 BOOT 引脚为 GPIO 9；ESP32-S3 与经典 ESP32 为 GPIO 0
            pin_num = 0
            try:
                import sys
                m = getattr(sys.implementation, '_machine', '').lower()
                if 'c3' in m:
                    pin_num = 9
            except Exception:
                pass
                
        self.pin_num = pin_num
        self.pin = machine.Pin(self.pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
        self.running = False
        self.pressed_ms = 0
        self.action_triggered = False

    async def start(self):
        self.running = True
        print(f'[Button] BOOT button watcher started on GPIO {self.pin_num} (Long press 5s to reset WiFi to AP mode).')
        while self.running:
            try:
                is_pressed = (self.pin.value() == 0)
                if is_pressed:
                    self.pressed_ms += 100
                    if 3000 <= self.pressed_ms < 5000:
                        if self.rgb_manager:
                            self.rgb_manager.set_state('busy')
                    if self.pressed_ms >= 5000 and not self.action_triggered:
                        self.action_triggered = True
                        print('[Button] Long press 5s detected! Resetting WiFi to AP mode...')
                        if self.rgb_manager:
                            for _ in range(4):
                                self.rgb_manager.test(180, 0, 255)
                                await asyncio.sleep(0.1)
                                self.rgb_manager.test(0, 0, 0)
                                await asyncio.sleep(0.1)
                            self.rgb_manager.set_state('warning')

                        wifi_cfg = self.config_mgr.config.setdefault('wifi', {})
                        wifi_cfg['ssid'] = ''
                        wifi_cfg['password'] = ''
                        wifi_cfg['use_static_ip'] = False
                        self.config_mgr.save_config()

                        self.wifi_mgr.start_ap()
                        self.config_mgr.add_log(
                            'system',
                            '硬件按键重置',
                            'warning',
                            '检测到板载 BOOT 键长按 5 秒，已清空 WiFi 配置并启动 AP 热点 (ESP32-Server-Setup)'
                        )
                        print('[Button] WiFi reset complete. AP mode started.')
                else:
                    self.pressed_ms = 0
                    self.action_triggered = False
            except Exception as e:
                print('[Button] Error in watcher:', e)

            await asyncio.sleep(0.1)
