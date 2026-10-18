"""签到判定逻辑。

参考 Luckylos/newapi-checkin 的 service.py，只保留判定相关的部分：

- api 模式：POST /api/user/checkin，据响应判断 成功 / 已签到 / 失败
- login 模式：POST 登录接口，登录成功即视为签到成功；
  若响应里 checked_in == true，则标记为「已签到」

判定口径（需求明确要求）：
- 签到成功：{"success": true, "quota_awarded": <数字>}
- 已签到  ：{"stats": {"checked_in_today": true}}  或 message 含「已签到」
- 失败    ：以上都不是
- HTTP 200 不是唯一的成功标准
- 返回 HTML → 明确报错并跳过该站点
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .client import AttendanceClient, CheckinClientError, ClientOptions
from .config import MODE_LOGIN, Site

logger = logging.getLogger("win_checkin")


class Outcome(str, Enum):
    SUCCESS = "success"    # 本次签到成功
    ALREADY = "already"    # 今天已签到（视为完成）
    FAILED = "failed"      # 签到失败
    ERROR = "error"        # 请求/解析异常，跳过该站点


@dataclass
class SiteResult:
    site_name: str
    outcome: Outcome
    message: str
    quota_awarded: int | None = None

    @property
    def done(self) -> bool:
        """该站点是否算「已完成」（成功 或 已签到）。"""
        return self.outcome in (Outcome.SUCCESS, Outcome.ALREADY)

    def describe(self) -> str:
        label = {
            Outcome.SUCCESS: "签到成功",
            Outcome.ALREADY: "今日已签到",
            Outcome.FAILED: "签到失败",
            Outcome.ERROR: "请求异常",
        }[self.outcome]
        suffix = f"，获得 {self.quota_awarded}" if self.quota_awarded is not None else ""
        return f"{label}{suffix}（{self.message}）"


def _maybe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _is_already_message(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return "已签到" in message or "already checked in" in lowered


def _checked_in_today(payload: dict) -> bool:
    """从签到状态响应里判断今天是否已签到。

    参考原项目：data.stats.checked_in_today。
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    stats = data.get("stats")
    if isinstance(stats, dict) and bool(stats.get("checked_in_today")):
        return True
    # 兼容部分部署把字段平铺在 data 下。
    return bool(data.get("checked_in_today"))


def checkin_site(site: Site, options: ClientOptions) -> SiteResult:
    """对一个站点执行签到，返回判定结果。

    该函数自身保证不向外抛异常（除极端情况外），确保单站点失败不影响其它站点。
    """

    client = AttendanceClient(options)
    try:
        if site.mode == MODE_LOGIN:
            return _run_login_mode(site, client)
        return _run_api_mode(site, client)
    except CheckinClientError as exc:
        # 网络异常 / HTML 响应 / 非法 JSON，均归为该站点异常，跳过。
        logger.warning("[%s] 站点异常: %s", site.name, exc)
        return SiteResult(site.name, Outcome.ERROR, str(exc))
    except Exception as exc:  # noqa: BLE001 - 兜底，防止单站点拖垮整体
        logger.exception("[%s] 未预期的错误", site.name)
        return SiteResult(site.name, Outcome.ERROR, f"未预期错误: {exc}")


def _run_api_mode(site: Site, client: AttendanceClient) -> SiteResult:
    """api 模式：POST /api/user/checkin。"""

    client.set_access_token(site.token)
    client.set_user_id(site.user_id)

    response = client.checkin()
    payload = response.payload

    # 1) 明确的成功：success == true 且带 quota_awarded。
    if response.success:
        quota = _maybe_int(response.data.get("quota_awarded"))
        return SiteResult(site.name, Outcome.SUCCESS, response.message or "ok", quota)

    # 2) 未成功，但可能只是「今天已经签过」。
    if _checked_in_today(payload) or _is_already_message(response.message):
        return SiteResult(site.name, Outcome.ALREADY, response.message or "今日已签到")

    # 3) 真正的失败。
    return SiteResult(site.name, Outcome.FAILED, response.message or f"HTTP {response.status}")


def _run_login_mode(site: Site, client: AttendanceClient) -> SiteResult:
    """login 模式：登录成功即签到成功。"""

    if not (site.username and site.password):
        return SiteResult(site.name, Outcome.ERROR, "login 模式缺少 username/password")

    response = client.login(site.username, site.password)
    payload = response.payload

    if not response.success:
        # 登录失败时也可能提示「已签到」。
        if _checked_in_today(payload) or _is_already_message(response.message):
            return SiteResult(site.name, Outcome.ALREADY, response.message or "今日已签到")
        return SiteResult(site.name, Outcome.FAILED, response.message or f"HTTP {response.status}")

    # 登录成功即视为签到成功；若响应里显式 checked_in == true，标为「已签到」。
    data = response.data
    user = data.get("user")
    user_data = user if isinstance(user, dict) else data
    checked_in = user_data.get("checked_in")
    if isinstance(checked_in, bool) and checked_in:
        return SiteResult(site.name, Outcome.ALREADY, "登录响应显示今日已签到")

    return SiteResult(site.name, Outcome.SUCCESS, response.message or "登录成功")
