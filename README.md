# ZCode

ZCode 是一个面向 Windows 终端的轻量本地 Coding Agent。它使用 DeepSeek 的 OpenAI-compatible API，自行实现 Agent Loop、上下文管理、工具协议、本地执行、结构化 Plan、错误恢复和终止控制，不依赖 LangChain、Agents SDK 或服务端 Shell/File 工具。

ZCode 提供一个清晰、可检查的编程闭环：

```text
理解任务 → 调查项目 → 创建 Plan → 修改文件 → 运行测试 → 根据结果修复 → 验证并总结
```

## 功能概览

- 在 workspace 内列出、读取、搜索、创建和精确编辑文件。
- 在本地 PowerShell 中运行命令、测试和 Git 检查。
- 为复杂任务创建结构化 Plan，并在执行过程中更新进度。
- 状态栏实时显示状态、模型、思考模式、会话、cwd、Git 和 Plan 进度。
- 支持多个持久会话；每个会话独立保存消息、Plan、cwd、错误和暂停状态。
- 支持 Esc 暂停、输入纠正内容后 `/continue` 恢复执行。
- 提交后的多行输入会渲染为稳定历史框，适配中英文宽度和 Windows 终端拉伸。
- 对失败输出生成错误 ID，默认显示摘要，需要时再展开完整内容。
- 支持运行时切换 DeepSeek 模型和思考配置。
- 对常见递归删除和危险 Git 命令执行风险检查。
- 将 DeepSeek API Key 保存到 Windows Credential Manager，不写入项目文件。

## 环境要求

- Windows 10/11
- Python 3.11 或更高版本
- 可用的 DeepSeek API Key
- 建议使用 Windows Terminal、PowerShell 或 CMD

## 安装与升级

在仓库根目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

安装脚本会在 `%LOCALAPPDATA%\ZCode` 创建独立虚拟环境，安装当前仓库并创建 `zcode.cmd`，同时将启动目录加入用户级 `PATH`。

首次安装后请打开一个新终端，在任意项目目录运行：

```powershell
zcode
```

也可以显式指定 workspace：

```powershell
zcode C:\Projects\example
```

重新运行 `install.ps1` 即可将本机安装更新到当前源码版本。

## API Key

首次启动时，ZCode 会提示输入 DeepSeek API Key，并保存到 Windows Credential Manager。

```powershell
zcode --configure       # 替换密钥
zcode --clear-api-key   # 删除密钥
```

也可以临时使用环境变量：

```powershell
$env:DEEPSEEK_API_KEY = "your-key"
zcode
```

## 基本使用

直接输入自然语言编程任务：

```text
请调查删除不存在任务时返回错误的问题，先补充回归测试，再实施最小修复并运行完整测试。不要创建 Git commit。
```

复杂任务通常会经历：

```text
只读调查 → 创建 Plan → 修改代码 → 运行测试 → 根据失败调整 → 完整验证 → 总结
```

输入 `/` 可以查看命令补全；使用上下方向键、Tab 和 Enter 选择候选。

## 命令参考

| 命令 | 说明 |
|---|---|
| `/help` | 显示固定列宽命令帮助 |
| `/plan` | 显示当前结构化 Plan |
| `/diff` | 显示当前任务由文件工具记录的修改 |
| `/model` | 显示当前模型与思考配置 |
| `/model <name>` | 切换后续模型请求使用的模型 |
| `/model thinking <off\|low\|high\|max>` | 切换思考模式或力度 |
| `/new [name]` | 创建全新会话 |
| `/sessions` | 列出当前 workspace 的会话 |
| `/switch [id]` | 选择或切换会话 |
| `/rename <name>` | 重命名当前会话 |
| `/clear` | 清除终端显示，不删除会话数据 |
| `/continue` | 根据暂停后的纠正继续原任务 |
| `/error [id]` | 展开最近或指定错误的完整内容 |
| `/errors` | 列出当前会话的错误记录 |
| `/safety` | 查看当前进程和当前 workspace 的 Shell 授权 |
| `/safety revoke <id>` | 撤销指定会话或永久授权，支持补全选择 |
| `/safety reset` | 清除当前 workspace 的全部永久授权 |
| `!<command>` | 不经过模型，直接运行 PowerShell |
| `/exit` | 保存当前会话并退出 |

### 直接 Shell

```text
!git status --short
!python -m pytest -q
```

简单的目录切换会持久更新当前会话 cwd：

```text
!cd docs
!cd ..
```

复合目录命令不会被接受，例如 `!cd docs; Get-ChildItem`。请拆成两次操作，避免临时 Shell cwd 与会话 cwd 不一致。

## 暂停、纠正与继续

Agent 思考或执行时按 Esc，会终止当前模型请求或工具执行并进入 `paused`：

```text
⏸ paused │ model: deepseek-v4-flash/high │ session: 修复搜索 │ cwd: .
```

暂停后，普通输入不会立即启动新任务，而是作为纠正内容记录：

```text
不要增加 description 字段，只搜索现有 content。
```

随后执行 `/continue`。ZCode 会保留原任务、当前 Plan、已有工具结果和磁盘修改，并让 Agent 根据纠正继续或调整 Plan。

如果不想继续，在 paused 输入框再按一次 Esc：

- 放弃暂停任务并恢复 `ready`。
- 清除当前 Plan 和暂停纠正。
- 保留已经写入磁盘的文件修改。
- 下一条普通输入会成为新任务。

暂停状态会随会话保存；退出或切换会话后仍可恢复。

## 会话

```text
/new 修复登录
/sessions
/switch <space>
```

输入 `/switch `（末尾有空格）后会出现候选列表，可使用名称、ID 或 cwd 过滤。

每个会话独立保存：

- 对话消息和工具结果
- Plan
- cwd
- 错误记录
- 暂停任务与纠正内容

不同会话共享同一个 workspace 文件系统。因此，新会话不会继承旧会话的对话记忆，但可以读取旧会话已经写入磁盘的代码。

## 模型与思考配置

```text
/model
/model deepseek-v4-flash
/model deepseek-chat
/model deepseek-reasoner
/model thinking off
/model thinking low
/model thinking high
/model thinking max
```

输入 `/model ` 或 `/model thinking ` 可用候选菜单选择，也可以手动输入其他 DeepSeek 模型名。模型是否可用取决于当前账号和接口。

当前接口不提供 `medium`。运行时切换只影响后续请求，不会清除会话、Plan 或文件状态。

## 错误记录

工具失败时默认显示摘要：

```text
✕ run_command · exit_code: 1 · error e-001
  AssertionError: expected 404, received 500
  Use /error e-001 for full output
```

```text
/errors
/error
/error e-001
```

错误记录按会话隔离并持久化。常见 `sk-...` 密钥格式会在记录前遮盖。

## 状态栏

```text
◐ thinking │ model: deepseek-v4-flash/high │ session: 修复搜索 │ cwd: src │ git: main* │ plan 2/5
```

- `thinking`：当前 Agent 状态。
- `model`：模型和思考配置，`off` 表示关闭思考。
- `session`：当前会话名称。
- `cwd`：会话持久工作目录，相对于 workspace。
- `git`：分支；`*` 表示存在未提交修改。
- `plan`：已完成步骤数与总步骤数。

## Workspace 与安全边界

文件工具只能访问启动时指定的 workspace，并拒绝访问 workspace 外路径和 `.git`、`.venv`、`.zcode`。

Shell 工具不是操作系统沙箱。Agent 发起的 Shell 命令与用户通过 `!` 直接运行的命令使用相同风险检查。递归或强制删除、`git reset --hard`、`git clean -f`、`git restore .`、强制删分支、rebase 和强制推送等操作会暂停执行并要求确认：

```text
[Y] once   [A] session   [P] permanent   [N] no
```

- `Y`：只允许当前命令。
- `A`：本次 ZCode 进程内允许相同风险与目标范围，退出后清除。
- `P`：永久允许当前 workspace 内相同风险与目标范围。
- `N` 或 Esc：拒绝当前命令，Agent 可以根据拒绝结果调整方案。

授权范围会绑定解析后的删除目标路径；删除 `build` 的许可不会放行删除 `src`。Git 风险绑定当前仓库目录。无法可靠解析目标、包含通配符、指向 workspace 外部或涉及 `.git`、`.zcode`、`.venv`、workspace 根目录时，不提供永久授权。永久授权保存在用户级 ZCode 配置，不会写入项目。使用 `/safety` 可查看带 ID 的授权表，输入 `/safety revoke ` 可补全选择并撤销会话或永久授权，`/safety reset` 清除当前 workspace 的全部永久授权。

如果永久授权写入失败，ZCode 会明确提示并仅放行当前命令。用户拒绝某条 Agent 命令后，同一任务再次请求完全相同的命令会被直接拒绝，不再重复弹窗。

仍需注意：

- Shell 命令本身可以访问 workspace 外部路径。
- Shell 或外部程序造成的修改不一定会出现在 `/diff` 中。
- `/diff` 只跟踪 ZCode 文件工具在当前任务中记录的修改。
- 高风险操作前应检查命令、cwd 和实际目标路径。

## 配置变量

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | Credential Manager | 临时覆盖已保存 API Key |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek-compatible API 地址 |
| `ZCODE_MODEL` | `deepseek-v4-flash` | 启动默认模型 |
| `ZCODE_THINKING` | `enabled` | `enabled` 或 `disabled` |
| `ZCODE_REASONING` | `high` | `low`、`high` 或 `max` |
| `ZCODE_COMMAND_TIMEOUT` | `120` | Shell 默认超时秒数 |
| `ZCODE_MAX_TOOL_OUTPUT` | `20000` | 单次工具输出字符上限 |
| `ZCODE_MAX_CONTEXT_CHARS` | `180000` | 上下文字符预算 |
| `ZCODE_NO_PROGRESS_LIMIT` | `3` | 重复无进展动作阈值 |
| `ZCODE_EMERGENCY_MAX_STEPS` | `100` | 内部应急循环上限 |

命令行参数：

```text
zcode [workspace] [--model MODEL] [--no-thinking]
zcode --configure
zcode --clear-api-key
```

## 项目结构

```text
zcode/
├── cli.py                 终端输入、命令路由、补全和暂停控制
├── config.py              配置与环境变量
├── credentials.py         Windows Credential Manager
├── core/
│   ├── controller.py      Agent Loop、暂停恢复和终止控制
│   ├── context.py         上下文裁剪与工具协议分组
│   ├── plan.py            结构化 Plan
│   ├── session.py         JSONL 会话持久化
│   └── types.py           内部消息与工具类型
├── llm/deepseek.py        DeepSeek OpenAI-compatible 适配器
├── tools/                 文件、搜索、Shell、目录和 Plan 工具
├── ui/renderer.py         Rich 输出、状态栏、Diff 和错误摘要
└── workspace.py           路径边界、修改快照和 Diff
```

## 本地开发

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q zcode
git diff --check
```

## 当前限制

- 当前主要支持 Windows 和 PowerShell。
- 仅实现 DeepSeek Provider，不包含自动模型路由。
- 上下文预算使用字符数近似，不依赖 tokenizer。
- 不提供完整 checkpoint、rewind 或跨任务文件回滚。
- `/diff` 不是完整 Git Diff，Shell 修改可能不在跟踪范围内。
- Shell 风险识别是防护层，不等同于 OS 级沙箱。

## License

本仓库当前未声明开源许可证。除非后续添加 LICENSE 文件，否则请勿假设拥有复制、分发或再授权权限。
