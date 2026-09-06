import gc
import esp
import network

esp.osdebug(None)
gc.enable()
try:
    network.hostname('esp32')
except Exception:
    pass
print("[Boot] ESP32-S3 MicroPython Nano-Server Initializing (Hostname: esp32.local)...")
