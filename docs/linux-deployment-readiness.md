# v0.1.0 Linux 上线可行性评估

> 评估对象：`rookie-agent` v0.1.0（`app/__init__.py` → `__version__ = "0.1.0"`）
> 评估日期：2026-09-20
> 评估方式：通读全部 21 个源文件（15 个 `app/*.py` + 2 个 `mcp_servers/*.py` + 4 个 `scripts/*.py`）
> + 外部依赖联网核实（Codex CLI Linux 产物、Linux 沙箱系统依赖）

---

> ## ⚠️ 更新（2026-09-20 晚）：P0 五项已全部修复并在真机验收通过
>
> 本文正文是**评估当时的快照**，其中的结论已在修复后失效。请以下面为准：
>
> | P0 | 修复方式 | 验收结果 |
> | --- | --- | --- |
> | P0-1 无 SIGTERM 处理 | `main.py` 注册 SIGTERM/SIGINT 优雅停机 | ✅ 真机实测：SIGTERM 后日志出现「优雅关闭」，**无残留 MCP 子进程** |
> | P0-2 MCP 命令硬编码 `python` | 默认值改 `sys.executable`，新增 `AGENT_MCP_COMMAND` | ✅ `mcp_command = /opt/rookie-agent/.venv/bin/python` |
> | P0-3 路径相对 CWD | `config.py` 新增 `project_root()` 等锚定函数 | ✅ 在 `/` 下加载配置，路径仍指向 `/opt/rookie-agent` |
> | P0-4 无部署产物 | 新增 `deploy/`（install.sh / verify.sh / unit 模板 / env 模板 / API 探针） | ✅ systemd unit 已安装，`systemd-analyze verify` 通过 |
> | P0-5 依赖未锁版本 | `requirements.txt` 加精确锁与上限；install.sh 自动产 `requirements.lock.txt` | ✅ 100 行锁文件 |
>
> **实测环境**：Ubuntu 26.04 LTS / Python 3.14.4 / N100 x86_64 / `/opt/rookie-agent`
>
> **全量验收结果**：`deploy/verify.sh` → **通过 25 / 失败 0**；`smoke_test 42/42`、
> `smoke_exec 65/65`、运行时 API 探针 **34/34**、健康检查 **tool_count=49（2/2 server healthy）**。
>
> 📖 **部署与运行请直接看 [`docs/deployment-linux.md`](./deployment-linux.md)** —— 那是操作手册。
> 本文保留为"为什么当初判定不能上线"的设计记录，以及若干**仍然成立**的风险提示（§2.3 沙箱、
> §2.5 首次运行需下载嵌入模型、§4.5 备份缺失等）。
>
> 另外本次实测**推翻/修正了原文的三处判断**，见 §9。

---

## 0. 结论

### ❌ 不可以直接上线（Not Ready for Production）

**平台相关代码写得很好，问题不在代码逻辑，而在「部署产物整体缺失」+「3 个 Linux 特有的行为缺陷」。**

| 维度 | 评级 | 一句话结论 |
| --- | --- | --- |
| 构建与打包 | 🔴 **阻塞** | 没有 Dockerfile、没有 systemd unit、没有锁版本、不是可安装包 |
| 运行时依赖与系统库 | 🟡 **基本满足** | Codex 有 Linux 预编译 wheel；需补 `bubblewrap`，否则沙箱 fail-closed |
| 配置与路径 | 🔴 **阻塞** | 全部数据路径相对 CWD；MCP 子进程命令硬编码 `python`（Linux 常见不存在） |
| 权限与服务管理 | 🔴 **阻塞** | **没有 SIGTERM 处理**，`systemctl stop` 会跳过全部清理逻辑 |

### 可以做什么 / 不可以做什么

| 场景 | 是否可行 |
| --- | --- |
| 开发机 / 内网自用试运行（手动 `python main.py`，前台跑） | ✅ **可以**，改 2 处即通 |
| 生产环境无人值守（systemd / 容器托管） | ❌ **不可以**，先修 P0 清单（§5） |
| 容器化部署 | ⚠️ **需额外配置**，默认容器配置下 Codex 沙箱会 fail-closed（§2.3） |

---

## 1. 构建与打包：不兼容

### 1.1 缺失的部署产物（全部为「无」）

| 应有产物 | 现状 | 后果 |
| --- | --- | --- |
| `Dockerfile` | ❌ 不存在 | 文档建议"运行在容器或专用账号下"，但没有容器化路径 |
| `docker-compose.yml` | ❌ 不存在 | 无法一键起服务 |
| `pyproject.toml` | ❌ 不存在 | 不是可安装包，只能 `python main.py`（**强依赖 CWD**） |
| `*.lock` / 精确 pin | ❌ 不存在 | `requirements.txt` 全部是 `>=`，**构建不可复现** |
| systemd unit | ❌ 不存在 | 无法接入 `systemctl`，无重启策略、无 `WorkingDirectory` |
| CI 配置 | ❌ 不存在 | 没有自动化验证 Linux 上是否真能跑通 |
| `Makefile` / `deploy.sh` | ❌ 不存在 | 无部署入口 |

> `.gitignore` 已正确排除 `var/`、`.env`、`*.sqlite3`、`*.jsonl`、`docs/sandbox-probe-report.md` —— 卫生习惯是对的，但这也意味着仓库里**完全没有可执行的部署模板**。

### 1.2 依赖版本不可复现

`requirements.txt` 全部使用下限约束：

```
openai-codex>=0.1.0
mempalace>=3.3.0
chromadb>=0.5.0
python-telegram-bot>=21.6
mcp>=1.9.0
openai>=1.40.0
httpx>=0.27.0
```

风险最集中的是 `openai-codex>=0.1.0`：

- 该 SDK 目前处于 **beta 阶段（`0.1.0b2`）**，公开资料明确说明 *"API surface changes between betas with no stability guarantee"*，官方建议 **pin 精确版本**；
- 本项目大量依赖 SDK 的内省细节（`ApprovalMode` 只有 `auto_review` / `deny_all`、`ThreadItem` 是 pydantic `RootModel` 有 `root` 字段、`cwd` 是 RootModel 外壳）—— 这些**都是 beta 版本内的实现细节**，升一个小版本就可能全部失效；
- 而 `>=0.1.0` 会把 `0.1.0b1`、`0.1.0b2`、以及未来的 `0.2.x` 全部纳入可安装范围。

**结论**：必须 pin 到精确版本，并且 CI 里加一道字节码级 import 自检。

### 1.3 不是包，只是脚本

没有 `pyproject.toml` → 无法 `pip install -e .` → 只能用 `sys.path` 相对导入。这直接把 1.4 的路径问题放大成硬依赖。

---

## 2. 运行时依赖与系统库：基本满足，但有 2 个前置条件

### 2.1 ✅ 好消息：Codex CLI 有 Linux 预编译产物，不需要 Node / Rust

联网核实（`openai-codex-cli-bin`，PyPI）：

- 该包**以平台 wheel 形式提供预编译 Codex 二进制**，已确认发布 Linux 产物：
  `manylinux_2_17_x86_64`、`manylinux_2_17_aarch64`（另有 macOS / Windows 产物）；
- 它是 **wheel-only**（构建钩子拒绝产出 sdist），安装时 pip 按平台标签自动选对二进制；
- 模块 `codex_cli_bin` 暴露 `bundled_codex_path()` / `bundled_package_dir()` 供程序定位；
- 因此 Linux 侧 **不需要 Node.js、不需要 Rust 工具链**（这是走 `npm install -g @openai/codex` 路线才需要的）。

> 补充：`scripts/probe_sandbox.py` 的二进制搜索已支持无扩展名的 `codex`（`EXACT_NAMES` 含 `"codex"`），Linux 上可正常定位。

### 2.2 Python 版本下限

| 组件 | 要求 | 来源 |
| --- | --- | --- |
| `openai-codex` | **≥ 3.10** | SDK 官方要求 |
| `mempalace` | ≥ 3.9 | MemPalace 官方要求 |

→ **有效下限 = Python 3.10**。但项目里**没有任何地方声明这个约束**（无 `pyproject.toml` 的 `requires-python`、无 `.python-version`、README 未写）。部署时容易踩到发行版自带的 3.9（如 RHEL 9 / Ubuntu 20.04 需自行装 3.10+）。

### 2.3 ⚠️ 关键前置条件一：Linux 沙箱需要 `bubblewrap`，否则 Codex fail-closed

这是本项目**最容易被忽略、后果最严重**的系统依赖。

核实结论（来自 `codex-rs/linux-sandbox/README.md` 与官方文档）：

- Codex 在 Linux 上的沙箱实现是 **bubblewrap（bwrap）+ seccomp**（Landlock 已降级为 legacy opt-in 兜底）；
- bwrap 查找顺序：**PATH 上的 bwrap** → 回退到 Codex 自带的 `codex-resources/bwrap`；
- 自带兜底存在，所以**裸机不装 bwrap 不会崩**，但会打印启动 warning；
- **真正的红线**：*如果 bubblewrap 无法创建 user namespace，Codex 会 fail-closed —— agent 完全无法执行任何工具调用*。

对部署形态的影响：

| 部署形态 | 需要做什么 |
| --- | --- |
| 裸机 / 云 VM（Ubuntu 22.04+ / Debian 12） | `apt install bubblewrap` 即可，无额外配置 |
| 容器（Docker / OCI） | 默认 seccomp profile 会**禁止 user namespace 创建** → 必须 `--cap-add SYS_ADMIN --security-opt seccomp=unconfined`，或改用 `[features] use_legacy_landlock = true`（隔离更弱），或直接 `--sandbox danger-full-access`（容器本身当边界） |
| Ubuntu 24.04+（AppArmor 限制 userns） | 需 `sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` |
| WSL2 | ✅ 正常走 Linux 的 bwrap 路径 |
| WSL1 | ❌ **不支持**（无法创建 user namespace，Codex 会直接拒绝沙箱化命令） |

⚠️ 本项目当前 `AGENT_CODEX_SANDBOX=read_only` 是默认值，且业务上确实需要 read_only 沙箱作为唯一安全边界（因为 `approval_mode=deny_all`）。**如果沙箱 fail-closed，整个 agent 会"活着但不干活"** —— 表现为模型收到工具结果全是失败，非常难排查。

> 另注：`docs/shell-tool-design.md` §0.3 已经正确指出"沙箱能力矩阵不能跨平台外推，换平台必须重跑 `probe_sandbox.py`"。**必须在目标 Linux 机上真正执行这一步**，不要把当前 Windows 机上的探测结果当作 Linux 结论。

### 2.4 ⚠️ 关键前置条件二：`ffmpeg`（可选，非阻塞）

- `MediaRouter.__init__` 用 `shutil.which(cfg.ffmpeg_bin) or shutil.which("ffmpeg")` 探测；
- 用途：TTS 产出的 mp3 → 转 OGG/Opus 以适配 Telegram `sendVoice`；
- **缺失时优雅降级**：返回 mp3，渠道改走 `sendAudio`，功能不中断，只是气泡样式不同。
- 建议装上（`apt install ffmpeg`），但不构成上线阻塞。

### 2.5 🟡 重量级 Python 依赖的隐性成本

- `mempalace` → `chromadb` → 传递依赖会拉入 **onnxruntime**（或 `sentence-transformers` 路线下的 **PyTorch**）。这意味着：
  - 镜像/虚拟环境体积会显著增大（数百 MB 级）；
  - **首次运行需要联网下载嵌入模型（约 80 MB）** —— 若目标机是内网环境，必须预置模型缓存，否则第一次写入记忆会卡住或失败；
- **SQLite 版本**：ChromaDB 在部分功能上要求 SQLite ≥ 3.35。CentOS 7 / Ubuntu 20.04 自带的 SQLite（3.31 / 3.31）低于该线，可能出现运行时告警或失败。
- 架构覆盖：x86_64 无问题；**ARM64（含国产化服务器 / 树莓派）需单独验证** onnxruntime 与 chromadb 的 wheel 可用性。

### 2.6 ✅ 加分项：不需要任何特权网络能力

Telegram 渠道使用 **长轮询**（`app.updater.start_polling(drop_pending_updates=True)`），**不监听任何入站端口**。

这是一个很实在的优点：

- 不需要 root（无 `CAP_NET_BIND_SERVICE` 需求，无 <1024 端口）；
- 不需要开放防火墙入站规则；
- 不需要公网 IP / 域名 / TLS 证书 / 反向代理；
- 不存在 Web 服务常见的暴露面。

---

## 3. 配置文件与路径：不兼容（阻塞）

### 3.1 🔴 阻塞项：所有数据路径都相对 CWD

`app/config.py` 中的路径全部是**相对路径字面量**，运行时按当前工作目录解析：

| 配置项 | 默认值 | 解析结果 |
| --- | --- | --- |
| `AppConfig.data_dir` | `./var` | `$CWD/var` |
| `TelegramConfig.media_dir` | `./var/media` | `$CWD/var/media` |
| `MediaConfig.media_dir` | `./var/media` | `$CWD/var/media` |
| `SchedulerConfig.clone_root` | `./var/clones` | `$CWD/var/clones` |
| `ExecConfig.workdir` | `./var/exec` | `$CWD/var/exec` |
| `ProcessConfig.workdir` | `./var/procs` | `$CWD/var/procs` |
| `CodexConfig.working_dir` | `Path.cwd()` | `$CWD` |
| `CodexBrain.session_db` | `./var/sessions.sqlite3` | `$CWD/var/sessions.sqlite3` |
| `.env` 加载路径 | `".env"` | `$CWD/.env` |
| 记忆 outbox | `./var/memory_outbox.jsonl` | `$CWD/var/memory_outbox.jsonl` |

**systemd 下的具体故障**：unit 若不设 `WorkingDirectory=`，进程 CWD 为 `/`。此时：

- `Path("./var")` → `/var` → 非 root 服务账号 **`PermissionError`，启动即失败**；
- 若以 root 运行，则会把运行数据写进系统目录 `/var` —— 更糟；
- `--env-file .env` 默认值 → `/.env` → 读不到，配置全部落回默认值（**静默降级，比报错更危险**）；
- Codex 的 `working_dir=/` → agent 的"工作区"变成根目录，配合 `workspace_write` 沙箱语义完全失控。

**修复方向（二选一，建议两者都做）**：

1. **立即缓解**：systemd unit 里显式 `WorkingDirectory=/opt/rookie-agent`（成本最低）；
2. **根本修复**：把默认路径改成基于 `Path(__file__).resolve().parent.parent` 推导的绝对路径，或强制要求 `AGENT_DATA_DIR` 为绝对路径并在启动时校验（fail-fast）。

### 3.2 🔴 阻塞项：MCP 子进程命令硬编码 `"python"`，且**无法用环境变量覆盖**

`app/config.py` 第 128-129、153-154 行：

```python
@dataclass(frozen=True, slots=True)
class MemoryConfig:
    mcp_command: str = "python"
    mcp_args: tuple[str, ...] = ("-m", "mempalace.mcp_server")

@dataclass(frozen=True, slots=True)
class MediaConfig:
    mcp_command: str = "python"
    mcp_args: tuple[str, ...] = ("-m", "mcp_servers.media_tools_server")
```

`app/mcp_hub.py` 直接消费这两个值：

```python
McpServerSpec(key="mempalace",   command=memory_cfg.mcp_command, args=tuple(memory_cfg.mcp_args), ...)
McpServerSpec(key="media_tools", command=media_cfg.mcp_command,  args=tuple(media_cfg.mcp_args),  ...)
```

问题有两层：

**第一层 —— Linux 上 `python` 常常不存在。**
Debian / Ubuntu 20.04+（含 WSL2 的默认 Ubuntu）遵循 PEP 394，**只提供 `python3`，不提供 `python`**。未激活虚拟环境时，`command="python"` 会直接 `FileNotFoundError`，两个 MCP server 全部起不来，结果是：

- `McpHub.start()` 静默失败（`_ServerWorker._run` 捕获 `BaseException` 后置 `_error`，只打 warning）；
- 主脑看不到任何 `mcp__*` 工具（识图 / STT / TTS / 记忆 全丢）；
- 记忆写入落到 outbox 待补投，检索与知识图谱走降级路径；
- **系统"看起来启动成功"，但核心能力全废** —— 最难排查的那类故障。

**第二层 —— 没有对应的环境变量，无法在不改代码的情况下修复。**
`load_config()` 构造 `MemoryConfig` / `MediaConfig` 时**没有传 `mcp_command` / `mcp_args`**，因此这两个字段永远是默认值 `"python"`。

**第三层 —— 这违反了项目自己声明的"工具挂载同源"约定。**
`app/config.py` 顶部注释与 `README §1.1` 都明确说"两个消费方从同一份 `McpServerSpec` 派生"。实际实现是：

| 消费方 | 实际使用的命令 | Linux 上是否正确 |
| --- | --- | --- |
| Codex 主脑（`scripts/setup_codex_mcp.py`） | `sys.executable`（绝对路径） | ✅ 正确 |
| 应用自己（`app/mcp_hub.py` ← `config.py`） | `"python"`（裸命令名） | ❌ 很可能不存在 |

两个消费方**拿到的不是同一个命令**。这是契约漂移，不只是 Linux 适配问题。

**修复方向**：新增 `AGENT_MCP_COMMAND` / `AGENT_MCP_PYTHON` 环境变量，默认值改为 `sys.executable`，并在 `load_config()` 中读入。

### 3.3 🟡 文件系统权限需预先铺设

服务账号需要对以下路径有写权限，且**必须预先创建并 chown**（首次运行前的准备动作）：

| 路径 | 用途 |
| --- | --- |
| `<项目根>/var/` 及其子目录 `media/`、`clones/`、`exec/`、`procs/` | 运行数据、附件落盘、分身工作区、临时文件 |
| `<项目根>/var/sessions.sqlite3` | `session_id → Codex thread_id` 映射 |
| `<项目根>/var/memory_outbox.jsonl` | 记忆写入失败时的补投队列 |
| `~/.mempalace/`（`palace/` + `knowledge_graph.sqlite3`） | 长期记忆本体（**核心资产**） |
| `~/.codex/`（`config.toml` + `auth.json` + `sessions/`） | Codex 配置与登录态 |
| `~/.cache/`（onnxruntime / huggingface 模型缓存） | 嵌入模型缓存 |

> `MemoryGateway` 有一处需要留意：`MemoryConfig.palace_path` 默认走 `Path.home() / ".mempalace"`。若服务以 system 账号（`User=rookie-agent`）运行，`Path.home()` 会解析到该账号的 home，与开发时 `~/` 指向的目录**可能不是同一份记忆库**。上线前必须确认这一点，否则会"记忆突然全空"。

### 3.4 🟡 日志编码

`main.py` 的 `configure_logging()` 使用 `logging.basicConfig(..., stream=sys.stdout)`，而日志消息与异常文本**大量包含中文**（`validate_security()` 的安全提示、模块日志、`/status` 输出等）。

systemd 环境下 `LANG` 默认为空（POSIX locale），存在 `UnicodeEncodeError` 风险。CPython 的 PEP 538（locale coercion）在大多数 Linux 上会兜住，但**不应依赖隐式行为**。

**修复方向**：unit 里显式设置 `Environment=LANG=C.UTF-8`（或 `PYTHONUTF8=1`）。

### 3.5 ✅ 已正确处理的部分

- 路径拼接全部用 `pathlib.Path` + `/` 运算符，没有手工拼 `\` 或 `os.sep`；
- `.mcp_hub.McpServerSpec.to_codex_toml()` 与 `setup_codex_mcp.py` 都显式 `.as_posix()` / `.replace("\\", "/")` 转换路径分隔符；
- `.env.example` 明确提示"Windows 路径统一用正斜杠"，且说明 Linux/macOS 用默认值即可；
- 文件名全部小写、无大小写冲突（Linux 大小写敏感文件系统安全）；
- 无硬编码 `C:\` 绝对路径（`grep` 全库确认，仅在 `probe_sandbox.py` 的**帮助文本示例**里出现 Windows 路径字符串）。

---

## 4. 权限与服务管理机制：不支持（阻塞）

### 4.1 🔴 最严重的 Linux 特有缺陷：没有信号处理，`systemctl stop` 会跳过全部清理

全库检索结果：

```
add_signal_handler  → 0 处
SIGTERM 处理        → 0 处
```

`app/local_exec.py` 里出现的 `signal.SIGTERM` 是**用来杀子进程**的，不是处理自身收到的信号。

`main.py` 的停机路径只有一条：

```python
try:
    await asyncio.Event().wait()
except (KeyboardInterrupt, asyncio.CancelledError):
    pass
finally:
    logger.info("正在关闭…")
    await orchestrator.stop()      # ← 只有 Ctrl+C 能走到这里
```

`KeyboardInterrupt` 对应 **SIGINT**（终端 Ctrl+C）。而：

- `systemctl stop` / `systemctl restart` 发的是 **SIGTERM**；
- `docker stop` 发的是 **SIGTERM**；
- 两者都不是 SIGINT。

**后果（按危害排序）**：

| # | 后果 | 说明 |
| --- | --- | --- |
| 1 | **孤儿进程泄漏** | `Orchestrator.stop()` 不执行 → `hub.stop()` 不执行 → mempalace / media_tools 两个 MCP 子进程 + `ProcessManager` 管理的长驻进程**全部变成孤儿**。重启后新旧进程并存，占用文件锁与 SQLite 连接 |
| 2 | **数据丢失** | `memory.flush_outbox()` 不执行 → `var/memory_outbox.jsonl` 里堆积的待补投记忆**永远不会被投递** |
| 3 | **SQLite 状态不确定** | `sessions.sqlite3` 的连接不作显式关闭；`CloneSystem` 的在途任务直接蒸发，任务结果丢失 |
| 4 | **Telegram 会话残留** | `app.updater.stop()` / `app.shutdown()` 不执行，可能出现"服务已死但 Telegram 侧仍在投递 update"的堆积 |

这是**唯一一个「不修就绝对不能上生产」的缺陷**，因为它是 100% 必现（每次正常停止/重启都会触发），而 systemd 的 `Restart=always` 会让它在崩溃重启时反复放大。

### 4.2 无 systemd unit 模板

缺少以下必要字段的定义，全部需要从零编写：

| 需要定义 | 作用 |
| --- | --- |
| `User=` / `Group=` | 降权到专用账号（**不能用 root**，因为 `ProcessManager` 不受沙箱约束） |
| `WorkingDirectory=` | **关键**，否则 §3.1 的路径问题直接导致启动失败 |
| `EnvironmentFile=` | 加载 `.env`（含密钥，需 `chmod 600`） |
| `ExecStart=` | 显式用 venv 的绝对路径 `python`，而不是裸 `python` |
| `Restart=always` / `RestartSec=` | 崩溃自愈 |
| `Environment=LANG=C.UTF-8` | 修 §3.4 |
| `KillSignal=SIGTERM` + `TimeoutStopSec=` | 配合 §4.1 的优雅停机 |
| `StateDirectory=` / `LogsDirectory=` | systemd 托管的可写目录 |

### 4.3 无健康检查 / 就绪探针

系统有 `health()` 方法族（`brain.health()` / `hub.health()` / `memory.backend_report()` / `processes.stats()`），但**没有任何对外暴露的检查入口**：

- 无法接 systemd `WatchdogSec=`（需要进程定时发 `sd_notify`）；
- 无法做容器 `HEALTHCHECK` / K8s liveness probe；
- 运维只能靠"日志里有没有报错"判断死活 —— 而 §3.2 那种"启动成功但能力全废"的故障，日志里只有一行 warning。

**建议**：`/status` 指令已有完整的健康聚合，把它抽成一个 CLI 子命令（`python main.py --health-check`，返回 exit 0/1），systemd/K8s 就能直接调用。

### 4.4 无日志轮转方案

日志走 `logging.basicConfig(stream=sys.stdout)`：

- 若用 systemd → journald 自动接管轮转，✅ 无需额外配置；
- 若用容器 → 依赖 Docker 的 `log-opts`，需显式配置；
- 若重定向到文件 → **需要自己配 logrotate，当前完全没有**。

### 4.5 无备份 / 恢复 / 升级回滚流程

`~/.mempalace/`（ChromaDB 向量库 + SQLite 知识图谱）是**长期积累型资产**，一旦损坏不可重建。当前：

- 无备份脚本；
- 无备份策略说明；
- 无升级步骤 / 回滚步骤；
- 无版本迁移说明（`sessions.sqlite3`、KG schema 变更时怎么办）。

### 4.6 ✅ 已正确处理的部分

执行层的平台分支写得相当扎实，Linux 上是正确的：

| 能力 | POSIX 分支 | 实现位置 |
| --- | --- | --- |
| 子进程独立进程组 | `start_new_session=True` | `local_exec.spawn_kwargs()` |
| 终止整个进程组 | `os.killpg(os.getpgid(pid), SIGTERM→SIGKILL)` 先礼后兵 | `local_exec.terminate_tree()` |
| 编码兜底链 | UTF-16 BOM → UTF-8 → GBK → latin-1 | `local_exec.decode_output()` |
| 最小环境变量 | `_ENV_KEEP` 保留 `PATH`/`HOME`/`LANG`/`TMPDIR` 等 POSIX 变量 | `local_exec.minimal_env()` |
| 输出双阈值 | 返回级截断 + 进程级字节上限（`read_stream`） | 已验证 65/65 |
| 管道死锁规避 | `asyncio.gather` 并发读 stdout/stderr | `_spawn_and_capture` |
| ANSI 剥离 | 正则覆盖 CSI + OSC | `local_exec.strip_ansi()` |

`ProcessManager` 的 `_HARDLINE_PROGRAMS` 黑名单也已包含 Linux 侧的危险程序（`mkfs`、`fdisk`、`wipefs`、`shred`、`parted`）与 shell 片段（`:(){`、`rm -rf /`、`of=/dev/sd`）。

---

## 5. 阻碍上线的关键问题清单

### P0 —— 不修就不能上生产（5 项）

| # | 问题 | 位置 | 修复要点 |
| --- | --- | --- | --- |
| **P0-1** | **无 SIGTERM 处理，优雅停机完全失效** | `main.py:L110-147` | 注册 `loop.add_signal_handler(SIGTERM/SIGINT, ...)`，触发 `orchestrator.stop()`；unit 配 `TimeoutStopSec` |
| **P0-2** | **MCP 子进程命令硬编码 `"python"`，Linux 常不存在，且无 env 可覆盖** | `app/config.py:L128,153` + `app/mcp_hub.py:L323,335` | 默认值改为 `sys.executable`，新增 `AGENT_MCP_COMMAND` 环境变量；同时消除与 `setup_codex_mcp.py` 的命令不一致 |
| **P0-3** | **全部数据路径相对 CWD；systemd 下非 root 直接启动失败 / root 则污染 `/var`** | `app/config.py` 多处 | 至少写 systemd unit 设 `WorkingDirectory=`；根本解法是改用基于 `__file__` 的绝对路径 + 启动时校验 |
| **P0-4** | **无 systemd unit / 部署产物** | 仓库根目录 | 补 unit（含 `User`/`WorkingDirectory`/`EnvironmentFile`/`Restart`/`LANG`）、venv 创建脚本、`var/` 目录预创建与 chown |
| **P0-5** | **依赖未锁版本，`openai-codex` 是 beta 且代码依赖其内省细节** | `requirements.txt` | pin 精确版本（含 `openai-codex-cli-bin`）；CI 加 import 自检 |

### P1 —— 上线后短期内必然遇到（5 项）

| # | 问题 | 影响 |
| --- | --- | --- |
| **P1-1** | 无健康检查入口 | 无法接 systemd watchdog / K8s liveness；"假活"故障无法自动发现 |
| **P1-2** | 启动时不校验 `bubblewrap` 可用性 | 沙箱 fail-closed 只在第一次工具调用时才暴露，排查成本极高 → 应在 `start()` 里前置探测并 fail-fast |
| **P1-3** | `~/.mempalace/` 无备份策略 | 长期记忆是不可重建资产，损坏即永久丢失 |
| **P1-4** | 首次运行需联网下载 ~80MB 嵌入模型 | 内网 / 离线环境会卡住记忆写入 → 需预置模型缓存并写入部署文档 |
| **P1-5** | 无日志轮转（非 journald 场景） | 长时间运行可能撑满磁盘 |

### P2 —— 文档与一致性问题（3 项）

| # | 问题 | 证据 |
| --- | --- | --- |
| **P2-1** | README 测试数量自相矛盾 | 文件头部写 **32/32**，§8 粘贴的输出写 **31/31**，§9 目录树写"31 项"，§11.5 写"31/31"。**逐行统计 `smoke_test.py` 的 `check()` 调用：34 处调用点，其中 2 对是 try/except 互斥分支 → 运行时 32 项**。所以 32 是正确的，README 的 31 是陈旧的 |
| **P2-2** | Python 版本下限未声明 | `openai-codex` 要求 ≥3.10，但 README / requirements.txt / 任何配置里都没写 |
| **P2-3** | "工具挂载同源"约定未被代码兑现 | 见 §3.2 第三层；文档声称同源，实现是两份不同的命令 |

---

## 6. 可以直接复用的部分（无需改动）

评估中确认以下部分是**平台无关且实现正确**的，Linux 上可直接使用：

1. **执行层的平台分支** —— `spawn_kwargs()` / `terminate_tree()` / `decode_output()` / `minimal_env()` 都做了正确的 POSIX 分支（§4.6 表格）；
2. **Telegram 长轮询** —— 无入站端口、无 root 需求、无防火墙/TLS 需求（§2.6）；
3. **ffmpeg 缺失的优雅降级** —— 不影响功能完整（§2.4）；
4. **记忆层两级降级 + outbox 补投** —— MemPalace 缺失时走只读 SQLite，写入失败落 outbox（`smoke_test` 场景 4 覆盖）；
5. **工具失败不抛异常** —— 全部返回 `{ok: false, error}`，符合 MCP 语义；
6. **依赖方向与契约层** —— 模块间只依赖 `contracts.py` 的 Protocol，替换实现不需要改上层；
7. **`validate_security()` 的 fail-closed 校验** —— 执行能力开启 + 白名单为空 → 拒绝启动（exit 2）；
8. **`.gitignore` 卫生** —— `var/`、`.env`、`*.sqlite3`、`*.jsonl` 均已排除。

---

## 7. 上线前必须完成的验证（建议顺序）

```bash
# ── 阶段 A：目标 Linux 机上的最小可运行验证 ────────────────────────
# A1. 确认 Python 版本与依赖（注意 openai-codex 需 ≥3.10）
python3 --version && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# A2. 离线架构验证（不需要 Codex 账号 / MemPalace / Telegram）
python scripts/smoke_test.py    # 目标 32/32（README 的 31/31 是陈旧数字）
python scripts/smoke_exec.py    # 目标 65/65

# A3. 【必做，不能跳过】在本机探测沙箱能力矩阵 —— 结论不可跨平台外推
python scripts/probe_sandbox.py --print-report

# A4. 确认 bubblewrap 就绪（否则 Codex 会 fail-closed）
which bwrap || echo "警告：bwrap 缺失，将回退到 Codex 自带版本"

# ── 阶段 B：修复 P0 后的完整链路验证 ──────────────────────────────
cp .env.example .env            # 填 TELEGRAM_BOT_TOKEN / API Key
python main.py --dry-run --console    # 验证装配（不调真实模型）
python main.py --console              # 终端渠道真实调用

# B1. 验证优雅停机 ← 当前必然失败，是 P0-1 的验收动作
#     在另一个终端： kill -TERM <pid>
#     期望日志出现"正在关闭…"，且 ps 中无残留 MCP / 长驻进程

# B2. 验证 MCP 工具真的挂上了 ← 当前在 Linux 上必然失败，是 P0-2 的验收动作
#     终端里执行 /status，确认 tool_count > 0 且 servers 全部 healthy
#     （而不是"看起来启动成功但工具数为 0"）

# B3. 接入 Telegram 后确认 sandbox 未 fail-closed
#     让 agent 跑一条 `ls`，确认 commandExecution 条目的 status 不是 `declined`
```

**放行标准**：A1-A4 全通过 + P0 五项全部修复 + B1/B2/B3 三项验收通过。

---

## 8. 附：评估覆盖范围

| 类别 | 文件数 | 已通读 |
| --- | --- | --- |
| 应用核心 | 15 (`app/*.py`) | ✅ 全部 |
| MCP server | 2 (`mcp_servers/*.py`) | ✅ 全部 |
| 脚本 | 4 (`scripts/*.py`) | ✅ 全部 |
| 配置 | `requirements.txt` / `.env.example` / `.gitignore` | ✅ 全部 |
| 文档 | `README.md` / `docs/shell-tool-design.md` / `docs/sandbox-probe-report.md` | ✅ 全部 |
| 外部事实核实 | Codex CLI Linux 产物、Linux 沙箱系统依赖、MemPalace/ChromaDB 依赖 | ✅ 联网核实 |

**未覆盖**：目标硬件的实机运行验证（本评估在 Windows 开发机上完成，运行时结论需在目标 Linux 机上按 §7 复验）。

---

## 9. 真机实测对本文的修正

修复后在目标机（Ubuntu 26.04 / Python 3.14.4 / N100）实跑，有三处判断需要修正。
记在这里是为了避免后人重踩 —— 都是"凭常识推断"会推错的地方。

### 9.1 修正：`openai-codex>=0.1.0` 的版本推演（§1.2）不准确

原文说"`>=0.1.0` 会把 `0.1.0b1`、`0.1.0b2`、未来的 `0.2.x` 全部纳入可安装范围"，
并称 SDK 处于 `0.1.0b2` 阶段。**实测 PyPI 上该包的版本序列是**
`0.1.0b1/b2/b3` → `0.144.4` → `0.147.0` → `0.154.0` → `0.155.1`。

也就是说 `>=0.1.0` 实际会跳到 `0.155.1`（`0.x` 前缀的预发布版默认不参与解析），
**不是**原文推测的那个范围。原文"必须 pin 精确版本"的结论仍然正确 —— 只是理由不同：
真正的风险不是"装到 b1/b2"，而是**跨小版本就换掉了 CLI 二进制**。

现已锁 `openai-codex==0.154.0`（与 `openai-codex-cli-bin==0.154.0` 强绑定）。

### 9.2 修正：Python 3.14 的 wheel 可用性（原文 §2.5 的隐含担忧）

原文担心"架构覆盖"与二进制 wheel 可用性，但没料到 Ubuntu 26.04 上**只有 Python 3.14**。
实测结论：**3.14 完全可用，96 个依赖全部有 cp314 wheel，`pip` 解析零报错**。

关键几个：

| 包 | wheel |
| --- | --- |
| `onnxruntime 1.30.0` | `cp314-cp314-manylinux_2_28_x86_64` |
| `chromadb 1.5.9` | `cp39-abi3-manylinux_2_17_x86_64` |
| `numpy 2.5.3` | `cp314-cp314-manylinux_2_27_x86_64` |
| `orjson` / `grpcio` / `mmh3` / `tokenizers` | 均 cp314 |
| `openai-codex-cli-bin 0.154.0` | `py3-none-manylinux_2_17_x86_64`（127 MB） |

不需要为了部署去装 Python 3.12。

### 9.3 新增发现：**镜像源缺失 `openai-codex`**（原文完全没提到）

这是本次部署**实际卡住的唯一硬阻塞**，比原文列的任何一条都更先撞上：

| 镜像 | 是否有 `openai-codex` |
| --- | --- |
| `pypi.tuna.tsinghua.edu.cn` | ❌ **404，没有这个项目** |
| `mirrors.aliyun.com` | ✅ 有，最高 `0.154.0` |
| `pypi.org` | ✅ 有，最高 `0.155.1` |

用清华源会得到 `ERROR: Could not find a version that satisfies the requirement openai-codex==0.154.0 (from versions: none)` ——
**报错信息看起来像包不存在，实际是源的问题**，极易误判为网络故障而反复重试。
`deploy/install.sh` 现已默认走阿里云并内置回退到 pypi.org。

### 9.4 新增发现：`mcp>=1.9.0` 会装到 2.x 并炸掉 MCP server

原文 §1.2 只点出 `openai-codex` 的版本风险，**漏了 `mcp`**。实测 `mcp>=1.9.0` 会解析到 **2.2.0**，
而 mcp 2.x 把 `mcp.server.fastmcp` 删了 —— 该模块现在只剩一句 `raise ModuleNotFoundError`，
官方提示"FastMCP 已改名 MCPServer，或 pin `mcp<2`"。

`mcp_servers/media_tools_server.py` 是**模块级 import**，装到 2.x 会在启动瞬间崩，
且 `McpHub` 捕获异常后只打 warning → 表现为"服务起来了但工具少一半"，属于最难排查的那类故障。

现已锁 `mcp>=1.9.0,<2`（实测解析到 `1.30.0`）。

### 9.5 新增发现：bubblewrap 的**包名与二进制名不一致**

apt 包叫 `bubblewrap`，提供的二进制叫 **`bwrap`**。
用 `command -v bubblewrap` 判断安装状态会**永远判为缺失**。
`install.sh` 与 `verify.sh` 的初版都踩了这个坑，在依赖齐全的机器上误报"缺少 bubblewrap"。
现已统一改为检查 `bwrap`。

### 9.6 新增发现：`set -o pipefail` + `grep -q` 会让「服务已安装」误报为「未安装」

`verify.sh` 的初版用 `systemctl list-unit-files | grep -q "^rookie-agent.service"` 判断 unit 是否安装。
`grep -q` 一命中就关闭管道 → `systemctl` 收到 SIGPIPE(141) → pipefail 下整条管道判为失败。
**服务明明装好了，验收却报"未安装"。** 现已改为直接检测 unit 文件存在性。

> 同类隐患：`... | head -1` 也会 SIGPIPE 上游。`verify.sh` 里已一并清理。

### 9.7 仍然成立的风险（未修，需在运营中处理）

| 原文条目 | 现状 |
| --- | --- |
| §2.3 沙箱 fail-closed | 仍然成立。`bwrap` 已确认安装，但**沙箱真实能力边界必须在目标机跑 `scripts/probe_sandbox.py` 确认** |
| §2.5 首次运行需联网下载嵌入模型（~80 MB） | 仍然成立。离线环境需预置缓存 |
| §4.5 无备份 / 恢复 / 升级回滚流程 | 仍然成立。`deployment-linux.md` §8.3 给出了需备份的三条路径，但**没有自动化脚本** |
| §1.1 无 CI | 仍然成立。Linux 侧回归目前靠 `deploy/verify.sh` 手工触发 |

### 9.8 新增能力：自定义 Codex 端点（附一个必须知道的协议约束）

原文把"模型凭据"简化为"填 API Key 或复用登录态"两种，**漏了第三种也是最常见的一种：
指向自建/第三方端点**。现已支持，配置项：

```bash
AGENT_CODEX_BASE_URL=https://your-endpoint.example.com/v1
AGENT_CODEX_BASE_URL_API_KEY=
AGENT_CODEX_MODEL=<该端点认识的模型名>
```

**实现要点**（原评估没意识到 SDK 这一层的限制）：

``openai_codex.CodexConfig`` 里**没有** ``base_url`` 字段 —— 端点属于 Codex CLI 的
``model_providers`` 配置，不属于 SDK 的 Python 面。可行的通路是 SDK 的
``config_overrides``：其中每个元素会被拼成 ``codex --config <key=value>`` 传给 CLI。
已实现为 ``app/config.py`` 的纯函数 ``codex_provider_overrides()``，
密钥走 ``env_key`` + 子进程环境变量，**不进命令行参数**（否则 ``ps`` 里同机用户可见）。

> 🔴 **协议约束（实测，非文档推断）**：端点必须实现 OpenAI 的 **Responses API**
> 并且**支持 SSE 流式**。Codex CLI 0.154.0 已把 Chat Completions 协议整个移除：
>
> | `wire_api` | 实测结果 |
> | --- | --- |
> | `"chat"` | `` `wire_api = "chat"` is no longer supported. How to fix: set `wire_api = "responses"` `` |
> | `"chat_completions"` | `` unknown variant `chat_completions`, expected `responses` `` |
> | `"responses"` / 缺省 | 实际请求 `{base_url}/responses`，请求头 `accept: text/event-stream` |
>
> 后果：**只提供 `/v1/chat/completions` 的第三方中转无法使用**，哪怕它自称
> "OpenAI 兼容"。这类失败很隐蔽 —— 配置能通过、服务能起、provider 也切过去了，
> 只是每轮对话都超时。
>
> 为此新增 `scripts/probe_codex_endpoint.py`：实际发一个最小 Responses 请求，
> 按响应形态给诊断（404 = 没有 /responses 路径、Content-Type 非
> `text/event-stream` = 不支持流式、401/403 = 密钥问题）。
> **选端点前先跑它**，比配完再排查便宜得多。

验证方式：`smoke_test.py` 新增 9 项断言（总计 **42/42**），覆盖
"留空不产生覆盖项 / 切换 provider / 锁定 responses / 密钥不进 argv /
专用 key 回落 OPENAI_API_KEY / 保留 provider id 被拒 / URL 形状被拒"。
另外做过一次端到端实证：用本地 HTTP 服务器捕获 CLI 真实请求，
确认打到 `/v1/responses` 且 `Authorization` 带上了注入的密钥。

