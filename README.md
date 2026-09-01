# ZCode

ZCode 是一个运行在本地终端的轻量 Coding Agent。它使用 DeepSeek 模型，自行实现 Agent 循环、上下文管理、工具调用、本地文件操作、命令执行、计划更新和终止控制，不依赖 Agent 框架或服务端代码执行工具。

## 主要功能

- 读取、搜索、创建和精确编辑工作区文件。
- 在本地 PowerShell 中运行命令并验证结果。
- 复杂任务自动建立 Plan，动态状态栏实时显示 cwd、Git 和进度。
- 支持会话目录切换、Diff、模型切换、命令补全和无进展保护。
- API Key 保存到 Windows Credential Manager，不写入项目。

## 安装

需要 Windows、Python 3.11+ 和 DeepSeek API Key。在 PowerShell 中执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

打开新终端，在需要操作的项目目录运行：

```powershell
zcode
```

首次启动会提示输入 API Key。更换或删除密钥：

```powershell
zcode --configure
zcode --clear-api-key
```

## 使用

```text
修复失败的测试并补充回归测试   自然语言 Agent 任务
/help                         ZCode 内置命令
/plan  /diff  /continue  /exit    计划、差异、暂停恢复与退出
/new alpha                    创建名为 alpha 的新会话
/sessions                     查看当前 workspace 的会话
/switch <id>                  切换到指定会话
/rename 修复登录问题          重命名当前会话
/clear                        清除终端显示但保留会话
/model                        查看当前模型和思考配置
/model deepseek-v4-flash      切换后续请求使用的模型
/model thinking high          设置思考模式：off、low、high 或 max
!git status                   直接运行 PowerShell，不经过模型
```

输入 `/` 可用上下键选择匹配命令。会话按 workspace 隔离，同一 workspace 可以有多个独立会话；启动时默认恢复最近活动会话。未命名会话会根据第一条任务在本地生成一次简短名称，也可用 `/rename` 修改；稳定的 `s-xxxxxxxx` ID 用于切换会话。`/model` 的切换只影响后续请求；DeepSeek 思考配置支持关闭或 `low/high/max`，不提供 `medium`。Agent 运行时按 Esc 可暂停，随后可输入纠正内容并用 `/continue` 继续；在 paused 输入框再按 Esc 会放弃暂停任务但保留文件修改。`!` 命令是真实本地 Shell，也不是操作系统沙箱，请谨慎执行修改或删除命令。
