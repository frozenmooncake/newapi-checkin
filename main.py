"""入口脚本。

- 加载配置
- 初始化日志（文件 + stdout 双写，因为 pythonw.exe 无控制台）
- 启动 BlockingScheduler 调度
- 由 scheduler 内部决定 sys.exit(0/1)

用法：
    python main.py                      # 使用同目录 config.json
    python main.py --config my.json     # 指定配置文件
"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from checkin.config import ConfigError, load_config
from checkin.scheduler import CheckinScheduler

BASE_DIR = Path(__file__).resolve().parent


def _setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("win_checkin")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件日志：pythonw.exe 无控制台，这是主要输出。
    file_handler = RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # 同时输出到 stdout，方便有控制台时调试。
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)


def main() -> None:
    parser = argparse.ArgumentParser(description="Windows 本地 New API 签到脚本")
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
        # 日志还没初始化，直接打印并退出。
        print(f"[FATAL] 配置错误: {exc}", file=sys.stderr)
        sys.exit(2)

    log_path = Path(config.log_file)
    if not log_path.is_absolute():
        log_path = BASE_DIR / log_path
    _setup_logging(log_path)

    logger = logging.getLogger("win_checkin")
    logger.info("=" * 60)
    logger.info("启动签到脚本，配置文件：%s", config_path)
    logger.info("站点数：%d", len(config.sites))

    state_path = Path(config.state_file)
    if not state_path.is_absolute():
        state_path = BASE_DIR / state_path

    try:
        exit_code = CheckinScheduler(config, state_path=state_path).run()
    except Exception:  # noqa: BLE001
        logger.exception("脚本运行未预期错误")
        sys.exit(1)

    logger.info("程序退出，退出码 %d。", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
