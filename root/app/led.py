import machine
import neopixel
import time

class RGBManager:
    """
    ESP32-S3 板载 WS2812 RGB 状态指示灯管理器
    默认引脚：GPIO 48（常见于 ESP32-S3 DevKit / SuperMini 等开发板）
    """
    def __init__(self, config_mgr):
        self.config_mgr = config_mgr
        self.np = None
        self.current_state = "off"
        self.is_available = False
        self._init_hardware()

    def _init_hardware(self):
        led_cfg = self.config_mgr.config.get("led", {})
        self.enabled = led_cfg.get("enabled", True)
        default_pin = 48
        is_c3 = False
        try:
            import sys
            m = getattr(sys.implementation, '_machine', '').lower()
            if 'c3' in m:
                is_c3 = True
                default_pin = 8
        except Exception:
            pass
        
        cfg_pin = led_cfg.get("pin")
        if is_c3 and (cfg_pin == 48 or cfg_pin is None or (isinstance(cfg_pin, int) and cfg_pin > 21)):
            self.pin_num = 8
        else:
            self.pin_num = cfg_pin if cfg_pin is not None else default_pin
        self.brightness = max(1, min(100, led_cfg.get("brightness", 20)))

        if not self.enabled:
            self._safe_off()
            return

        try:
            pin = machine.Pin(self.pin_num, machine.Pin.OUT)
            self.np = neopixel.NeoPixel(pin, 1)
            self.is_available = True
            print("[RGB] NeoPixel initialized on GPIO", self.pin_num, "Brightness:", self.brightness, "%")
        except Exception as e:
            self.is_available = False
            print("[RGB] Hardware init warning (RGB disabled):", e)

    def _apply_brightness(self, val):
        return int(val * (self.brightness / 100.0))

    def set_color(self, r, g, b):
        if not self.enabled or not self.is_available or not self.np:
            return
        try:
            ar = self._apply_brightness(r)
            ag = self._apply_brightness(g)
            ab = self._apply_brightness(b)
            self.np[0] = (ar, ag, ab)
            self.np.write()
        except Exception as e:
            print("[RGB] Set color failed:", e)

    def set_state(self, state):
        self.current_state = state
        if not self.enabled:
            self._safe_off()
            return

        if state in ("idle", "online"):
            self.set_color(0, 255, 0)      # 绿色 (常驻在线)
        elif state in ("busy", "running"):
            self.set_color(0, 120, 255)    # 科技蓝 (任务执行中)
        elif state in ("error", "alert"):
            self.set_color(255, 0, 0)      # 红色 (打卡失败/断网)
        elif state == "warning":
            self.set_color(255, 140, 0)    # 橙黄 (Cookie快到期)
        elif state == "off":
            self.set_color(0, 0, 0)

    def _safe_off(self):
        if self.is_available and self.np:
            try:
                self.np[0] = (0, 0, 0)
                self.np.write()
            except Exception:
                pass

    def update_config(self, enabled=None, pin_num=None, brightness=None):
        led_cfg = self.config_mgr.config.get("led", {})
        if enabled is not None:
            led_cfg["enabled"] = bool(enabled)
        if pin_num is not None:
            try:
                led_cfg["pin"] = int(pin_num)
            except Exception:
                pass
        if brightness is not None:
            try:
                led_cfg["brightness"] = max(1, min(100, int(brightness)))
            except Exception:
                pass

        self.config_mgr.config["led"] = led_cfg
        self.config_mgr.save_config()
        self._init_hardware()
        self.set_state(self.current_state)

    def test(self, r=0, g=255, b=0):
        if not self.is_available or not self.np:
            return False, "RGB 硬件未就绪或引脚无效"
        try:
            self.set_color(r, g, b)
            return True, "测试信号已发送至引脚 GPIO " + str(self.pin_num)
        except Exception as e:
            return False, str(e)
