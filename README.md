# xd-agent

基于 Python 3.12 的轻量 Agent。支持 CLI、飞书、钉钉和企业微信渠道。各渠道及评测复用同一套模型循环、工具及 SQLite 会话存储。

## 安装与运行

```powershell
uv sync --frozen --extra dev
$env:APP_AGENT__API_KEY = "你的模型 API Key"
uv run --frozen xd-agent run "检查当前项目并给出改进建议"
uv run --frozen xd-agent chat --session daily
```

Linux/macOS 请用对应 shell 的环境变量语法。默认模型为智谱的 OpenAI 兼容接口 `glm-4.5-air`。可通过 `APP_AGENT__PROVIDER`、`APP_AGENT__MODEL` 和 `APP_AGENT__BASE_URL` 选择其他 OpenAI 兼容或 Anthropic 兼容服务。价格参数只用于估算成本，应按所选模型实际价格设置。

`run` 默认创建新会话；使用 `--session NAME` 可以继续已有会话。`--dir PATH` 指定工作区，`--plan` 让 Agent 维护 `PLAN.md` 与 `TODO.md`。`chat` 是多轮终端会话，输入 `/exit` 结束。会话存入 `.xd-agent/sessions.sqlite3`，每次运行的简要追踪保存在工作区的 `.xd-agent/traces/`。

模型服务返回 HTTP 错误、请求超时或网络异常时，`run` / `chat` 默认只记录一条包含处理建议的错误日志，并以退出码 `5` 结束；HTTP 错误会保留状态码和可用的服务端错误码。需要排查堆栈时使用 `xd-agent run "任务" --debug` 或 `xd-agent chat --debug`。HTTP 429 表示请求受到限制，需结合错误码检查服务商的并发、速率及配额限制；当前不会自动重试请求。

## 配置

配置来自 `config/base.yaml` 与当前环境 YAML，并可通过 `APP_*` 环境变量覆盖。`APP_ENV` 可选 `development`、`test`、`staging` 或 `production`。预发布和生产环境需要用绝对路径设置 `APP_CONFIG_DIR`。本地开发可复制 `.env.example` 为 `.env`；不要将密钥写入 YAML 或提交到版本控制。

默认配置为：

```yaml
channel:
  type: cli
agent:
  provider: openai_compatible
  model: glm-4.5-air
  workspace_dir: workspace
  max_turns: 30
```

`channel.type` 支持 `cli`、`feishu`、`dingtalk` 和 `wecom`。各渠道属性均可写入 `config/base.yaml` 或对应环境的 YAML，环境文件按字段覆盖基础文件；也可用 `APP_CHANNEL__...` 环境变量覆盖。默认配置在 `config/base.yaml` 列出了全部渠道字段。选中渠道时才要求该渠道的必需凭据和审批人 ID。

例如，在 `config/production.yaml` 中选择钉钉并配置非密钥属性：

```yaml
channel:
  type: dingtalk
  dingtalk:
    client_id: dingxxxxxxxx
    allowed_approver_ids: ["admin-user-id"]
    allowed_sender_ids: ["requester-user-id"]
    approval_timeout_seconds: 300
```

其余字段（包括 `client_secret`）同样支持 YAML；生产环境建议通过环境变量提供密钥。`allowed_sender_ids` 留空时，只有 `allowed_approver_ids` 中的用户能发起任务。审批人必须在原会话中回复 `approve ID` 或 `reject ID`；企业微信应用单聊会将审批请求同时发送给各审批人。

## 飞书 Webhook

安装可选依赖并配置飞书应用：

```powershell
uv sync --frozen --extra feishu
$env:APP_CHANNEL__TYPE = "feishu"
$env:APP_CHANNEL__FEISHU__APP_ID = "cli_xxx"
$env:APP_CHANNEL__FEISHU__APP_SECRET = "..."
$env:APP_CHANNEL__FEISHU__VERIFICATION_TOKEN = "..."
$env:APP_CHANNEL__FEISHU__ENCRYPT_KEY = "..."
$env:APP_CHANNEL__FEISHU__ALLOWED_APPROVER_IDS = '["ou_xxx"]'
uv run --frozen --extra feishu uvicorn xd_agent.web.app:create_app --factory --host 0.0.0.0 --port 8000
```

将飞书事件请求地址指向 `POST /webhook/event`，订阅消息接收事件，并授予机器人收发消息权限。默认仅 `allowed_approver_ids` 中的用户可以发起任务；如需将发起人与审批人分开，可另设 `APP_CHANNEL__FEISHU__ALLOWED_SENDER_IDS`。飞书的写文件、编辑文件和命令工具均需审批，审批消息只能由配置的审批人在原会话中回复。请求超时后会拒绝执行。Webhook 的签名校验、事件解密和消息归一化交给飞书 Channel SDK 的严格模式处理。

目前飞书审批等待和同会话执行锁位于进程内，部署飞书模式时请使用单个 Uvicorn 工作进程。多进程部署需要外部队列与共享审批状态。

## 钉钉 Stream 机器人

安装 `dingtalk` 可选依赖，在钉钉开放平台创建企业机器人并启用 Stream 模式。`client_id` 为应用的 AppKey，`client_secret` 为 AppSecret。运行 Web 服务即可由 SDK 主动建立 Stream 连接，无需配置入站 Webhook：

```powershell
uv sync --frozen --extra dingtalk
$env:APP_CHANNEL__TYPE = "dingtalk"
$env:APP_CHANNEL__DINGTALK__CLIENT_ID = "dingxxx"
$env:APP_CHANNEL__DINGTALK__CLIENT_SECRET = "..."
$env:APP_CHANNEL__DINGTALK__ALLOWED_APPROVER_IDS = '["admin-user-id"]'
uv run --frozen --extra dingtalk uvicorn xd_agent.web.app:create_app --factory --host 0.0.0.0 --port 8000
```

钉钉消息按会话 ID 维护上下文，使用收到消息时提供的 `sessionWebhook` 发送进度和审批提示。`allowed_approver_ids` 和 `allowed_sender_ids` 填机器人消息中的 `senderStaffId`。

## 企业微信自建应用

安装 `wecom` 可选依赖，在企业微信自建应用中启用消息接收，并将回调 URL 指向 `GET/POST /webhook/event`。回调的 Token、EncodingAESKey 与配置值必须一致。应用需要允许目标成员使用并具备发送应用消息能力：

```powershell
uv sync --frozen --extra wecom
$env:APP_CHANNEL__TYPE = "wecom"
$env:APP_CHANNEL__WECOM__CORP_ID = "wwxxx"
$env:APP_CHANNEL__WECOM__CORP_SECRET = "..."
$env:APP_CHANNEL__WECOM__AGENT_ID = "1000001"
$env:APP_CHANNEL__WECOM__TOKEN = "..."
$env:APP_CHANNEL__WECOM__ENCODING_AES_KEY = "..."
$env:APP_CHANNEL__WECOM__ALLOWED_APPROVER_IDS = '["admin-user-id"]'
uv run --frozen --extra wecom uvicorn xd_agent.web.app:create_app --factory --host 0.0.0.0 --port 8000
```

企业微信仅处理发给该自建应用的文本消息；按成员 UserID 保存会话，使用应用消息接口发送回复。回调验证和消息接收均校验签名并解密。审批人可以在自己的应用单聊中回复审批 ID。

所有消息渠道的审批状态保存在单个进程内，部署时请使用一个 Uvicorn 工作进程。

## 工具与评测

提供 `read_file`、`list_files`、`search_text`、`write_file`、`edit_file`、`run_command` 和只读的 `explore_workspace`。文件工具将路径限制在工作区中；`run_command` 会在宿主机执行 PowerShell（Windows）或 Bash（Linux/macOS），CLI 使用者应只向可信任务开放。命令失败、超时、重复工具失败以及最大轮数均会明确反馈。

评测使用 JSON 文件描述准备文件、任务和文件内容断言，不依赖平台特定的 shell 脚本：

```powershell
uv run --frozen xd-agent benchmark examples/benchmark.json
```

## 开发检查

```powershell
uv sync --frozen --extra dev --extra feishu --extra dingtalk --extra wecom
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync mypy src
```

现有 `/health/live` 和 `/health/ready` 可用于服务健康检查。脚手架原有的 `greet` 与 `/greetings` 示例仍保留，便于验证 CLI 与 Web 的基本运行环境。
