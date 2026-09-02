# 用户配置

> 本文是 [User configuration](../configuration.md) 的中文翻译。

`coding-agent` 会在解析常规命令之前，读取一个可选的、路径固定的用户级配置文件：

```text
Windows: C:\Users\<you>\.coding-agent\settings.json
其他系统: ~/.coding-agent/settings.json
```

本项目不支持项目级本地配置，也不提供修改该路径的命令行参数。配置文件不存在是合法情况，此时环境变量和内置默认值仍然生效。即使配置文件格式错误，`--help` 仍然可用。

## 示例

```json
{
  "api_key": "<your-api-key>",
  "model": "deepseek-v4-flash",
  "base_url": "https://api.deepseek.com",
  "thinking": "disabled",
  "max_turns": 30,
  "max_seconds": 900,
  "command_timeout": 120,
  "approval_mode": "prompt",
  "allow_programs": ["python"],
  "runs_dir": "runs",
  "sessions_dir": "sessions",
  "session_context_chars": 12000
}
```

相对路径形式的 `runs_dir` 和 `sessions_dir` 会以 `settings.json` 所在目录为基准进行解析，因此上面的示例会把两类日志都存放在 `~/.coding-agent` 下。

## 配置模式

如果配置中包含未知字段、类型错误、超出范围的数值、非 UTF-8 输入、格式错误的 JSON，或者文件大小超过 64 KiB，系统会在设置工作区或请求模型之前拒绝该配置。

| 字段 | 类型及允许值 | 回退值 |
|---|---|---|
| `api_key` | 非空字符串 | 由 `api_key_env` 指定的环境变量 |
| `api_key_env` | 环境变量名称 | `OPENAI_API_KEY` |
| `model` | 非空字符串 | `CODING_AGENT_MODEL`；否则为必填项 |
| `base_url` | HTTP(S) 绝对 URL | `CODING_AGENT_BASE_URL`，然后是 `https://api.openai.com/v1` |
| `thinking` | `"enabled"`、`"disabled"` 或 `null` | `CODING_AGENT_THINKING`，然后使用供应商默认值 |
| `max_turns` | 1 到 200 的整数 | `30` |
| `max_seconds` | 1 到 7200 的整数 | `900` |
| `command_timeout` | 1 到 600 的整数 | `120` |
| `approval_mode` | `"prompt"` 或 `"deny"` | `"prompt"` |
| `allow_programs` | 可通过 PATH 解析的可执行文件名数组 | 空数组 |
| `runs_dir` | 非空的绝对路径或相对于配置文件的路径 | `CODING_AGENT_RUNS_DIR`，然后是 `~/.coding-agent/runs` |
| `sessions_dir` | 非空的绝对路径或相对于配置文件的路径 | `CODING_AGENT_SESSIONS_DIR`，然后是 `~/.coding-agent/sessions` |
| `session_context_chars` | 2000 到 18000 的整数 | `12000` |

将 `thinking` 显式设置为 JSON 的 `null`，表示“使用供应商默认值”，并且会覆盖 `CODING_AGENT_THINKING`。其他看似可空的字段必须直接省略，而不能设为 `null`。

当 `thinking` 设为 `"enabled"` 时，HTTP 适配器会使用自动工具选择，而不是强制工具选择，因为支持 Thinking 的端点可能拒绝 `tool_choice="required"`。本地协议仍然保持严格：包含零个、多个、格式错误或未知动作的响应，都会在执行之前被拒绝。

## 优先级

对于同时存在以下四种配置形式的选项，优先级依次为：

1. 显式命令行选项；
2. `settings.json` 中存在的值；
3. 对应的环境变量；
4. 内置默认值。

本次调用中，重复使用 `--allow-program NAME` 会整体替换 `allow_programs` 数组。系统特意不提供 `--api-key` 选项，因为命令行参数可能被其他本地进程看到。配置文件中直接设置的 `api_key` 优先于环境变量密钥回退方案。

评测套件维护自己的 `allowed_programs` 列表，以保证评测的执行策略可复现；用户级的 `allow_programs` 字段仅应用于 `tui` 和 `run`。

`session_context_chars` 应用于 `tui` 和 `resume`，也可以被 `--session-context-chars` 覆盖。它只限制恢复的对话历史；每次独立的智能体运行仍然保留由控制器管理的提示词和事件限制。

## 安全边界

使用 `api_key` 时，配置文件会包含明文凭据。请将它放在目标仓库之外，并通过操作系统账户权限限制访问。加载器不会在校验错误中包含密钥值，配置对象的字符串表示中也不会显示密钥。

面向模型的文件工具会拒绝直接访问任何 `.coding-agent` 目录，即使用户把主目录选作工作区也是如此。这是一项工作区级控制，而不是操作系统沙箱：通过审批的子进程启动的代码仍然拥有当前用户权限，并且能够访问操作系统允许访问的路径。
