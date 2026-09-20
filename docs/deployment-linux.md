# rookie-agent Linux 部署与运行手册

> 适用版本：v0.1.0 · 无容器化，直接在 Linux 物理机/云主机上运行
> 本文以 **N100 物理机 / Ubuntu 26.04 LTS / 192.168.5.55 / 用户 n100** 为实证环境，
> 但所有步骤对同架构（x86_64 / aarch64）的 apt 系发行版通用。

---

## 0. 一分钟速览

```bash
# 1) 部署（幂等，可反复跑）
cd /opt/rookie-agent
./deploy/install.sh

# 2) 填密钥（至少 3 项）
nano .env

# 3) 装配自检（不需要任何密钥）
.venv/bin/python main.py --health-check --dry-run

# 4) 全量验收（含优雅停机与孤儿进程检查）
./deploy/verify.sh

# 5) 交给 systemd
sudo systemctl enable --now rookie-agent
journalctl -u rookie-agent -f
```

---

## 1. 目标环境

| 项目 | 值 | 说明 |
| --- | --- | --- |
| 主机 | N100 物理机（x86_64） | 4 核 / 7 GB 内存 / 62 GB 可用磁盘 |
| 系统 | Ubuntu 26.04 LTS | apt 系；systemd 托管 |
| IP | `192.168.5.55` | 局域网 |
| 运行账号 | `n100` | 有 `sudo` NOPASSWD，**不要用 root 跑 agent** |
| 系统 Python | 3.14.4 | 项目要求 ≥ 3.10，实测 3.14 全部依赖有 wheel |
| 部署目录 | `/opt/rookie-agent` | 归 `n100:n100` |
| venv | `/opt/rookie-agent/.venv` | 约 839 MB |

### 1.1 为什么不需要容器

- 没有多实例、没有服务发现、没有跨机依赖 —— 容器带来的编排收益为零；
- **反而有硬冲突**：Codex 的 Linux 沙箱本身要用 bubblewrap 起 user namespace，
  而容器默认的 seccomp/AppArmor 配置经常把 `CLONE_NEWUSER` 挡掉，
  于是"容器里的沙箱"会 fail-closed，agent 活着但任何工具调用都做不了。
- 单机单实例场景下，systemd 的 `Restart=` / `ReadWritePaths=` / `PrivateTmp=`
  已经提供了等价的隔离与自愈能力。

---

## 2. 部署前必须知道的三条硬约束

### 2.1 不能用清华 PyPI 镜像

`pypi.tuna.tsinghua.edu.cn` 上**不存在** `openai-codex`
（`/simple/openai-codex/` 返回 404），pip 会直接报：

```
ERROR: Could not find a version that satisfies the requirement openai-codex==0.154.0 (from versions: none)
```

这不是网络故障，换源才能解决。本项目默认使用**阿里云镜像**
（`https://mirrors.aliyun.com/pypi/simple`，已同步到 0.154.0），
且 `install.sh` 内置了「首选源失败 → 自动回退 pypi.org」的两级重试。

### 2.2 `openai-codex` 必须精确锁版本

它是 beta SDK，API 面随版本变化，且它**硬 pin 了同版本的 CLI 二进制**
（`openai-codex-cli-bin`，Linux 版 wheel 约 127 MB）。
跨版本升级会同时换掉 SDK 与 CLI，回归面太大。

`requirements.txt` 锁 `openai-codex==0.154.0`；
`install.sh` 每步都会跑 `deploy/probe_runtime_api.py` 验证 API 面，
版本不符会在部署阶段就失败，而不是运行期才炸。

### 2.3 `mcp` 必须 `<2`

mcp 2.x **删除了** `mcp.server.fastmcp`（只剩一个 `raise ModuleNotFoundError` 的桩），
官方给的迁移路径是把 `FastMCP` 改名成 `MCPServer`。
本项目 `mcp_servers/media_tools_server.py` 是**模块级 import**，
一旦装到 2.x，该 server 在启动瞬间就崩，且没有任何降级路径
（`McpHub` 会把它记为 unhealthy，`tool_count` 少一半）。

`requirements.txt` 已加 `mcp>=1.9.0,<2`。

---

## 3. 前置条件

- Ubuntu 26.04 或同代 apt 系发行版
- 一个**非 root** 的运行账号，且能 `sudo`（安装系统包与 systemd unit）
- 能访问 `mirrors.aliyun.com`（或 `pypi.org`）
- 出网访问 Telegram API（长轮询，**不需要开放任何入站端口**）

`install.sh` 会自动用 apt 安装以下系统包：

| 包 | 用途 | 缺失后果 |
| --- | --- | --- |
| `python3-venv` `python3-dev` `build-essential` | venv 与 C 扩展编译 | 依赖装不上 |
| `bubblewrap` | **Codex 的 Linux 沙箱依赖** | fail-closed，agent 能启动但**无法执行任何工具调用** |
| `ffmpeg` | TTS → OGG/Opus 转码 | 降级为发音频文件（功能不中断） |
| `procps` | `pgrep`/`pkill` | 验收脚本无法检查孤儿进程 |
| `git` `curl` `ca-certificates` `sqlite3` | 常规 |

> ⚠️ **包名 ≠ 二进制名**：apt 包叫 `bubblewrap`，它提供的二进制叫 **`bwrap`**。
> 检查安装时要用 `command -v bwrap`。`install.sh` / `verify.sh` 里都踩过这个坑
> （早期版本按包名去 `command -v`，永远误报"缺少 bubblewrap"）。

---

## 4. 部署

### 4.1 执行安装

```bash
sudo mkdir -p /opt/rookie-agent
sudo chown -R n100:n100 /opt/rookie-agent
# 把项目源码放到 /opt/rookie-agent（git clone 或 rsync/scp 均可）
cd /opt/rookie-agent
./deploy/install.sh
```

`install.sh` 是**幂等**的 7 步流程，已存在的 venv / `.env` 不会被覆盖：

| 步骤 | 做什么 | 失败会怎样 |
| --- | --- | --- |
| 1/7 平台预检 | 检查 Linux、架构、发行版 | 非 Linux 直接拒绝 |
| 2/7 选 Python | 依次找 3.12→3.11→3.13→3.10→python3，校验 ≥ 3.10 | 版本过低则拒绝并给出修法 |
| 3/7 系统依赖 | 按 `dpkg -s` 差集补装 apt 包，再验 `bwrap`/`ffmpeg` | 缺 `bwrap` 只告警（但强烈建议装上） |
| 4/7 venv 与依赖 | 建 venv → 装 `requirements.txt`（两级源回退）→ import 自检 → **API 探针** → 写 `requirements.lock.txt` | 任一步失败则**整脚本退出**，不留半成品 |
| 5/7 数据目录 | `var/{media,clones,exec,procs}`、`~/.mempalace`、`~/.codex` | — |
| 6/7 配置文件 | 从模板生成 `.env` 并 `chmod 600` | — |
| 7/7 systemd | 渲染并安装 `rookie-agent.service`（**刻意不 enable/start**） | — |

常用参数：

```bash
./deploy/install.sh --no-apt                              # 跳过系统包
./deploy/install.sh --no-unit                             # 不装 systemd unit
./deploy/install.sh --index-url https://pypi.org/simple    # 换源
APP_USER=agent ./deploy/install.sh                        # 指定服务账号
```

### 4.2 检查安装结果

```bash
.venv/bin/python -c "import sys; print(sys.version)"
cat deploy/requirements.lock.txt | wc -l      # 应为 100 行（实测）
ls -l .env                                     # 应为 -rw------- 600
ls -ld var var/media var/clones var/exec var/procs
```

---

## 5. 配置 `.env`

`.env` 由 `deploy/env.production.example` 生成，模板里每项都有注释。
**至少要填这三项**，否则服务起不来或跑不动：

### 5.1 `TELEGRAM_BOT_TOKEN`（必填）

从 [@BotFather](https://t.me/BotFather) 获取。

### 5.2 `TELEGRAM_ALLOWED_CHAT_IDS`（必填，fail-closed）

```bash
# 给机器人发一条消息，然后：
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"id":[0-9-]*'
```

> 🔴 **为什么必须填**：白名单语义是「空 = 不限制」，而本系统默认
> `AGENT_EXEC_ENABLED=true`（应用侧可执行白名单命令）。
> 两者叠加 = 任何人都能让你这台机器跑命令。
> 所以 `validate_security()` 是 fail-closed：**白名单为空直接拒绝启动（exit 2）**。
> 绕过需要显式加 `--allow-insecure`，不要这么做。

### 5.3 模型凭据（二选一）

- **方式 A（推荐）**：填 `OPENAI_API_KEY`，启动时由 CodexBrain 登录；
- **方式 B**：不填 Key，改用机器上已有的 Codex 登录态 ——
  先在**服务运行账号**下执行一次 `codex login`。
  ⚠️ `~/.codex/auth.json` 必须属于服务账号（`n100`），否则读不到。

### 5.4 自定义 Codex 端点（可选）

默认走官方 `openai` provider。想换成自建网关、公司中转或本地推理服务，填这三项：

```bash
AGENT_CODEX_BASE_URL=https://your-endpoint.example.com/v1
AGENT_CODEX_BASE_URL_API_KEY=...      # 留空则复用 OPENAI_API_KEY
AGENT_CODEX_MODEL=<该端点认识的模型名>  # 默认的 gpt-5.6-terra 它多半不认识
```

> 🔴 **硬约束：端点必须实现 OpenAI 的 Responses API，且支持 SSE 流式。**
>
> 请求会打到 `{AGENT_CODEX_BASE_URL}/responses`，请求头带 `accept: text/event-stream`。
> Codex CLI 0.154.0 已经把 **Chat Completions 协议整个移除**（实测）：
>
> | `wire_api` 取值 | 结果 |
> | --- | --- |
> | `"chat"` | `` `wire_api = "chat"` is no longer supported. How to fix: set `wire_api = "responses"` `` |
> | `"chat_completions"` | `unknown variant, expected 'responses'` |
> | `"responses"` / 缺省 | 实际请求 `{base_url}/responses` |
>
> 所以**只提供 `/v1/chat/completions` 的第三方中转配了也用不了**，哪怕它自称"OpenAI 兼容"。
> 这是选端点时最容易踩的坑 —— 配置能过、服务能起，只是每轮对话都失败。
>
> **先验再配**：
>
> ```bash
> cd /opt/rookie-agent
> .venv/bin/python scripts/probe_codex_endpoint.py --show-events
> ```
>
> 它实际发一个最小请求，按响应形态给出诊断（404 = 没有 /responses 路径、
> Content-Type 不是 `text/event-stream` = 不支持流式、401/403 = 密钥问题）。
> 退出码 0 = 可用 / 1 = 有问题 / 2 = 没配自定义端点。

**生效机制**（排障时会用到）：端点**不在 SDK 层配** ——
`openai_codex.CodexConfig` 里根本没有 `base_url` 字段。它属于 Codex CLI 的
`model_providers` 配置，本项目通过 SDK 的 `config_overrides`
（每个元素拼成 `codex --config k=v`）传下去，逻辑在
`app/config.py` 的 `codex_provider_overrides()`。

密钥走 `env_key` + 子进程环境变量，**不进命令行参数** ——
否则 `ps` 里同机用户能看到你的 API Key。

如果不想动 `.env`，也可以直接写用户级 `~/.codex/config.toml`：

```toml
model_provider = "myrelay"

[model_providers.myrelay]
name = "My Relay"
base_url = "https://your-endpoint.example.com/v1"
wire_api = "responses"
env_key = "MYRELAY_API_KEY"      # 这个环境变量必须存在于服务进程的环境里
requires_openai_auth = false
```

> ⚠️ 项目级 `.codex/config.toml` 里写 `model_provider` / `model_providers`
> 会被 Codex **忽略**，必须写在用户级 `~/.codex/config.toml`。
> `openai` / `ollama` / `lmstudio` 是 CLI 的保留 provider id，不能用于自定义。

### 5.5 建议保持默认的关键项

| 变量 | 默认 | 为什么 |
| --- | --- | --- |
| `AGENT_CODEX_SANDBOX` | `read_only` | agent 照样能跑 `ls`/`git status`/`pytest`，只是不能写文件、不能联网，爆炸半径最小 |
| `AGENT_CODEX_APPROVAL_MODE` | `deny_all` | SDK 只有 `auto_review`/`deny_all`；无人值守必须用 `deny_all`，让沙箱成为唯一边界 |
| `AGENT_MCP_COMMAND` | 留空 | 留空 = 用当前解释器（`sys.executable`），保证 MCP 子进程跑在同一个 venv 里。**不要填裸 `python`** |
| `AGENT_DATA_DIR` | 留空 | 默认 `<项目根>/var`，与 systemd 的 `ReadWritePaths=` 对齐 |
| `AGENT_PROCESS_ENABLED` | `false` | 长驻进程**不受任何沙箱约束**，开启前请确认服务跑在专用账号 |

---

## 6. 三种启动方式

### 6.1 装配自检（最常用，不需要任何密钥）

```bash
cd /opt/rookie-agent
.venv/bin/python main.py --health-check --dry-run
```

输出 JSON 健康报告，`tool_count > 0` 才算通过（退出码 0）。

### 6.2 前台试跑（排障用）

```bash
cd /opt/rookie-agent
.venv/bin/python main.py --console          # 控制台渠道
```

`Ctrl+C` 即触发优雅停机。

### 6.3 systemd 托管（生产）

```bash
sudo systemctl enable --now rookie-agent
systemctl status rookie-agent
journalctl -u rookie-agent -f
```

`deploy/rookie-agent.service.in` 的几个关键设计：

```ini
After=network-online.target
WorkingDirectory=@APP_DIR@
EnvironmentFile=@APP_DIR@/.env
Environment=LANG=C.UTF-8
Environment=AGENT_MCP_COMMAND=@APP_DIR@/.venv/bin/python
Restart=always
RestartSec=5
StartLimitBurst=5
KillSignal=SIGTERM      # 触发应用内的优雅停机
KillMode=mixed          # 主进程 SIGTERM，子进程保留给应用自己收
TimeoutStopSec=60       # 给 MCP 子进程与 outbox 补投留时间
ProtectSystem=full
PrivateTmp=yes
ReadWritePaths=@APP_DIR@/var
```

> ⚠️ **刻意没有加**这两个加固项，加了会坏：
>
> - `NoNewPrivileges=yes` —— 会切断 bwrap 创建 user namespace 的能力，
>   导致 Codex 沙箱 fail-closed，agent 无法执行任何工具调用；
> - `ProtectHome=read-only` —— 会锁死 `~/.codex`（登录态）与 `~/.mempalace`（长期记忆）。

---

## 7. 验收步骤

### 7.1 一键全量验收

```bash
cd /opt/rookie-agent
./deploy/verify.sh          # 全量；--quick 跳过两个较慢的测试套件
```

`verify.sh` 按 8 个维度检查，专门盯那些**不会报错、只会静默失能**的问题：

| # | 检查项 | 不通过意味着 |
| --- | --- | --- |
| 1 | 平台与系统依赖（`bwrap` / `ffmpeg`） | 缺 `bwrap` → 沙箱 fail-closed，agent 活着但不干活 |
| 2 | venv、依赖可导入、`ApprovalMode` 内省、**运行时 API 探针** | 装上了但 API 面不匹配（探针会点名缺什么） |
| 3 | 数据目录可写、`.env` 权限 600、必填项 | 白名单为空 → fail-closed 拒绝启动 |
| 4 | **路径锚定**（在 `/` 下加载配置） | 路径跟着 CWD 跑 → systemd 下 CWD=`/`，写进系统 `/var` 或直接启动失败 |
| 5 | 离线测试套件 `smoke_test` / `smoke_exec` | 架构或执行层回归 |
| 6 | 装配自检 `--health-check`，`tool_count > 0` | MCP 解释器写错 → 服务起来了但识图/STT/TTS/记忆全废 |
| 7 | **优雅停机**：起真实实例 → 发 SIGTERM → 查日志「优雅关闭」与孤儿进程 | MCP 子进程泄漏、outbox 不补投 |
| 8 | systemd 服务状态 | — |

退出码 0 = 全通过。**第 7 项是 systemd 部署的核心验收点**：
它等价于 `systemctl stop`，验证 SIGTERM 真的走到了 `orchestrator.stop()`。

### 7.2 手工分步验证

```bash
cd /opt/rookie-agent

# 依赖与 API 面
.venv/bin/python deploy/probe_runtime_api.py             # 34/34

# 离线测试套件
.venv/bin/python scripts/smoke_test.py                   # 42/42
.venv/bin/python scripts/smoke_exec.py                   # 65/65（需能 import openai_codex）

# 路径锚定（模拟 systemd 的 CWD=/）
cd / && /opt/rookie-agent/.venv/bin/python -c "
import sys; sys.path.insert(0, '/opt/rookie-agent')
from app.config import load_config, project_root
cfg = load_config('/opt/rookie-agent/.env')
print('data_dir   =', cfg.data_dir)
print('outbox     =', cfg.memory.outbox_path)
print('mcp_command=', cfg.memory.mcp_command)
print('project_root=', project_root())
"      # 三者都应指向 /opt/rookie-agent，而不是 /var 或 /

# 优雅停机
cd /opt/rookie-agent && .venv/bin/python main.py --dry-run --console &
MAIN=$!; sleep 8; kill -TERM $MAIN; wait $MAIN; echo "exit=$?"
pgrep -af "media_tools_server|mempalace" || echo "无孤儿进程 ✓"
```

### 7.3 沙箱能力探测（必须在目标机上跑）

```bash
cd /opt/rookie-agent
.venv/bin/python scripts/probe_sandbox.py
```

> **沙箱是平台相关实现**（Linux `bwrap` + seccomp / macOS Seatbelt / Windows 受限令牌），
> 能力矩阵**不可跨平台外推**。换平台或升级内核后必须重跑。
> 报告写入 `sandbox-probe-report.md`。

---

## 8. 运维

### 8.1 日常命令

```bash
systemctl status rookie-agent          # 状态
journalctl -u rookie-agent -f          # 实时日志
journalctl -u rookie-agent -n 200      # 最近 200 行
sudo systemctl restart rookie-agent    # 重启
sudo systemctl stop rookie-agent       # 停止（走优雅停机）
sudo systemctl disable rookie-agent    # 取消自启
```

### 8.2 升级代码

```bash
cd /opt/rookie-agent
sudo systemctl stop rookie-agent
git pull                     # 或 rsync 新代码
./deploy/install.sh          # 幂等：补依赖、重装 unit，不动 .env
./deploy/verify.sh
sudo systemctl start rookie-agent
```

依赖有变更时，用锁文件保证可复现：

```bash
.venv/bin/pip install -r deploy/requirements.lock.txt -i https://mirrors.aliyun.com/pypi/simple
```

### 8.3 备份

需要备份的只有三处：

| 路径 | 内容 |
| --- | --- |
| `/opt/rookie-agent/.env` | 全部密钥（权限 600） |
| `/opt/rookie-agent/var/` | 媒体缓存、分身数据、执行产物、进程日志 |
| `/home/n100/.mempalace/` | **长期记忆**（ChromaDB 向量库 + SQLite 知识图谱） |

`.venv/` 不需要备份 —— `install.sh` + `requirements.lock.txt` 能完整重建。

---

## 9. 故障排查

| 症状 | 根因 | 修法 |
| --- | --- | --- |
| pip 报 `Could not find a version ... openai-codex (from versions: none)` | 用了清华镜像（该包在 tuna 上不存在） | 换 `--index-url https://mirrors.aliyun.com/pypi/simple` 或 `https://pypi.org/simple` |
| 探针报 `mcp.server.fastmcp.FastMCP` 缺失 | mcp 被装成 2.x | 确认 `requirements.txt` 里 `mcp>=1.9.0,<2`，重装 |
| 探针报 `Application/Updater 生命周期方法` 缺失 | python-telegram-bot 大版本变了 | 锁回 `python-telegram-bot>=21.6,<23` |
| 启动 `exit 2`，日志含 `validate_security` | `TELEGRAM_ALLOWED_CHAT_IDS` 为空（fail-closed） | 填白名单；**不要**用 `--allow-insecure` 绕过 |
| 服务起来了但 `tool_count = 0` | MCP 子进程起不来：解释器路径错 / 依赖没装 | 跑 `--health-check` 看每个 server 的 `error`；确认 `AGENT_MCP_COMMAND` 不是裸 `python` |
| agent 能回话但**任何工具都调不动** | 缺 `bwrap`，Codex 沙箱 fail-closed | `sudo apt install bubblewrap`；用 `command -v bwrap` 验证 |
| `/opt` 下找不到数据，反而多了 `/var/media` | 代码里出现了相对 CWD 的路径 | v0.1.0 已把全部路径锚定到项目根；检查是否被本地改动破坏 |
| `systemctl stop` 后仍有 `media_tools_server` 进程 | SIGTERM 处理缺失 | v0.1.0 已在 `main.py` 注册优雅停机；检查 unit 里是否误删 `KillSignal=SIGTERM` / `KillMode=mixed` |
| 语音回复是文件而不是语音气泡 | 缺 `ffmpeg` | `sudo apt install ffmpeg`（功能不中断，仅样式降级） |
| Telegram 收不到消息 | token 错 / 白名单不含自己 / 机器出不去网 | `curl -s "https://api.telegram.org/bot<TOKEN>/getMe"` |

---

## 10. 环境事实（本次实测）

以下结论均在本环境**实测**得出，不是推断：

| 事项 | 实测结果 |
| --- | --- |
| Python 3.14.4 是否够用 | ✅ 够用。96 个依赖全部有 cp314 wheel，`pip` 解析零报错 |
| Codex CLI 有无 Linux 二进制 | ✅ `openai_codex_cli_bin-0.154.0-py3-none-manylinux_2_17_x86_64.whl`（127 MB），`py3` 标签与 Python 版本无关 |
| `onnxruntime`（chromadb 硬依赖） | ✅ `1.30.0-cp314-cp314-manylinux_2_28_x86_64` |
| 清华镜像有无 `openai-codex` | ❌ 404，**没有** |
| 阿里云镜像有无 `openai-codex` | ✅ 有，最高 0.154.0 |
| bubblewrap | ✅ 已装 `0.11.1-1ubuntu0.3`，二进制 `/usr/bin/bwrap` |
| ffmpeg | ✅ 已装 `7:8.0.1-3ubuntu2` |
| Telegram 是否需要开放入站端口 | ❌ 不需要（长轮询，只出站） |
| venv 体积 | 839 MB |
| `requirements.lock.txt` | 100 行 |

---

## 11. 相关文件

| 文件 | 作用 |
| --- | --- |
| `deploy/install.sh` | 幂等一键部署（7 步） |
| `deploy/verify.sh` | 8 维度部署验收 |
| `deploy/probe_runtime_api.py` | 运行时 API 探针：验证已装库与代码用法一致（34 项） |
| `deploy/env.production.example` | `.env` 模板，逐项注释 |
| `deploy/rookie-agent.service.in` | systemd unit 模板 |
| `deploy/requirements.lock.txt` | 安装后自动生成的精确版本锁 |
| `docs/linux-deployment-readiness.md` | 上线可行性评估（P0/P1/P2 清单） |
| `scripts/probe_sandbox.py` | 沙箱能力探测（平台相关，必须在目标机跑） |
| `scripts/probe_codex_endpoint.py` | 自定义 Codex 端点自检（验 Responses 协议 + SSE 流式） |
