# Feishu Bridge for Codex

把飞书消息转发到 Codex，并把 Codex 回复转发回飞书。项目默认走 Codex 官方
`app-server` 协议，不依赖自建共享 websocket app-server。

## 功能

| 能力 | 说明 |
|---|---|
| `appserver` 模式 | 绑定已有 Codex thread，通过 `thread/resume + turn/start` 继续对话。 |
| `direct` 模式 | 绑定已有 Codex thread，并通过 Codex app-server 发送飞书消息。 |
| `simulate` 模式 | 通过 HAINDY 操作 Codex Desktop UI，适合必须操作当前可见窗口的场景。 |
| 飞书命令 | 默认前缀为 `$`，支持 `$sessions`、`$attach`、`$mode`、`$status`、`$detach`。 |
| 配置隔离 | 公开配置和私密 key 分开，避免误提交飞书凭据、chat id 和本机路径。 |

## 安装

```powershell
git clone https://github.com/pixel7868/feishu_bridge.git
cd feishu_bridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Codex CLI 需要已安装并能在命令行执行：

```powershell
codex --version
```

## 配置

复制模板：

```powershell
Copy-Item .\feishu_bridge\local_settings.example.json .\feishu_bridge\local_settings.json
Copy-Item .\feishu_bridge\local_secrets.example.json .\feishu_bridge\local_secrets.json
```

`local_settings.json` 放非敏感运行配置，可以提交模板；`local_secrets.json`
放飞书 app 凭据、默认 chat/thread、本机 workspace 路径，必须只留在本地。

`local_secrets.json` 示例：

```json
{
  "app_id": "cli_xxx",
  "app_secret": "xxx",
  "verification_token": "",
  "encrypt_key": "",
  "default_chat_id": "oc_xxx",
  "default_thread_id": "",
  "direct_cwd": "D:\\path\\to\\workspace",
  "appserver_cwd": "D:\\path\\to\\workspace",
  "haindy_cwd": "D:\\path\\to\\haindy",
  "haindy_session_id": ""
}
```

也可以用环境变量指定 secrets 文件：

```powershell
$env:FEISHU_BRIDGE_SECRETS = "D:\private\feishu_bridge_secrets.json"
```

## 启动

默认使用 `local_settings.json` 里的 `receive_mode`：

```powershell
python -m feishu_bridge.main
```

强制 webhook 模式：

```powershell
python -m feishu_bridge.main --mode webhook
```

webhook 默认地址：

```text
http://127.0.0.1:9000/webhook/feishu
```

## 飞书命令

| 命令 | 说明 |
|---|---|
| `$sessions` | 列出最近 Codex threads，并保存编号快照。 |
| `$attach <编号或线程ID>` | 绑定当前飞书 chat 到 Codex thread。 |
| `$mode` | 查看当前发送模式。 |
| `$mode appserver` | 切到 appserver 模式，并自动绑定最近 Codex thread。 |
| `$mode direct` | 切到 direct 模式，并自动绑定最近 Codex thread。 |
| `$mode simulate` | 切到 simulate 模式。 |
| `$status` | 查看当前绑定。 |
| `$detach` | 解除绑定。 |

## Codex 同步机制

VS Code Codex 和 Codex Desktop 会各自启动自己的 `codex.exe app-server`
进程，并共享 Codex 本地 session 存储。bridge 的稳定路径是每次通过
`codex app-server --listen stdio://` 恢复绑定 thread 并写入同一套
`~/.codex/sessions` 数据。

不要设置 `CODEX_APP_SERVER_WS_URL`，也不要把
`appserver_websocket_url` 指向自建 `ws://127.0.0.1:17920` 服务。这条旧路径
容易导致 Codex Desktop 登录失败或 thread resume path 不一致。

## 安全边界

| 不应提交 | 原因 |
|---|---|
| `feishu_bridge/local_secrets.json` | 包含飞书 app id、app secret、chat id、本机路径。 |
| `feishu_bridge/runtime/` | 包含运行日志、消息 id、access key、thread 绑定和截图。 |
| `*.log` / `*.png` / `*.sock` | 可能包含 token、路径、截图或运行态数据。 |

仓库 `.gitignore` 已默认忽略这些文件。发布前可运行：

```powershell
python -m pytest -q
```

生成干净导出目录：

```powershell
.\scripts\export_public.ps1
```

