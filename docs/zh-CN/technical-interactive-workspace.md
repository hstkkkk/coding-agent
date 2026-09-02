# 技术设计：可恢复的交互式 CLI 与 Git 工作区初始化

> 本文是 [Technical design: resumable interactive CLI and Git workspace bootstrap](../technical-interactive-workspace.md) 的中文翻译。

## 设计约束

实现必须保持当前的控制器契约：

- `AgentEngine` 仍然是唯一决定运行终止状态的模块；
- 模型的每一轮响应仍然只会被规范化为一个动作；
- `ANSWERED` 仍然不同于经过验证的 `SUCCEEDED`，并且只能在尚未记录任何工作区修改时使用；
- 验证证据仍然限定在一次运行和一个工作区版本内；
- 目标仓库内容和先前输出仍然是不可信数据；
- 暴露给模型的 Git 操作仍然是只读的；
- 运行时依赖仍然仅限 Python 标准库。

即使存在单动作契约，供应商适配器仍可能收到多个原生工具调用。此时它只规范化第一个调用，并记录原始调用数量，从而把提案串行化。被丢弃的调用绝不会到达引擎、策略或工具运行时；模型必须在收到第一个动作的真实观察结果后重新提出这些调用。这样无需依赖供应商专用的并行工具设置，也能保持单动作状态转换。

自动初始化是由用户请求、受信任的 CLI 启动操作。它不是新的模型可见工具，也不会放宽运行时的 Git 写入策略。

## 模块结构

```text
CLI composition root
  |
  +--> WorkspaceBootstrap.prepare(path) -> WorkspaceSetupResult
  |
  +--> LocalAgentRunner.run(objective) -> RunResult
  |       |
  |       +--> AgentEngine + model/tools/events/artifacts
  |
  +--> ConversationStore.create/resume/list
  |       |
  |       +--> validated append-only JSONL + deterministic compaction
  |
  +--> InteractiveSession.run(run_task) -> int
          |
          +--> TerminalPrompt.readline(prompt) -> str
          +--> ConversationSession.prepare/record/history

AgentEngine event projection
  |
  +--> describe_tool(name, arguments) -> bounded detail
  +--> describe_tool_result(name, arguments, data) -> bounded outcome

LocalToolRuntime approval
  |
  +--> PromptApprovalAdapter.request(request) -> ApprovalDecision
  +--> BrowserRenderer.render(path) -> DOM + opaque screenshot ID
```

这些都是深模块：调用方只需要了解一组很小的操作，而 Git 恢复、每次运行的组件装配、命令解析、持久化重放、上下文边界和界面展示等复杂性都被封装在各自实现中。

## 1. `WorkspaceBootstrap`

### 接口

`src/coding_agent/workspace.py` 暴露：

```python
@dataclass(frozen=True, slots=True)
class WorkspaceSetupResult:
    workspace: Path
    initialized: bool
    gitignore_updated: bool
    initial_commit: str | None
    messages: tuple[str, ...]

def prepare_workspace(path: Path) -> WorkspaceSetupResult:
    ...
```

调用方会收到规范状态和面向用户且不包含密钥的初始化消息。失败时抛出 `WorkspaceSetupError`，CLI 会将其规范化为配置错误。

### 算法

1. 解析路径，并要求它是一个已经存在的目录。
2. 使用有边界且不经过 Shell 的子进程，验证 `git` 是否可执行。
3. 探测 `git rev-parse --show-toplevel`。
   - 如果规范化的顶层目录等于目标目录，则校验 `git status` 并返回未修改结果。
   - 如果存在不同的外层顶层目录，则拒绝目标，而不是悄悄扩大工作区或创建嵌套仓库。
   - 如果存在 `.git` 项但探测失败，则报告仓库无效。
4. 修改之前，使用经过过滤的 Git 环境运行 `git var GIT_AUTHOR_IDENT`。失败时报告缺少身份配置。
5. 运行 `git init -q`。
6. 以原子方式合并所需的忽略模式。
7. 运行 `git add --all` 并创建 `chore: initialize repository` 提交。
8. 解析 `HEAD`，要求 `git status --porcelain` 为空，并返回缩写提交 ID。

所有 Git 调用都使用参数数组、`shell=False`、超时、大小受限的捕获输出，以及只包含操作系统执行信息、区域设置、主目录/Git 配置和 Git 身份变量的过滤环境。模型凭据会被排除。会修改状态的命令不会自动重试。

### `.gitignore` 构造

实现维护一份有序的模式列表：

1. 与 `PathPolicy` 一致的敏感文件名和后缀；
2. 操作系统、编辑器和智能体本地状态；
3. 根据项目标记选择的语言专用分组。

已有内容会逐字保留。一套完整且内部去重的托管规则会追加到 `# Added by coding-agent workspace setup` 标题下。即使前面已有相同模式，托管规则仍会刻意保留在末尾：这样可以防止用户之前写下的 `!.env` 等取反规则把凭据暴露给初始提交。写入使用同目录临时文件和 `os.replace`，同时保留已有内容和换行结尾。

项目检测是确定性的，并且只依赖文件系统：

| 标记 | 额外忽略项 |
|---|---|
| `pyproject.toml`、`setup.py`、`requirements*.txt` | Python 缓存、虚拟环境、覆盖率文件和打包输出 |
| `package.json` | `node_modules`、包管理器缓存、覆盖率文件和分发输出 |
| `CMakeLists.txt`、`*.sln`、`*.vcxproj` | CMake 和编译器构建输出 |

初始提交会暂存所有未被最终规则排除的文件。忽略列表包含当前被 `PathPolicy` 阻止的每一个凭据文件名，因此这些文件会保持未跟踪状态。

### 幂等性与失败状态

对于已有的有效仓库，初始化严格不执行任何操作，也绝不会修订或改写历史。

预检查失败发生在修改之前。如果在 `git init` 之后失败，新创建的仓库和 `.gitignore` 可能会保留；错误会报告已经完成的阶段，供用户检查。实现不会删除 `.git` 来回滚，因为这比用户请求的初始化操作具有更广泛的破坏性。

## 2. `LocalAgentRunner`

### 接口

`src/coding_agent/local_runner.py` 暴露一个配置数据类和一个方法：

```python
class LocalAgentRunner:
    def run(self, objective: str) -> RunResult:
        ...
```

构造函数接收经过校验的运行配置、工作区、审批机制、模型适配器、运行目录和事件展示模式。每次调用 `run` 时，都会创建全新的运行 ID、事件日志、制品存储、工具运行时和 `AgentEngine`。

这样消除了 `run` 命令和终端 UI 中重复的每次运行组件装配逻辑。该模块不负责工作区初始化、参数解析或终端输入。

单次命令会把 `SUCCEEDED` 和 `ANSWERED` 都映射为进程退出码 0，同时通过不同状态保留二者的语义区别。交互式会话无论结果状态如何，都会记录结果并继续运行。

## 3. `ConversationStore` 与 `ConversationSession`

### 接口

`src/coding_agent/conversation.py` 暴露持久化边界：

```python
class ConversationStore:
    def create(self, workspace: Path) -> ConversationSession: ...
    def resume(self, reference: str, workspace: Path) -> ConversationSession: ...
    def list_sessions(self, *, workspace: Path | None, limit: int) -> tuple[SessionInfo, ...]: ...

class ConversationSession:
    def prepare(self, request: str) -> PreparedConversation: ...
    def record(self, request: str, result: RunResult) -> None: ...
    def history(self, *, limit: int = 20) -> ConversationHistory: ...
    def resumable_sessions(self, *, limit: int = 20) -> tuple[SessionInfo, ...]: ...
    def switch(self, reference: str) -> ConversationSession: ...
    def discard_if_empty(self) -> bool: ...
```

CLI 决定会话存储位置，并提供脱敏器和上下文限制。调用方无需解析日志、管理修订号、选择压缩检查点或自行构造模型上下文。

### 持久化格式与重放

每个会话使用随机的小写 32 位十六进制 ID，并在用户级会话目录下使用一个只追加的 `session.jsonl`。第一条记录绑定模式版本、ID、规范工作区和创建时间。后续的 `turn` 和 `compaction` 记录携带连续修订号。重放过程会校验记录类型、字段大小、UTF-8、轮次顺序、修订顺序、状态值和整体文件大小限制，然后才返回状态。

写入时会获取操作系统文件锁，追加一条有边界的 JSON 记录，刷新缓冲区并调用 `fsync`。配置的 API 密钥和形似凭据的值会在持久化前脱敏。每一轮只保存用户请求、助手最终结果、终止状态、运行 ID 和变更路径名称，绝不会序列化验证记录、审批决策、工具观察、控制器预算或实时引擎状态。

恢复操作接受完整 ID，或长度为 2–32 个字符的十六进制前缀。前缀解析在 `ConversationStore` 内完成，只考虑绑定到调用方规范工作区的会话，并且只有唯一匹配时才成功。零匹配和多匹配是不同错误；发生歧义时，会报告不会冲突的候选引用。完整 ID 仍然兼容。因此，有效引用无法把历史文本移动到另一个目标仓库。已完成的轮次可以恢复；尚未产生 `RunResult` 就被中断的运行不会设置检查点。

除计数和工作区之外，`SessionInfo` 还包含不会冲突的显示引用、最近一个已完成轮次的时间戳，以及最近一次经过脱敏的用户请求。展示适配器会把 UTC 时间戳转换为本地时间，并把请求限制为单行；调用方无需解析 JSONL 来发现会话。

零轮次会话不会出现在发现结果中。`discard_if_empty` 会在会话锁保护下重放日志，并且只在没有任何已完成轮次时将其移除；TUI 会在退出和成功切换后调用它。这样可以清理仅仅打开又立即退出 TUI 所产生的孤立会话，同时不危及已完成历史。

### 自动上下文压缩

`prepare` 会在硬字符预算内派生提示词视图。它首先保留近期原始轮次。如果渲染结果超过目标大小，就会把最早且符合条件的轮次折叠为紧凑的结构化摘要，其中包括长度受限的请求和结果片段、状态、运行 ID 和变更路径。默认至少保留最近两个原始轮次。检查点会被持久化，因此另一个进程可以重建相同的压缩视图，而无需重复工作。

这是确定性的控制器压缩，而不是递归调用模型生成摘要。提示词会给压缩记忆和近期轮次统一加上警告，说明恢复文本只是不可信上下文，不能授予权限、批准操作、提供验证证据或获取仓库权威性。当前请求和历史会在模型调用前被共同限制在硬边界内。

## 4. `InteractiveSession`

### 接口

`src/coding_agent/interactive.py` 暴露：

```python
class InteractiveSession:
    def run(self, run_task: Callable[[str], RunResult]) -> int:
        ...
```

构造函数接收一个 `ConversationSession`、模型标签、Thinking 模式标签，以及可选的输入/输出流。测试使用临时 `ConversationStore`、`StringIO` 和伪造的 `run_task`；生产环境使用持久化会话和 `LocalAgentRunner.run`。

### 循环

1. 渲染紧凑的欢迎横幅和帮助提示。
2. 从 `coding-agent> ` 读取一行。
3. 忽略空行；在本地分派以 `/` 开头的命令。
4. 请求 `ConversationSession.prepare` 生成有边界的目标。
5. 调用一次 `run_task`。
6. 通过 `ConversationSession.record` 持久化已完成的 `RunResult`，并输出状态/运行 ID。
7. 持续执行，直到输入 `/exit`、`/quit` 或文件结束符。

`/history` 会读取持久化的近期轮次，并报告压缩记忆中还有多少更早的轮次。`/session` 会输出短引用和完整 ID。`/resume` 使用终端的可复用高亮选择界面，展示同工作区内的 `SessionInfo`；`/resume <prefix>` 则跳过选择器。切换时只需替换 `ConversationSession`，因为所有候选会话都绑定到已经构造好的运行器工作区。欢迎横幅会区分新会话和恢复会话，在模型旁显示配置的 Thinking 模式，并在退出时输出使用短引用恢复的命令。

只有当输出连接到 TTY 且未设置 `NO_COLOR` 时，终端才使用 ANSI 样式。纯文本是完整的回退方案，从而保证 Windows 和重定向测试的确定性。`/clear` 也只会在样式模式下输出 ANSI 清屏代码。

`TerminalTheme` 是欢迎面板、提示符、菜单、进度层级、终止状态和审批卡片共同使用的展示接缝。它不包含控制器状态，并且只接收已经过长度限制的显示字符串。交互式会话、控制台事件接收器、提示符选择器和审批适配器保留原有行为接口，只把样式和布局交给该模块。未知动作名称会被确定性地转换为易读文本；结构化 JSONL 日志仍会保留准确的协议名称。

`TerminalPrompt.readline(prompt) -> str` 通过单一接口封装跨平台按键解码和斜杠命令选择器。Windows 使用 `msvcrt.getwch`，并同时识别 Windows 扩展键和 ConPTY 转义序列。POSIX 会暂时切换到原始终端模式，并解码常见 ANSI 导航序列。实现会在返回前恢复终端模式，因此命令审批仍然可以使用正常的行输入。重定向的流会绕过按键处理，直接调用 `readline`。

提示符维护一个支持 Unicode 的可编辑缓冲区，提供左/右、Home/End、Backspace 和 Delete 操作。输入开头的 `/` 会立即打开静态命令目录。菜单会原地重绘固定高度的候选区域，并用彩色整行高亮标识当前命令。禁用样式时，仍会使用可见的 `>` 标记保留选择状态。Enter 会关闭菜单，并把所选命令复制到现有可编辑缓冲区；只有再次按 Enter 时，该行才会返回给 `InteractiveSession`。选择和前缀筛选都封装在模块内部。追加输入时使用终端原生的增量回显，而不是重绘整个缓冲区，因此较长、会换行的中文请求或粘贴内容可以在线性时间内处理，也不会产生重复提示符片段。移动光标后的编辑仍会显式重绘。测试在同一个 `readline` 接口注入语义化按键事件。

在提示符读取期间触发 `KeyboardInterrupt` 会输出取消提示，然后返回提示符。文件结束符会正常退出。如果 `KeyboardInterrupt` 从一次运行内部传播出来，则会被报告为任务中断，并且不会伪造成功的 `RunResult`。

## 进度投影与审批

`src/coding_agent/presentation.py` 负责为每个工具生成有边界的说明。根据具体工具，它会包含路径、行号范围、条目数/匹配数、可执行文件、当前目录、用途、参数数量、内联代码长度、退出码和修改状态。它绝不会包含文件内容、脚本正文、搜索词或完整输出。控制字符和格式字符会在进入控制台之前被转义。

`AgentEngine` 只把这些有边界的说明加入 `model_action` 和 `tool_finished` 事件。`ConsoleEventSink` 负责渲染，并且不会为空的推理说明添加多余标点；JSONL 和 JSON 控制台事件结构只做增量扩展，并保持脱敏。样式化输出会把每个模型决策与缩进的工具结果配对，并把 `read_file` 等协议名称映射为 `Read` 等简短显示标签；重定向输出则保留稳定的方括号纯文本形式。对于面向用户的控制台运行，`LocalAgentRunner` 会用一个仅用于展示的适配器包装配置的 `ModelPort`。该适配器会在调用 `complete(...)` 前要求 `ConsoleEventSink` 立即显示一条已刷新的临时 `Working…`，并在 `finally` 代码块中清除它。包装器不会改变请求、响应、控制器状态或结构化事件协议。纯文本控制台和 JSON 模式会绕过这一视觉指示。

工具计时会区分控制器/审批延迟和实际执行时间。控制台输出 `execution_ms`，并在不为零时输出 `approval_wait_ms`；总 `duration_ms` 仍然保存在结构化事件中。成功的 `read_file` 观察还会形成按工作区版本隔离的控制器缓存：已覆盖范围会以 `CACHED` 返回，无需再次访问文件系统。连续两次命中缓存后，系统会在下一次模型决策中暂时移除 `read_file`，从而打破完全相同或交替重叠范围的读取循环。在这一次恢复决策之外，不相交和其他未覆盖读取仍然正常可用。对于其他只读工具，引擎会跳过连续第二次相同动作，并在第三次相同动作时以 `STAGNATION` 终止。上下文构造也会移除同一工作区版本中较旧且字节完全相同的观察。协议错误事件会通过同一脱敏和控制字符安全的展示路径，携带有边界的具体校验原因。

引擎还会投影两种预算控制转换。跨工具连续执行八次只读动作后会显示 `[FOCUS]`，并暂停所有检查工具，直到模型选择工作、验证、回答或终止动作。工作区发生修改且只剩四个工作轮次时会显示 `[WRAP-UP]`；它会在剩余运行期间移除源代码读取和修改工具，只保留验证、验证输出读取、`finish` 和 `report_blocked`。即使模型适配器直接返回被隐藏的动作，该动作也会在执行前被拒绝。

模型协议新增了 `respond(message) -> AnswerRequest`。只有在 `workspace_version == 0` 且没有记录变更路径时，`AgentEngine` 才会接受它并以 `ANSWERED` 终止。工作区修改之后，系统会记录 `answer_rejected` 观察、回到 `RUNNING`，并要求使用最新证据调用 `finish` 或调用 `report_blocked`。编程评测仍然只有在 `SUCCEEDED` 且独立判定器成功时才能通过。

模型轮次预算统计正常工作决策。如果最后一个工作决策产生了最新完成证据，`AgentEngine` 会输出 `[FINALIZE]`，并额外提供一次只包含 `finish` 和 `report_blocked` 的决策。这样可以避免成功的最终验证因为没有普通轮次可供引用，而被错误转化为 `BUDGET_EXHAUSTED`。当证据过期或缺失时，控制器不会提供宽限决策，也不会延长总时间预算；任何试图执行工作动作的行为都会被防御性拒绝且不予执行。

审批前，`LocalToolRuntime` 会根据原始参数计算操作摘要，然后向 `PromptApprovalAdapter` 提供有边界的工具说明和单独脱敏的参数对象。适配器默认只展示摘要。输入 `d` 会显示经过视觉换行的完整脱敏 JSON 和完整摘要，输入 `y` 表示批准，空输入或 `n` 表示拒绝。精确操作由摘要标识，而不是由显示时的换行方式标识。

已有文件的编辑、整文件替换和删除都有恢复支持。在校验预期哈希之后、执行修改之前，`LocalToolRuntime` 会把准确的 UTF-8 修改前快照保存到当前运行的制品存储中。如果脱敏会改变内容，或者制品大小上限会截断内容，运行时就会拒绝修改。工具事件和 `inspect-run` 会暴露不透明的恢复 ID；`recover-file` 使用排他创建把快照复制到调用方选择的新路径，绝不会覆盖文件。Git 差异投影会为未跟踪的 UTF-8 文件合成统一格式补丁；控制器只有在获取完整且非空的最终差异制品后，才会接受 `finish`。

可视化 Web 目标使用专门的验证路径。`browser_check` 接受一个工作区相对 HTML 文件，以及有边界的视口和等待参数；然后由控制器解析出的 Edge、Chrome 或 Chromium 可执行文件使用一次性配置目录完成渲染。渲染器会阻止普通主机名解析、禁用浏览器后台服务、限制时间和 PNG 大小，并校验 PNG 尺寸。脱敏 DOM 会进入文本制品存储；截图保存在受限二进制目录中，模型只能得到它的不透明 ID。引擎会记录一条 `browser` 验证；如果当前可视化目标修改了 Web 文件，则要求模型引用这条记录。`export-screenshot` 可以把 PNG 复制到调用方选择的新路径，且不会覆盖任何文件。该过程验证的是渲染，而不是主观视觉质量；明确审批后，浏览器 JavaScript 仍然以用户的操作系统账户运行。

## CLI 集成

`build_parser` 为 `tui` 暴露与 `run` 相同的工作区、模型、预算、审批和程序允许列表选项，但不包含 `task` 和 `--json`。它还暴露用于交互的 `resume REFERENCE`，以及用于发现会话的 `sessions`。

`main` 会把空参数列表规范化为 `tui`，因此：

```text
coding-agent              -> interactive session in Path.cwd()
coding-agent tui ...      -> explicit interactive session
coding-agent resume ID    -> resume in Path.cwd()
coding-agent sessions     -> list saved sessions without model credentials
coding-agent run ...      -> one bounded run
```

配置校验发生在 `prepare_workspace` 之前；缺少模型、端点无效、预算无效或缺少 API 密钥都不能修改目标。对于新 TUI，工作区初始化先于会话创建。对于恢复操作，系统会先校验会话 ID 与工作区绑定，然后把已有 Git 工作区作为无操作进行准备。`sessions` 不需要模型或 API 密钥。`inspect-run` 和 `eval` 保持不变。

会话存储优先级依次为：用户配置中的 `sessions_dir`、`CODING_AGENT_SESSIONS_DIR`，然后是 `~/.coding-agent/sessions`。TUI 和恢复命令接受 `--session-context-chars`；同名配置字段的有效范围是 2,000 到 18,000 个字符。

软件包仍然可以安装为控制台脚本。对于希望从任意仓库直接使用 `coding-agent` 的用户，文档建议一次性执行 `uv tool install --editable <project>`。

## 安全分析

- Git 初始化写入是由用户请求的启动行为，位于模型动作循环之外。
- 模型永远不会获得 Git 初始化或提交工具。
- 已有仓库不会被自动提交或清理。
- 系统会拒绝外层仓库中的子目录，避免扩大工作区或形成含义不清的嵌套。
- Git 子进程无法获得 `OPENAI_API_KEY` 或其他任意继承的环境变量。
- 在 `git add --all` 之前，形似凭据的文件会被忽略。
- 提交身份继承自 Git 配置，绝不伪造。
- 初始化消息和失败信息只包含有边界的 Git 输出，不包含环境变量转储。
- 初始审批提示绝不会输出文件或脚本内容；展开后的详情已经脱敏，并且仍然绑定到原始操作摘要。
- 控制台进度会转义模型参数中提供的控制字符，并限制每个自由文本字段的长度。
- 目标和持久化事件字符串会在共同边界上替换未配对的 Unicode 代理项，防止格式错误的重定向输入导致 UTF-8 日志崩溃，或未经处理地进入模型协议。
- 持久化会话 ID 是不可作为路径使用的 32 位十六进制值。用户引用必须是校验过的十六进制前缀；前缀解析要求唯一匹配并受工作区绑定；重放会拒绝格式错误、体积过大、顺序错误或不受支持的记录。
- 会话文本会在追加之前脱敏；恢复的轮次会被明确标记为不可信，并且不包含审批、验证证据或控制器状态。
- 每个会话的文件锁可防止记录被重叠写入。压缩是一项有边界、确定性的控制器转换，而不是可执行内容或额外的特权模型动作。
- 非修改回答不能授权工具、免除审批或满足编程评测；工作区修改后的 `respond` 会被拒绝。

## 测试

### 工作区测试

- 非 Git 目录会变为只有一个提交且状态干净的仓库；
- 已有源文件会被跟踪，而 `.env`、密钥文件、缓存和构建输出保持未跟踪/被忽略；
- 已有 `.gitignore` 内容会被保留，并追加缺失规则；
- 已有仓库保持不变，初始化具有幂等性；
- 外层仓库的子目录会被拒绝；
- 无效的 `.git` 项和 Git 命令失败会转化为初始化错误。

这些测试在模块接口处使用真实的临时 Git 仓库。Git 是可以在本地替换的依赖，因此不会仅为了测试而增加一个仿生产形态的子进程端口。

### 交互式测试

- 空参数规范化会选择 `tui`；
- 一行自然语言只会调用运行器一次；
- 多个请求会产生相互独立的持久化历史记录和有边界的上下文；
- 第二个 `InteractiveSession` 可以恢复该 ID，并获得先前请求和助手结果；
- 唯一短引用可以成功解析，有歧义的前缀会被拒绝，会话摘要会暴露最近请求和时间戳；
- `/resume` 可以选择同工作区会话并切换上下文，而不会调用智能体运行器；
- 旧轮次会自动压缩，检查点能够在重放后保留，当前请求与上下文总和绝不会超过目标的硬边界；
- 错误工作区和形似路径穿越的会话 ID 会被拒绝；
- 密钥和验证 ID 不会进入会话 JSONL；
- 斜杠命令不会调用运行器；
- `/` 会打开命令目录；方向键能够明显高亮候选项，第一次按 Enter 只补全而不提交；筛选、Backspace、Escape、Unicode 输入和重定向行模式都具有确定性行为；
- 较长的 Unicode 输入只会返回和回显一次，不会因为每个字符都重绘整行；
- `/history`、文件结束符、`/exit` 和 `KeyboardInterrupt` 的行为具有确定性；
- 纯文本输出包含工作区、结果状态和运行 ID。

### 展示与审批测试

- 每个文件工具都会标明路径，但不包含文件内容；
- 较长的内联命令只会在初始进度/审批摘要中报告程序、元数据和代码长度；
- 工具结果会报告条目数、变更路径或退出码；
- 可视化 Web 完成会拒绝只有语法检查的证据，而成功的本地浏览器渲染会记录 DOM 和截图证据；
- 输入 `d` 会在再次询问之前显示自动换行的脱敏参数和完整摘要；
- 空审批输入表示拒绝，简洁提示始终保持有界。

### 回归与冒烟检查

- 运行已有的 CLI、引擎、工具、集成和评测单元测试；
- 运行单次 `run --help`、显式 `tui --help`、`resume --help`、会话列表和直接启动 TUI 后退出的冒烟测试；
- 用两个进程进行恢复冒烟测试，确认模型可见的对话连续性，同时每一轮仍创建独立运行；
- 在临时非 Git 目录中进行自动初始化冒烟测试；
- 使用实际安装的 Edge/Chrome/Chromium 进行冒烟渲染，在模拟之外校验 PNG 尺寸和渲染 DOM；
- 执行 `git diff --check` 和最终仓库状态检查。

由于 `respond` 和 `ANSWERED` 扩展了控制器协议，工具模式版本也会递增。完成配置后应运行真实评测套件，并继续要求智能体状态为 `SUCCEEDED`、判定器退出码为 `0` 且虚假成功数为零。还应运行一次真实的对话冒烟检查，要求结果为 `ANSWERED` 且没有文件变更。
