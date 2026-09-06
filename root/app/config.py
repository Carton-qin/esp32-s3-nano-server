import json
import os
import time

CONFIG_PATH = '/data/config.json'
TASKS_PATH = '/data/tasks.json'
LOGS_PATH = '/data/logs.json'
SYSTEM_LOG_PATH = '/data/system.log'
MAX_LOG_SIZE = 64 * 1024
BACKUPS_DIR = '/data/backups'

DEFAULT_CONFIG = {
    "auth": {
        "enabled": True,
        "password": "admin"
    },
    "wifi": {
        "ssid": "",
        "password": "",
        "use_static_ip": False,
        "static_ip": "",
        "subnet": "255.255.255.0",
        "gateway": "",
        "dns": "223.5.5.5"
    },
    "ap": {
        "enabled": True,
        "ssid": "ESP32-Server-Setup",
        "password": ""
    },
    "llm": {
        "base_url": "",
        "api_key": "",
        "model": "",
        "temperature": 0.7
    },
    "llm_history": [],
    "notify": {
        "policy": {
            "mode": "all",
            "dnd_enabled": True,
            "dnd_start": 23,
            "dnd_end": 8
        },
        "pushplus": {"enabled": False, "token": ""},
        "feishu": {"enabled": False, "webhook": ""},
        "dingtalk": {"enabled": False, "webhook": ""},
        "wechat_work": {"enabled": False, "webhook": ""},
        "custom": {
            "enabled": False,
            "url": "",
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "template": "{\"title\": \"{title}\", \"content\": \"{content}\"}"
        }
    },
    "power": {
        "dfs_enabled": True,
        "idle_freq": 160,
        "busy_freq": 240
    },
    "led": {
        "enabled": True,
        "pin": 48,
        "brightness": 20
    }
}

class ConfigManager:
    def __init__(self):
        self._ensure_dirs()
        self._last_config_cache = None
        self._last_tasks_cache = None
        self.last_reset_cause = "正在检测..."
        self.config = self.load_config()
        self.tasks = self.load_tasks()
        self.logs = self.load_logs()

    def _ensure_dirs(self):
        for d in ['/data', '/data/backups', '/static']:
            try:
                os.mkdir(d)
            except OSError:
                pass

    def load_config(self):
        try:
            with open(CONFIG_PATH, 'r') as f:
                c = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in c:
                        c[k] = v
                if "notify" not in c:
                    c["notify"] = DEFAULT_CONFIG["notify"]
                elif "policy" not in c["notify"]:
                    c["notify"]["policy"] = DEFAULT_CONFIG["notify"]["policy"]
                if "power" not in c:
                    c["power"] = DEFAULT_CONFIG["power"]
                if "llm_history" in c and isinstance(c["llm_history"], list):
                    c["llm_history"] = [
                        item for item in c["llm_history"]
                        if isinstance(item, dict) and item.get("name") not in ["DeepSeek", "SiliconFlow"]
                    ]
                    for item in c["llm_history"]:
                        item.pop("model", None)
                return c
        except Exception:
            self.save_config(DEFAULT_CONFIG)
            return DEFAULT_CONFIG

    def apply_power_freq(self, busy=False):
        power_cfg = self.config.get("power", {})
        if not power_cfg.get("dfs_enabled", True):
            return
        target_mhz = int(power_cfg.get("busy_freq", 240) if busy else power_cfg.get("idle_freq", 160))
        try:
            import machine
            cur_mhz = machine.freq() // 1000000
            if cur_mhz != target_mhz:
                machine.freq(target_mhz * 1000000)
                print("[Power] Dynamic CPU clock scaled to {}MHz (busy={})".format(target_mhz, busy))
        except Exception as e:
            print("[Power] Error setting CPU frequency:", e)

    def save_config(self, cfg=None):
        if cfg is not None:
            self.config = cfg
        try:
            content = json.dumps(self.config)
            if content == self._last_config_cache:
                return True
            with open(CONFIG_PATH, 'w') as f:
                f.write(content)
            self._last_config_cache = content
            return True
        except Exception as e:
            print("Failed to save config:", e)
            return False

    def add_llm_history(self, base_url, api_key, name=""):
        if not base_url or not api_key:
            return
        history = self.config.get("llm_history", [])
        cleaned_history = []
        for item in history:
            if isinstance(item, dict):
                cleaned_history.append({
                    "name": item.get("name", ""),
                    "base_url": item.get("base_url", ""),
                    "api_key": item.get("api_key", "")
                })
        cleaned_history = [item for item in cleaned_history if item.get("base_url") != base_url]
        if not name:
            if "deepseek" in base_url.lower():
                name = "DeepSeek"
            elif "openai" in base_url.lower():
                name = "OpenAI"
            elif "aliyun" in base_url.lower() or "dashscope" in base_url.lower():
                name = "通义千问"
            elif "moonshot" in base_url.lower() or "kimi" in base_url.lower():
                name = "Kimi"
            elif "siliconflow" in base_url.lower():
                name = "SiliconFlow"
            else:
                name = base_url.split("//")[-1].split("/")[0]

        cleaned_history.insert(0, {
            "name": name,
            "base_url": base_url,
            "api_key": api_key
        })
        self.config["llm_history"] = cleaned_history[:5]
        self.save_config()

    def load_tasks(self):
        try:
            with open(TASKS_PATH, 'r') as f:
                return json.load(f)
        except Exception:
            return []

    def save_tasks(self, tasks=None):
        if tasks is not None:
            self.tasks = tasks
        try:
            content = json.dumps(self.tasks)
            if content == self._last_tasks_cache:
                return True
            with open(TASKS_PATH, 'w') as f:
                f.write(content)
            self._last_tasks_cache = content
            return True
        except Exception as e:
            print("Failed to save tasks:", e)
            return False

    def auto_rotate_backup(self):
        try:
            t_str = self._format_now()
            ts_tag = t_str.replace(" ", "_").replace(":", "").replace("-", "")
            backup_file = "{}/backup_{}.json".format(BACKUPS_DIR, ts_tag)
            snapshot = {
                "created_at": t_str,
                "config": self.config,
                "tasks": self.tasks
            }
            with open(backup_file, 'w') as f:
                json.dump(snapshot, f)
            print("[Backup] Created auto snapshot:", backup_file)

            # Prune to keep only newest 5 backups
            files = []
            try:
                for fname in os.listdir(BACKUPS_DIR):
                    if fname.startswith("backup_") and fname.endswith(".json"):
                        files.append(fname)
            except Exception:
                pass
            files.sort()
            if len(files) > 5:
                for old_f in files[:-5]:
                    try:
                        os.remove("{}/{}".format(BACKUPS_DIR, old_f))
                        print("[Backup] Pruned old snapshot:", old_f)
                    except Exception:
                        pass
            return True, backup_file
        except Exception as e:
            print("[Backup] Error creating auto backup:", e)
            return False, str(e)

    def list_backups(self):
        try:
            files = []
            for fname in os.listdir(BACKUPS_DIR):
                if fname.startswith("backup_") and fname.endswith(".json"):
                    try:
                        st = os.stat("{}/{}".format(BACKUPS_DIR, fname))
                        files.append({
                            "filename": fname,
                            "size": st[6],
                            "name": fname.replace("backup_", "").replace(".json", "")
                        })
                    except Exception:
                        files.append({"filename": fname, "size": 0, "name": fname})
            files.sort(key=lambda x: x["filename"], reverse=True)
            return files
        except Exception:
            return []

    def restore_backup(self, filename):
        target_path = "{}/{}".format(BACKUPS_DIR, filename)
        try:
            with open(target_path, 'r') as f:
                data = json.load(f)
            if "config" in data and "tasks" in data:
                self.config = data["config"]
                self.tasks = data["tasks"]
                self._last_config_cache = None
                self._last_tasks_cache = None
                self.save_config()
                self.save_tasks()
                return True, "快照已成功恢复并生效"
            return False, "无效的快照文件格式"
        except Exception as e:
            return False, "恢复失败: " + str(e)

    def get_task(self, task_id):
        for t in self.tasks:
            if t.get("id") == task_id:
                return t
        return None

    def upsert_task(self, task_data):
        if "id" not in task_data or not task_data["id"]:
            task_data["id"] = "t_" + str(int(time.time()))
        
        found = False
        for i, t in enumerate(self.tasks):
            if t.get("id") == task_data["id"]:
                self.tasks[i] = task_data
                found = True
                break
        if not found:
            self.tasks.append(task_data)
        self.save_tasks()
        return task_data

    def delete_task(self, task_id):
        self.tasks = [t for t in self.tasks if t.get("id") != task_id]
        self.save_tasks()

    def load_logs(self):
        try:
            with open(LOGS_PATH, 'r') as f:
                return json.load(f)
        except Exception:
            return []

    def _trim_log_file(self):
        try:
            with open(SYSTEM_LOG_PATH, 'r') as f:
                lines = f.readlines()
            if len(lines) > 20:
                keep_lines = lines[len(lines) // 3:]
                with open(SYSTEM_LOG_PATH, 'w') as f:
                    f.writelines(keep_lines)
                print("[ConfigManager] FIFO trimmed system log (kept {} lines)".format(len(keep_lines)))
        except Exception as e:
            print("[ConfigManager] Error trimming log file:", e)

    def _append_log_file(self, entry):
        line = "[{}] [{}] [{}] [{}] {}{}\n".format(
            entry.get("time", ""),
            entry.get("category", "task").upper(),
            entry.get("status", "info").upper(),
            entry.get("task_name", entry.get("task_id", "")),
            entry.get("message", ""),
            " | 诊断: " + entry["ai_diag"] if entry.get("ai_diag") else ""
        )
        try:
            try:
                st = os.stat(SYSTEM_LOG_PATH)
                if st[6] >= MAX_LOG_SIZE:
                    self._trim_log_file()
            except OSError:
                pass
            with open(SYSTEM_LOG_PATH, 'a') as f:
                f.write(line)
        except Exception as e:
            print("[ConfigManager] Error appending to system log:", e)

    def add_log(self, task_id, task_name, status, message, ai_diag=None, category=None):
        if category is None:
            if task_id in ["system", "auth", "wifi", "wdt", "hardware", "ntp", "net", "security"]:
                category = "system"
            else:
                category = "task"
        t_str = self._format_now()
        log_entry = {
            "time": t_str,
            "category": category,
            "task_id": task_id,
            "task_name": task_name,
            "status": status,
            "message": message,
            "ai_diag": ai_diag or ""
        }
        self.logs.insert(0, log_entry)
        if len(self.logs) > 100:
            self.logs = self.logs[:100]
        try:
            with open(LOGS_PATH, 'w') as f:
                json.dump(self.logs[:60], f)
        except Exception:
            pass
        self._append_log_file(log_entry)

    def get_logs(self, category=None):
        if not category or category == "all":
            return self.logs
        return [l for l in self.logs if l.get("category") == category]

    def clear_logs(self):
        self.logs = []
        try:
            with open(LOGS_PATH, 'w') as f:
                json.dump([], f)
        except Exception:
            pass
        try:
            with open(SYSTEM_LOG_PATH, 'w') as f:
                f.write("")
        except Exception:
            pass

    def _format_now(self):
        # MicroPython RTC runs UTC. Convert to Beijing Time (UTC+8)
        t = time.localtime(time.time() + 8 * 3600)
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            t[0], t[1], t[2], t[3], t[4], t[5]
        )

config_mgr = ConfigManager()
