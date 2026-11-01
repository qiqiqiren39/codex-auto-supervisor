# Codex Auto Supervisor（Codex 自动监工）

[English](README.md) | [简体中文](README.zh-CN.md)

一个不改 Codex 配置的 Codex 自动监工工具和 skill。它监听你明确指定的目标线程，在完成、失败、请求审批或提问时，唤醒另一个协调线程。

## 从 Release 安装（推荐）

最新版本下载页：

https://github.com/qiqiqiren39/codex-auto-supervisor/releases/latest

1. 打开上面的 Releases 页面，下载最新 ZIP，例如 codex-auto-supervisor-v1.0.0.zip。
2. 解压 ZIP。你会得到一个完整的 codex-auto-supervisor 文件夹。
3. 把整个 codex-auto-supervisor 文件夹复制到 Codex 的 skills 目录：

       %USERPROFILE%\.codex\skills\

   如果设置了 CODEX_HOME，则复制到：

       %CODEX_HOME%\skills\

4. 最终路径必须类似：

       %USERPROFILE%\.codex\skills\codex-auto-supervisor\SKILL.md

   不要只复制 SKILL.md 或 scripts 里面的单个文件，必须复制整个文件夹。

5. 重启 Codex 桌面端，或重新加载 skills。
6. 在会话里明确要求监听，例如：

       用 $codex-auto-supervisor 监听目标线程，完成后唤醒协调线程。

   agent 会确认 target 和 coordinator 线程，登记监听并启动后台进程。如果你已经有线程 UUID，也可以直接告诉它。
7. 监听期间，可以先让 agent 查看状态：

       threadwatch status

   如果 threadwatch 不在 PATH，使用完整路径：

       py -3 <skill目录>\scripts\threadwatch.py status

不需要管理员权限，不需要 pip install，不需要修改 config.toml，也不会安装登录启动项。

## 环境要求

- Windows 10 或更高版本
- Python 3.11 或更高版本
- 已安装并登录 Codex 桌面端，或有可用的 Codex CLI app-server

检查 Python：

    py -3 --version

## 使用方式

推荐让 Codex 帮你创建或选择一个协调会话，再登记目标会话。典型请求：

    用 $codex-auto-supervisor 监听这个目标线程。它完成、失败、提问或请求审批时，唤醒协调线程。

agent 会执行等价于下面的操作：

    threadwatch add --target <目标线程UUID> --wake <协调线程UUID> --project <项目绝对路径>
    threadwatch start
    threadwatch status

默认从登记时的日志末尾开始监听。若目标已经结束，需要补发最近一次终态，可以加 --replay-last。

目标线程正常完成、失败、HTTP 429、请求审批或异步提问时，watcher 会通过本机 codex queue 向协调线程投递事件。协调线程收到以 [threadwatch] 开头的消息后，会先检查目标状态，再决定下一步。

## 停止和卸载

停止后台监听，但保留登记：

    threadwatch stop

删除一个目标的登记：

    threadwatch remove --target <目标线程UUID>

查看诊断和日志：

    threadwatch doctor
    threadwatch logs --follow

完全卸载：

1. 停止监听并删除目标。
2. 删除 skills 目录下的 codex-auto-supervisor 文件夹。
3. 如需删除本地状态，运行：

       powershell.exe -NoProfile -ExecutionPolicy Bypass -File <skill目录>\scripts\uninstall.ps1 -PurgeState

状态目录位于 %LOCALAPPDATA%\CodexAutoSupervisor。它只保存目标 UUID、日志偏移、事件键和运行日志。

## 常见问题

### Codex 里看不到 skill

确认最终位置是 ...\skills\codex-auto-supervisor\SKILL.md，而不是 ZIP 解压后的外层目录里还有一层多余文件夹。然后重启 Codex。

### 提示找不到 py 或 Python

安装 Python 3.11 或更高版本，确保 py -3 --version 能运行。若不能，让 agent 使用你已配置的 Python 绝对路径调用 scripts\threadwatch.py。

### threadwatch 不在 PATH

不需要把 CLI 加入 PATH。使用：

    py -3 <skill目录>\scripts\threadwatch.py <命令>

如需安装可选择启动器：

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File <skill目录>\scripts\install.ps1

### 监听没有收到事件

运行 threadwatch status --json 和 threadwatch doctor，检查目标线程是否已登记、日志文件是否找到、daemon 是否运行。目标必须真的产生新的 task_complete、问题或审批事件；登记前的历史事件默认不会重放。

## 为什么需要它

现有工具通常只覆盖闭环中的一环：查看线程状态、播放完成提示音、在同一个线程里恢复额度，或者监听任意 shell 命令。Codex Auto Supervisor 聚焦两个 Codex 线程之间的交接边界：

- 只读取追加的会话事件，不修改 Codex 配置、不安装 hook、不注册插件。
- 监听目标必须显式登记，其他线程和项目不会被误处理。
- 完成、失败、HTTP 429、审批请求和异步提问都可以转换为协调事件。
- 事件通过本机已有的 codex queue 唤醒独立协调线程，让协调方决定下一步，而不是只提醒人。
- 空闲检查只读本地文件元数据，不调用模型，不消耗 API token。
- 同一份包既是 Codex skill，也是独立 CLI；手动启停，不创建登录启动项。
- 持久化字节偏移和事件键，守护进程重启后从断点继续，不重复回放同一个事件。

## 开发者安装

源码开发时可以把仓库克隆到 skills 目录：

    git clone https://github.com/qiqiqiren39/codex-auto-supervisor.git
    py -3 -m unittest discover -s tests -v

源码仓库包含测试和构建脚本；Release ZIP 只包含使用方需要的运行文件。
