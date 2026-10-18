"""HTTP 客户端封装。

参考自 Luckylos/newapi-checkin 的 client.py，只保留两块：

1. 认证头 / 接口地址 / 请求参数
2. 响应解析（区分 JSON 与 HTML）

与原项目的差异：
- 使用 requests.Session，而不是 urllib
- 不引入 cf_turnstile / Turnstile 相关逻辑
- 保留 429 识别，但不在此层做任何重试之外的业务判断
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger("win_checkin")

# ======================= 接口地址（如需适配其它部署请改这里） =======================
# 参考原项目 client.py：
#   登录      : POST /api/user/login?turnstile=
#   签到      : POST /api/user/checkin
#   签到状态  : GET  /api/user/checkin?month=YYYY-MM
# 这里主要用前两个；状态接口在判定"已签到"时可选用。
PATH_LOGIN = "/api/user/login"
PATH_CHECKIN = "/api/user/checkin"
PATH_CHECKIN_STATS = "/api/user/checkin"
# ================================================================================


class CheckinClientError(RuntimeError):
    """客户端层错误基类。"""


class HtmlResponseError(CheckinClientError):
    """返回的是 HTML 而不是 JSON，疑似被 Cloudflare / WAF 拦截。"""


class RateLimitedError(CheckinClientError):
    """HTTP 429，站点限流；不应视作账号失败。"""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class ClientOptions:
    base_url: str
    user_agent: str
    timeout: float
    max_retries: int = 2
    retry_backoff_s: float = 2.0


@dataclass
class ApiResponse:
    """一个已解析的 JSON 响应。"""

    status: int
    payload: dict[str, Any]

    @property
    def success(self) -> bool:
        return bool(self.payload.get("success"))

    @property
    def message(self) -> str | None:
        value = self.payload.get("message")
        return str(value) if value is not None else None

    @property
    def data(self) -> dict[str, Any]:
        data = self.payload.get("data")
        return data if isinstance(data, dict) else {}


class AttendanceClient:
    """针对单个站点的 HTTP 客户端。"""

    def __init__(self, options: ClientOptions) -> None:
        self._options = options
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": options.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Cache-Control": "no-store",
            }
        )
        # 站点级 token / user_id，登录后可能被刷新。
        self._access_token: str | None = None
        self._user_id: int | None = None

    # -- 认证信息 ------------------------------------------------------------

    def set_access_token(self, token: str | None) -> None:
        self._access_token = token

    def set_user_id(self, user_id: int | None) -> None:
        self._user_id = user_id

    def _build_headers(self, *, json_body: bool) -> dict[str, str]:
        """构造本次请求的头，含认证头。

        参考原项目 client.py 的 _request_raw：
          - Authorization: Bearer {access_token}
          - New-API-User: {user_id}   （注意原项目头名是 New-API-User）
        这里同时兼容 New-Api-User 与 New-API-User 两种大小写（HTTP 头大小写不敏感，
        requests 会按原样发送，服务端一般大小写不敏感，这里统一用 New-API-User）。
        """
        headers: dict[str, str] = {
            "Referer": self._options.base_url.rstrip("/") + "/console",
        }
        if self._access_token:
            # 这里需要根据实际接口调整：认证头名称/格式。
            headers["Authorization"] = f"Bearer {self._access_token}"
        if self._user_id is not None and self._user_id > 0:
            headers["New-API-User"] = str(self._user_id)
        if json_body:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = self._options.base_url.rstrip("/")
        return headers

    # -- 请求发送 ------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> ApiResponse:
        """发送请求并解析响应，只对网络层错误重试。"""

        url = self._options.base_url.rstrip("/") + path
        headers = self._build_headers(json_body=body is not None)
        attempts = self._options.max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers=headers,
                    timeout=self._options.timeout,
                )
                return self._parse(response)
            except (requests.ConnectionError, requests.Timeout) as exc:
                # 只对网络层错误重试。
                last_exc = exc
                if attempt + 1 >= attempts:
                    break
                sleep_s = self._options.retry_backoff_s * (2 ** attempt)
                logger.warning(
                    "网络错误，%s 秒后重试 (%d/%d): %s",
                    sleep_s,
                    attempt + 1,
                    attempts,
                    exc,
                )
                time.sleep(sleep_s)
        raise CheckinClientError(f"网络请求失败: {last_exc}") from last_exc

    @staticmethod
    def _parse(response: requests.Response) -> ApiResponse:
        """把响应解析成 ApiResponse，HTML 直接报错。"""

        status = response.status_code
        content_type = response.headers.get("Content-Type", "").lower()
        text = response.text or ""

        if status == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise RateLimitedError("站点限流 (HTTP 429)", retry_after_s=retry_after)

        # 明显的 HTML 响应（Cloudflare 验证页 / WAF 拦截页）。
        stripped = text.lstrip()
        looks_html = (
            "text/html" in content_type
            or stripped.startswith("<!DOCTYPE")
            or stripped.startswith("<html")
            or stripped.startswith("<")
        )
        if looks_html:
            snippet = stripped[:120].replace("\n", " ")
            raise HtmlResponseError(
                f"返回 HTML 而非 JSON（疑似 Cloudflare / WAF 拦截），HTTP {status}: {snippet}"
            )

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CheckinClientError(
                f"响应不是合法 JSON，HTTP {status}: {text[:120]}"
            ) from exc
        if not isinstance(payload, dict):
            raise CheckinClientError(f"响应 JSON 不是对象，HTTP {status}")
        return ApiResponse(status=status, payload=payload)

    # -- 业务接口 ------------------------------------------------------------

    def login(self, username: str, password: str) -> ApiResponse:
        """登录。参考原项目：POST /api/user/login。

        这里需要根据实际接口调整：请求体字段名（原项目为 username/password）。
        """
        return self._request(
            "POST",
            PATH_LOGIN,
            body={"username": username, "password": password},
        )

    def checkin(self, turnstile_token: str | None = None) -> ApiResponse:
        """签到。参考原项目：POST /api/user/checkin。

        turnstile_token 仅在站点启用 Turnstile 时需要；本脚本默认不启用。
        若启用，按原项目那样把 token 放 query（urlencode）。
        """
        params = None
        if turnstile_token:
            params = {"turnstile": turnstile_token}
        return self._request("POST", PATH_CHECKIN, params=params)

    def get_checkin_stats(self, month: str) -> ApiResponse:
        """查询某月签到状态。参考原项目：GET /api/user/checkin?month=YYYY-MM。"""
        return self._request("GET", PATH_CHECKIN_STATS, params={"month": month})


def _parse_retry_after(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None
