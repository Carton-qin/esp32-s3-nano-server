import network
import time
import socket
import machine
import ubinascii
import ntptime

class CaptiveDNS:
    def __init__(self, ip="192.168.4.1"):
        self.ip = ip
        self.ip_bytes = bytes([int(x) for x in ip.split('.')])
        self.running = False
        self.sock = None

    async def run(self):
        self.running = True
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setblocking(False)
            self.sock.bind(('0.0.0.0', 53))
            print("[DNS] Captive Portal DNS server active on port 53 (Catch-all -> " + self.ip + ")")
        except Exception as e:
            print("[DNS] Note: UDP 53 bind notice:", e)
            return

        try:
            import uasyncio as asyncio
        except ImportError:
            import asyncio

        while self.running:
            try:
                data, addr = self.sock.recvfrom(512)
                if data and len(data) >= 12:
                    resp = (
                        data[:2] + b'\x81\x80' + data[4:6] +
                        b'\x00\x01\x00\x00\x00\x00' +
                        data[12:] +
                        b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04' +
                        self.ip_bytes
                    )
                    self.sock.sendto(resp, addr)
            except OSError:
                pass
            except Exception:
                pass
            await asyncio.sleep(0.05)

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

class WiFiManager:
    def __init__(self, config_mgr):
        self.config_mgr = config_mgr
        self.set_hostname("esp32")
        self.sta = network.WLAN(network.STA_IF)
        self.ap = network.WLAN(network.AP_IF)
        self.is_connected = False
        self.current_ip = "0.0.0.0"
        self.mode = "NONE"
        self.ntp_synced = False
        self.dns_server = CaptiveDNS("192.168.4.1")

    def set_hostname(self, name="esp32"):
        try:
            network.hostname(name)
            print("[WiFi] Registered mDNS Hostname: " + name + ".local")
        except Exception as e:
            print("[WiFi] Set hostname error:", e)

    def auto_connect(self):
        wifi_cfg = self.config_mgr.config.get("wifi", {})
        ssid = wifi_cfg.get("ssid", "").strip()
        pwd = wifi_cfg.get("password", "").strip()

        if ssid:
            print("[WiFi] Attempting to connect to STA:", ssid)
            self.sta.active(True)
            try:
                self.sta.config(pm=0)
            except Exception:
                pass
            
            # Check static IP
            if wifi_cfg.get("use_static_ip", False):
                sip = wifi_cfg.get("static_ip", "").strip()
                smask = wifi_cfg.get("subnet", "255.255.255.0").strip()
                sgw = wifi_cfg.get("gateway", "").strip()
                sdns = wifi_cfg.get("dns", sgw).strip()
                if sip and sgw:
                    print("[WiFi] Applying Static IP:", sip)
                    try:
                        self.sta.ifconfig((sip, smask, sgw, sdns))
                    except Exception as e:
                        print("[WiFi] Failed to set static IP:", e)

            self.sta.connect(ssid, pwd)
            
            # Wait up to 15 seconds
            for _ in range(30):
                if self.sta.isconnected():
                    try:
                        self.sta.config(pm=0)
                    except Exception:
                        pass
                    self.is_connected = True
                    self.current_ip = self.sta.ifconfig()[0]
                    self.mode = "STA"
                    # 关闭 AP 热点，让无线电与射频带宽 100% 独占 STA，消除共存信道跳频与丢包
                    try:
                        self.ap.active(False)
                    except Exception:
                        pass
                    print("[WiFi] Connected to STA! IP:", self.current_ip)
                    self.config_mgr.add_log("system", "网络连接", "success", "WiFi (STA) 连接成功，分配 IP: " + self.current_ip, category="system")
                    self.sync_ntp()
                    return True
                time.sleep(0.5)

            try:
                self.sta.disconnect()
                self.sta.active(False)
            except Exception:
                pass

        print("[WiFi] STA not connected. Activating AP rescue hotspot...")
        self.start_ap()
        return False

    def start_ap(self, ssid=None, password=None):
        ap_cfg = self.config_mgr.config.get("ap", {})
        target_ssid = (ssid or ap_cfg.get("ssid") or "ESP32-Server-Setup").strip()
        target_pwd = (password if password is not None else ap_cfg.get("password", "")).strip()

        # 必须先激活 AP 接口才能配置参数，否则 ESP-IDF 会报 Wifi Invalid Mode
        self.ap.active(True)

        try:
            if target_pwd and len(target_pwd) >= 8:
                self.ap.config(essid=target_ssid, password=target_pwd, authmode=network.AUTH_WPA2_PSK)
            else:
                self.ap.config(essid=target_ssid, authmode=network.AUTH_OPEN)
        except Exception as e:
            print("[WiFi] AP config warning:", e)

        try:
            if self.ap.ifconfig()[0] != '192.168.4.1':
                self.ap.ifconfig(('192.168.4.1', '255.255.255.0', '192.168.4.1', '192.168.4.1'))
        except Exception:
            pass

        self.mode = "STA+AP" if self.is_connected else "AP"
        print("[WiFi] AP active. SSID:", target_ssid, "IP:", self.ap.ifconfig()[0], "Security:", "WPA2" if (target_pwd and len(target_pwd) >= 8) else "OPEN")
        return True

    def update_ap_config(self, ssid, password):
        ssid = ssid.strip() or "ESP32-Server-Setup"
        pwd = password.strip()
        if pwd and len(pwd) < 8:
            return False, "热点密码长度不能少于8位字符"

        self.config_mgr.config["ap"] = {
            "enabled": self.ap.active(),
            "ssid": ssid,
            "password": pwd
        }
        self.config_mgr.save_config()
        self.start_ap(ssid, pwd)
        return True, "热点设置已保存并生效"

    def set_ap_active(self, active):
        self.ap.active(active)
        self.mode = ("STA+AP" if active else "STA") if self.is_connected else ("AP" if active else "OFF")
        return self.ap.active()

    def scan_networks(self):
        try:
            was_sta_active = self.sta.active()
            if not was_sta_active:
                self.sta.active(True)
            scan_res = self.sta.scan()
            networks = []
            seen = set()
            for item in scan_res:
                ssid = item[0].decode('utf-8', 'ignore').strip()
                rssi = item[3]
                if ssid and ssid not in seen:
                    seen.add(ssid)
                    networks.append({"ssid": ssid, "rssi": rssi})
            networks.sort(key=lambda x: x["rssi"], reverse=True)
            return networks
        except Exception as e:
            print("[WiFi] Scan error:", e)
            return []

    async def set_sta(self, ssid, password, use_static=False, static_ip="", subnet="", gateway="", dns=""):
        wifi_cfg = {
            "ssid": ssid.strip(),
            "password": password.strip(),
            "use_static_ip": bool(use_static),
            "static_ip": static_ip.strip(),
            "subnet": subnet.strip() or "255.255.255.0",
            "gateway": gateway.strip(),
            "dns": dns.strip() or gateway.strip()
        }
        self.config_mgr.config["wifi"] = wifi_cfg
        self.config_mgr.save_config()
        
        # 激活 STA 接口并关闭硬件休眠
        self.sta.active(True)
        try:
            self.sta.config(pm=0)
        except Exception:
            pass

        # 若之前执行过扫描或连接，先断开以保证底层握手状态机重置
        try:
            self.sta.disconnect()
        except Exception:
            pass

        if use_static and static_ip.strip() and gateway.strip():
            print("[WiFi] Configuring Static IP:", static_ip)
            try:
                self.sta.ifconfig((static_ip.strip(), wifi_cfg["subnet"], gateway.strip(), wifi_cfg["dns"]))
            except Exception as e:
                print("[WiFi] Error setting static IP:", e)

        try:
            import uasyncio as asyncio
        except ImportError:
            import asyncio

        await asyncio.sleep(0.2)

        print("[WiFi] Web UI connecting to STA:", wifi_cfg["ssid"])
        self.sta.connect(wifi_cfg["ssid"], wifi_cfg["password"])
        
        # 非阻塞轮询等待 WiFi 握手与 DHCP 获取 IP（最长等待 15 秒）
        for _ in range(50):
            if self.sta.isconnected():
                try:
                    self.sta.config(pm=0)
                except Exception:
                    pass
                self.is_connected = True
                self.current_ip = self.sta.ifconfig()[0]
                self.mode = "STA+AP"
                print("[WiFi] Switch to STA success! IP:", self.current_ip)
                self.config_mgr.add_log("system", "网络连接", "success", "网页配网成功，已连接至 " + wifi_cfg["ssid"] + " (IP: " + self.current_ip + ")", category="system")
                try:
                    asyncio.create_task(self._async_sync_ntp())
                except Exception:
                    pass
                return True, self.current_ip
            await asyncio.sleep(0.3)
            
        try:
            self.sta.disconnect()
            self.sta.active(False)
        except Exception:
            pass
        return False, "连接超时，请检查WiFi密码或确认网络为2.4GHz"

    async def _async_sync_ntp(self):
        try:
            import uasyncio as asyncio
        except ImportError:
            import asyncio
        await asyncio.sleep(1)
        self.sync_ntp()

    def sync_ntp(self):
        servers = ["ntp.aliyun.com", "cn.pool.ntp.org", "pool.ntp.org"]
        for s in servers:
            try:
                ntptime.host = s
                ntptime.settime()
                self.ntp_synced = True
                print("[NTP] Synced successfully via", s)
                self.config_mgr.add_log("system", "网络对时", "success", "NTP 网络对时完成 (服务器: {})".format(s), category="system")
                return True
            except Exception as e:
                print("[NTP] Failed to sync with", s, ":", e)
        return False

    def get_network_details(self):
        sta_if = self.sta.ifconfig() if self.sta.active() else ('0.0.0.0', '0.0.0.0', '0.0.0.0', '0.0.0.0')
        ap_if = self.ap.ifconfig() if self.ap.active() else ('0.0.0.0', '0.0.0.0', '0.0.0.0', '0.0.0.0')
        
        mac_sta = ""
        mac_ap = ""
        try:
            mac_sta = ubinascii.hexlify(self.sta.config('mac'), ':').decode()
        except Exception:
            pass
        try:
            mac_ap = ubinascii.hexlify(self.ap.config('mac'), ':').decode()
        except Exception:
            pass

        rssi = 0
        if self.is_connected:
            try:
                rssi = self.sta.status('rssi')
            except Exception:
                pass

        wifi_cfg = self.config_mgr.config.get("wifi", {})
        ap_cfg = self.config_mgr.config.get("ap", {})

        return {
            "mode": self.mode,
            "connected": self.is_connected,
            "sta": {
                "active": self.sta.active(),
                "ssid": wifi_cfg.get("ssid", "") if self.is_connected else "",
                "ip": sta_if[0],
                "subnet": sta_if[1],
                "gateway": sta_if[2],
                "dns": sta_if[3],
                "mac": mac_sta,
                "rssi": rssi,
                "use_static_ip": wifi_cfg.get("use_static_ip", False),
                "static_ip": wifi_cfg.get("static_ip", ""),
                "saved_gateway": wifi_cfg.get("gateway", ""),
                "saved_dns": wifi_cfg.get("dns", "")
            },
            "ap": {
                "active": self.ap.active(),
                "ssid": ap_cfg.get("ssid", "ESP32-Server-Setup"),
                "has_password": bool(ap_cfg.get("password")),
                "password": ap_cfg.get("password", ""),
                "ip": ap_if[0],
                "mac": mac_ap
            },
            "mdns": {
                "hostname": "esp32.local",
                "url": "http://esp32.local"
            },
            "ntp_synced": self.ntp_synced
        }

    async def keepalive_loop(self, rgb_manager=None):
        """
        WiFi 后台保活与自愈协程：
        - 每 30 秒检测一次连接；
        - 若掉线自动发起重连并在成功后重校 NTP 时间；
        - 联动 RGB 状态灯与周期性 GC 内存整理。
        """
        import asyncio
        import gc

        while True:
            await asyncio.sleep(30)
            wifi_cfg = self.config_mgr.config.get("wifi", {})
            ssid = wifi_cfg.get("ssid", "").strip()
            pwd = wifi_cfg.get("password", "").strip()

            if not ssid:
                continue

            try:
                is_conn = self.sta.isconnected()
                if not is_conn:
                    self.is_connected = False
                    print("[WiFi Watchdog] Connection lost! Attempting auto-reconnect to:", ssid)
                    if rgb_manager:
                        rgb_manager.set_state("error")

                    if not self.sta.active():
                        self.sta.active(True)
                    try:
                        self.sta.config(pm=0)
                    except Exception:
                        pass
                    self.sta.connect(ssid, pwd)

                    for _ in range(24):
                        await asyncio.sleep(0.5)
                        if self.sta.isconnected():
                            try:
                                self.sta.config(pm=0)
                            except Exception:
                                pass
                            self.is_connected = True
                            self.current_ip = self.sta.ifconfig()[0]
                            print("[WiFi Watchdog] Reconnected successfully! IP:", self.current_ip)
                            if rgb_manager:
                                rgb_manager.set_state("idle")
                            self.sync_ntp()
                            break

                    if not self.is_connected:
                        print("[WiFi Watchdog] Reconnect attempt failed. Will retry in 30s.")
                        try:
                            self.sta.disconnect()
                            self.sta.active(False)
                        except Exception:
                            pass
                else:
                    self.is_connected = True
            except Exception as e:
                print("[WiFi Watchdog] Error during keepalive check:", e)

            gc.collect()

