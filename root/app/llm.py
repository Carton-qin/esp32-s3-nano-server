import urequests
import json

class LLMClient:
    def __init__(self, config_mgr):
        self.config_mgr = config_mgr

    def _get_endpoints(self, base_url):
        url = base_url.strip().rstrip("/")
        if not url:
            url = "https://api.deepseek.com/v1"
        if not url.endswith("/v1"):
            return url + "/v1/models", url + "/v1/chat/completions"
        return url + "/models", url + "/chat/completions"

    def fetch_models(self, base_url=None, api_key=None):
        llm_cfg = self.config_mgr.config.get("llm", {})
        base_url = base_url or llm_cfg.get("base_url", "")
        api_key = api_key or llm_cfg.get("api_key", "")
        fallback_models = ["deepseek-chat", "deepseek-reasoner", "gpt-4o-mini", "qwen-plus"]
        
        if not base_url or not api_key:
            return False, fallback_models, "Base URL 或 API Key 为空"

        models_url, _ = self._get_endpoints(base_url)
        is_agentrouter = "agentrouter" in base_url.lower()
        ua = "claude-cli/2.1.119 (external, cli)" if is_agentrouter else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        headers = {
            "Authorization": "Bearer " + api_key,
            "User-Agent": ua
        }
        if is_agentrouter:
            headers["Originator"] = "claude-cli"

        resp = None
        try:
            print("[LLM] Fetching models from:", models_url)
            resp = urequests.get(models_url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                model_list = []
                for item in data.get("data", []):
                    if isinstance(item, dict) and "id" in item:
                        model_list.append(item["id"])
                    elif isinstance(item, str):
                        model_list.append(item)
                if model_list:
                    model_list.sort()
                    return True, model_list, ""
                return False, fallback_models, "响应中未发现有效模型列表"
            else:
                err_text = resp.text[:200]
                print("[LLM] Fetch models failed HTTP", resp.status_code, err_text)
                return False, fallback_models, "HTTP " + str(resp.status_code) + ": " + err_text
        except Exception as e:
            print("[LLM] Failed to fetch models:", e)
            return False, fallback_models, str(e)
        finally:
            if resp:
                resp.close()

    def chat_completion(self, messages, base_url=None, api_key=None, model=None, temperature=0.7, max_tokens=4096):
        llm_cfg = self.config_mgr.config.get("llm", {})
        base_url = base_url or llm_cfg.get("base_url", "")
        api_key = api_key or llm_cfg.get("api_key", "")
        model = model or llm_cfg.get("model", "deepseek-chat")
        
        if not api_key:
            return False, "API Key not configured"

        _, chat_url = self._get_endpoints(base_url)
        is_agentrouter = "agentrouter" in (base_url or "").lower()
        ua = "claude-cli/2.1.119 (external, cli)" if is_agentrouter else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        headers = {
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "User-Agent": ua
        }
        if is_agentrouter:
            headers["Originator"] = "claude-cli"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        resp = None
        try:
            print("[LLM] Calling chat completion:", chat_url, "model:", model)
            resp = urequests.post(chat_url, headers=headers, data=json.dumps(payload))
            if resp.status_code == 200:
                data = resp.json()
                choice = data.get("choices", [{}])[0]
                content = choice.get("message", {}).get("content", "")
                if not content:
                    if choice.get("finish_reason") == "length":
                        return False, "模型推理消耗了全部Token，未能输出最终内容，请重试或更换模型"
                    return False, "大模型返回内容为空"
                return True, content
            else:
                err_msg = "HTTP {}: {}".format(resp.status_code, resp.text[:200])
                return False, err_msg
        except Exception as e:
            return False, str(e)
        finally:
            if resp:
                resp.close()

    def diagnose_failure(self, task_name, error_details):
        llm_cfg = self.config_mgr.config.get("llm", {})
        if not llm_cfg.get("api_key"):
            return "未配置大模型 API Key，无法进行 AI 智能诊断。"

        system_prompt = (
            "你是一个轻量级自动化任务的故障诊断助手。"
            "请根据任务名称和错误响应，极其精炼地输出1~2句话："
            "明确指出失败原因（如Cookie过期、被风控、接口变更、网络超时），并提供一步排查指引。不要客套话。"
        )
        user_prompt = "任务：{}\n错误详情/响应片段：\n{}".format(task_name, error_details[:500])
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        ok, res = self.chat_completion(messages, temperature=0.3)
        if ok:
            return res.strip()
        return "AI诊断调用失败：" + res

    def _extract_and_sanitize_curl(self, curl_text):
        if not curl_text:
            return "", ""
        extracted_cookie = ""
        sanitized_lines = []
        # Strip Windows cmd caret escapes
        clean_text = curl_text.replace('^"', '"').replace("^^", "^")
        lines = clean_text.split("\n")
        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue
            lower = line_str.lower()
            # Filter out unnecessary browser tracking/platform headers to prevent bloated prompts
            if any(k in lower for k in ["sec-ch-ua", "sec-fetch-", "priority:", "accept-language:", "accept-encoding:"]):
                continue

            # Standard header format: -H "Cookie: ..." or -H 'cookie: ...'
            if "cookie:" in lower:
                c_idx = lower.find("cookie:")
                raw_c = line_str[c_idx + 7:].strip()
                while raw_c and raw_c[-1] in ("'", '"', "\\", " "):
                    raw_c = raw_c[:-1]
                while raw_c and raw_c[0] in ("'", '"', " "):
                    raw_c = raw_c[1:]
                if raw_c:
                    extracted_cookie = raw_c
                # Mask cookie in prompt to avoid triggering LLM API WAF / content-blocking filters
                line_str = line_str[:c_idx + 7] + " [MASKED_COOKIE]'" + (" \\" if line_str.endswith("\\") else "")
            elif "-b " in line_str or "--cookie " in line_str:
                # -b "cookie" or --cookie "cookie"
                for flag in ["--cookie ", "-b "]:
                    if flag in line_str:
                        b_idx = line_str.find(flag)
                        raw_c = line_str[b_idx + len(flag):].strip()
                        while raw_c and raw_c[-1] in ("'", '"', "\\", " "):
                            raw_c = raw_c[:-1]
                        while raw_c and raw_c[0] in ("'", '"', " "):
                            raw_c = raw_c[1:]
                        if raw_c:
                            extracted_cookie = raw_c
                        line_str = line_str[:b_idx + len(flag)] + "'[MASKED_COOKIE]'" + (" \\" if line_str.endswith("\\") else "")
                        break
            elif "authorization: bearer " in lower:
                a_idx = lower.find("authorization: bearer ")
                line_str = line_str[:a_idx + 22] + " [MASKED_TOKEN]'" + (" \\" if line_str.endswith("\\") else "")
            sanitized_lines.append(line_str)
        return "\n".join(sanitized_lines), extracted_cookie

    def analyze_and_generate_task(self, url="", user_prompt="", curl_text=""):
        llm_cfg = self.config_mgr.config.get("llm", {})
        if not llm_cfg.get("api_key"):
            return False, "请先在「大模型中枢」配置 API Key（如 DeepSeek、OpenAI、通义千问等）后再使用 AI 自动生成功能！"

        sanitized_curl, extracted_cookie = self._extract_and_sanitize_curl(curl_text)

        system_prompt = (
            "你是一个专业的自动化任务配置专家。\n"
            "【极度重要】严禁冗长思考，必须直接输出目标合法的纯 JSON 对象，严禁包含 markdown 代码块或任何多余文字！\n"
            "【支持的任务类型 type】\n"
            "- checkin: 网站自动签到打卡，通常需要登录凭据(Cookie/Token)、POST/GET 请求与成功关键字匹配\n"
            "- ai_digest: AI 智能情报/早报/摘要订阅，抓取源内容并用大模型归纳提炼\n"
            "- uptime: 网站或服务存活探测哨兵\n"
            "- custom_http: 通用 HTTP 调用或智能家居 Webhook 触发\n\n"
            "【支持的调度类型 schedule_type】\n"
            "- interval: 间隔循环，schedule_val 填分钟数字字符串，如 '10' 或 '30'\n"
            "- daily: 每日固定时间点，schedule_val 填 'HH:MM' 如 '08:30'\n"
            "- cron: 5段标准 Linux Crontab，如 '0 9 * * *'，若用户提及时间范围（如8-9点），可给出随机分钟如 '42 8 * * *'\n\n"
            "【输出规范模板】\n"
            "{\n"
            '  "name": "任务名称",\n'
            '  "type": "checkin",\n'
            '  "schedule_type": "daily",\n'
            '  "schedule_val": "08:30",\n'
            '  "params": {\n'
            '    "url": "https://...",\n'
            '    "method": "POST",\n'
            '    "headers": {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 ...", "Cookie": "..."},\n'
            '    "body": "{}",\n'
            '    "match_keyword": "success|签到成功|今天已完成签到|已经签到",\n'
            '    "source_url": "",\n'
            '    "prompt": "",\n'
            '    "target_url": ""\n'
            '  },\n'
            '  "retry_count": 1,\n'
            '  "ai_diagnose": true,\n'
            '  "notify_on_failure": true,\n'
            '  "notify_on_success": true\n'
            "}"
        )

        user_content = ""
        if url:
            user_content += "【目标 URL】\n" + url + "\n\n"
        if user_prompt:
            user_content += "【用户需求描述】\n" + user_prompt + "\n\n"
        if sanitized_curl:
            user_content += "【请求抓包/cURL】\n" + sanitized_curl + "\n\n"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        ok, content = self.chat_completion(messages, temperature=0.1, max_tokens=4096)
        if not ok:
            return False, "大模型请求失败: " + str(content)

        raw = content.strip()
        s_idx = raw.find("{")
        e_idx = raw.rfind("}")
        if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
            raw = raw[s_idx:e_idx+1]

        try:
            task_data = json.loads(raw)
            # Restore user's real cookie extracted locally
            if extracted_cookie and "params" in task_data:
                if "headers" not in task_data["params"] or not isinstance(task_data["params"]["headers"], dict):
                    task_data["params"]["headers"] = {}
                headers_dict = task_data["params"]["headers"]
                if "cookie" in headers_dict:
                    del headers_dict["cookie"]
                headers_dict["Cookie"] = extracted_cookie

            # Enhance match_keyword for checkin tasks to be tolerant
            if task_data.get("type") == "checkin" and "params" in task_data:
                kw = task_data["params"].get("match_keyword", "")
                if kw and "已完成签到" not in kw and "已签到" not in kw:
                    task_data["params"]["match_keyword"] = kw + "|今天已完成签到|已经签到"

            # Set notify_on_success if user asked for notifications
            if user_prompt and any(k in user_prompt for k in ["推送", "微信", "成功后", "企业微信", "钉钉", "飞书"]):
                task_data["notify_on_success"] = True
            return True, task_data
        except Exception as e:
            return False, "解析 AI 返回的 JSON 失败: " + str(e) + "\n原始输出: " + content[:200]

