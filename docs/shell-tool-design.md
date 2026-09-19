# 终端命令执行能力 —— 设计方案

> 目标：给当前 Codex Agent System 增加类似 Hermes Agent 的终端命令执行能力。
> 本文档只讲设计与改动清单，不含实现代码（实现按第 10 节分阶段推进）。
>
> **实现状态（2026-09-20）**：第 0 节列出的四个缺口**已全部落地并验证通过**
> （`scripts/smoke_exec.py` **65/65**、`scripts/smoke_test.py` **31/31**）。
> 其中原先设想的"策略引擎 + shell MCP server + backend 抽象"经核实属于**重复建设，已取消** ——
> 详见 §0.1 的实测证据。
> 仍未做的是第 10 节里的"按需"项：本地策略引擎与 Docker backend，
> 只有出现"必须比 Codex 沙箱更严"或"必须容器隔离"的真实需求时才值得动。

---

## 0. 结论先行

### 0.1 核实结论：**是，Codex 自带命令执行能力，不需要我们自己造执行器**

Codex Agent 内置一个 shell 命令执行工具，SDK 只是把它暴露出来。
**SDK 里没有"开关命令执行"的参数** —— 它是 agent 的核心能力，不是可选插件。

依据（按证据强度排序）：

| # | 证据 | 出处 |
| --- | --- | --- |
| 1 | app-server 的 `ThreadItem` 类型里有 **`commandExecution`**，定义为「**Sandboxed shell command**」，字段含 `command`（要执行的命令）、`cwd`（工作目录）、`processId`（PTY 进程号）、`status`（`inProgress` / `completed` / `failed` / **`declined`**）、`commandActions`、`aggregatedOutput`、`exitCode`、`durationMs`；并有专门的流式通知 `item/commandExecution/outputDelta` | Codex App Server API → Items |
| 2 | 共享 item 类型定义：「A ThreadItem represents a single unit of work within a turn — an agent message, **a command execution**, a file change, etc.」；`CommandExecutionItem` = 「**a shell command the agent ran**」；另有 `CommandExecutionStatus` 枚举 | `codex-codes` crate 类型文档 |
| 3 | `codex exec --json` 真实输出样本：`{"type":"item.started","item":{"type":"command_execution","command":"bash -lc ls","status":"in_progress"}}` —— 注意命令是 **login shell** | codex exec JSONL 参考 |
| 4 | Python SDK 的 `TurnResult.items: list[ThreadItem]`；流式事件中 `event.item.type == "command_execution"` 可直接取 `event.item.command` | SDK API 参考 + 流式示例 |
| 5 | 审批的对象就是命令：`ApprovalRequest` 携带 `ctx.request.command`，回调可返回 `Approved` / `Denied` | `codex-cli-sdk` 文档 |
| 6 | 配置层 `shell_environment_policy` 明确写「Codex spawns subprocesses (e.g. when executing a **`local_shell`** tool-call suggested by the assistant)」 | `openai/codex` docs/config.md |

**关键推论：`Sandbox` 预设的作用不是"允许/禁止执行命令"，而是规定命令能碰到什么。**
`read_only` 下 agent 照样能跑 `ls` / `git status` / `pytest`，只是不能写文件、不能联网。
Hermes 的 `terminal` 工具在这个层面**是重复建设**。

### 0.2 对原计划的修正

原计划里"阶段 1-4 造执行器（策略引擎 + shell MCP server + backend 抽象）"**基本不需要做**。
核对下来真正的缺口只剩四个，而且都比原方案小：

| 缺口 | 说明 | 代价 |
| --- | --- | --- |
| **1. `approval_mode` 我们没显式传** | `CodexBrain.thread_start()` 目前只传了 `sandbox`，用的是 SDK 默认值 `ApprovalMode.auto_review`。无人值守（Telegram）场景下没有真人可答审批，行为不可预期 —— 这是**必须先处理的正确性问题** | 小：显式传参 + 想清策略 |
| **2. 命令审计进不了我们的记忆层** | Codex 跑的命令不会主动告诉 MemPalace。但 **`TurnResult.items` 里本来就有 `commandExecution` 条目** —— 直接筛出来写进记忆即可，**零新增工具** | 小：约 30 行 |
| **3. 长驻进程管理** | Hermes 有 `terminal` + `process` 两个工具；Codex 只有前者。`npm run dev` 这类会等到超时 | 中：这是唯一还值得新建的模块 |
| **4. 非模型消费方的执行入口** | orchestrator 的健康检查、cron 巡检要跑命令，不该绕道模型 | 小：`asyncio.create_subprocess_exec` 20 行，不必包成 MCP tool |

原先设想的"策略引擎 + 审批流 + Docker/SSH backend"要降级为**按需**：
只有当出现"必须比 Codex 沙箱更严"或"必须跑在容器里"的真实需求时才做。

### 0.3 平台前提（重要）

**Codex 的沙箱实现是平台相关的，结论不能跨平台外推：**

| 平台 | 实现 | 备注 |
| --- | --- | --- |
| Linux | **bwrap + seccomp** | 部署目标，路径成熟 |
| macOS 12+ | Apple Seatbelt（`sandbox-exec -p`） | |
| Windows（原生） | Windows restricted token 沙箱 | 与上面两套完全不同的实现 |
| Windows + WSL2 | **走 Linux 实现** | 官方推荐的 Windows 路线 |

因此本项目的**探测与验证必须以部署目标（Linux）为准**。
`scripts/probe_sandbox.py` 会在报告头记录 `sys.platform`，但"沙箱能力矩阵"本身是平台局部的，
换平台必须重跑。第 10 节的阶段 0 相应改为「**在部署目标机上**跑一次探测」。

---

## 1. Hermes 的做法（作为参照系）

| 概念 | Hermes 的实现 | 关键点 |
| --- | --- | --- |
| 一次性命令 | `terminal({ command })` | 返回 stdout / stderr / exit_code；**文件系统在调用之间持久** |
| 长驻进程 | `process({ action: start\|logs\|send\|wait\|kill })` | 把生命周期与单次调用解耦，否则 `npm run dev` 会等到超时 |
| 执行环境 | `BaseEnvironment` 抽象，6 个 backend：local / docker / ssh / singularity / modal / daytona | **"在哪执行"与"执行什么"解耦** |
| 危险命令 | 三级：allow（默认放行）/ ask（弹审批）/ **hardline blocklist（无论是否审批都拒绝）** | 硬黑名单不受 `--yolo` 影响 |
| 容器模式 | 容器 backend 下**跳过**危险命令检查 | 因为容器本身就是边界 |
| 审批治理 | `hermes approvals` 把审批历史 mining 成 allowlist 建议 | 让策略随使用演化，而不是一次写死 |
| 工具优先级 | "Reach for terminal last" —— `cat` 该用 read_file、`grep` 该用 search_files | 提示词层面的安全设计，成本极低 |

**从 Hermes 的设计里能读出一条核心判断：爆炸半径由 backend 决定，不由命令决定。**
`rm -rf node_modules` 在容器里无所谓，在一个有未提交改动的目录里是灾难 —— 同一条命令。

---

## 2. 当前系统已经具备什么（已核实）

### 2.1 Codex 侧：两条正交的控制轴

| 轴 | 取值 | 含义 |
| --- | --- | --- |
| `sandbox_mode` | `read-only` | **可执行 shell 命令**，不可写文件，不可联网 |
| | `workspace-write` | 工作区内可写；网络默认仍关闭（可用 `[sandbox_workspace_write] network_access = true` 打开） |
| | `danger-full-access` | 无沙箱（仅限一次性环境） |
| `approval_policy` | `untrusted` | 除内置信任命令（`ls`/`cat` 等）外每条命令都要审批 |
| | `on-request` / `on-failure` | 越出沙箱时才问 / 命令失败后才问 |
| | `never` | 从不提示，**沙箱是唯一护栏** |

OS 级实现：macOS Seatbelt（`sandbox-exec -p`）、Linux bwrap + seccomp、Windows 原生沙箱或 WSL2。
另有 `allow_login_shell = false` 这个硬化开关（禁用 login shell，正是为了避开 rc 文件陷阱）。
可用 `codex sandbox macos|linux|windows <cmd>` 直接探测沙箱边界。

**⚠️ 一个必须处理的坑：SDK 的 `approval_mode` 默认是 `ApprovalMode.auto_review`**
（已核实的签名默认值）。在 Telegram 这种**无人值守**场景下，"审批请求"没有真人可答，
会被 Auto-review 处理掉 —— 行为不可预期。

**内省实测结果**：SDK 的 `ApprovalMode` **只有两个成员** —— `auto_review` 与 `deny_all`。
（上表列的是 CLI / `config.toml` 的 `approval_policy` 取值，与 SDK 的 `ApprovalMode`
枚举**不是同一套**，别混用。）CLI 侧的 `never` / `untrusted` / `on_request`
**在 SDK 里不存在**，照搬官方文档写会踩空。

所以正确做法是显式传 **`deny_all`**：行为确定、fail-closed，让沙箱成为唯一边界，
而不是把决定权交给一个没人监督的"自动审查器"。

⚠️ 解析成员时**必须用 `isinstance(member, ApprovalMode)` 而不是 `hasattr`** ——
两个类都是 `str` 的子类，`hasattr(ApprovalMode, "capitalize")` 同样为 `True`，
一个拼错的配置项会静默拿到 str 的方法对象传进 SDK，然后报一个完全看不懂的 RPC 错误。

### 2.2 我们这层已有的可复用资产

| 资产 | 位置 | 复用方式 |
| --- | --- | --- |
| `ToolResult` / `ToolSpec.to_openai_tool()` | `app/contracts.py` | shell 工具直接返回 `ToolResult`，零改动 |
| `McpServerSpec` + "每 server 一个 worker task" | `app/mcp_hub.py` | 加一个 spec 即注册 shell server；**anyio 跨任务问题已解决** |
| `call_qualified()` 宽容解析 | `app/mcp_hub.py` | `mcp__shell__shell_run` / `shell__shell_run` / `shell_run` 都能调 |
| `media_tools_server.py` 骨架 | `mcp_servers/` | shell server 照抄 `_ok/_fail` 约定与结构 |
| `CloneSpec.sandbox` + `tools_allowlist` | `app/contracts.py` | **分身权限分级已经支持** —— `researcher` 不给 shell、`coder` 给 |
| `CloneSystem` 的超时 / 重试 / 信号量 | `app/clones.py` | 长命令超时与并发控制已就绪（但超时杀的是任务，**不是进程组**，见风险 5） |
| `MemoryGateway.remember()` + outbox | `app/memory.py` | 命令审计落库；写失败进 outbox 不丢 |
| `EmotionEvent("error")` 效果向量 | `app/emotion.py` | 命令失败 → 情绪收紧，无需新增情绪事件类型 |
| `ChannelPort` / `OutboundMessage` | `app/channel_telegram.py` | 审批提示与结果通过渠道发，不用碰渠道实现 |
| `env_list` / `env_bool` | `app/config.py` | 白黑名单从环境变量读，工具函数现成 |
| `check()` / `FakeHub` | `scripts/smoke_test.py` | 策略引擎单测直接挂进现有框架 |

**结论：新增工作的 60% 是"接线"，不是"造轮子"。**

---

## 3. 推荐架构

三层，与 Hermes 一一对应但收了一层（我们不需要 7 个 backend）：

```
              ┌──────────────────────────────────────────────┐
  模型 / 分身 │ shell_run(command, cwd?, timeout?)           │
  或本地代码   │                                              │
              └───────────────────┬──────────────────────────┘
                                  ▼
        ①  策略引擎  ShellPolicy   纯函数 · 无 IO · 可单测
            解析 argv → allow / ask / deny / hardline
                                  │
                    ask ──────────┴──────▶ 审批（渠道回问 /approve <id>）
                                  │
                                  ▼
        ②  执行后端  ShellBackendPort   可插拔 · 决定爆炸半径
            LocalBackend  asyncio.create_subprocess_exec
            DockerBackend docker run --rm -i -w /workspace
            SshBackend    ssh host -- argv
                                  │
                                  ▼
        ③  捕获与归一化  ShellResult
            stdout/stderr 分流 · 双阈值截断 · ANSI 剥离 · 编码兜底
                                  │
                                  ▼
            审计 → MemoryGateway.remember(room="shell-audit")
            失败 → EmotionEngine.appraise(EmotionEvent("error"))
```

**为什么不让模型直接拿 shell，而要自己包一层？** 四个理由：

1. **审计断了** —— Codex 内置 shell 的调用永远进不了我们的记忆层，"这个 agent 上周执行过什么"无法回答；
2. **策略不可控** —— 内置 shell 不是 MCP 工具，`enabled_tools` 管不到它；我们无法注入自己的判决逻辑；
3. **非模型消费方拿不到** —— orchestrator 的健康检查、cron 巡检、分身任务都需要执行命令，但不该绕道模型；
4. **跨 backend 不通** —— 想让命令跑在 Docker 里（复现环境 / 隔离），Codex 的 sandbox 给不了。

反过来也要认清：**我们这层是"减少误操作"，不是"阻止攻击"**。真正的边界永远是沙箱（见风险 1）。

---

## 4. 需要新增的模块

### 4.1 `app/shell_policy.py` ← 新增（最高优先级）

纯函数策略引擎，**无 IO、无副作用**，因此可以 100% 离线单测。这是整个设计里最该先写的一块。

```python
class Verdict(str, Enum):
    ALLOW = "allow"; ASK = "ask"; DENY = "deny"

@dataclass(frozen=True, slots=True)
class PolicyDecision:
    verdict: Verdict
    rule: str            # 命中的规则名，用于审计与提示
    reason: str          # 人类可读理由
    argv: tuple[str, ...]  # 解析后的 argv（审计与日志用）
    escalated_by: str | None = None   # 因何升级（metachar / path_jail / code_exec）

class ShellPolicyEngine:
    def __init__(self, cfg: ShellConfig) -> None: ...
    def parse(self, command: str) -> tuple[list[str], str | None]:
        """返回 (argv, 危险元字符)。posix 与 windows 分别处理，见风险 10。"""
    def decide(self, command: str, *, backend: str = "local") -> PolicyDecision:
        """判决顺序：hardline → deny → 元字符升级 → deny → ask → allow → 默认 ask"""
    def explain(self) -> dict[str, Any]:
        """把当前规则表吐出来，给 /status 展示，也让模型知道边界"""
```

判决顺序很重要（先 deny 后 ask，硬黑名单最先且不可被 backend 豁免）：

```
1. 命中 hardline              → DENY（无视 backend、无视审批）
2. 命中 denylist              → DENY
3. 命中 code_exec 类          → ASK  （python -c / node -e / find -exec / xargs / awk system()）
4. 含 shell 元字符            → ASK  （argv 级判定已失效）
5. 参数含越狱路径             → DENY （规范化后不在 allowed_roots 内）
6. backend == docker/ssh      → ALLOW（容器/远端本身就是边界，借鉴 Hermes）
7. 命中 allowlist 且无元字符   → ALLOW
8. 命中 asklist               → ASK
9. 其它                       → ASK  （fail-closed，默认不信任）
```

### 4.2 `app/shell_backend.py` ← 新增（阶段 3）

```python
class ShellBackendPort(Protocol):
    name: str
    async def run(self, argv: Sequence[str], *, cwd: Path, timeout_sec: float,
                  env: Mapping[str, str] | None) -> ShellResult: ...
    async def available(self) -> bool: ...

class LocalBackend:   # asyncio.create_subprocess_exec，绝不 shell=True
class DockerBackend:  # docker run --rm -i -w /workspace --network=none -v <cwd>:/workspace
class SshBackend:     # ssh -o BatchMode=yes host -- argv（注意：argv 要正确引用）
```

### 4.3 `mcp_servers/shell_server.py` ← 新增（阶段 2）

照抄 `media_tools_server.py` 的结构。工具规范：

| 工具 | 用途 | 输入 | 输出 |
| --- | --- | --- | --- |
| `shell_run` | 执行一次性命令 | `command`, `cwd?`, `timeout_sec?`, `stdin?` | `{ok, exit_code, stdout, stderr, truncated, duration_ms, policy}` |
| `shell_which` | 探测命令是否存在 | `program` | `{ok, found, path}` |
| `shell_policy` | 查询当前策略边界 | — | `{allow, ask, deny, hardline, backend, cwd}` |
| `process_start` / `process_logs` / `process_kill` | 长驻进程 | 见阶段 4 | 进程句柄 + 增量日志 |

`shell_policy` 这个"自描述"工具值得单独说：**让模型先知道自己能做什么，比让它盲试再被拒绝要高效得多，
也大幅降低"越界尝试"的诱因。**

---

## 5. 需要改动的现有模块

| 文件 | 改动 | 规模 |
| --- | --- | --- |
| `app/contracts.py` | 新增 `ShellRequest` / `ShellResult` / `PolicyDecision` / `ApprovalRequest` / `ApprovalOutcome`；新增 `ShellPort` / `ApprovalPort` / `ShellBackendPort` 三个 Protocol；同步 `__all__` | 中（+120 行） |
| `app/config.py` | 新增 `ShellConfig`（**`enabled` 默认 `False`**）并挂到 `AppConfig`；`load_config` 读取 | 小（+60 行） |
| `app/mcp_hub.py` | `McpServerSpec` 补 `enabled_tools` / `disabled_tools` 字段（Codex TOML 支持但我们现在没生成）；`from_app_config` 在 `shell.enabled` 时追加 shell server | 小（+20 行） |
| `app/orchestrator.py` | 新增审批流（`_pending_approvals` + `/approve` `/deny` 指令）；命令审计写入 `room="shell-audit"`；失败时 `appraise(error)`；`/status` 增加 shell 策略展示 | 中（+90 行） |
| `app/brain.py` | `TOOL_INSTRUCTIONS` 增加 shell 段（工具清单 + 使用纪律 + "能用专用工具就别用 shell"） | 小（+20 行） |
| `scripts/setup_codex_mcp.py` | `build_specs` 在 `shell.enabled` 时追加 shell；渲染时输出 `enabled_tools` 收窄工具面 | 小（+25 行） |
| `scripts/smoke_test.py` | 新增"策略引擎判决表"场景（可完全离线）；审批流场景 | 中（+80 行） |
| `README.md` | 新增"终端执行"章节，含策略表与风险说明 | 小 |
| `.env.example` | 新增 `AGENT_SHELL_*` 配置项 | 小 |

`app/clones.py` 与 `app/emotion.py` **零改动** —— `tools_allowlist` 已经在做分身级权限隔离，
`EmotionEvent("error")` 已经在做失败回灌。这是契约层设计生效的地方。

---

## 6. 关键环节设计

### 6.1 命令解析

**核心原则：一切基于 argv，不基于字符串正则。**

```python
argv = shlex.split(command, posix=(os.name != "nt"))
```

理由：`rm -rf` 的正则匹配会被 `rm  -rf`、`rm -r -f`、`${IFS}`、`r''m` 轻松绕过。
拿到 argv 之后，判定 `argv[0]`（命令名，取 basename 去掉 `.exe`）与参数列表，规则才可靠。

同时必须检测 **shell 元字符**：`;` `&&` `||` `|` `>` `>>` `<` `` ` `` `$(` `${` 换行。
出现即说明"这条命令不是一个 argv 序列"，argv 级判定不再成立 → **升级为 ASK**。

**绝不允许 `shell=True`**，除非整条命令命中 allowlist 且不含元字符。

### 6.2 执行

```python
proc = await asyncio.create_subprocess_exec(
    *argv,
    cwd=str(cwd),
    env=minimal_env,                     # 显式构造，不继承全环境（见风险 6）
    stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    start_new_session=True,              # 关键：独立进程组，超时可整组击杀
    creationflags=CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
)
```

三个必须做对的地方：

1. **`start_new_session=True`**（POSIX）／`CREATE_NEW_PROCESS_GROUP`（Windows）——
   否则超时只能杀掉直接子进程，`bash -c "sleep 1000 &"` 留下的孙进程会活着；
2. **stdout 与 stderr 必须并发读** —— 只读一个会因管道缓冲区写满而死锁，
   这是 subprocess 最经典的坑。用 `asyncio.gather` 或两个 task；
3. **显式最小环境** —— `PATH` / `HOME` / `LANG` / `TMPDIR` + 白名单变量，
   绝不把整个 `os.environ` 传下去（里面有 API Key）。

### 6.3 权限与安全限制

**四级判决**，规则表（`allow` / `ask` / `deny` / `hardline` 从配置读取）：

```toml
# hardline —— 无论是否审批、无论什么 backend 都拒绝
hardline = [
  "rm -rf /", "rm -rf /*", "rm -rf ~", "rm -rf $HOME",
  "mkfs", "wipefs", "fdisk", "parted", "shred /dev/",
  "dd if= of=/dev/", "chmod -R 777 /", "chown -R",
  ":(){ :|:& };:",
  "> /dev/sd", "curl|sh", "wget|bash",
  "~/.ssh", "~/.codex/auth.json", ".env", "id_rsa",
]

# ask —— 需要审批（默认）
ask = [
  "sudo", "runas", "su",
  "rm", "rmdir", "mv", "truncate", "shred",
  "git push", "git reset", "git clean", "git checkout",
  "pip", "npm", "yarn", "pnpm", "apt", "brew", "choco", "winget",
  "curl", "wget", "Invoke-WebRequest",
  "docker", "systemctl", "service", "taskkill", "kill", "pkill", "Stop-Process",
  "ssh", "scp", "rsync", "nc", "telnet",
]

# code_exec —— 等价于任意代码执行，必须 ask（很容易被漏掉）
code_exec = ["python -c", "python3 -c", "node -e", "perl -e", "ruby -e",
             "find -exec", "xargs", "awk", "bash -c", "sh -c", "powershell -c"]

# allow —— 只读、低风险，无需审批
allow = [
  "ls", "dir", "cat", "head", "tail", "wc", "file", "stat", "which", "where",
  "echo", "pwd", "date", "whoami", "env", "printenv", "sort", "uniq",
  "git status", "git log", "git diff", "git show", "git branch",
  "python --version", "node -v", "pip list", "npm ls", "df", "du", "free", "uname",
]
```

外加两条**结构性限制**（不是命令名单能表达的）：

* **路径越狱检查**：提取 argv 中所有绝对路径与含 `..` 的 token，`Path.resolve()`
  后必须落在 `allowed_roots`（默认 `var/clones/<id>` 与项目根）内，否则 `DENY`；
* **backend 例外**：`backend != "local"` 时跳过 ask 类检查（借鉴 Hermes 的容器判断），
  **但 hardline 永不豁免**。

### 6.4 输出捕获

* **双阈值**：进程级 `max_capture_bytes`（默认 256 KB，读到即 kill）+ 返回级
  `max_output_chars`（默认 8000/流，截断并附 `...[truncated N bytes]` 提示）。
  只做返回级截断不够 —— `yes` / `cat /dev/urandom` 会在截断之前就把内存吃光；
* **ANSI 剥离**：`re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)`，否则日志不可读、浪费 token；
* **编码兜底链**：`utf-8` → `gbk`（Windows 中文控制台）→ `latin-1`，
  一律 `errors="replace"`。Windows 上 PowerShell 重定向产生的 UTF-16 也要识别；
* **二进制检测**：头部含 `\x00` 即判定二进制，不返回内容只返回 `{binary: true, bytes: N}`。

### 6.5 错误处理

**工具内部永不抛异常**（沿用现有约定：MCP 的 `isError` 会中断模型的工具调用循环）。
但要把四种"错误"区分开，因为模型对它们的反应完全不同：

| 类别 | 返回 | 模型应该做什么 |
| --- | --- | --- |
| 非零退出 | `ok=true, exit_code=1` | 命令跑了但失败，读 stderr 自行修正 |
| 超时 | `ok=false, error_code="timeout"` | 换更快的方式，或改用长驻进程 |
| 启动失败 | `ok=false, error_code="not_found"` | 命令不存在，别重试同一串 |
| 策略拒绝 | `ok=false, error_code="denied", reason=..., alternatives=[...]` | 换允许内的等价做法 |

**关键**：非零退出必须是 `ok=true`。把 `grep` 没匹配到（exit 1）当成"工具故障"会让模型
无谓地重试。同时把退出码翻译成人类可读提示：Unix 的 `126`（无执行权限）、
`127`（命令未找到）、`128+N`（被信号 N 终止），Windows 的 `9009`、`-1`。

`denied` 一定要带 `alternatives`：例如 `cat` 被拒就提示"用 `shell_run("head -100 file")`
或直接读文件"。**拒绝要给出路，否则模型会绕**。

---

## 7. 值得借鉴的 Hermes 设计（按价值排序）

1. **爆炸半径由 backend 决定，不由命令决定。** 这条决定了整个架构的重心应该放在 backend 选择上，
   而不是把精力全花在命令正则上。对应到我们：`local` 走严格策略，`docker`/`ssh` 放宽。
2. **hardline blocklist 独立于审批**。把"绝不能做"和"需要确认"分开，且硬黑名单不受
   `--yolo` 影响 —— 这个分层比单一黑名单健壮得多。
3. **容器内跳过危险检查**。承认容器本身是边界，避免"沙箱里再套一层审批"的形式主义。
4. **`terminal` / `process` 分离**。这是我们在 Codex 里**真正缺失**的能力。
   长驻进程（dev server、watch 任务）用一次性调用会等到超时，必须把生命周期从单次调用里解耦。
5. **"Reach for terminal last"**。把"能用专用工具就别用 shell"写进工具描述。
   成本几乎为零，收益是显著减少越界尝试 —— 对提示词工程出身的你来说这是最顺手的一条。
6. **审批历史 mining 成 allowlist 建议**（`hermes approvals`）。跑一段时间后统计"哪些 ask
   被 100% 批准"，输出成建议清单，让策略随使用演化而不是一次写死。
7. **非交互式 shell 的初始化防御**。Hermes 文档明确要求 `.bashrc` 顶部加
   `case $- in *i*) ;; *) return;; esac` 守卫。原因：nvm 之类的初始化会让每次命令多几百毫秒，
   而 TTY 交互代码会让命令永久挂起。Codex 侧的等价开关是 `allow_login_shell = false`。
8. **凭据防火墙（`hermes egress`）**。远程沙箱出站时注入凭据而不是把 Key 放进沙箱 ——
   如果我们做 Docker backend，这是下一步该考虑的方向。

---

## 8. 风险与注意事项（按严重度排序）

**1. 正则匹配危险命令是纸糊的防线 —— 这是最重要的一条。**
`rm -rf` 的名单挡不住 `find . -delete`、`python -c "import shutil;shutil.rmtree('.')"`、
`base64 -d <<< ... | sh`、`${IFS}` 拼接、自定义脚本文件。
**因此：真正的边界必须是沙箱**（Codex 的 Seatbelt/bwrap，或 Docker backend），
策略引擎的定位是"降低误操作概率"而非"抵御攻击"。文档与代码注释都要写死这个定位，
否则会给人虚假的安全感，反而更危险。

**2. `shell=True` 绝对不能开**（除非命令命中 allowlist 且已确认无元字符）。命令注入第一入口。

**3. 路径越狱只能粗筛。** `cwd` 约定在 `var/clones/<id>`，但命令里 `cd /` 就跑了；
`path_jail` 只检查 argv 里字面出现的路径，绕过方式极多。同风险 1，靠沙箱兜底。

**4. 输出爆炸会同时打爆内存和上下文。** `yes`、`find /`、`cat /dev/urandom`。
必须有进程级阈值（不是只做返回级截断）。

**5. 超时必须杀整个进程组。** 否则 `bash -c "sleep 1000 &"` 的孙进程会存活成僵尸。
现有 `CloneSystem` 的 `asyncio.wait_for` **只取消协程，不会杀子进程** —— 这是接线时最容易漏的一处。

**6. 环境变量泄漏。** 子进程默认继承全环境 → API Key 会进入命令作用域，
一条 `env` 就能把 Key 打出来回传到聊天里。必须显式构造最小环境。

**7. 提示词注入 → 命令执行。** 图片 OCR 结果、上传文档内容、记忆召回内容都会进 prompt，
恶意内容可以诱导模型执行命令。这与风险 1 是同一根问题 —— **边界靠沙箱，不靠"模型会不会听话"**。

**8. Telegram 渠道的授权面会被放大。** 现有 `TelegramConfig.is_allowed()` 的语义是
**空集合 = 不限制**。配上 shell 能力就等于"任何人可执行任意命令"。
必须在 `shell.enabled = true` 时**强制要求** `TELEGRAM_ALLOWED_CHAT_IDS` 非空，
启动时校验并拒绝启动（fail-closed），而不是打条 warning。

**9. 审计日志本身可能泄密。** 命令里可能带密钥（`curl -H "Authorization: Bearer sk-..."`）。
写进 MemPalace 之前必须脱敏：对 `sk-`/`ghp_`/`Bearer`/`password=`/`token=` 等模式做掩码。

**10. Windows 差异（我们实际会遇到）。**
* `shlex.split(posix=True)` 会错误处理反斜杠路径 —— Windows 上要么 `posix=False` 单独处理，
  要么统一走 Git Bash 并显式 `posix=True`；
* 本机 PowerShell 环境里 `dirname`/`head` 都缺失（这次搭建时已踩到），
  命令能不能跑取决于 shell —— 所以 `shell_which` 探针工具不是可选的；
* UTF-16 输出（PowerShell 重定向的典型产物）必须转码而不是当成二进制拒绝。

**11. 审计写入不能阻塞主流程。** 沿用现有做法（`asyncio.create_task`），
但 `ShellPort` 需要能拿到 `tool_call_id` 把"调用"与"结果"关联起来，否则审计日志是断的。

---

## 9. 配置草案

```bash
# .env —— 默认全部关闭，启用需要显式打开
AGENT_SHELL_ENABLED=false
AGENT_SHELL_BACKEND=local            # local | docker | ssh
AGENT_SHELL_CWD=./var/shell          # 默认工作目录（会被关进 path jail）
AGENT_SHELL_ALLOWED_ROOTS=./var/shell,./var/clones
AGENT_SHELL_TIMEOUT_SEC=180
AGENT_SHELL_MAX_CAPTURE_BYTES=262144
AGENT_SHELL_MAX_OUTPUT_CHARS=8000
AGENT_SHELL_APPROVAL_MODE=chat       # chat | deny | auto（默认 chat：渠道回问）
AGENT_SHELL_ALLOWLIST=
AGENT_SHELL_ASKLIST=
AGENT_SHELL_DENYLIST=
AGENT_SHELL_AUDIT_TO_MEMORY=true
```

`ShellConfig.enabled` 默认 `False` 是刻意的：**新增能力默认关闭，开启是一个显式动作。**

---

## 10. 分阶段落地计划

| 阶段 | 内容 | 风险 | 验收标准 |
| --- | --- | --- | --- |
| **0** | **在部署目标机（Linux）上**跑 `scripts/probe_sandbox.py`，产出**该平台**的沙箱能力矩阵 | 零 | 报告里能读出「`read-only` 下能做/不能做什么」 |
| **1** | `CodexBrain` 显式传 `approval_mode`（**正确性修复**）；把 `TurnResult.items` 里的 `commandExecution` 筛出来写进 MemPalace 审计房间 | 小（~30 行） | 跑一次含命令的任务，`/memory` 能查到命令与 `exit_code` |
| **2** | `TOOL_INSTRUCTIONS` 增加「能用专用工具就别用 shell」+「危险命令先说明再执行」的纪律 | 小 | 越界尝试的观测次数下降 |
| **3** | （按需）`app/process_manager.py`：长驻进程 `start` / `logs` / `kill` | 中 | 起一个 dev server，poll 到日志，kill 干净无残留 |
| **4** | （按需）本地策略引擎 / Docker backend | 中 | 仅当「Codex 沙箱不够严」或「必须容器隔离」成为真实需求时才做 |

**阶段 1-2 才是真正该做的全部。** 阶段 3-4 属于"出现了才做"，不是路线图承诺。

### 阶段 0 的具体操作（在部署目标机上执行）

```bash
pip install openai-codex        # 自带 CLI runtime（Linux 版二进制）

python scripts/probe_sandbox.py --list                # 先看探针清单
python scripts/probe_sandbox.py --calibrate-only      # 只做调用形式自标定，秒级出结果
python scripts/probe_sandbox.py                        # 完整探测 → 生成报告
python scripts/probe_sandbox.py --skip-outside-writes   # 最保守，不测工作区外的写
```

**这一步不需要 Codex 账号** —— `codex sandbox` 只是沙箱包装器，不触发模型调用。
脚本按 `CODEX_BIN` → `PATH` → 解释器同级 → `site-packages` 四种方式定位可执行文件。

两个实测踩出来的注意事项（脚本已内建处理）：

* **不要假设 platform 子命令存在。** 官方文档写的是
  `codex sandbox macos|linux|windows <cmd>`，但实测 0.154.0 的 help 只有
  `Usage: codex sandbox [OPTIONS] [COMMAND]...` —— 传进去的 `windows` 会被当成
  **要执行的命令名**，报出完全误导人的 `CreateProcessAsUserW failed: 2 系统找不到指定的文件`。
  因此脚本不判断版本，而是逐个候选形式实跑一条无害命令、取第一个成功的（自标定）。
* **不要在另一个沙箱里跑它。** 脚本自身若被上层沙箱包裹，受限令牌叠加嵌套执行会失败并报
  `Unable to create process using ...`。脚本会把这一失败模式与"平台沙箱不可用"区分诊断，
  避免把"跑错地方"误判成"平台不支持"。

四个必须重点看结论的探针：

| 探针 | 若结果是「允许」，意味着 |
| --- | --- |
| `read_secrets` | `.env` / `~/.codex/auth.json` 对 agent 完全暴露 → 密钥必须靠子进程环境隔离，不能指望沙箱 |
| `write_project_root` | agent 能修改自己的源码 → 必须靠 Docker backend 或独立工作目录隔离 |
| `net_outbound` | 数据外传通道开着 → 提示词注入的危害等级上升 |
| `env_secrets` | 子进程继承了凭据类环境变量 → 一条 `env` 就能把 Key 带出来 |

判定口径（脚本会自动分档）：`PROBE_OK`=允许；`PROBE_DENIED`=命令跑了但被 OS 拦；
三哨兵皆无且退出码非零=**沙箱根本没让命令启动**（最强的拒绝形态）；
`PROBE_ERROR`=失败与沙箱无关。把后两者混为一谈会误判边界。

**建议：阶段 0 先做，因为它可能直接砍掉阶段 1-4 一半的工作量。**
先测量，再建设。

---

## 11. 一句话总结

Hermes 的终端能力强大不在于"能跑命令"，而在于**它把"在哪跑"（backend）、
"能不能跑"（审批）、"跑了什么"（审计）三件事拆成了独立可替换的三层**。
我们现在已经有了 Codex 的第一层（OS 沙箱）和一个成熟的工具挂载层（`McpHub`），
缺的是第二层的本地策略和第三层的审计闭环，以及真正被漏掉的"长驻进程"形态。
