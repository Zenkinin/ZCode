# ZCode

ZCode 是一个运行在本地终端的轻量 Coding Agent。它使用 DeepSeek 模型，自行实现 Agent 循环、上下文管理、工具调用、本地文件操作、命令执行、计划更新和终止控制，不依赖 Agent 框架或服务端代码执行工具。

## 主要功能

- 读取、搜索、创建和精确编辑工作区文件。
- 在本地 PowerShell 中运行命令并验证结果。
- 复杂任务自动建立 Plan，状态栏实时显示 cwd、Git 和进度。
- 支持会话目录切换、Diff、Undo、命令补全和无进展保护。
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
/plan  /diff  /undo  /exit    计划、差异、撤销与退出
!git status                   直接运行 PowerShell，不经过模型
```

输入 `/` 可用上下键选择匹配命令。`!` 命令是真实本地 Shell，不受 `/undo` 保护，也不是操作系统沙箱，请谨慎执行修改或删除命令。
