# Agent Host (`agent-host`) for Antigravity

**Agent Host** 是面向反重力 **Antigravity** 的多 Agent 协同插件，灵感来自 `BytePioneer-AI/codex-host` 与 `zetbrush/multiagents`。它让主控统一调度外部 CLI 编程助手，并通过可视化控制台查看指令、运行过程、代码变更和最终结果。

支持 **OpenAI Codex CLI**、**Cursor CLI**、**Claude Code**、**Gemini CLI**，也支持通过本地 `custom_agents.json` 或 `register_custom_agent` MCP 工具注册自定义 CLI 扩展。内置 `mock` 适配器可用于离线流程验证。

## 核心功能

- **简体中文自然语言翻译与解读**：为主控指令、实时运行流和最终结果提供通俗中文说明，同时保留原始命令、事件与回复以便核对。当前使用离线规则解释常见命令和事件，并优先提取 Agent 按要求生成的中文回复；无法可靠翻译的内容会明确提示并保留原文。
- **按 Agent 分组的任务列表**：左侧按 Agent 展示任务，支持折叠与展开，便于切换和查看多个助手的进展。
- **任务删除与陈旧任务清理**：支持永久删除单个任务，以及按 Agent 或全局批量清理已结束的任务；批量清理保留运行中的任务。
- **Git Diff 审查**：查看任务捕获的 `diff.patch`，结合工作目录、Git worktree、沙箱设置和实际 CLI 命令审查代码变更。
- **多轮 Resume**：向已有任务追加指令或审查反馈，延续同一个 Agent 会话；需要适配器支持恢复且任务已记录 `session_id`。
- **实时可视化与调度**：通过 Sidecar 侧栏或 MCP App 查看执行事件，支持健康检查、任务派发、状态检查与停止任务。

## 安装

准备好 Git、Python（建议 3.11 或更新版本）和 Node.js，确保 `python`、`node` 可从命令行调用。按需安装并登录要使用的 Agent CLI；Antigravity 环境需支持插件及 Sidecar 运行时。

将仓库克隆到 `~/.gemini/config/plugins/agent-host` 即可由 Antigravity 自动加载。私有仓库需要使用具有访问权限的 GitHub 账号。

**Windows PowerShell：**

```powershell
git clone https://github.com/lan683010-spec/antigravity-agent-host.git "$HOME/.gemini/config/plugins/agent-host"
```

**macOS / Linux：**

```bash
git clone https://github.com/lan683010-spec/antigravity-agent-host.git ~/.gemini/config/plugins/agent-host
```

若 Antigravity 已在运行且尚未发现插件，可重启后加载。插件通过 `plugin.json`、`mcp_config.json` 和 `sidecars/agent_host_board/sidecar.json` 提供元数据、MCP 服务与侧栏入口。

## 快速使用

1. 在 Antigravity 中要求主控打开 Agent Host 控制台并检查各 Agent 的健康状态。
2. 指定目标 Agent、工作目录和任务要求，派发任务；在控制台查看主控命令与实时运行流。
3. 查看最终结果与 Git Diff，通过 Resume 继续追问，或清理已结束的任务。

控制台入口：`sidecar://agent-host/agent_host_board/`；MCP App 入口：`ui://agent-host/app.html`。

也可在插件目录运行 CLI 查看帮助或检查环境：

```text
python -m agentctl --help
python -m agentctl doctor
```

## 项目结构与本地数据

```text
agentctl/                    CLI、Agent 适配器与任务运行核心
assets/                      插件图标
bin/                         MCP 服务与嵌入式界面
rules/                       主控协同规则
sidecars/agent_host_board/   Sidecar 服务与可视化控制台
skills/agent-host/           Antigravity 协同技能说明
```

`.agent-runs/` 保存本地运行数据，`runs_roots.json` 记录本机任务目录索引，`custom_agents.json` 保存本地扩展配置。这些内容以及 Python 缓存、字节码、日志和操作系统元数据均由 `.gitignore` 排除，不上传到 GitHub。自定义 Agent 配置需要在安装机器上单独注册。
