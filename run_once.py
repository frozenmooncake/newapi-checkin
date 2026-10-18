"""手动一次性签到脚本。

与 main.py 的调度版不同：**立即对所有站点签到一次**，然后退出，不等待时间点。

用法：
    python run_once.py                    # 使用同目录 config.json
    python run_once.py --config my.json   # 指定配置文件

退出码：
    0  所有站点已签到成功（或今日已签到）
    1  存在签到失败 / 请求异常
    2  配置文件错误
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from checkin import state
from checkin.client import ClientOptions
from checkin.config import AppConfig, ConfigError, load_config
from checkin.service import checkin_site
from main import BASE_DIR, _setup_logging

logger = logging.getLogger("win_checkin")


def _run_all_sites(config: AppConfig) -> int:
    ok_count = 0
    for site in config.sites:
        options = ClientOptions(
            base_url=site.normalized_url,
            user_agent=config.user_agent,
            timeout=config.request_timeout,
            max_retries=config.max_retries,
            retry_backoff_s=config.retry_backoff_s,
        )
        logger.info(
            "[%s] 开始签到（mode=%s, url=%s）", site.name, site.mode, site.normalized_url
        )
        result = checkin_site(site, options)
        logger.info("[%s] 结果：%s", site.name, result.describe())
        if result.done:
            ok_count += 1

    total = len(config.sites)
    logger.info("本次签到小结：成功/已签到 %d，失败/异常 %d，共 %d", ok_count, total - ok_count, total)
    return 0 if ok_count == total else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Windows 本地 New API 手动一次性签到")
    parser.add_argument(
        "--config",
        default=str(BASE_DIR / "config.json"),
        help="配置文件路径（默认：同目录 config.json）",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = BASE_DIR / config_path

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"[FATAL] 配置错误: {exc}", file=sys.stderr)
        sys.exit(2)

    log_path = Path(config.log_file)
    if not log_path.is_absolute():
        log_path = BASE_DIR / log_path
    _setup_logging(log_path)

    logger.info("=" * 60)
    logger.info("手动一次性签到，配置文件：%s", config_path)
    logger.info("站点数：%d", len(config.sites))

    state_path = Path(config.state_file)
    if not state_path.is_absolute():
        state_path = BASE_DIR / state_path

    try:
        exit_code = _run_all_sites(config)
    except Exception:  # noqa: BLE001
        logger.exception("脚本运行未预期错误")
        sys.exit(1)

    if exit_code == 0:
        state.mark_done(state_path, config.timezone)
        logger.info("已记录今日手动签到成功，当天自动签到将直接退出。")

    logger.info("程序退出，退出码 %d。", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
