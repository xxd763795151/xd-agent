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

`run` 默认创建新会话；使用 `--session NAME` 可以继续已有会话。`--dir PATH` 指定工作区，`--plan` 让 Agent 维护 `PLAN.md` 与 `TODO.md`。`chat` 是多轮终端会话，输入 `/exit` 结束。写文件、修改文件和运行命令前默认展示完整参数并逐次确认，空输入默认拒绝；无人值守时可显式加 `--auto-approve` 自动批准。会话存入 `.xd-agent/sessions.sqlite3`，每次运行的简要追踪保存在工作区的 `.xd-agent/traces/`。

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

飞书的审批等待和同会话执行锁位于进程内，审批快照及审计事件保存到 SQLite。部署飞书模式时请使用单个 Uvicorn 工作进程。

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

所有消息渠道目前仅支持单进程部署，请使用一个 Uvicorn 工作进程。飞书和钉钉仍要求审批人在原会话回复；个人使用时可将自己的 ID 配为审批人，在群聊中也可由同群审批人处理。

审批请求会完整展示工具参数，长参数按 UTF-8 字节数分段，全部发送成功后才接受批准。参数超过 64KB 时会拒绝执行并要求拆分调用，不会截断内容后继续执行。通知发送失败、超时、拒绝或任务取消时，尚未获批的工具不会执行。

审批快照和审计事件分别保存在会话数据库的 `approvals`、`approval_events` 表中，包括会话、工具调用 ID、完整参数、审批策略、审批人、决定时间、拒绝原因、非法或重复回复，以及工具执行开始、完成、失败和中断记录。本地自动放行也会记录。服务重启后，未完成审批会失效；已批准但没有确认完成的执行会标记为中断，需重新检查实际状态。旧审批 ID 不能继续批准或自动恢复执行。

## 工具与评测

工作区级技能放在 `.agents/skills/<skill-name>/SKILL.md`，例如 `.agents/skills/document-summary/SKILL.md`；技能目录可按需包含 `scripts/`、`references/` 和 `assets/`。这里的工作区由 `--dir` 或 `agent.workspace_dir` 指定。当前启动任务时会将技能文件内容加入系统提示词，仅加载工作区级技能，尚不扫描用户级目录。旧的 `.claw/skills/` 不再扫描，已有技能需迁移到 `.agents/skills/`。技能文件可纳入版本控制，运行数据仍保存在 `.xd-agent/`。

提供 `read_file`、`read_tool_output`、`list_files`、`search_text`、`write_file`、`edit_file`、`run_command` 和只读的 `explore_workspace`。文件工具将路径限制在工作区中；`run_command` 会在宿主机执行 PowerShell（Windows）或 Bash（Linux/macOS），CLI 使用者应只向可信任务开放。命令失败、超时、重复工具失败以及最大轮数均会明确反馈。

工具成功、失败和审批拒绝的结果统一受 `max_tool_output_chars`（默认 8000 字符）限制，实际每页预算还不超过 `max_context_chars` 的四分之一。长结果保存在工作区 `.xd-agent/tool-outputs/`，返回预览和存档 ID；模型可用 `read_tool_output(output_id, offset, limit)` 按需取回。命令输出流式存档，避免完整日志占用内存；`read_file(path, offset, limit)` 可直接分页读取原文件。`offset` 按 Unicode 字符计数，续读提示也计入页面长度，按提示继续不会跳过正文。小于续读提示长度的页面会返回参数错误。

大量文件调查优先使用 `explore_workspace`。探索助手只持有自己的局部历史，主会话仅接收“结论、关键证据、未确认事项”报告，默认上限由 `max_exploration_summary_chars` 控制（3000 字符），同时服从工具输出预算。过长报告保留各部分摘要，并附完整报告的存档 ID；模型没有提供结构化证据时会明确标记待核实。只读探索助手可分页读取存档，不能调用命令和编辑工具。

主会话和探索助手在达到消息数或字符预算时，会先调用模型压缩旧历史，再移除对应消息；一个助手消息中的工具调用及其结果作为整体保留或压缩。摘要要求保留用户目标与约束、决策、文件位置、验证结果和未完成事项，主会话摘要通过 `session_memory` 表持久化，原始 `messages` 不删除。`memory_summary_chars` 默认 6000 字符，有效摘要预算不超过上下文的八分之一。摘要模型调用会产生额外用量，并计入主会话；摘要请求分块限长，失败时保留原历史并终止本次运行。摘要仍可能省略细节，需要时应重新核实文件和工具存档。

所有工具调用都在 `ToolRegistry.execute()` 的执行入口检查审批。新增工具默认需要审批，只有显式声明 `read_only = True` 的无副作用工具才能免审。只读探索助手仅注册文件读取、列表和搜索工具。

CLI 的 `run`、`chat` 默认使用 `TerminalApproval` 逐次确认，只有显式传入 `--auto-approve` 才使用 `LocalApproval` 自动放行；`benchmark` 为无人值守评测，显式使用 `LocalApproval`。这些策略均记录到审批审计。代码调用 `AgentService.run()` 时必须明确传入 `approval`；遗漏会报错，直接调用没有审批策略的工具注册表也不能执行有副作用的工具。

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
