import json
import gc
import os
import time
import machine
import esp32
import sys
import urequests
try:
    import uasyncio as asyncio
except ImportError:
    import asyncio
from microdot.microdot import Microdot, Response, send_file

def create_app(config_mgr, wifi_mgr, llm_client, notifier, executor, rgb_manager=None, scheduler=None):
    app = Microdot()
    boot_ticks = time.ticks_ms()

    def json_resp(data, status=200):
        return Response(
            body=json.dumps(data),
            status_code=status,
            headers={
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "*"
            }
        )

    def check_auth(req):
        auth_cfg = config_mgr.config.get("auth", {})
        if not auth_cfg.get("enabled", True):
            return True
        pwd = auth_cfg.get("password", "admin")
        
        # Check header
        auth_header = req.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if token == pwd:
                return True
        
        # Check query
        if req.args.get("token") == pwd:
            return True
            
        return False

    # 1. Static Web UI (Streamed via send_file)
    @app.route('/')
    async def index(req):
        try:
            return send_file('/static/index.html')
        except Exception as e:
            return Response(body="Index file error: " + str(e), status_code=500)

    @app.route('/static/<path:path>')
    async def static_files(req, path):
        try:
            return send_file('/static/' + path)
        except Exception:
            return Response(body="File not found", status_code=404)

    @app.route('/manifest.json')
    async def manifest_json(req):
        manifest = {
            "name": "ESP32-S3 Nano 控制台",
            "short_name": "ESP32控制台",
            "description": "ESP32-S3 IoT 自动化微服务器与智能中枢",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0f172a",
            "theme_color": "#0f172a",
            "icons": [
                {
                    "src": "/static/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png"
                },
                {
                    "src": "/static/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png"
                }
            ]
        }
        return Response(
            body=json.dumps(manifest),
            status_code=200,
            headers={
                "Content-Type": "application/manifest+json",
                "Access-Control-Allow-Origin": "*"
            }
        )

    # Captive portal detection probes for Apple iOS / Android / Windows
    @app.route('/hotspot-detect.html')
    @app.route('/generate_204')
    @app.route('/gen_204')
    @app.route('/ncsi.txt')
    @app.route('/connecttest.txt')
    @app.route('/canonical.html')
    @app.route('/success.txt')
    async def captive_portal_probe(req):
        if not wifi_mgr.is_connected:
            return Response.redirect('http://192.168.4.1/')
        return Response(body="", status_code=204)

    # Captive portal for AP mode
    @app.errorhandler(404)
    async def not_found(req):
        if not wifi_mgr.is_connected and not req.path.startswith("/api/"):
            return Response.redirect('http://192.168.4.1/')
        return json_resp({"error": "Not found"}, 404)

    login_lock_state = {
        "failed_attempts": 0,
        "lockout_until": 0
    }

    # 2. Auth APIs
    @app.route('/api/auth/verify', methods=['GET', 'POST'])
    async def api_auth_verify(req):
        auth_cfg = config_mgr.config.get("auth", {})
        if not auth_cfg.get("enabled", True):
            return json_resp({"ok": True, "auth_enabled": False, "authenticated": True})
        if check_auth(req):
            return json_resp({"ok": True, "auth_enabled": True, "authenticated": True})
        return json_resp({"ok": False, "auth_enabled": True, "authenticated": False, "error": "Unauthorized"}, 401)

    @app.route('/api/auth/status')
    async def api_auth_status(req):
        now_ts = time.time()
        is_locked = (now_ts < login_lock_state["lockout_until"])
        rem_sec = int(login_lock_state["lockout_until"] - now_ts) if is_locked else 0
        return json_resp({
            "ok": True,
            "locked": is_locked,
            "retry_after": rem_sec,
            "failed_attempts": login_lock_state["failed_attempts"]
        })

    @app.route('/api/login', methods=['POST'])
    async def api_login(req):
        now_ts = time.time()
        if now_ts < login_lock_state["lockout_until"]:
            rem_sec = int(login_lock_state["lockout_until"] - now_ts)
            return json_resp({
                "ok": False,
                "error": "输错密码过多，控制台已临时锁定！请等待 {} 秒后重试。".format(rem_sec),
                "locked": True,
                "retry_after": rem_sec
            }, 429)

        data = req.json or {}
        pwd = data.get("password", "")
        cfg_pwd = config_mgr.config.get("auth", {}).get("password", "admin")

        if pwd == cfg_pwd:
            login_lock_state["failed_attempts"] = 0
            login_lock_state["lockout_until"] = 0
            config_mgr.add_log("system", "控制台认证", "success", "管理员登录成功", category="system")
            return json_resp({"ok": True, "token": cfg_pwd})

        login_lock_state["failed_attempts"] += 1
        fails = login_lock_state["failed_attempts"]
        if fails >= 5:
            login_lock_state["lockout_until"] = now_ts + 300
            config_mgr.add_log("system", "安全防护", "fail", "密码连续错误 5 次，控制台触发防爆破锁定 5 分钟", category="system")
            return json_resp({
                "ok": False,
                "error": "连续输错 5 次密码！系统已触发防暴力破解锁定，请等待 300 秒后再试。",
                "locked": True,
                "retry_after": 300
            }, 429)
        else:
            rem = 5 - fails
            config_mgr.add_log("system", "控制台认证", "warning", "密码错误 (已累计 {} 次)".format(fails), category="system")
            return json_resp({
                "ok": False,
                "error": "密码错误！还剩 {} 次尝试机会，连续 5 次将锁定 5 分钟。".format(rem),
                "remaining_attempts": rem
            }, 401)

    # 3. System Status
    @app.route('/api/status')
    async def api_status(req):
        if not check_auth(req):
            return json_resp({"ok": False, "error": "Unauthorized", "auth_required": True}, 401)
        gc.collect()
        free_ram = gc.mem_free()
        alloc_ram = gc.mem_alloc()
        
        # Flash space
        try:
            st = os.statvfs('/')
            flash_free = st[0] * st[3]
            flash_total = st[0] * st[2]
        except Exception:
            flash_free = 0
            flash_total = 0

        # Hardware metrics
        mcu_temp = 0.0
        try:
            mcu_temp = round(esp32.mcu_temperature(), 1)
        except Exception:
            pass

        now_str = config_mgr._format_now()
        # Monotonic uptime in seconds using ticks_diff to prevent NTP jump bugs
        uptime_sec = time.ticks_diff(time.ticks_ms(), boot_ticks) // 1000

        # Detailed network status
        net_details = wifi_mgr.get_network_details()

        return json_resp({
            "ok": True,
            "status": {
                "uptime_seconds": uptime_sec,
                "time": now_str,
                "temperature": mcu_temp,
                "cpu_freq_mhz": machine.freq() // 1000000,
                "platform": "ESP32-S3 (Xtensa Dual-Core 240MHz)",
                "firmware": "MicroPython v1.29.0 (SPIRAM_OCT)",
                "free_ram_bytes": free_ram,
                "free_ram_mb": round(free_ram / 1024 / 1024, 2),
                "total_ram_mb": round((free_ram + alloc_ram) / 1024 / 1024, 2),
                "flash_free_mb": round(flash_free / 1024 / 1024, 2),
                "flash_total_mb": round(flash_total / 1024 / 1024, 2),
                "wifi": net_details,
                "task_count": len(config_mgr.tasks),
                "auth_enabled": config_mgr.config.get("auth", {}).get("enabled", True),
                "reset_cause": getattr(config_mgr, "last_reset_cause", "正常上电运行"),
                "power": config_mgr.config.get("power", {"dfs_enabled": True, "idle_freq": 160, "busy_freq": 240})
            }
        })

    # Power Management & Dynamic Frequency Scaling (DFS) API
    @app.route('/api/system/power', methods=['GET', 'POST'])
    async def api_system_power(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        if req.method == 'POST':
            data = req.json or {}
            pcfg = config_mgr.config.setdefault("power", {})
            if "dfs_enabled" in data:
                pcfg["dfs_enabled"] = bool(data["dfs_enabled"])
            if "idle_freq" in data:
                pcfg["idle_freq"] = int(data["idle_freq"])
            if "busy_freq" in data:
                pcfg["busy_freq"] = int(data["busy_freq"])
            config_mgr.save_config()
            config_mgr.apply_power_freq(busy=False)
            config_mgr.add_log("system", "能效管理", "info", "CPU变频策略已更新 (空闲: {}MHz, 动态变频: {})".format(pcfg.get("idle_freq", 160), pcfg.get("dfs_enabled")), category="system")
            return json_resp({
                "ok": True,
                "power": pcfg,
                "current_freq_mhz": machine.freq() // 1000000
            })
        return json_resp({
            "ok": True,
            "power": config_mgr.config.get("power", {}),
            "current_freq_mhz": machine.freq() // 1000000
        })

    # Network Ping & AP Toggle APIs
    @app.route('/api/wifi/ap_toggle', methods=['POST'])
    async def api_ap_toggle(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        active = bool(data.get("active", True))
        current_state = wifi_mgr.set_ap_active(active)
        return json_resp({"ok": True, "ap_active": current_state})

    @app.route('/api/system/net_ping')
    async def api_net_ping(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        t_start = time.ticks_ms()
        resp = None
        try:
            resp = urequests.get('http://connect.rom.miui.com/generate_204')
            latency = time.ticks_diff(time.ticks_ms(), t_start)
            return json_resp({"ok": True, "online": True, "latency_ms": latency, "status": resp.status_code})
        except Exception as e:
            return json_resp({"ok": True, "online": False, "error": str(e)})
        finally:
            if resp:
                resp.close()

    # 4. Tasks APIs
    @app.route('/api/tasks', methods=['GET', 'POST'])
    async def api_tasks(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        
        if req.method == 'POST':
            task_data = req.json
            saved = config_mgr.upsert_task(task_data)
            return json_resp({"ok": True, "task": saved})
        
        from app.executor import get_cookie_expiration_days
        enriched_tasks = []
        for t in config_mgr.tasks:
            tc = dict(t)
            headers = t.get("params", {}).get("headers", {})
            has_jwt, rem_days, exp_ts, exp_date = get_cookie_expiration_days(headers)
            if has_jwt and rem_days is not None:
                tc["cookie_days"] = rem_days
                tc["cookie_exp_date"] = exp_date
                if rem_days <= 0:
                    tc["cookie_status"] = "expired"
                elif rem_days <= 3:
                    tc["cookie_status"] = "expiring_soon"
                else:
                    tc["cookie_status"] = "valid"
            else:
                tc["cookie_status"] = "none"
                tc["cookie_days"] = None
                tc["cookie_exp_date"] = None
            if tc.get("schedule_type") == "window" and scheduler and hasattr(scheduler, "task_window_targets"):
                tc["today_target_time"] = scheduler.task_window_targets.get(tc.get("id"), "")
            enriched_tasks.append(tc)

        return json_resp({"ok": True, "tasks": enriched_tasks})

    @app.route('/api/tasks/heatmap')
    async def api_tasks_heatmap(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        
        epoch_year = time.gmtime(0)[0]
        offset = 946684800 if epoch_year == 2000 else 0
        now_unix = time.time() + offset

        days_map = {}
        for i in range(59, -1, -1):
            t_sec = (now_unix - offset) - i * 86400 + 8 * 3600
            t_struct = time.localtime(t_sec)
            d_str = "{:04d}-{:02d}-{:02d}".format(t_struct[0], t_struct[1], t_struct[2])
            days_map[d_str] = {"success": 0, "fail": 0}

        for t in config_mgr.tasks:
            hist = t.get("stats", {}).get("history", {})
            for d_str, h_info in hist.items():
                if d_str in days_map:
                    if h_info.get("status") == "success":
                        days_map[d_str]["success"] += 1
                    elif h_info.get("status") == "fail":
                        days_map[d_str]["fail"] += 1

        for l in config_mgr.logs:
            if l.get("category") == "task" and l.get("time"):
                d_str = l["time"].split(" ")[0]
                if d_str in days_map:
                    if days_map[d_str]["success"] == 0 and days_map[d_str]["fail"] == 0:
                        st = l.get("status", "")
                        if st == "success":
                            days_map[d_str]["success"] += 1
                        elif st == "fail":
                            days_map[d_str]["fail"] += 1

        return json_resp({"ok": True, "heatmap": days_map})

    @app.route('/api/tasks/<task_id>', methods=['DELETE'])
    async def api_delete_task(req, task_id):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        config_mgr.delete_task(task_id)
        return json_resp({"ok": True})

    @app.route('/api/tasks/<task_id>/run', methods=['POST'])
    async def api_run_task(req, task_id):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        task = config_mgr.get_task(task_id)
        if not task:
            return json_resp({"ok": False, "error": "Task not found"}, 404)
        
        success, msg, ai_diag = await executor.run_task(task)
        return json_resp({
            "ok": True,
            "success": success,
            "result": msg,
            "ai_diag": ai_diag
        })

    @app.route('/api/tasks/<task_id>/cookie', methods=['POST', 'OPTIONS'])
    async def api_task_cookie(req, task_id):
        if req.method == 'OPTIONS':
            return json_resp({"ok": True})
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        cookie_str = data.get("cookie", "").strip()
        if not cookie_str:
            return json_resp({"ok": False, "error": "Cookie不能为空"}, 400)
        
        task = config_mgr.get_task(task_id)
        if not task:
            return json_resp({"ok": False, "error": "Task not found"}, 404)
        
        if "params" not in task:
            task["params"] = {}
        if "headers" not in task["params"]:
            task["params"]["headers"] = {}
            
        task["params"]["headers"]["Cookie"] = cookie_str
        
        from app.executor import get_cookie_expiration_days
        has_jwt, rem_days, exp_ts, exp_date = get_cookie_expiration_days(task["params"]["headers"])
        
        if "stats" in task and task["stats"].get("circuit_tripped"):
            task["stats"]["circuit_tripped"] = False
            task["stats"]["consecutive_fails"] = 0
            task["stats"]["circuit_tripped_until"] = 0
            
        config_mgr.upsert_task(task)
        
        return json_resp({
            "ok": True,
            "message": "Cookie 更新成功！",
            "cookie_days": rem_days,
            "cookie_exp_date": exp_date
        })

    @app.route('/api/tasks/<task_id>/reset_circuit', methods=['POST'])
    async def api_reset_circuit(req, task_id):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        task = config_mgr.get_task(task_id)
        if not task:
            return json_resp({"ok": False, "error": "Task not found"}, 404)
        
        if "stats" not in task:
            task["stats"] = {}
        task["stats"]["circuit_tripped"] = False
        task["stats"]["consecutive_fails"] = 0
        task["stats"]["circuit_tripped_until"] = 0
        config_mgr.upsert_task(task)
        return json_resp({"ok": True, "message": "熔断保护已重置，任务恢复正常调度！"})

    @app.route('/api/http/debug', methods=['POST'])
    async def api_http_debug(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        
        task_id = data.get("task_id")
        if task_id:
            task = config_mgr.get_task(task_id)
            if not task:
                return json_resp({"ok": False, "error": "Task not found"}, 404)
            params = task.get("params", {})
            method = params.get("method", "GET").upper()
            url = params.get("url", "").strip()
            headers = params.get("headers", {})
            body = params.get("body", "")
        else:
            method = data.get("method", "GET").upper()
            url = data.get("url", "").strip()
            headers = data.get("headers", {})
            body = data.get("body", "")

        if not url:
            return json_resp({"ok": False, "error": "URL 不能为空"}, 400)

        t_start = time.ticks_ms()
        resp = None
        try:
            from app import http_client
            resp = http_client.request(method, url, headers=headers, data=body, timeout=15)
            latency = time.ticks_diff(time.ticks_ms(), t_start)
            status_code = resp.status_code
            resp_headers = resp.headers
            set_cookies = getattr(resp, "set_cookies", [])
            body_text = resp.text
            
            return json_resp({
                "ok": True,
                "status_code": status_code,
                "latency_ms": latency,
                "headers": resp_headers,
                "set_cookies": set_cookies,
                "body": body_text[:4096],
                "body_length": len(body_text),
                "truncated": len(body_text) > 4096
            })
        except Exception as e:
            latency = time.ticks_diff(time.ticks_ms(), t_start)
            return json_resp({
                "ok": False,
                "latency_ms": latency,
                "error": str(e)
            })
        finally:
            if resp:
                resp.close()

    @app.route('/api/tasks/ai_generate', methods=['POST'])
    async def api_tasks_ai_generate(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        url = data.get("url", "").strip()
        user_prompt = data.get("prompt", "").strip()
        curl_text = data.get("curl", "").strip()

        if not url and not user_prompt and not curl_text:
            return json_resp({"ok": False, "message": "请至少输入目标网址、需求描述或 cURL！"})

        ok, res = llm_client.analyze_and_generate_task(url, user_prompt, curl_text)
        if ok:
            return json_resp({"ok": True, "task": res})
        else:
            return json_resp({"ok": False, "message": res})

    # 5. LLM APIs
    @app.route('/api/llm/config', methods=['GET', 'POST'])
    async def api_llm_config(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        
        if req.method == 'POST':
            data = req.json or {}
            config_mgr.config["llm"] = {
                "base_url": data.get("base_url", "").strip(),
                "api_key": data.get("api_key", "").strip(),
                "model": data.get("model", "deepseek-chat").strip(),
                "temperature": float(data.get("temperature", 0.7))
            }
            config_mgr.add_llm_history(
                data.get("base_url"),
                data.get("api_key"),
                data.get("name", "")
            )
            config_mgr.save_config()
            return json_resp({"ok": True})
        
        return json_resp({
            "ok": True,
            "config": config_mgr.config.get("llm", {}),
            "history": config_mgr.config.get("llm_history", [])
        })

    @app.route('/api/llm/models', methods=['POST'])
    async def api_llm_models(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        base_url = data.get("base_url")
        api_key = data.get("api_key")
        ok, models, err_msg = llm_client.fetch_models(base_url, api_key)
        return json_resp({"ok": ok, "models": models, "error": err_msg})

    @app.route('/api/llm/test', methods=['POST'])
    async def api_llm_test(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        prompt = data.get("prompt", "你好，请用一句话回答：1+1等于几？")
        messages = [{"role": "user", "content": prompt}]
        ok, content = llm_client.chat_completion(messages)
        return json_resp({"ok": ok, "reply": content})

    # 6. Push APIs
    @app.route('/api/push/config', methods=['GET', 'POST'])
    async def api_push_config(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        
        if req.method == 'POST':
            data = req.json or {}
            config_mgr.config["notify"] = data
            config_mgr.save_config()
            return json_resp({"ok": True})
        
        return json_resp({"ok": True, "config": config_mgr.config.get("notify", {})})

    @app.route('/api/push/test', methods=['POST'])
    async def api_push_test(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        channel = data.get("channel", "all")
        override_cfg = data.get("override_cfg")
        res = notifier.send("【ESP32 测试推送】", "恭喜！ESP32-S3 消息推送配置成功！当前时间：" + config_mgr._format_now(), channel, override_cfg)
        return json_resp({"ok": True, "results": res})

    # 7. WiFi & System APIs
    @app.route('/api/wifi/scan')
    async def api_wifi_scan(req):
        nets = wifi_mgr.scan_networks()
        return json_resp({"ok": True, "networks": nets})

    @app.route('/api/wifi/connect', methods=['POST'])
    async def api_wifi_connect(req):
        data = req.json or {}
        ssid = data.get("ssid", "").strip()
        pwd = data.get("password", "").strip()
        if not ssid:
            return json_resp({"ok": False, "error": "SSID不能为空"}, 400)
        
        use_static = bool(data.get("use_static", False))
        static_ip = data.get("static_ip", "").strip()
        subnet = data.get("subnet", "").strip()
        gateway = data.get("gateway", "").strip()
        dns = data.get("dns", "").strip()

        ok, msg = wifi_mgr.set_sta(ssid, pwd, use_static, static_ip, subnet, gateway, dns)
        return json_resp({"ok": ok, "message": msg})

    @app.route('/api/wifi/ap_config', methods=['POST'])
    async def api_ap_config(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        ssid = data.get("ssid", "").strip()
        pwd = data.get("password", "").strip()
        ok, msg = wifi_mgr.update_ap_config(ssid, pwd)
        return json_resp({"ok": ok, "message": msg})

    @app.route('/api/system/password', methods=['POST'])
    async def api_system_pwd(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        enabled = bool(data.get("enabled", True))
        pwd = data.get("password", "admin").strip()
        if not pwd and enabled:
            return json_resp({"ok": False, "error": "密码不能为空"}, 400)
        
        config_mgr.config["auth"] = {"enabled": enabled, "password": pwd}
        config_mgr.save_config()
        return json_resp({"ok": True})

    @app.route('/api/system/reboot', methods=['POST'])
    async def api_reboot(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        # Delay reboot by 1 second to return response
        async def do_reset():
            await asyncio.sleep(1)
            machine.reset()
        asyncio.create_task(do_reset())
        return json_resp({"ok": True, "message": "Rebooting in 1s..."})

    @app.route('/api/system/trigger_report', methods=['POST'])
    async def api_trigger_report(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        if not scheduler:
            return json_resp({"ok": False, "error": "调度器未就绪"}, 500)
        
        report_text = scheduler.generate_weekly_report()
        res = notifier.send("【ESP32 系统健康体检与周报】", report_text, level="urgent")
        ok_backup, backup_file = config_mgr.auto_rotate_backup()
        
        return json_resp({
            "ok": True,
            "message": "体检周报已生成并推送，自动备份快照已创建！",
            "report": report_text,
            "backup_file": backup_file,
            "push_result": res
        })

    # 8. Logs APIs
    @app.route('/api/logs', methods=['GET', 'DELETE'])
    async def api_logs(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        if req.method == 'DELETE':
            config_mgr.clear_logs()
            return json_resp({"ok": True})
        category = req.args.get('category', 'all')
        logs = config_mgr.get_logs(category)
        return json_resp({
            "ok": True,
            "logs": logs,
            "counts": {
                "all": len(config_mgr.logs),
                "task": len([l for l in config_mgr.logs if l.get("category") == "task"]),
                "system": len([l for l in config_mgr.logs if l.get("category") == "system"])
            }
        })

    @app.route('/api/logs/download')
    async def api_logs_download(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        log_content = ""
        from app.config import SYSTEM_LOG_PATH
        try:
            with open(SYSTEM_LOG_PATH, 'r') as f:
                log_content = f.read()
        except Exception:
            pass
        if not log_content:
            lines = []
            for l in reversed(config_mgr.logs):
                lines.append("[{}] [{}] [{}] [{}] {}{}".format(
                    l.get("time", ""),
                    l.get("category", "task").upper(),
                    l.get("status", "info").upper(),
                    l.get("task_name", l.get("task_id", "")),
                    l.get("message", ""),
                    " | 诊断: " + l["ai_diag"] if l.get("ai_diag") else ""
                ))
            log_content = "\n".join(lines) + "\n"

        return Response(
            body=log_content,
            status_code=200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Disposition": "attachment; filename=esp32_system_logs.txt",
                "Access-Control-Allow-Origin": "*"
            }
        )

    # 8.1 Flash File Explorer APIs
    @app.route('/api/fs/list')
    async def api_fs_list(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        req_path = req.args.get('path', '/data').strip()
        if not req_path:
            req_path = '/'
        if '..' in req_path or not req_path.startswith('/'):
            return json_resp({"ok": False, "error": "非法路径访问"}, 400)
        
        entries = []
        try:
            for item in os.listdir(req_path):
                full_p = (req_path.rstrip('/') + '/' + item) if req_path != '/' else ('/' + item)
                is_dir = False
                size = 0
                try:
                    st = os.stat(full_p)
                    is_dir = (st[0] & 0x4000) != 0
                    size = st[6] if not is_dir else 0
                except Exception:
                    pass
                entries.append({
                    "name": item,
                    "is_dir": is_dir,
                    "size": size,
                    "path": full_p
                })
            entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            return json_resp({"ok": True, "path": req_path, "entries": entries})
        except Exception as e:
            return json_resp({"ok": False, "error": str(e)}, 500)

    @app.route('/api/fs/read')
    async def api_fs_read(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        req_path = req.args.get('path', '').strip()
        if not req_path or '..' in req_path or not req_path.startswith('/'):
            return json_resp({"ok": False, "error": "非法路径访问"}, 400)
        
        try:
            st = os.stat(req_path)
            if (st[0] & 0x4000) != 0:
                return json_resp({"ok": False, "error": "目标为目录，无法作为文本读取"}, 400)
            
            size = st[6]
            max_read = 16 * 1024
            truncated = (size > max_read)
            
            with open(req_path, 'rb') as f:
                raw = f.read(max_read)
            
            try:
                content = raw.decode('utf-8')
            except Exception:
                content = "[非 UTF-8 文本或二进制数据文件，无法在线预览]"

            return json_resp({
                "ok": True,
                "path": req_path,
                "size": size,
                "truncated": truncated,
                "content": content
            })
        except Exception as e:
            return json_resp({"ok": False, "error": str(e)}, 500)

    @app.route('/api/fs/delete', methods=['POST'])
    async def api_fs_delete(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        req_path = data.get("path", "").strip()
        if not req_path or '..' in req_path or not req_path.startswith('/data/'):
            return json_resp({"ok": False, "error": "安全防护拦截：仅允许清理 /data 目录下的用户资产文件！"}, 403)
        if req_path in ['/data', '/data/', '/data/config.json', '/data/tasks.json']:
            return json_resp({"ok": False, "error": "系统核心配置文件受保护，禁止删除！"}, 403)

        try:
            st = os.stat(req_path)
            is_dir = (st[0] & 0x4000) != 0
            if is_dir:
                os.rmdir(req_path)
            else:
                os.remove(req_path)
            config_mgr.add_log("system", "闪存运维", "warning", "删除了闪存文件: " + req_path, category="system")
            return json_resp({"ok": True, "message": "文件已成功删除"})
        except Exception as e:
            return json_resp({"ok": False, "error": "删除失败: " + str(e)}, 500)

    @app.route('/api/fs/rename', methods=['POST'])
    async def api_fs_rename(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        old_path = data.get("old_path", "").strip()
        new_path = data.get("new_path", "").strip()
        if not old_path or not new_path or '..' in old_path or '..' in new_path:
            return json_resp({"ok": False, "error": "非法路径参数"}, 400)
        if not old_path.startswith('/data/') or not new_path.startswith('/data/'):
            return json_resp({"ok": False, "error": "安全防护拦截：仅允许在 /data 目录下重命名文件！"}, 403)
        if old_path in ['/data/config.json', '/data/tasks.json']:
            return json_resp({"ok": False, "error": "系统核心配置文件禁止重命名！"}, 403)

        try:
            os.rename(old_path, new_path)
            config_mgr.add_log("system", "闪存运维", "info", "重命名文件 {} -> {}".format(old_path, new_path), category="system")
            return json_resp({"ok": True, "message": "重命名成功"})
        except Exception as e:
            return json_resp({"ok": False, "error": "重命名失败: " + str(e)}, 500)

    # 9. Backup & Restore APIs
    @app.route('/api/backup/export')
    async def api_backup_export(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        backup_bundle = {
            "version": "1.0",
            "timestamp": config_mgr._format_now(),
            "config": config_mgr.config,
            "tasks": config_mgr.tasks
        }
        return Response(
            body=json.dumps(backup_bundle),
            status_code=200,
            headers={
                "Content-Type": "application/json",
                "Content-Disposition": "attachment; filename=esp32_backup.json",
                "Access-Control-Allow-Origin": "*"
            }
        )

    @app.route('/api/backup/import', methods=['POST'])
    async def api_backup_import(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        cfg = data.get("config")
        tasks = data.get("tasks")
        if not isinstance(cfg, dict) and not isinstance(tasks, list):
            return json_resp({"ok": False, "error": "备份文件不合法，需包含有效的 config 或 tasks！"}, 400)

        if isinstance(cfg, dict):
            config_mgr.config = cfg
            config_mgr.save_config()
        if isinstance(tasks, list):
            config_mgr.tasks = tasks
            config_mgr.save_tasks()

        if rgb_manager:
            rgb_manager._init_hardware()
            rgb_manager.set_state("idle")

        return json_resp({"ok": True, "message": "配置与自动化任务已成功恢复！"})

    @app.route('/api/backup/snapshots')
    async def api_backup_snapshots(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        snapshots = config_mgr.list_backups()
        return json_resp({"ok": True, "snapshots": snapshots})

    @app.route('/api/backup/snapshots/restore', methods=['POST'])
    async def api_backup_restore_snapshot(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        data = req.json or {}
        filename = data.get("filename", "")
        if not filename:
            return json_resp({"ok": False, "error": "文件名不能为空"}, 400)
        ok, msg = config_mgr.restore_backup(filename)
        if ok:
            if rgb_manager:
                rgb_manager._init_hardware()
                rgb_manager.set_state("idle")
            return json_resp({"ok": True, "message": msg})
        return json_resp({"ok": False, "error": msg}, 500)

    # 10. RGB LED APIs
    @app.route('/api/led/config', methods=['GET', 'POST'])
    async def api_led_config(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        if req.method == 'POST':
            data = req.json or {}
            enabled = data.get("enabled", True)
            pin = data.get("pin", 48)
            brightness = data.get("brightness", 20)
            if rgb_manager:
                rgb_manager.update_config(enabled, pin, brightness)
            else:
                config_mgr.config["led"] = {"enabled": enabled, "pin": pin, "brightness": brightness}
                config_mgr.save_config()
            return json_resp({"ok": True})

        led_cfg = config_mgr.config.get("led", {"enabled": True, "pin": 48, "brightness": 20})
        is_avail = rgb_manager.is_available if rgb_manager else False
        return json_resp({"ok": True, "config": led_cfg, "available": is_avail})

    @app.route('/api/led/test', methods=['POST'])
    async def api_led_test(req):
        if not check_auth(req):
            return json_resp({"error": "Unauthorized"}, 401)
        if not rgb_manager:
            return json_resp({"ok": False, "error": "RGB硬件管理器未初始化"}, 500)
        data = req.json or {}
        color = data.get("color", "green")
        color_map = {
            "green": (0, 255, 0),
            "blue": (0, 120, 255),
            "red": (255, 0, 0),
            "yellow": (255, 140, 0),
            "white": (255, 255, 255)
        }
        rgb = color_map.get(color, (0, 255, 0))
        ok, msg = rgb_manager.test(rgb[0], rgb[1], rgb[2])
        return json_resp({"ok": ok, "message": msg})

    return app
