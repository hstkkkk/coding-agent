项目名称：Bounded Coding Agent
Git 仓库：https://github.com/hstkkkk/coding-agent

一、运行方式
环境要求：Python 3.11+、Git 和支持原生工具调用的 OpenAI-compatible 模型接口。

在本项目根目录安装：
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .

在 C:\Users\<用户>\.coding-agent\settings.json 配置 api_key、model 和 base_url（字段见 docs/zh-CN/configuration.md）。进入目标项目目录运行：
coding-agent

该命令会打开交互终端；非 Git 目录会自动生成保护性 .gitignore、初始化仓库并创建初始提交。单次运行方式：
coding-agent run --workspace <目标目录> --allow-program python "<编程任务>"

离线测试：python -m unittest discover -s tests -v

二、特色功能
1. 未使用任何 Agent 框架或 SDK；智能体循环、工具协议、上下文与评测均自行实现，运行时仅使用 Python 标准库。
2. 模型每轮仅提出一个结构化动作；控制器负责校验、审批、执行、重试、预算和终止。多个工具调用只处理第一个。
3. 文件编辑采用哈希前置条件、原子写入和修改前快照；命令以 shell=False 运行且不继承 API 密钥；输出经过限长、脱敏和审计记录。
4. 支持键盘命令选择、连续对话、短 ID 恢复会话和自动上下文压缩；恢复内容不会携带旧审批或验证状态。
5. 完成请求必须由控制器依据当前工作区的最新验证和完整 Git diff 接受；网页界面还需浏览器证据。独立评测器通过隐藏测试与 Oracle 统计虚假成功。

三、其它说明
本项目提供工作区级安全控制，不是操作系统沙箱。经批准的程序仍以当前账户权限运行；暂不支持运行中断点恢复、多智能体或大型仓库索引。

