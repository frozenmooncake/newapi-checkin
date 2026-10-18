"""手动签到状态记录。

手动签到成功后，把「今天」写入状态文件；调度版启动或到点时读取，
若当天已手动成功，则直接退出，不再重复自动签到。

日期以配置的时区为准（默认 Asia/Shanghai），跨天后自动失效。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo


def _today(tz_name: str) -> str:
    return dt.datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def mark_done(path: Path, tz_name: str) -> None:
    """记录「今天已手动签到成功」。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_today(tz_name), encoding="utf-8")


def done_today(path: Path, tz_name: str) -> bool:
    """状态文件是否记录了「今天」手动签到成功。"""
    try:
        recorded = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(recorded) and recorded == _today(tz_name)
