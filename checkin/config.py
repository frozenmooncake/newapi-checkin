from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("win_checkin")

# 支持的签到模式。
MODE_API = "api"      # 正常签到型：POST /api/user/checkin
MODE_LOGIN = "login"  # 登录即签到型：POST /api/user/login
VALID_MODES = {MODE_API, MODE_LOGIN}


class ConfigError(ValueError):
    """配置文件内容不合法。"""


@dataclass(frozen=True)
class Site:
    """一个待签到的站点。"""

    name: str
    url: str
    mode: str
    # api 模式使用 token（access_token）认证。
    token: str | None = None
    # token 可选的 user_id，用于 New-Api-User 头。
    user_id: int | None = None
    # login 模式使用用户名/密码认证。
    username: str | None = None
    password: str | None = None

    @property
    def normalized_url(self) -> str:
        return self.url.rstrip("/")


@dataclass
class AppConfig:
    """脚本级配置。"""

    sites: list[Site] = field(default_factory=list)
    # 每个时间点之间、以及单站点请求之间的超时。
    request_timeout: float = 30.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    timezone: str = "Asia/Shanghai"
    # 固定尝试时间点（HH:MM），以脚本启动时刻为准过滤过期项。
    fixed_times: list[str] = field(default_factory=lambda: ["15:00", "20:00", "23:30"])
    # 开机后延迟多少分钟做第一次尝试。
    startup_delay_minutes: int = 10
    log_file: str = "logs/checkin.log"
    # 手动签到成功标记文件；调度版据此在当天跳过自动签到。
    state_file: str = "logs/manual_done.txt"
    # 单个站点请求失败后的重试次数（仅网络层错误）。
    max_retries: int = 2
    retry_backoff_s: float = 2.0


def _require_str(raw: dict[str, Any], key: str, context: str, *, required: bool = True) -> str | None:
    value = raw.get(key)
    if value is None:
        if required:
            raise ConfigError(f"{context} 缺少必填字段 '{key}'")
        return None
    text = str(value).strip()
    if required and not text:
        raise ConfigError(f"{context} 的字段 '{key}' 不能为空")
    return text or None


def _parse_site(raw: Any, index: int) -> Site:
    context = f"站点 #{index}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{context} 必须是一个 JSON 对象")

    name = _require_str(raw, "name", context) or ""
    url = _require_str(raw, "url", context) or ""
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"{context} '{name}' 的 url 必须以 http:// 或 https:// 开头")

    mode = (raw.get("mode") or MODE_API)
    mode = str(mode).strip().lower()
    if mode not in VALID_MODES:
        raise ConfigError(
            f"{context} '{name}' 的 mode 必须是 {sorted(VALID_MODES)} 之一，当前为 '{mode}'"
        )

    token = _require_str(raw, "token", context, required=False)
    username = _require_str(raw, "username", context, required=False)
    password = _require_str(raw, "password", context, required=False)

    user_id_raw = raw.get("user_id")
    user_id: int | None = None
    if user_id_raw is not None:
        # user_id 是可选的（New-Api-User 头），允许 int 或可转成 int 的字符串。
        try:
            parsed_id = int(user_id_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{context} '{name}' 的 user_id 必须是整数") from exc
        # 0 或负数视为「未提供」（常被当作占位值），此时不会发送 New-API-User 头。
        user_id = parsed_id if parsed_id > 0 else None

    if mode == MODE_API:
        if not token:
            raise ConfigError(f"{context} '{name}' 是 api 模式，必须提供 token")
    elif mode == MODE_LOGIN:
        if not (username and password):
            raise ConfigError(f"{context} '{name}' 是 login 模式，必须提供 username 和 password")

    return Site(
        name=name,
        url=url,
        mode=mode,
        token=token,
        user_id=user_id,
        username=username,
        password=password,
    )


def _parse_fixed_times(raw: Any) -> list[str]:
    if raw is None:
        return ["15:00", "20:00", "23:30"]
    # 允许空列表：表示当天只做「开机延迟」那一次尝试。
    if not isinstance(raw, list):
        raise ConfigError("'fixed_times' 必须是字符串列表，例如 [\"15:00\", \"20:00\"]")
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        parts = text.split(":")
        if len(parts) != 2:
            raise ConfigError(f"时间点 '{text}' 格式错误，应为 HH:MM")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ConfigError(f"时间点 '{text}' 格式错误，应为 HH:MM") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ConfigError(f"时间点 '{text}' 超出范围")
        result.append(f"{hour:02d}:{minute:02d}")
    return result


def load_config(path: str | Path) -> AppConfig:
    """读取 JSON 配置文件，返回校验后的 AppConfig。"""

    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在: {config_path}")
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法的 JSON: {config_path}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("配置文件根节点必须是 JSON 对象")

    raw_sites = payload.get("sites")
    if not isinstance(raw_sites, list) or not raw_sites:
        raise ConfigError("配置文件必须包含非空的 'sites' 列表")

    sites = [_parse_site(item, idx) for idx, item in enumerate(raw_sites, start=1)]

    fixed_times = _parse_fixed_times(payload.get("fixed_times"))

    def _get_float(key: str, default: float, *, minimum: float = 0.0) -> float:
        raw = payload.get(key)
        if raw is None:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"'{key}' 必须是数字") from exc
        if value < minimum:
            raise ConfigError(f"'{key}' 必须 >= {minimum}")
        return value

    def _get_int(key: str, default: int, *, minimum: int = 0) -> int:
        raw = payload.get(key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"'{key}' 必须是整数") from exc
        if value < minimum:
            raise ConfigError(f"'{key}' 必须 >= {minimum}")
        return value

    return AppConfig(
        sites=sites,
        request_timeout=_get_float("request_timeout", 30.0, minimum=1.0),
        user_agent=str(payload.get("user_agent") or AppConfig.user_agent).strip(),
        timezone=str(payload.get("timezone") or "Asia/Shanghai").strip(),
        fixed_times=fixed_times,
        startup_delay_minutes=_get_int("startup_delay_minutes", 10, minimum=0),
        log_file=str(payload.get("log_file") or "logs/checkin.log").strip(),
        state_file=str(payload.get("state_file") or "logs/manual_done.txt").strip(),
        max_retries=_get_int("max_retries", 2, minimum=0),
        retry_backoff_s=_get_float("retry_backoff_s", 2.0, minimum=0.0),
    )
