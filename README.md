# win-checkin

Windows 本地运行的轻量 New API 签到脚本。**开机自启，只做一件事**：

> 在多个时间点尝试签到；全部站点成功就自动退出；有失败就等下一个时间点重试；
> 当天所有时间点都失败则打印日志后退出。

本项目参考 [Luckylos/newapi-checkin](https://github.com/Luckylos/newapi-checkin) 重写，
**只借鉴了两块**：HTTP 客户端的认证头/接口封装，以及「成功/已签到/失败」的判定逻辑。
调度逻辑为全新实现，不含 Docker、不含 cf_turnstile 依赖、不含长期循环。

---

## 1. 目录结构

```
win-checkin/
├── main.py                # 入口：加载配置 → 初始化日志 → 启动调度
├── run_once.py            # 手动一次性签到：立即签到一次后退出
├── 手动签到.bat            # 双击运行 run_once.py
├── config.json            # 你的站点配置（可编辑）
├── config.json.example    # 配置模板
├── requirements.txt       # requests + APScheduler
├── checkin/
│   ├── __init__.py
│   ├── config.py          # 读取/校验 JSON 配置
│   ├── client.py          # HTTP 封装：认证头、接口地址、参数、响应解析
│   ├── service.py         # 签到判定：成功 / 已签到 / 失败 / 异常
│   ├── state.py           # 手动签到成功标记，供调度版当天跳过
│   └── scheduler.py       # 调度 + 过期时间点过滤 + 退出决策
├── logs/                  # 日志目录（自动创建）
└── README.md
```

各模块职责：

| 模块 | 负责 |
| --- | --- |
| `config.py` | 读 `config.json`，校验站点字段、模式、时间点格式 |
| `client.py` | 用 `requests.Session` 发请求；拼 `Authorization: Bearer …` 与 `New-API-User` 头；识别 429 / HTML |
| `service.py` | 对单个站点执行签到并给出 `success / already / failed / error` 判定 |
| `scheduler.py` | `BlockingScheduler` 注册任务、过滤过期时间点、决定退出码 |
| `main.py` | 组装全部、配置日志、`sys.exit(退出码)` |

---

## 2. 安装

```powershell
cd win-checkin
python -m pip install -r requirements.txt
```

依赖仅两个：`requests`、`APScheduler`。

---

## 3. 配置站点

复制 `config.json.example` 为 `config.json`，按需修改。

```json
{
  "timezone": "Asia/Shanghai",
  "startup_delay_minutes": 10,
  "fixed_times": ["15:00", "20:00", "23:30"],
  "request_timeout": 30.0,
  "max_retries": 2,
  "retry_backoff_s": 2.0,
  "log_file": "logs/checkin.log",
  "sites": [
    {
      "name": "我的新API站点",
      "url": "https://newapi.example.com",
      "mode": "api",
      "token": "你的 access_token",
      "user_id": 123
    },
    {
      "name": "登录即签到站点",
      "url": "https://another.example.com",
      "mode": "login",
      "username": "your_username",
      "password": "your_password"
    }
  ]
}
```

### 站点字段说明

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 站点名，仅用于日志 |
| `url` | 是 | 站点根地址，须以 `http://` 或 `https://` 开头 |
| `mode` | 是 | `api` 或 `login` |
| `token` | `api` 模式必填 | access_token，作为 `Authorization: Bearer {token}` |
| `user_id` | 可选 | 用于 `New-API-User` 头；有些站点需要 |
| `username` / `password` | `login` 模式必填 | 登录凭证 |

### 全局字段说明

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `timezone` | `Asia/Shanghai` | 调度时区 |
| `startup_delay_minutes` | `10` | 开机后延迟多久做第一次尝试 |
| `fixed_times` | `["15:00","20:00","23:30"]` | 其余固定尝试时间点 |
| `request_timeout` | `30.0` | 单请求超时（秒） |
| `max_retries` | `2` | 仅网络层错误的重试次数 |
| `retry_backoff_s` | `2.0` | 重试指数退避基数 |
| `log_file` | `logs/checkin.log` | 日志文件路径 |

---

## 4. 运行

### 手动试运行（有控制台，便于观察）

```powershell
cd win-checkin
python main.py
```

或指定配置文件：

```powershell
python main.py --config D:\path\to\config.json
```

### 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 所有站点已签到成功（或今日已签到），程序主动退出 |
| `1` | 当天所有时间点用完仍有站点失败 |
| `2` | 配置文件错误 |

### 手动一次性签到（立即执行，不等时间点）

```powershell
python run_once.py                    # 立即对所有站点签到一次
python run_once.py --config my.json   # 指定配置文件
```

也可直接双击 `手动签到.bat`（跑完暂停显示结果）。

- 退出码同 `main.py`：`0` 全成功 / `1` 有失败 / `2` 配置错误。
- **成功后会写标记 `logs/manual_done.txt`**：当天调度版一启动（或到点）即检测到并直接退出，
  不再重复自动签到；跨天后标记自动失效。标记文件可用 `config.json` 的 `state_file` 改名。
- `run_once.py` 不影响任何站点数据，只登录/签到一次。

---

## 5. 调度行为（重点）

时间点设计：

- 开机后延迟 `startup_delay_minutes` 分钟：第一次尝试
- `fixed_times` 中各时间点：后续尝试（默认 15:00 / 20:00 / 23:30）

执行规则：

- 每次到点，遍历所有站点；单站点异常不影响其它站点
- 所有站点 `success` 或 `already` → 退出码 `0`
- 有站点失败 → 记录日志，等下一个时间点
- 当天所有时间点都失败 → 退出码 `1`

**过期时间点处理**（以脚本启动时刻为准，不补签）：

| 启动时刻 | 当天注册的任务 |
| --- | --- |
| 08:00 | 开机延迟 + 15:00 + 20:00 + 23:30 |
| 16:00 | 开机延迟 + 20:00 + 23:30（跳过 15:00） |
| 21:00 | 开机延迟 + 23:30（跳过 15:00、20:00） |
| 23:45 | 仅开机延迟（三个固定点全过期） |

---

## 6. 开机自启（Windows 任务计划程序）

推荐方式：**用户登录时触发**，操作用 `pythonw.exe`（无控制台黑窗）。

> 先确认 `pythonw.exe` 的完整路径（通常是
> `C:\Users\<你>\AppData\Local\Programs\Python\Python3xx\pythonw.exe`）。
> 在 PowerShell 里执行 `where.exe pythonw` 即可查看。

### 6.1 图形界面配置

1. 按 `Win + R`，输入 `taskschd.msc`，回车打开「任务计划程序」。
2. 右侧点「创建任务…」（**不是**「创建基本任务」）。
3. **常规**选项卡：
   - 名称：`NewAPI 签到`
   - 勾选「只在用户登录时运行」
   - 可勾选「不管用户是否登录都要运行」→ 但那样不会随登录触发，按需取舍
   - 勾选「使用最高权限运行」（可选）
   - 配置：`Windows 10`
4. **触发器**选项卡 → 新建：
   - 开始任务：`登录时`
   - 特定用户：选择你自己的账户
   - 确定
5. **操作**选项卡 → 新建：
   - 操作：`启动程序`
   - 程序或脚本：`C:\...\pythonw.exe`（完整路径）
   - 添加参数（可选）：`main.py`
   - 起始于：`C:\...\win-checkin`（**项目目录，必须填**）
   - 确定
6. **条件**选项卡：取消勾选「只有在计算机使用交流电源时才启动」等限制（按需）。
7. **设置**选项卡：勾选「如果任务失败，按以下频率重新启动」，可设为每 5 分钟、最多 3 次（可选）。
8. 确定保存。

### 6.2 命令行配置（schtasks，方便复制）

以**当前用户登录时触发**为例：

```powershell
# 1) 找到 pythonw.exe 路径
$pythonw = (Get-Command pythonw.exe).Source
$workdir = "C:\Users\FangYuan\Desktop\Code\项目\newapi自动签到\win-checkin"

# 2) 创建任务（登录时触发）
schtasks /Create /TN "NewAPI签到" /SC ONLOGON /RL LIMITED `
  /TR "\"$pythonw\" \"$workdir\main.py\"" /F
```

> 注意：`schtasks /TR` 无法直接设置「起始于」目录，因此 `main.py` 内部用
> `Path(__file__).resolve().parent` 作为基准目录解析 `config.json` 与 `logs`，
> 不依赖工作目录。上面用绝对路径调用 `main.py` 即可。

如果希望明确设置工作目录，可用 XML 导入（见 6.4），或在参数里传绝对配置路径：

```powershell
schtasks /Create /TN "NewAPI签到" /SC ONLOGON /RL LIMITED `
  /TR "\"$pythonw\" \"$workdir\main.py\" --config \"$workdir\config.json\"" /F
```

### 6.3 验证是否生效

```powershell
# 查看任务是否存在
schtasks /Query /TN "NewAPI签到" /V /FO LIST

# 立即手动运行一次（验证路径/权限）
schtasks /Run /TN "NewAPI签到"

# 查看任务状态与上次结果
schtasks /Query /TN "NewAPI签到"
```

再检查日志文件是否开始写入：

```powershell
Get-Content "C:\Users\FangYuan\Desktop\Code\项目\newapi自动签到\win-checkin\logs\checkin.log" -Tail 30
```

注销并重新登录一次，确认任务自动触发（`上次运行结果` 应为 `0x0`）。

### 6.4 用 XML 导入（可精确控制工作目录）

保存以下内容为 `newapi-checkin-task.xml`（把路径改成你自己的），然后导入：

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\Users\FangYuan\AppData\Local\Programs\Python\Python312\pythonw.exe</Command>
      <Arguments>main.py</Arguments>
      <WorkingDirectory>C:\Users\FangYuan\Desktop\Code\项目\newapi自动签到\win-checkin</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
```

导入：

```powershell
schtasks /Create /TN "NewAPI签到" /XML ".\newapi-checkin-task.xml" /F
```

### 6.5 卸载 / 禁用

```powershell
# 禁用（保留任务）
schtasks /Change /TN "NewAPI签到" /DISABLE

# 重新启用
schtasks /Change /TN "NewAPI签到" /ENABLE

# 彻底删除
schtasks /Delete /TN "NewAPI签到" /F
```

图形界面：任务计划程序 → 找到「NewAPI 签到」→ 右键「禁用」或「删除」。

---

## 7. 签到判定规则

`service.py` 对每个站点独立判定，单站点异常不会影响其它站点：

| 结果 | 判定依据 |
| --- | --- |
| **success** | `api`：JSON 含 `success == true`（读取 `data.quota_awarded`）。`login`：登录返回 `success == true` |
| **already** | 响应含 `data.stats.checked_in_today == true`，或 message 包含「已签到 / already checked in」；`login` 模式下响应含 `checked_in == true` |
| **failed** | 以上都不是（例如 `success == false` 且无已签到标记） |
| **error** | 网络异常、返回 HTML（疑似 Cloudflare/WAF）、非法 JSON、HTTP 429 |

要点：

- **HTTP 200 不等于签到成功**，必须看 JSON 内容。
- 返回 HTML 会明确报错并跳过该站点，不会误判为成功。
- `api` 模式若只有 `success` 而无 `quota_awarded`，仍算成功（`quota_awarded` 记录为 `None`）。

---

## 8. 日志

因为 `pythonw.exe` 没有控制台，日志**同时**写入文件和 stdout：

- 文件：`logs/checkin.log`（自动轮转，单文件 2MB，保留 3 份）
- stdout：控制台调试时可见

日志包含：时间戳、站点名、成功/失败状态、当前是第几次尝试。示例：

```
2026-01-01 08:12:03 [INFO] ============================================================
2026-01-01 08:12:03 [INFO] 第 1/4 次尝试（开机延迟10分钟）开始
2026-01-01 08:12:03 [INFO] [我的新API站点] 开始签到（mode=api, url=https://newapi.example.com）
2026-01-01 08:12:04 [INFO] [我的新API站点] 结果：签到成功，获得 100（ok）
2026-01-01 08:12:04 [INFO] 本次尝试小结：成功/已签到 1，失败/异常 0，共 1
2026-01-01 08:12:04 [INFO] 所有站点均已签到成功，程序即将退出。
```

---

## 9. 常见问题

- **找不到 `pythonw`**：用 `where.exe pythonw` 查询；或在虚拟环境里用
  `.venv\Scripts\pythonw.exe`。
- **任务运行但日志没更新**：检查「起始于」目录是否设为项目目录，或改用
  `--config` 传绝对路径。确认 `pythonw.exe` 有权限访问该目录。
- **签到返回 HTML**：站点开了 Cloudflare 校验。本项目不做 Turnstile，
  该站点会记为 `error` 并跳过，换用 `login` 模式或改用带 token 的 `api` 模式重试。
- **想改时间点**：编辑 `config.json` 的 `fixed_times`，无需改代码。
- **如何适配你自己的站点接口**：`checkin/client.py` 里搜索注释
  `这里需要根据实际接口调整`，那里标出了接口路径与请求体字段名。

---

## 10. 免责声明

请仅用于你拥有或已获授权的账户。遵守各站点服务条款。
