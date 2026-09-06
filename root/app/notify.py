import urequests
import json
import time

class Notifier:
    def __init__(self, config_mgr):
        self.config_mgr = config_mgr

    def send(self, title, content, channel="all", override_cfg=None, level="info"):
        notify_cfg = self.config_mgr.config.get("notify", {})
        if override_cfg:
            notify_cfg = override_cfg

        # 0. Check push policy & DND (bypassed if specific channel test)
        if channel == "all":
            policy = notify_cfg.get("policy", {})
            mode = policy.get("mode", "all")  # "all", "fail_only", "silent"
            if mode == "silent":
                print("[Notifier] Push suppressed by policy: silent mode")
                return {"suppressed": True, "reason": "silent"}
            if mode == "fail_only" and level == "success":
                print("[Notifier] Push suppressed by policy: fail_only mode")
                return {"suppressed": True, "reason": "fail_only"}

            dnd_enabled = policy.get("dnd_enabled", False)
            if dnd_enabled and level in ["success", "info"]:
                try:
                    t_bj = time.localtime(time.time() + 8 * 3600)
                    cur_hour = t_bj[3]
                    dnd_start = int(policy.get("dnd_start", 23))
                    dnd_end = int(policy.get("dnd_end", 8))
                    is_dnd = False
                    if dnd_start > dnd_end:
                        if cur_hour >= dnd_start or cur_hour < dnd_end:
                            is_dnd = True
                    elif dnd_start < dnd_end:
                        if dnd_start <= cur_hour < dnd_end:
                            is_dnd = True
                    else:
                        is_dnd = True

                    if is_dnd:
                        print("[Notifier] Push suppressed by DND window ({:02d}:00-{:02d}:00), hour: {:02d}".format(dnd_start, dnd_end, cur_hour))
                        return {"suppressed": True, "reason": "dnd"}
                except Exception as e:
                    print("[Notifier] DND check error:", e)

        results = {}

        # PushPlus
        if channel in ["all", "pushplus"]:
            enabled = notify_cfg.get("pushplus", {}).get("enabled") if channel == "all" else True
            token = notify_cfg.get("pushplus", {}).get("token", "").strip()
            if enabled and token:
                results["pushplus"] = self._send_pushplus(token, title, content)

        # Feishu
        if channel in ["all", "feishu"]:
            enabled = notify_cfg.get("feishu", {}).get("enabled") if channel == "all" else True
            webhook = notify_cfg.get("feishu", {}).get("webhook", "").strip()
            if enabled and webhook:
                results["feishu"] = self._send_feishu(webhook, title, content)

        # DingTalk
        if channel in ["all", "dingtalk"]:
            enabled = notify_cfg.get("dingtalk", {}).get("enabled") if channel == "all" else True
            webhook = notify_cfg.get("dingtalk", {}).get("webhook", "").strip()
            if enabled and webhook:
                results["dingtalk"] = self._send_dingtalk(webhook, title, content)

        # WeChat Work (企业微信群机器人)
        if channel in ["all", "wechat_work"]:
            enabled = notify_cfg.get("wechat_work", {}).get("enabled") if channel == "all" else True
            webhook = notify_cfg.get("wechat_work", {}).get("webhook", "").strip()
            if enabled and webhook:
                results["wechat_work"] = self._send_wechat_work(webhook, title, content)

        # Custom Webhook
        if channel in ["all", "custom"]:
            enabled = notify_cfg.get("custom", {}).get("enabled") if channel == "all" else True
            custom_cfg = notify_cfg.get("custom", {})
            if enabled and custom_cfg.get("url"):
                results["custom"] = self._send_custom(custom_cfg, title, content)

        return results

    def _send_wechat_work(self, webhook, title, content):
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": "### {}\n\n{}".format(title, content)
            }
        }
        resp = None
        try:
            resp = urequests.post(webhook, headers={"Content-Type": "application/json"}, data=json.dumps(payload))
            if resp.status_code == 200:
                try:
                    res_json = resp.json()
                    if res_json.get("errcode") == 0:
                        return {"ok": True, "status": 200, "msg": "ok"}
                    else:
                        return {"ok": False, "status": 200, "error": res_json.get("errmsg", "企业微信返回错误")}
                except Exception:
                    return {"ok": True, "status": 200, "msg": resp.text[:100]}
            return {"ok": False, "status": resp.status_code, "error": "HTTP " + str(resp.status_code) + ": " + resp.text[:100]}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if resp:
                resp.close()

    def _send_pushplus(self, token, title, content):
        url = "http://www.pushplus.plus/send"
        payload = {
            "token": token,
            "title": title,
            "content": content,
            "template": "markdown"
        }
        resp = None
        try:
            resp = urequests.post(url, headers={"Content-Type": "application/json"}, data=json.dumps(payload))
            ok = (resp.status_code == 200)
            return {"ok": ok, "status": resp.status_code, "msg": resp.text[:100]}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if resp:
                resp.close()

    def _send_feishu(self, webhook, title, content):
        payload = {
            "msg_type": "text",
            "content": {
                "text": "{}\n\n{}".format(title, content)
            }
        }
        resp = None
        try:
            resp = urequests.post(webhook, headers={"Content-Type": "application/json"}, data=json.dumps(payload))
            ok = (resp.status_code == 200)
            return {"ok": ok, "status": resp.status_code, "msg": resp.text[:100]}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if resp:
                resp.close()

    def _send_dingtalk(self, webhook, title, content):
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": "### {}\n\n{}".format(title, content)
            }
        }
        resp = None
        try:
            resp = urequests.post(webhook, headers={"Content-Type": "application/json"}, data=json.dumps(payload))
            ok = (resp.status_code == 200)
            return {"ok": ok, "status": resp.status_code, "msg": resp.text[:100]}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if resp:
                resp.close()

    def _send_custom(self, custom_cfg, title, content):
        url = custom_cfg.get("url", "").strip()
        if not url:
            return {"ok": False, "error": "Custom URL is empty"}
        method = custom_cfg.get("method", "POST").upper()
        headers = custom_cfg.get("headers", {"Content-Type": "application/json"})
        tpl = custom_cfg.get("template", "{\"title\": \"{title}\", \"content\": \"{content}\"}")
        
        # Replace template
        safe_title = title.replace('"', '\\"').replace('\n', '\\n')
        safe_content = content.replace('"', '\\"').replace('\n', '\\n')
        body = tpl.replace("{title}", safe_title).replace("{content}", safe_content)
        
        resp = None
        try:
            if method == "GET":
                get_url = url.replace("{title}", safe_title).replace("{content}", safe_content)
                resp = urequests.get(get_url, headers=headers)
            else:
                resp = urequests.post(url, headers=headers, data=body)
            ok = (200 <= resp.status_code < 300)
            return {"ok": ok, "status": resp.status_code, "msg": resp.text[:100]}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if resp:
                resp.close()
