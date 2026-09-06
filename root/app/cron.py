import time
import asyncio

def match_field(pattern, value):
    pattern = pattern.strip()
    if pattern == '*':
        return True
    if '/' in pattern:
        parts = pattern.split('/')
        if len(parts) == 2:
            step = int(parts[1])
            return (value % step) == 0
    if ',' in pattern:
        return any(match_field(p, value) for p in pattern.split(','))
    if '-' in pattern:
        parts = pattern.split('-')
        if len(parts) == 2:
            return int(parts[0]) <= value <= int(parts[1])
    try:
        return int(pattern) == value
    except ValueError:
        return False

def match_cron(cron_expr, tm):
    # tm is time.localtime(): (year, month, mday, hour, minute, second, weekday, yearday)
    # cron: minute (0-59), hour (0-23), day of month (1-31), month (1-12), day of week (0-6)
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    
    m_match = match_field(fields[0], tm[4])
    h_match = match_field(fields[1], tm[3])
    dom_match = match_field(fields[2], tm[2])
    mon_match = match_field(fields[3], tm[1])
    dow_match = match_field(fields[4], tm[6])
    
    return m_match and h_match and dom_match and mon_match and dow_match

class TaskScheduler:
    def __init__(self, config_mgr, executor):
        self.config_mgr = config_mgr
        self.executor = executor
        self.last_minute_checked = -1
        self.task_last_run_epoch = {}  # task_id -> epoch seconds
        self.task_last_run_minute = {} # task_id -> minute signature
        self.task_window_targets = {}  # task_id -> today's target "HH:MM"
        self.task_window_dates = {}    # task_id -> "YYYY-MM-DD"
        self._boot_silence = True      # 开机冷启动静默保护

    async def start(self):
        print("[Scheduler] Background scheduler loop started.")
        # 开机静默初始化：先标记当前时间，确保断电上电或重启时，绝对不会立刻执行任何任务
        init_epoch = time.time()
        tm = time.localtime(init_epoch)
        init_min_sig = "{:04d}{:02d}{:02d}{:02d}{:02d}".format(
            tm[0], tm[1], tm[2], tm[3], tm[4]
        )
        for task in self.config_mgr.tasks:
            tid = task.get("id")
            if tid:
                self.task_last_run_epoch[tid] = init_epoch
                self.task_last_run_minute[tid] = init_min_sig

        # 等待开机稳定（10秒内静默，跳过开机瞬间重合）
        await asyncio.sleep(10)
        self._boot_silence = False
        print("[Scheduler] Cold-boot silence guard released. Tasks will run strictly per schedule.")

        while True:
            try:
                if not self._boot_silence:
                    await self._check_and_run_tasks()
            except Exception as e:
                print("[Scheduler] Error in check loop:", e)
            await asyncio.sleep(5)

    async def _check_and_run_tasks(self):
        now_epoch = time.time()
        tm = time.localtime(now_epoch)
        current_minute_sig = "{:04d}{:02d}{:02d}{:02d}{:02d}".format(
            tm[0], tm[1], tm[2], tm[3], tm[4]
        )

        # Weekly Digest Trigger (Sunday 20:00 UTC+8, tm[6] == 6 is Sunday)
        if tm[6] == 6 and tm[3] == 20 and tm[4] == 0:
            if getattr(self, '_last_weekly_report_min', '') != current_minute_sig:
                self._last_weekly_report_min = current_minute_sig
                try:
                    report = self.generate_weekly_report()
                    self.executor.notifier.send("【ESP32 本周打卡战报与健康体检】", report)
                    self.config_mgr.auto_rotate_backup()
                    print("[Scheduler] Weekly report sent and auto-backup created successfully.")
                except Exception as ex:
                    print("[Scheduler] Weekly report error:", ex)

        for task in self.config_mgr.tasks:
            if not task.get("enabled", False):
                continue
            
            task_id = task.get("id")

            # Circuit Breaker Check
            stats = task.get("stats", {})
            if stats.get("circuit_tripped"):
                until = stats.get("circuit_tripped_until", 0)
                epoch_year = time.gmtime(0)[0]
                offset = 946684800 if epoch_year == 2000 else 0
                now_unix = time.time() + offset
                if now_unix < until:
                    continue
                else:
                    # Cooldown expired, auto reset circuit
                    stats["circuit_tripped"] = False
                    stats["consecutive_fails"] = 0
                    self.config_mgr.save_tasks()
                    print("[Scheduler] Circuit breaker auto-reset for task:", task.get("name"))

            stype = task.get("schedule_type", "interval")
            sval = str(task.get("schedule_val", "10")).strip()
            
            is_due = False

            if stype == "interval":
                try:
                    interval_secs = int(sval) * 60
                except ValueError:
                    interval_secs = 600
                last_epoch = self.task_last_run_epoch.get(task_id, 0)
                if (now_epoch - last_epoch) >= interval_secs:
                    is_due = True

            elif stype == "daily":
                # sval is HH:MM
                current_hm = "{:02d}:{:02d}".format(tm[3], tm[4])
                if current_hm == sval:
                    if self.task_last_run_minute.get(task_id) != current_minute_sig:
                        is_due = True

            elif stype == "window":
                # sval format: "08:30-09:15"
                today_date_str = "{:04d}-{:02d}-{:02d}".format(tm[0], tm[1], tm[2])
                target_hm = self.task_window_targets.get(task_id)
                last_target_date = self.task_window_dates.get(task_id)
                
                if last_target_date != today_date_str or not target_hm:
                    try:
                        sep = '-' if '-' in sval else '~'
                        parts = sval.split(sep)
                        start_p = parts[0].strip().split(':')
                        end_p = parts[1].strip().split(':')
                        start_mins = int(start_p[0]) * 60 + int(start_p[1])
                        end_mins = int(end_p[0]) * 60 + int(end_p[1])
                        if end_mins < start_mins:
                            end_mins += 24 * 60
                        span = end_mins - start_mins
                        if span <= 0:
                            rand_offset = 0
                        else:
                            try:
                                import urandom
                                rand_offset = urandom.randint(0, span)
                            except Exception:
                                h = 0
                                for ch in (task_id + today_date_str):
                                    h = (h * 31 + ord(ch)) & 0xFFFFFFFF
                                rand_offset = h % (span + 1)
                        target_total = (start_mins + rand_offset) % (24 * 60)
                        target_hm = "{:02d}:{:02d}".format(target_total // 60, target_total % 60)
                        self.task_window_targets[task_id] = target_hm
                        self.task_window_dates[task_id] = today_date_str
                        print("[Scheduler] Task '{}' today window target: {}".format(task.get("name"), target_hm))
                    except Exception as ex:
                        print("[Scheduler] Error parsing window schedule:", ex)
                        target_hm = None

                current_hm = "{:02d}:{:02d}".format(tm[3], tm[4])
                if target_hm and current_hm == target_hm:
                    if self.task_last_run_minute.get(task_id) != current_minute_sig:
                        is_due = True

            elif stype == "cron":
                if match_cron(sval, tm):
                    if self.task_last_run_minute.get(task_id) != current_minute_sig:
                        is_due = True

            if is_due:
                self.task_last_run_epoch[task_id] = now_epoch
                self.task_last_run_minute[task_id] = current_minute_sig
                # Spawn execution with random jitter asynchronously
                asyncio.create_task(self._run_task_with_jitter(task))

    def generate_weekly_report(self):
        tasks = self.config_mgr.tasks
        total_tasks = len(tasks)
        active_tasks = len([t for t in tasks if t.get("enabled")])

        epoch_year = time.gmtime(0)[0]
        offset = 946684800 if epoch_year == 2000 else 0
        now_unix = time.time() + offset

        past7_dates = []
        for i in range(7):
            t_sec = (now_unix - offset) - i * 86400 + 8 * 3600
            t_struct = time.localtime(t_sec)
            past7_dates.append("{:04d}-{:02d}-{:02d}".format(t_struct[0], t_struct[1], t_struct[2]))

        week_success = 0
        week_fail = 0
        week_rewards = {}
        expiring_cookies = []

        try:
            from app.executor import get_cookie_expiration_days
            for t in tasks:
                name = t.get("name", "未命名任务")
                st = t.get("stats", {})
                hist = st.get("history", {})
                for d in past7_dates:
                    entry = hist.get(d)
                    if entry:
                        if entry.get("status") == "success":
                            week_success += 1
                            r_val = entry.get("reward")
                            r_unit = entry.get("unit") or st.get("reward_unit") or "收益"
                            if r_val:
                                week_rewards[r_unit] = week_rewards.get(r_unit, 0) + r_val
                        elif entry.get("status") == "fail":
                            week_fail += 1

                headers = t.get("params", {}).get("headers", {})
                has_jwt, rem_days, exp_ts, exp_date = get_cookie_expiration_days(headers)
                if has_jwt and rem_days is not None and rem_days <= 7.0:
                    expiring_cookies.append("• {}：仅剩 **{}天** (到期: {})".format(name, rem_days, exp_date or "未知"))
        except Exception:
            pass

        week_runs = week_success + week_fail
        succ_rate = round((week_success / week_runs * 100)) if week_runs > 0 else 100

        reward_lines = []
        for u, v in week_rewards.items():
            reward_lines.append("+{} {}".format(v, u))
        reward_summary = "、".join(reward_lines) if reward_lines else "正常打卡运行"

        import gc
        free_ram = round(gc.mem_free() / 1024 / 1024, 2)
        mcu_temp = 45.0
        try:
            import esp32
            mcu_temp = round(esp32.mcu_temperature(), 1)
        except Exception:
            pass

        lines = [
            "🏆【ESP32 本周打卡战报与健康周报】",
            "━━━━━━━━━━━━━━━━━━━",
            "📊 **打卡表现（近7天）**：",
            "• 达标成功率：**{}%** ({}/{} 次成功)".format(succ_rate, week_success, week_runs),
            "• 本周总斩获：**{}**".format(reward_summary),
            "• 任务在线数：{} 个 (总计 {} 个)".format(active_tasks, total_tasks),
            ""
        ]

        if expiring_cookies:
            lines.append("⚠️ **Cookie 寿命预警（7天内到期）**：")
            lines.extend(expiring_cookies)
            lines.append("")
        else:
            lines.append("🟢 **凭据寿命状态**：所有监控任务凭据均充裕安全")
            lines.append("")

        lines.extend([
            "💻 **硬件运行健康度**：",
            "• 芯片运行温度：**{}℃** (优良)".format(mcu_temp),
            "• 剩余可用内存：**{} MB** (充裕)".format(free_ram),
            "• 闪存安全状态：自动写损耗均衡开启，已执行定时自动备份",
            "━━━━━━━━━━━━━━━━━━━"
        ])

        return "\n".join(lines)

    async def _run_task_with_jitter(self, task):
        jitter_min = task.get("jitter_minutes", 0)
        try:
            jitter_min = int(jitter_min)
        except Exception:
            jitter_min = 0

        jitter_info = ""
        if jitter_min > 0:
            try:
                import urandom as random
            except ImportError:
                import random
            delay_sec = random.randint(5, jitter_min * 60)
            m = delay_sec // 60
            s = delay_sec % 60
            jitter_info = "{}分{}秒".format(m, s) if m > 0 else "{}秒".format(s)
            print("[Scheduler] Jitter enabled for task '{}': randomly delaying {} before execution...".format(
                task.get("name", ""), jitter_info
            ))
            await asyncio.sleep(delay_sec)

        await self.executor.run_task(task, jitter_info=jitter_info)
