import json
import time
import asyncio
import gc
from app import http_client

def decode_b64_json(s):
    try:
        try:
            import ubinascii as binascii
        except ImportError:
            import binascii
        s = s.replace("-", "+").replace("_", "/")
        s += "=" * ((4 - len(s) % 4) % 4)
        raw = binascii.a2b_base64(s).decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None

def parse_jwt_exp(token_str):
    try:
        start = token_str.find("eyJ")
        if start == -1:
            return None
        sub = token_str[start:].split(";")[0].split("&")[0].split(" ")[0].strip("\"'")
        parts = sub.split(".")
        data = None
        if len(parts) >= 2:
            data = decode_b64_json(parts[1])
        if not data and len(parts) >= 1:
            data = decode_b64_json(parts[0])

        if not isinstance(data, dict):
            return None

        if "exp" in data and isinstance(data["exp"], (int, float)):
            return int(data["exp"])
        if "expires_at" in data and isinstance(data["expires_at"], (int, float)):
            return int(data["expires_at"])
        if "ts" in data and isinstance(data["ts"], (int, float)):
            return int(data["ts"]) + 30 * 86400
        if "iat" in data and isinstance(data["iat"], (int, float)):
            return int(data["iat"]) + 30 * 86400
        return None
    except Exception:
        return None

def get_cookie_expiration_days(headers):
    """
    扫描 Headers 中的 Cookie 或 Authorization，提取 JWT exp 过期时间，并计算剩余天数及具体到期日期。
    返回: (has_jwt: bool, remaining_days: float or None, exp_timestamp: int or None, exp_date_str: str)
    """
    if not isinstance(headers, dict):
        return False, None, None, ""
    for k, v in headers.items():
        if not isinstance(v, str):
            continue
        if "eyJ" in v:
            exp = parse_jwt_exp(v)
            if exp:
                epoch_year = time.gmtime(0)[0]
                offset = 946684800 if epoch_year == 2000 else 0
                now_unix = time.time() + offset
                diff_sec = exp - now_unix
                days = round(diff_sec / 86400.0, 1)

                t = time.localtime(exp - offset + 8 * 3600)
                exp_date_str = "{:04d}-{:02d}-{:02d}".format(t[0], t[1], t[2])
                return True, days, exp, exp_date_str
    return False, None, None, ""

def extract_reward(result_msg, response_text):
    combined = (result_msg + " " + response_text).strip()
    try:
        import re
        m = re.search(r'(?:获得|奖励|赠送|领到|增加|领取)\s*(\d+)\s*(?:个|颗|点|枚)?\s*(鸡腿|京豆|积分|经验|米粒|金币|铜币|收益)?', combined)
        if m:
            val = int(m.group(1))
            unit = m.group(2) if m.group(2) else ""
            return val, unit

        m2 = re.search(r'(\d+)\s*(?:个|颗|点|枚)\s*(鸡腿|京豆|积分|经验|米粒|金币|铜币)', combined)
        if m2:
            return int(m2.group(1)), m2.group(2)
    except Exception:
        pass

    units = ["鸡腿", "京豆", "金币", "积分", "经验", "米粒", "铜币", "点数"]
    for u in units:
        if u in combined:
            try:
                import re
                m = re.search(r'(\d+)\s*(?:个|颗|点|枚)?\s*' + u, combined)
                if m:
                    return int(m.group(1)), u
            except Exception:
                pass

    try:
        data = json.loads(response_text)
        if isinstance(data, dict):
            for k in ["gain", "reward", "points", "beans"]:
                if k in data and isinstance(data[k], (int, float)):
                    return int(data[k]), "点"
    except Exception:
        pass

    return None, None

class TaskExecutor:
    def __init__(self, config_mgr, llm_client, notifier, rgb_manager=None):
        self.config_mgr = config_mgr
        self.llm_client = llm_client
        self.notifier = notifier
        self.rgb_manager = rgb_manager
        self.is_running = False

    def _update_task_stats(self, task, success, result_msg, response_text, now_str):
        try:
            today = now_str.split(" ")[0]
            stats = task.setdefault("stats", {})
            history = stats.setdefault("history", {})

            y_t = time.localtime(time.time() + 8 * 3600 - 86400)
            yesterday = "{:04d}-{:02d}-{:02d}".format(y_t[0], y_t[1], y_t[2])

            last_checkin = stats.get("last_checkin_date", "")

            if success:
                if last_checkin != today:
                    if last_checkin == yesterday:
                        stats["streak"] = stats.get("streak", 0) + 1
                    else:
                        stats["streak"] = 1
                    stats["last_checkin_date"] = today
                    stats["total_success"] = stats.get("total_success", 0) + 1

                r_val, r_unit = extract_reward(result_msg, response_text)
                if r_val is not None:
                    old_day = history.get(today, {})
                    if not old_day.get("reward_counted"):
                        stats["total_reward"] = stats.get("total_reward", 0) + r_val
                    if r_unit:
                        stats["reward_unit"] = r_unit
                    history[today] = {
                        "status": "success",
                        "reward": r_val,
                        "unit": stats.get("reward_unit", r_unit),
                        "reward_counted": True
                    }
                else:
                    if today not in history or history[today].get("status") != "success":
                        history[today] = {"status": "success"}
            else:
                if today not in history:
                    history[today] = {"status": "fail"}

            if len(history) > 35:
                sorted_keys = sorted(history.keys())
                for k in sorted_keys[:-35]:
                    del history[k]
        except Exception as e:
            print("[Executor] Stats update error:", e)

    async def run_task(self, task, jitter_info=""):
        self.config_mgr.apply_power_freq(busy=True)
        task_id = task.get("id", "")
        task_name = task.get("name", "未命名任务")
        ttype = task.get("type", "checkin")
        retries = task.get("retry_count", 1)
        
        print("[Executor] Starting task:", task_name, "(type:", ttype, ")")
        if self.rgb_manager:
            self.rgb_manager.set_state("busy")
        gc.collect()
        
        success = False
        result_msg = ""
        error_details = ""

        for attempt in range(retries + 1):
            if attempt > 0:
                print("[Executor] Retrying task", task_name, attempt)
                await asyncio.sleep(3)
            
            try:
                if ttype == "checkin" or ttype == "custom_http":
                    success, result_msg, error_details = self._run_http_task(task)
                elif ttype == "ai_digest":
                    success, result_msg, error_details = self._run_ai_digest_task(task)
                elif ttype == "uptime":
                    success, result_msg, error_details = self._run_uptime_task(task)
                else:
                    success, result_msg, error_details = False, "未知任务类型", "Unknown type"

                if success:
                    break
            except Exception as e:
                success = False
                error_details = "Exception: " + str(e)
                result_msg = "执行异常: " + str(e)

        # Record results
        now_str = self.config_mgr._format_now()
        task["last_run"] = now_str
        task["last_status"] = "success" if success else "fail"
        task["last_result"] = result_msg

        # Circuit Breaker Tracking
        stats = task.setdefault("stats", {})
        circuit_tripped_just_now = False
        if success:
            stats["consecutive_fails"] = 0
            stats["circuit_tripped"] = False
            stats["circuit_tripped_until"] = 0
        else:
            fails = stats.get("consecutive_fails", 0) + 1
            stats["consecutive_fails"] = fails
            if fails >= 3 and not stats.get("circuit_tripped"):
                stats["circuit_tripped"] = True
                epoch_year = time.gmtime(0)[0]
                offset = 946684800 if epoch_year == 2000 else 0
                now_unix = time.time() + offset
                stats["circuit_tripped_until"] = now_unix + 6 * 3600
                circuit_tripped_just_now = True
                self.config_mgr.add_log(task_id, task_name, "warning", "🛡️ 防封熔断触发: 连续失败 3 次，保护性暂停定时调度 6 小时", category="system")

        # Update per-task streak & rewards stats for checkin
        if ttype == "checkin":
            self._update_task_stats(task, success, result_msg, error_details, now_str)

        self.config_mgr.save_tasks()

        ai_diag = ""
        if not success:
            # AI Diagnosis
            if task.get("ai_diagnose", True):
                print("[Executor] Calling LLM for failure diagnosis...")
                try:
                    ai_diag = self.llm_client.diagnose_failure(task_name, error_details)
                except Exception as ex:
                    ai_diag = "诊断异常: " + str(ex)

            # Failure Notification
            if task.get("notify_on_failure", True):
                notify_content = "任务名称：{}\n执行状态：❌ 失败\n失败信息：{}\n".format(task_name, result_msg)
                if circuit_tripped_just_now:
                    notify_content += "\n🛡️ **防封熔断保护已触发**：任务已连续失败 3 次！为保护您的家庭宽带 IP 不被目标网站防火墙拉黑，已自动暂停该任务定时调度 6 小时。\n"
                if jitter_info:
                    notify_content += "防封延迟：已随机推迟 {}\n".format(jitter_info)
                if ai_diag:
                    notify_content += "\n🤖 **AI 诊断建议**：\n" + ai_diag
                self.notifier.send("【任务失败告警】" + task_name, notify_content, level="fail")
        else:
            # Success Notification
            if task.get("notify_on_success", False) or ttype == "ai_digest":
                notify_content = "任务名称：{}\n执行状态：✅ 成功\n\n{}".format(task_name, result_msg)
                if ttype == "checkin" and "stats" in task:
                    st = task["stats"]
                    streak = st.get("streak", 1)
                    tot_s = st.get("total_success", 1)
                    tot_r = st.get("total_reward", 0)
                    r_unit = st.get("reward_unit", "")
                    notify_content += "\n\n🔥 **连续打卡**：{} 天\n🎯 **累计达标**：{} 次".format(streak, tot_s)
                    if tot_r > 0 and r_unit:
                        notify_content += "\n🎁 **累计斩获**：{} {}".format(tot_r, r_unit)
                if jitter_info:
                    notify_content += "\n🎲 **防封随机延迟**：已随机推迟 {} 执行".format(jitter_info)
                self.notifier.send("【任务执行结果】" + task_name, notify_content, level="success")

        # Cookie / JWT expiration pre-warning (Warn proactively if <= 3 days)
        try:
            params = task.get("params", {})
            headers = params.get("headers", {})
            has_jwt, rem_days, exp_ts, exp_date = get_cookie_expiration_days(headers)
            if has_jwt and rem_days is not None and rem_days <= 3.0:
                today_str = now_str.split(" ")[0]
                if task.get("last_cookie_warn") != today_str:
                    task["last_cookie_warn"] = today_str
                    self.config_mgr.save_tasks()
                    warn_title = "⚠️【Cookie 即将过期预警】" + task_name
                    warn_body = (
                        "任务名称：{}\n"
                        "预警状态：{}\n"
                        "到期日期：{}\n"
                        "登录凭据 (Cookie/Token) 预计将在 **{} 天** 后失效。\n\n"
                        "💡 建议您抽空在电脑浏览器中重新登录并抓取最新 Cookie 填入任务表单，以免后续打卡中断！"
                    ).format(
                        task_name,
                        "已过期！" if rem_days <= 0 else "仅剩 {} 天".format(rem_days),
                        exp_date or "未知",
                        rem_days
                    )
                    self.notifier.send(warn_title, warn_body, level="fail")
                    print("[Executor] Sent cookie expiration warning for", task_name, "rem_days:", rem_days)
        except Exception as ex:
            print("[Executor] Cookie expiration check error:", ex)

        if self.rgb_manager:
            self.rgb_manager.set_state("idle" if success else "error")
        gc.collect()

        log_msg = result_msg + (" (防封延迟: {})".format(jitter_info) if jitter_info else "")
        self.config_mgr.add_log(task_id, task_name, "success" if success else "fail", log_msg, ai_diag, category="task")
        print("[Executor] Finished task:", task_name, "status:", "success" if success else "fail")
        self.config_mgr.apply_power_freq(busy=False)
        return success, result_msg, ai_diag

    def _evaluate_task_response(self, task, status, text):
        snippet = text[:400]
        params = task.get("params", {})
        keyword = params.get("match_keyword", "").strip()
        ttype = task.get("type", "checkin")

        # 1. Attempt to extract structured summary from JSON
        extracted_msg = ""
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for k in ["message", "msg", "detail", "info", "notice"]:
                    if k in data and isinstance(data[k], str):
                        extracted_msg = data[k]
                        break
        except Exception:
            pass

        detail_suffix = "\n响应内容：" + (extracted_msg if extracted_msg else snippet)

        # 2. Check for Cloudflare / Anti-bot challenges
        text_lower = text.lower()
        if "just a moment..." in text_lower or "cf-turnstile" in text_lower or (status == 403 and "cloudflare" in text_lower):
            return False, "❌ 触发网站人机验证拦截(Cloudflare WAF)" + detail_suffix, snippet

        # 3. Check for expired/missing credentials
        auth_fail_kws = ["未登录", "请先登录", "token expired", "user not found", "未授权", "cookie已失效", "登录超时", "invalid token"]
        for afk in auth_fail_kws:
            if afk in text_lower:
                return False, "❌ 登录凭证失效或未登录 (需更新 Cookie)" + detail_suffix, snippet

        # 4. Checkin Idempotent Success ("already signed in" patterns)
        if ttype == "checkin":
            already_signed_kws = [
                "今天已完成签到", "已经签到", "已签到", "请勿重复", "明天再来", "明日再来", 
                "已经打卡", "今日已打卡", "重复签到", "重复操作", "already signed", "already checked"
            ]
            for ask in already_signed_kws:
                if ask in text:
                    return True, "今日已完成签到 (包含 '{}', HTTP {}){}".format(ask, status, detail_suffix), snippet

        # 5. Check user match_keyword (supports multiple keywords separated by | or comma)
        if keyword:
            keywords = [k.strip() for k in keyword.replace(",", "|").split("|") if k.strip()]
            for kw in keywords:
                if kw in text:
                    return True, "匹配成功 (包含关键字 '{}', HTTP {}){}".format(kw, status, detail_suffix), snippet
            return False, "关键字未匹配 (缺少 '{}', HTTP {}){}".format(keyword, status, detail_suffix), snippet

        # 6. Fallback standard HTTP status check
        if 200 <= status < 300:
            return True, "请求成功 (HTTP {}){}".format(status, detail_suffix), snippet
        return False, "HTTP 状态码异常 ({}){}".format(status, detail_suffix), snippet

    def _run_http_task(self, task):
        params = task.get("params", {})
        url = params.get("url", "").strip()
        if not url:
            return False, "URL为空", "No URL provided"

        method = params.get("method", "GET").upper()
        headers = params.get("headers", {})
        body = params.get("body", "")

        resp = None
        try:
            resp = http_client.request(method, url, headers=headers, data=body, timeout=15)
            status = resp.status_code
            text = resp.text
            ok, msg, err = self._evaluate_task_response(task, status, text)
            if ok and hasattr(resp, "set_cookies") and resp.set_cookies:
                if self._merge_set_cookies(task, resp.set_cookies):
                    msg += " (🍪 凭据已自动续期)"
            return ok, msg, err
        except Exception as e:
            return False, "网络请求错误: " + str(e), str(e)
        finally:
            if resp:
                resp.close()

    def _merge_set_cookies(self, task, set_cookies):
        try:
            params = task.setdefault("params", {})
            headers = params.setdefault("headers", {})
            current_cookie_str = headers.get("Cookie") or headers.get("cookie") or ""
            
            cookie_dict = {}
            if current_cookie_str:
                for part in current_cookie_str.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookie_dict[k.strip()] = v.strip()
            
            updated = False
            for sc in set_cookies:
                first_seg = sc.split(";")[0].strip()
                if "=" in first_seg:
                    k, v = first_seg.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k and k.lower() not in ("path", "domain", "expires", "max-age", "samesite", "priority"):
                        if cookie_dict.get(k) != v:
                            cookie_dict[k] = v
                            updated = True
            
            if updated:
                new_cookie_str = "; ".join(["{}={}".format(k, v) for k, v in cookie_dict.items()])
                if "cookie" in headers and "Cookie" not in headers:
                    headers["cookie"] = new_cookie_str
                else:
                    headers["Cookie"] = new_cookie_str
                
                self.config_mgr.upsert_task(task)
                print("[Executor] Auto-renewed Set-Cookie for task:", task.get("name"))
                return True
            return False
        except Exception as e:
            print("[Executor] Error merging Set-Cookie:", e)
            return False

def _clean_html_noise(html_text, max_bytes=30*1024):
    """
    轻量快速剔除 HTML 中的 script、style、svg、head、注释等噪音，
    提取出高密度的有效正文文本，并截取到指定 max_bytes 以内
    """
    if not html_text:
        return ""
    text = html_text
    lower = text.lower()
    
    noise_tags = [
        ("<script", "</script>"),
        ("<style", "</style>"),
        ("<svg", "</svg>"),
        ("<head", "</head>"),
        ("<!--", "-->"),
    ]
    
    for open_tag, close_tag in noise_tags:
        while True:
            idx = lower.find(open_tag)
            if idx == -1:
                break
            end_idx = lower.find(close_tag, idx + len(open_tag))
            if end_idx == -1:
                text = text[:idx]
                lower = lower[:idx]
                break
            else:
                end_idx += len(close_tag)
                text = text[:idx] + " " + text[end_idx:]
                lower = lower[:idx] + " " + lower[end_idx:]
            if len(text) <= max_bytes and open_tag not in lower:
                break

    clean_chars = []
    in_tag = False
    for ch in text:
        if ch == '<':
            in_tag = True
            clean_chars.append(' ')
        elif ch == '>':
            in_tag = False
            clean_chars.append(' ')
        elif not in_tag:
            clean_chars.append(ch)
        if len(clean_chars) >= max_bytes * 2:
            break
            
    filtered = "".join(clean_chars)
    lines = []
    for line in filtered.split("\n"):
        line_s = " ".join(line.split())
        if line_s:
            lines.append(line_s)
    
    res = "\n".join(lines)
    return res[:max_bytes]

    def _run_ai_digest_task(self, task):
        params = task.get("params", {})
        source_url = params.get("source_url", "").strip()
        prompt = params.get("prompt", "请将以下内容精炼概括为3~5条核心摘要，语言简练客观：").strip()

        # 读取系统配置中的截取上限 (5 ~ 128 KB)
        llm_cfg = self.config_mgr.config.get("llm", {})
        try:
            max_kb = max(5, min(128, int(llm_cfg.get("digest_max_kb", 30))))
        except Exception:
            max_kb = 30
        max_bytes = max_kb * 1024

        raw_text = ""
        if source_url:
            resp = None
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                }
                task_headers = params.get("headers", {})
                if isinstance(task_headers, dict):
                    headers.update(task_headers)

                print("[Executor] AI Digest fetching:", source_url, "max_kb:", max_kb)
                resp = http_client.get(source_url, headers=headers, timeout=20)
                if resp.status_code == 200:
                    raw_text = _clean_html_noise(resp.text, max_bytes=max_bytes)
                else:
                    return False, "获取订阅源失败 HTTP {}".format(resp.status_code), resp.text[:300]
            except Exception as e:
                return False, "抓取订阅源出错: " + str(e), str(e)
            finally:
                if resp:
                    resp.close()
                gc.collect()
        else:
            raw_text = params.get("raw_content", "").strip()

        messages = [
            {"role": "system", "content": "你是一个严谨的信息提炼与科技速报分析师。"}
        ]
        if raw_text:
            # 携带抓取正文模式
            messages.append({"role": "user", "content": prompt + "\n\n【抓取内容】\n" + raw_text})
        else:
            # 纯 Agent 模式：大模型自主联网分析与检索，免除单片机爬虫
            messages.append({"role": "user", "content": prompt})

        ok, ai_res = self.llm_client.chat_completion(messages)
        if ok:
            return True, ai_res, ""
        else:
            return False, "大模型提炼失败: " + ai_res, ai_res

    def _run_uptime_task(self, task):
        params = task.get("params", {})
        url = params.get("target_url", "").strip()
        if not url:
            return False, "监控目标URL为空", "No target URL"

        resp = None
        try:
            resp = http_client.get(url, timeout=10)
            if 200 <= resp.status_code < 400:
                return True, "服务正常 (HTTP {})".format(resp.status_code), ""
            else:
                return False, "服务异常 (HTTP {})".format(resp.status_code), "HTTP Status: {}".format(resp.status_code)
        except Exception as e:
            return False, "服务无法连通: " + str(e), str(e)
        finally:
            if resp:
                resp.close()
