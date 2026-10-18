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

`run` 默认创建新会话；使用 `--session NAME` 可以继续已有会话。`--dir PATH` 指定工作区，`--plan` 让 Agent 维护 `PLAN.md` 与 `TODO.md`。`chat` 是多轮终端会话，输入 `/new` 开启新会话，`/exit` 结束。写文件、修改文件和需要人工审批的命令会展示完整参数并逐次确认，空输入默认拒绝；命令默认支持经分析后的只读免审。无人值守时可显式加 `--auto-approve` 自动批准全部操作。会话存入 `.xd-agent/sessions.sqlite3`，每次运行的简要追踪保存在工作区的 `.xd-agent/traces/`。

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
  auto_approve_read_only_commands: true
  command_review_timeout_seconds: 30
  command_approval_required_fragments: []
```

`channel.type` 支持 `cli`、`feishu`、`dingtalk` 和 `wecom`。各渠道属性均可写入 `config/base.yaml` 或对应环境的 YAML，环境文件按字段覆盖基础文件；也可用 `APP_CHANNEL__...` 环境变量覆盖。默认配置在 `config/base.yaml` 列出了全部渠道字段。选中渠道时才要求该渠道的必需凭据和审批人 ID。

配置好渠道后，用 `xd-agent serve` 启动 HTTP 服务及所选消息渠道，按 Ctrl+C 停止。监听地址和端口默认读取 `http.host`、`http.port`（也支持 `APP_HTTP__HOST`、`APP_HTTP__PORT`），可用 `serve --host 0.0.0.0 --port 8000` 覆盖。全局选项放在子命令之前，例如 `xd-agent --env production --config-dir C:/xd-agent/config serve`。`serve` 固定使用单个工作进程；`channel.type=cli` 时只提供 HTTP 接口，终端对话仍使用 `chat`。

CLI、飞书、钉钉和企业微信统一支持单独发送 `/new`：直接开启新上下文，不调用模型；旧聊天、摘要和工作区文件保留。当前会话映射保存在数据库中，服务重启后继续使用新会话。消息渠道仅允许有权发起任务的用户使用；当前窗口有任务排队、执行或等待审批时，会提示先完成任务。飞书和钉钉的群聊会切换整个群的机器人上下文，企业微信按成员单聊切换。CLI 在任务完成后可输入 `/new`；指定 `--session NAME` 时，后续 `chat` 和 `run` 均继续该名称对应的新会话，也可用 `xd-agent run /new --session NAME` 直接切换。

命令审批共用以下流程，适用于 CLI、飞书、钉钉和企业微信：

1. 先检查 `src/xd_agent/agent/command_review.py` 中的固定高危规则。包含删除、磁盘/分区操作、文件写入和权限变更、系统/服务/账户操作、软件安装、远程传输、Git 变更、解释器或动态执行等风险特征时，直接转人工审批，使用固定的作用与影响说明，不请求 LLM。规则保守匹配整个命令，包含参数和引号内容，因此可能将部分无害命令也送审。
2. 其余命令使用当前配置的模型独立分析完整命令的作用、影响和是否只读；不向分析请求提供工具或任务历史。仅在返回有效且明确的只读结论时自动批准，并展示只读免审说明。模型请求失败、超时、输出格式无效或不能确认只读时，回退人工审批。
3. 人工审批消息包含完整参数及命令的作用、影响和判断来源。关闭 `agent.auto_approve_read_only_commands`（或设置 `APP_AGENT__AUTO_APPROVE_READ_ONLY_COMMANDS=false`）后，每条命令均需人工审批，仍会执行上述分析并补充说明；固定高危规则命中时仍不询问 LLM。文件读取等原有只读工具的免审行为不受该开关影响。

`agent.command_review_timeout_seconds` 控制分析超时，默认 30 秒。`agent.command_approval_required_fragments` 可增加必须审批的片段，例如 `["my-sensitive-tool"]`；按大小写不敏感的包含关系匹配，不能移除内置规则。LLM 分析会产生额外请求和 token 用量，计入当前会话。它是基于命令文本的风险判断，不能穷举所有命令和运行环境，也不能替代操作系统权限或沙箱隔离；不希望依赖此判断时可关闭只读免审。

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
uv run --frozen --extra feishu xd-agent serve --host 0.0.0.0 --port 8000
```

将飞书事件请求地址指向 `POST /webhook/event`，订阅消息接收事件，并授予机器人收发消息权限。默认仅 `allowed_approver_ids` 中的用户可以发起任务；如需将发起人与审批人分开，可另设 `APP_CHANNEL__FEISHU__ALLOWED_SENDER_IDS`。飞书的写文件、编辑文件和未通过只读免审的命令需人工审批，审批消息只能由配置的审批人在原会话中回复。请求超时后会拒绝执行。Webhook 的签名校验、事件解密和消息归一化交给飞书 Channel SDK 的严格模式处理。

飞书的审批等待和同会话执行锁位于进程内，审批快照及审计事件保存到 SQLite。部署飞书模式时请使用单个 Uvicorn 工作进程。

## 钉钉 Stream 机器人

安装 `dingtalk` 可选依赖，在钉钉开放平台创建企业机器人并启用 Stream 模式。`client_id` 为应用的 AppKey，`client_secret` 为 AppSecret。运行 Web 服务即可由 SDK 主动建立 Stream 连接，无需配置入站 Webhook：

```powershell
uv sync --frozen --extra dingtalk
$env:APP_CHANNEL__TYPE = "dingtalk"
$env:APP_CHANNEL__DINGTALK__CLIENT_ID = "dingxxx"
$env:APP_CHANNEL__DINGTALK__CLIENT_SECRET = "..."
$env:APP_CHANNEL__DINGTALK__ALLOWED_APPROVER_IDS = '["admin-user-id"]'
uv run --frozen --extra dingtalk xd-agent serve
```

钉钉消息按会话 ID 维护上下文，使用收到消息时提供的 `sessionWebhook` 发送进度和审批提示。`allowed_approver_ids` 和 `allowed_sender_ids` 填机器人消息中的 `senderStaffId`。

如果日志出现 `DingTalk reply delivery failed`，表示回复发送失败；同一行会保留 HTTP 状态及钉钉返回的 `errcode`、`errmsg` 等诊断字段，Webhook 会话凭据会脱敏。请根据具体错误码检查接口权限、会话 Webhook 有效期或发送限制；Stream 的 `PING` 日志本身不是发送失败原因。

工具调用回复中的正文可能只有空格或换行；Agent 会跳过这类纯空白正文，继续工具和审批流程，避免钉钉以 `400202: miss param : text->content` 拒绝空白消息。

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
uv run --frozen --extra wecom xd-agent serve --host 0.0.0.0 --port 8000
```

企业微信仅处理发给该自建应用的文本消息；按成员 UserID 保存会话，使用应用消息接口发送回复。回调验证和消息接收均校验签名并解密。审批人可以在自己的应用单聊中回复审批 ID。

所有消息渠道目前仅支持单进程部署，请使用一个 Uvicorn 工作进程。飞书和钉钉仍要求审批人在原会话回复；个人使用时可将自己的 ID 配为审批人，在群聊中也可由同群审批人处理。

审批请求会完整展示工具参数，长参数按 UTF-8 字节数分段，全部发送成功后才接受批准。参数超过 64KB 时会拒绝执行并要求拆分调用，不会截断内容后继续执行。通知发送失败、超时、拒绝或任务取消时，尚未获批的工具不会执行。

审批快照和审计事件分别保存在会话数据库的 `approvals`、`approval_events` 表中，包括会话、工具调用 ID、完整参数、审批策略、审批人、决定时间、拒绝原因、非法或重复回复，以及工具执行开始、完成、失败和中断记录。命令分析说明保存在 `command_assessed` 事件中；只读自动批准的 `decided_by` 为 `command-review`。本地自动放行也会记录。服务重启后，未完成审批会失效；已批准但没有确认完成的执行会标记为中断，需重新检查实际状态。旧审批 ID 不能继续批准或自动恢复执行。

## 工具与评测

工作区级技能放在 `.agents/skills/<skill-name>/SKILL.md`，例如 `.agents/skills/document-summary/SKILL.md`；技能目录可按需包含 `scripts/`、`references/` 和 `assets/`。这里的工作区由 `--dir` 或 `agent.workspace_dir` 指定。当前启动任务时会将技能文件内容加入系统提示词，仅加载工作区级技能，尚不扫描用户级目录。旧的 `.claw/skills/` 不再扫描，已有技能需迁移到 `.agents/skills/`。技能文件可纳入版本控制，运行数据仍保存在 `.xd-agent/`。

提供 `read_file`、`read_tool_output`、`list_files`、`search_text`、`write_file`、`edit_file`、`run_command` 和只读的 `explore_workspace`。文件工具将路径限制在工作区中；`run_command` 会在宿主机执行 PowerShell（Windows）或 Bash（Linux/macOS），CLI 使用者应只向可信任务开放。命令失败、超时、重复工具失败以及最大轮数均会明确反馈。

工具成功、失败和审批拒绝的结果统一受 `max_tool_output_chars`（默认 8000 字符）限制，实际每页预算还不超过 `max_context_chars` 的四分之一。长结果保存在工作区 `.xd-agent/tool-outputs/`，返回预览和存档 ID；模型可用 `read_tool_output(output_id, offset, limit)` 按需取回。命令输出流式存档，避免完整日志占用内存；`read_file(path, offset, limit)` 可直接分页读取原文件。`offset` 按 Unicode 字符计数，续读提示也计入页面长度，按提示继续不会跳过正文。小于续读提示长度的页面会返回参数错误。

大量文件调查优先使用 `explore_workspace`。探索助手只持有自己的局部历史，主会话仅接收“结论、关键证据、未确认事项”报告，默认上限由 `max_exploration_summary_chars` 控制（3000 字符），同时服从工具输出预算。过长报告保留各部分摘要，并附完整报告的存档 ID；模型没有提供结构化证据时会明确标记待核实。只读探索助手可分页读取存档，不能调用命令和编辑工具。

主会话和探索助手在达到消息数或字符预算时，会先调用模型压缩旧历史，再移除对应消息；一个助手消息中的工具调用及其结果作为整体保留或压缩。摘要要求保留用户目标与约束、决策、文件位置、验证结果和未完成事项，主会话摘要通过 `session_memory` 表持久化，原始 `messages` 不删除。`memory_summary_chars` 默认 6000 字符，有效摘要预算不超过上下文的八分之一。摘要模型调用会产生额外用量，并计入主会话；摘要请求分块限长，失败时保留原历史并终止本次运行。摘要仍可能省略细节，需要时应重新核实文件和工具存档。

所有工具调用都在 `ToolRegistry.execute()` 的执行入口检查审批。新增工具默认需要审批；显式声明 `read_only = True` 的无副作用工具免审，`run_command` 则按上述独立分析流程决定是否免人工审批。只读探索助手仅注册文件读取、列表和搜索工具。

CLI 的 `run`、`chat` 默认对未通过只读免审的命令使用 `TerminalApproval` 逐次确认。显式传入 `--auto-approve` 时使用 `LocalApproval` 自动放行全部操作，并跳过命令分析；`benchmark` 同样使用这一显式无人值守策略。这一选项独立于只读免审开关，仍可自动批准高危命令。这些策略均记录到审批审计。代码调用 `AgentService.run()` 时必须明确传入 `approval`；遗漏会报错，直接调用没有审批策略的工具注册表也不能执行有副作用的工具。

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

## 开源协议

本项目采用 [MIT License](LICENSE) 开源协议。
