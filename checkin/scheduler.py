"""调度逻辑（完全重写，不沿用原项目）。

设计目标：
- 在若干时间点尝试签到
- 所有站点成功 → 退出码 0
- 有站点失败 → 记录并等下一个时间点
- 当天所有时间点都失败 → 退出码 1
- 过期时间点直接跳过（以脚本启动时刻为准），不补签

实现：
- apscheduler.BlockingScheduler，timezone=Asia/Shanghai
- 开机延迟：DateTrigger(now + startup_delay_minutes)
- 固定时间点：CronTrigger，注册前判断是否已过期
- 每个时间点一个独立任务

退出机制说明：
APScheduler 的任务在 worker 线程里执行，如果在任务里直接 sys.exit 会被
executor 捕获并打印 "raised an exception"，主线程仍阻塞。因此这里改为：
任务只负责「设置退出码 + 唤醒主线程」，真正的 shutdown 与 sys.exit 在主线程
的 run() 里完成，保证退出干净、无后台线程残留。
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from . import state
from .client import ClientOptions
from .config import AppConfig
from .service import SiteResult, checkin_site

logger = logging.getLogger("win_checkin")


class CheckinScheduler:
    def __init__(self, config: AppConfig, state_path: Path | None = None) -> None:
        self._config = config
        self._state_path = state_path
        try:
            self._tz = ZoneInfo(config.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"无效的时区: {config.timezone}") from exc

        self._scheduler = BlockingScheduler(timezone=self._tz)
        # 记录本次运行已注册的任务总数，用于判断是否跑完。
        self._total_tasks = 0
        self._tasks_completed = 0
        # 主线程退出信号 + 退出码，由任务线程设置。
        self._finished = threading.Event()
        self._exit_code = 1

    # -- 调度注册 ------------------------------------------------------------

    def _now(self) -> dt.datetime:
        return dt.datetime.now(self._tz)

    def _manual_done_today(self) -> bool:
        """当天是否已有一次成功的手动签到。"""
        if self._state_path is None:
            return False
        return state.done_today(self._state_path, self._config.timezone)

    def build(self) -> None:
        """根据启动时刻注册任务。"""

        now = self._now()

        # 1) 开机延迟一次。
        delay = self._config.startup_delay_minutes
        run_at = now + dt.timedelta(minutes=delay)
        self._add_job(
            trigger=DateTrigger(run_date=run_at),
            label=f"开机延迟{delay}分钟",
        )

        # 2) 固定时间点：仅注册「还没到」的。
        #    这里需要根据实际需求调整时间点（改 config.json 的 fixed_times）。
        for hhmm in sorted(self._config.fixed_times):
            hour, minute = (int(x) for x in hhmm.split(":"))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                logger.info(
                    "跳过已过期时间点 %s（启动时为 %s）", hhmm, now.strftime("%H:%M:%S")
                )
                continue
            self._add_job(
                trigger=CronTrigger(hour=hour, minute=minute, timezone=self._tz),
                label=hhmm,
            )

        self._total_tasks = len(self._scheduler.get_jobs())
        if self._total_tasks == 0:
            logger.error("没有注册任何任务，无法签到")
            self._finished.set()
            self._exit_code = 1
            return

        logger.info(
            "已注册 %d 个签到时间点：%s",
            self._total_tasks,
            ", ".join(job.id for job in self._scheduler.get_jobs()),
        )

    def _add_job(self, *, trigger, label: str) -> None:
        self._scheduler.add_job(
            self._run_pass,
            trigger=trigger,
            id=label,
            name=label,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
            kwargs={"label": label},
        )

    # -- 单次尝试 ------------------------------------------------------------

    def _run_pass(self, label: str) -> None:
        """一次尝试：遍历所有站点，或决定退出，或等待下一时间点。"""

        if self._manual_done_today():
            logger.info("检测到今日已手动签到成功，跳过自动签到并直接退出。")
            self._request_exit(0)
            return

        self._tasks_completed += 1
        attempt = self._tasks_completed
        is_last = attempt >= self._total_tasks

        logger.info("=" * 60)
        logger.info(
            "第 %d/%d 次尝试（%s）开始%s",
            attempt,
            self._total_tasks,
            label,
            "（当天最后一个时间点）" if is_last else "",
        )

        results = self._run_all_sites()
        self._log_results(results)

        all_ok = all(r.done for r in results)
        if all_ok:
            logger.info("所有站点均已签到成功，程序即将退出。")
            self._request_exit(0)
            return

        failed = [r for r in results if not r.done]
        if is_last:
            logger.error(
                "当天所有时间点已用完，仍有 %d 个站点未成功：%s",
                len(failed),
                ", ".join(f"{r.site_name}({r.outcome.value})" for r in failed),
            )
            self._request_exit(1)
            return

        logger.warning("本次有 %d 个站点未成功，等待下一个时间点重试。", len(failed))

    def _request_exit(self, code: int) -> None:
        """由任务线程设置退出码并唤醒主线程。真正的退出在主线程完成。"""
        self._exit_code = code
        self._finished.set()

    def _run_all_sites(self) -> list[SiteResult]:
        """遍历所有站点，单站点异常已在 service 层隔离。"""
        results: list[SiteResult] = []
        for site in self._config.sites:
            options = ClientOptions(
                base_url=site.normalized_url,
                user_agent=self._config.user_agent,
                timeout=self._config.request_timeout,
                max_retries=self._config.max_retries,
                retry_backoff_s=self._config.retry_backoff_s,
            )
            logger.info(
                "[%s] 开始签到（mode=%s, url=%s）", site.name, site.mode, site.normalized_url
            )
            result = checkin_site(site, options)
            logger.info("[%s] 结果：%s", site.name, result.describe())
            results.append(result)
        return results

    def _log_results(self, results: list[SiteResult]) -> None:
        ok = sum(1 for r in results if r.done)
        logger.info(
            "本次尝试小结：成功/已签到 %d，失败/异常 %d，共 %d",
            ok,
            len(results) - ok,
            len(results),
        )

    # -- 生命周期 ------------------------------------------------------------

    def run(self) -> int:
        """启动调度器并阻塞，直到需要退出；返回退出码。"""

        if self._manual_done_today():
            logger.info("检测到今日已手动签到成功，跳过自动签到，直接退出。")
            return 0

        self.build()
        if self._finished.is_set():
            # build 阶段就没有任务。
            return self._exit_code

        # 监听线程：一收到退出信号就关闭调度器，让主线程的 start() 返回。
        watcher = threading.Thread(target=self._watch, daemon=True, name="exit-watcher")
        watcher.start()

        logger.info("调度器启动，等待第一个时间点……")
        try:
            self._scheduler.start()  # BlockingScheduler：阻塞直到 shutdown
        except (KeyboardInterrupt, SystemExit):
            logger.info("收到中断信号，退出。")
            self._exit_code = 0
        finally:
            # start() 返回后调度器可能仍在运行，确保关闭，避免残留线程。
            if self._scheduler.running:
                try:
                    self._scheduler.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    logger.exception("关闭调度器时出错")

        return self._exit_code

    def _watch(self) -> None:
        """等待退出信号，然后关闭调度器（在主线程中 start() 才会返回）。"""
        self._finished.wait()
        logger.info("退出信号已收到，正在关闭调度器……")
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            logger.exception("关闭调度器时出错")
