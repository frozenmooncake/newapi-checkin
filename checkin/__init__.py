"""Windows 本地 New API 签到脚本。

模块划分：

- config   : 读取并校验 JSON 配置
- client   : HTTP 封装（认证头 / 接口地址 / 参数 / 响应解析）
- service  : 单个站点的签到判定（成功 / 已签到 / 失败）
- scheduler: apscheduler 调度 + 过期时间点过滤 + 退出决策
"""

__all__ = ["config", "client", "service", "scheduler"]
