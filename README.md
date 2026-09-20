# Codex Agent System

**v0.1.0** · <https://github.com/Rrrker/rookie-agent>

一个基于 **Codex Agent SDK** 搭建的完整 Agent 系统：长期记忆 + Telegram 渠道 + 分身调度 +
PAD 情绪 + 多模态路由 + MCP 工具挂载。

六个模块彼此只依赖 `app/contracts.py`，实现类之间互不 import —— 任何一个模块都能被替换成
Mock。两个离线测试正是靠这一点验证架构：**`smoke_test.py` 42/42**（架构层）、
**`smoke_exec.py` 65/65**（执行层），全程不需要 MemPalace、Telegram 或 Codex 账号。

---

## 0. 一分钟跑起来

```bash
git clone https://github.com/Rrrker/rookie-agent.git
cd rookie-agent
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# ① 离线验证：不装任何外部依赖也能跑通全链路（用桩替换工具/渠道/模型）
python scripts/smoke_test.py    # 架构层 42/42
python scripts/smoke_exec.py    # 执行层 65/65

# ② 把识图/STT/TTS/记忆 四个 MCP server 挂载进 Codex（只追加，不重写你的配置）
python scripts/setup_codex_mcp.py --print     # 先看一眼
python scripts/setup_codex_mcp.py

# ③ 初始化记忆宫殿
mempalace init ~/.mempalace/palace

# ④ 配置并启动
cp .env.example .env      # 填 TELEGRAM_BOT_TOKEN 与 API Key
python main.py --console  # 先用终端聊，通了再接 Telegram
python main.py            # 正式启动
```

---

## 1. 先说清楚两个技术底座的真实形态

这两条是**动手前必须核实过的事实**，它们直接决定了架构长什么样。

### 1.1 Codex Python SDK 不提供"工具注册"接口

`pip install openai-codex`（Python ≥ 3.10）控制的不是进程内的 LLM，而是一个本地
Codex app-server 进程：

```python
from openai_codex import AsyncCodex, Sandbox

async with AsyncCodex() as codex:
    thread = await codex.thread_start(model="gpt-5.6-terra", sandbox=Sandbox.workspace_write)
    result = await thread.run("...")
    print(result.final_response)     # TurnResult: final_response / items / usage / status
```

关键结论：

| 事实 | 后果 |
| --- | --- |
| SDK 里**没有** `tools=` 参数，也没有工具注册 API | 给主脑挂工具的唯一途径是写 Codex 自己的 `~/.codex/config.toml` |
| 工具通过 `[mcp_servers.<key>]` 挂载 | 模型侧看到的工具名是 `mcp__<key>__<tool>`，server key 参与命名所以不会冲突 |
| `thread` 是**有状态的**，服务端保存历史 | 会话记忆 = thread_id，应持久化后用 `thread_resume()` 接回，而不是把历史塞进 prompt |
| 线程不能被并发写 | 每个 session 一把 `asyncio.Lock` |

→ 于是"工具挂载"天然有两个消费方：**应用自己**（`app/mcp_hub.py`）和 **Codex 主脑**
（Codex 自己拉起 server）。`scripts/setup_codex_mcp.py` 从同一份 `McpServerSpec` 生成配置，
保证两边看到同一份工具定义。

### 1.2 MemPalace 有两套访问面，读写同一份磁盘数据

| 访问面 | 用什么 | 适用场景 |
| --- | --- | --- |
| 进程内 Python API | `mempalace.searcher.search_memories`<br>`mempalace.knowledge_graph.KnowledgeGraph`<br>`mempalace.layers.MemoryStack` | 高频调用，省掉子进程往返 |
| MCP Server | `python -m mempalace.mcp_server`（29 个工具） | 给别的 agent 用；也是官方公开的写入通道 |

`MemoryGateway` 按能力选路径：**抽屉写入走 MCP**（官方 Python API 未公开写入函数），
**检索 / 知识图谱 / 唤醒走进程内**（快）。两条路写的是同一个 `palace/` 与
`knowledge_graph.sqlite3`，混用是安全的。

### 1.3 数据 Schema

**drawer（向量库，collection = `mempalace_drawers`）**

| 字段 | 说明 |
| --- | --- |
| `text` | 逐字原文，**不摘要**（MemPalace 96.6% R@5 正是靠原文直存） |
| `wing` | 翼（人 / 项目） |
| `room` | 房间（主题） |
| `source_file` / `added_by` | 来源追溯 |

**entity（`knowledge_graph.sqlite3` → `entities`）**：`id`(小写规范化名) / `name` / `type` / `properties`

**triple（`knowledge_graph.sqlite3` → `triples`）**

| 字段 | 说明 |
| --- | --- |
| `subject` / `predicate` / `object` | 主语 → 谓语 → 宾语 |
| `valid_from` / `valid_to` | 时间窗；`valid_to IS NULL` = 至今有效 |
| `confidence` | 0.0 ~ 1.0 |
| `source_closet` | 回指对应 drawer，事实与原文可追溯 |

事实变更用 `supersede()`（一个事务内关旧开新，交接点不重叠），事实结束用 `invalidate()`。

---

## 2. 系统总体架构

```
                        ┌──────────────────────────────────────┐
   Telegram ───────────▶│  ① 消息渠道  ChannelPort              │
   (文本/图/语音/文档)     │     TelegramChannel / ConsoleChannel  │
                        └──────────────┬───────────────────────┘
                                       │ InboundMessage（含已落盘的 MediaRef）
                                       ▼
      ┌────────────────────────────────────────────────────────────────┐
      │                    编排层  Orchestrator                          │
      │  情绪评估 → 多模态路由 → 记忆召回 → 情绪指令 → 主脑 → 语音合成     │
      │                                            → 出站 → 异步落库     │
      └──┬─────────┬─────────┬─────────┬─────────┬─────────┬───────────┘
         │         │         │         │         │         │
         ▼         ▼         ▼         ▼         ▼         ▼
   ┌─────────┐┌─────────┐┌────────┐┌────────┐┌────────┐┌───────────┐
   │②记忆层   ││④情绪系统 ││⑤多模态  ││⑥工具挂载││③分身调度 ││  主脑      │
   │Memory   ││PadEmo-  ││路由     ││McpHub  ││Clone   ││ CodexBrain │
   │Gateway  ││tionEngine││Media-  ││        ││System  ││            │
   │         ││         ││Router  ││        ││        ││            │
   └────┬────┘└─────────┘└───┬────┘└───┬────┘└───┬────┘└─────┬─────┘
        │                    │         │         │           │
        ▼                    ▼         ▼         ▼           ▼
  MemPalace            vision/stt/tts  MCP     任务队列    AsyncCodex
  Chroma + SQLite      MCP 工具        stdio    worker池    (app-server)
                                        │
                                        ├── mcp_servers/media_tools_server.py
                                        └── python -m mempalace.mcp_server
```

**依赖方向严格单向**：

```
contracts / config  ← 所有模块都依赖，且只依赖这两个
      ↑
memory / emotion / mcp_hub / media_router
      ↑
brain / clones
      ↑
orchestrator
      ↑
main.py   （只有它知道所有具体实现类）
```

---

## 3. 模块边界与交互接口

| 模块 | 实现类 | 对外端口（`app/contracts.py`） | 依赖谁 |
| --- | --- | --- | --- |
| ① 消息渠道 | `TelegramChannel` / `ConsoleChannel` | `ChannelPort` | 无 |
| ② 记忆层 | `MemoryGateway` | `MemoryPort` | `ToolPort`（仅写入） |
| ③ 分身调度 | `CloneSystem` | `SchedulerPort` | `IsolatedRunnerPort` |
| ④ 情绪系统 | `PadEmotionEngine` | `EmotionPort` | 无 |
| ⑤ 多模态路由 | `MediaRouter` | `MediaPort` | `ToolPort` |
| ⑥ 工具挂载 | `McpHub` | `ToolPort` | 无 |
| 主脑 | `CodexBrain` | `BrainPort` + `IsolatedRunnerPort` | `ToolPort` |

核心端口签名（完整定义见 `app/contracts.py`）：

```python
class ChannelPort(Protocol):
    async def start(self, handler: InboundHandler) -> None: ...
    async def send(self, msg: OutboundMessage) -> None: ...

class MemoryPort(Protocol):
    async def remember(self, write: MemoryWrite) -> str: ...
    async def recall(self, query, *, wing=None, room=None, k=5) -> list[MemoryRecord]: ...
    async def wake_up(self, wing=None) -> str: ...                     # L0/L1 唤醒
    async def assert_fact(self, triple: Triple) -> None: ...
    async def supersede_fact(self, s, p, *, old_obj, new_obj, at) -> None: ...
    async def facts_about(self, entity, *, as_of=None) -> list[Triple]: ...

class EmotionPort(Protocol):
    def appraise_text(self, text: str) -> EmotionState: ...
    def style_directive(self) -> str: ...        # PAD → 语气指令
    def voice_params(self) -> dict[str, float]: ...  # PAD → TTS 参数

class ToolPort(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...
    async def call(self, server, tool, arguments) -> ToolResult: ...
    async def call_qualified(self, qualified_name, arguments) -> ToolResult: ...
    def openai_tools(self, allowed=None) -> list[dict]: ...

class IsolatedRunnerPort(Protocol):
    async def run_isolated(self, prompt: str, *, clone: CloneSpec) -> str: ...
```

**三条硬规则**（写在 `app/contracts.py` 顶部）：

1. 跨模块只传 contracts 里的 dataclass，不传裸 dict；
2. 模块之间只依赖 Protocol，实现类互不 import；
3. 接口变了先改 contracts，再改实现。

`IsolatedRunnerPort` 是个好例子：由 `CodexBrain` 实现、由 `CloneSystem` 消费，
依赖方向是 `clones → 端口 ← brain`，于是分身调度完全不需要知道底层是 Codex。

---

## 4. 六大模块设计说明

### 4.1 记忆层 —— `app/memory.py`

**一句话**：把 MemPalace 的原始返回结构收敛成 `MemoryRecord` / `Triple`，
上层不需要知道 drawer 和 triple 的区别。

要点：

* **读写接口**：`remember()` / `recall()` / `wake_up()` / `assert_fact()` /
  `invalidate_fact()` / `supersede_fact()` / `facts_about()` / `timeline()` / `stats()`；
* **组合接口** `build_context()`：把 L0/L1 唤醒 + 语义召回 + 知识图谱事实拼成一段
  可注入主脑的上下文。这是记忆层对外的**唯一读接口**；
* **两级降级**：没装 `mempalace` → 知识图谱走只读 SQLite 直读（schema 固定，不写）；
  MCP 写入失败 → 落 `var/memory_outbox.jsonl`，`flush_outbox()` 可补投，**不丢数据**；
* `remember_turn()` 把一轮对话落成两条逐字 drawer（用户侧 + 助手侧），刻意不摘要。

### 4.2 消息渠道 —— `app/channel_telegram.py`

**一句话**：只做三件事 —— 收、落盘、发。不做任何语义判断。

* Telegram 各种 message 形态 → `InboundMessage`；远端 `file_id` 下载成本地文件，
  上层只认本地路径（于是 vision/stt 完全不需要知道 Telegram 存在）；
* **4096 字符上限**在渠道层分片，不让上层回复长度受制于渠道；
* 发送前先发 typing 指示 —— 主脑一轮十几秒，没有反馈用户会以为机器人死了；
* `ConsoleChannel` 是第二个实现，用来证明"加渠道不用改 orchestrator"。

### 4.3 定时任务：分身系统 —— `app/clones.py`

**什么是分身**：不是"再起一个模型进程"，而是同一套 runtime 上的一份隔离执行上下文。
隔离体现在四个正交维度：

| 维度 | 隔离手段 | 解决什么问题 |
| --- | --- | --- |
| 上下文隔离 | 每个任务一条独立 Codex thread（`ephemeral`） | 长任务历史不污染主对话 |
| 文件系统隔离 | 每个分身独立 `workspace_dir` | 写文件不越界 |
| 工具面隔离 | `tools_allowlist` 白名单 | 最小权限 |
| 记忆隔离 | 每个分身写自己的 `wing` | 领域知识不串味 |

**任务隔离为什么重要**（这是设计优势，不是装饰）：

1. **故障不外溢** —— 一个分身卡死/超时/报错只影响它自己那条任务，主对话毫发无伤
   （因为压根不在同一条 thread 上）；
2. **可重投递** —— `CloneTask` 不持有任何"属于某个分身"的字段，失败任务可以原样丢回
   队列换一个分身重跑。若任务持有执行者状态，这一步做不到；
3. **权限可分级** —— 定时巡检给 `read_only`，只有明确要改代码的分身才给 `workspace_write`。
   权限跟着分身走，而不是跟着进程走。

**水平扩展为什么成立**：扩展单位是 **worker**（消费队列的协程）。
任务无状态 ⇒ 任意 worker 能处理任意任务，不存在任务亲和性约束；
分身隔离边界由 `cwd` / `tools_allowlist` 决定 ⇒ 扩容只需复制 `CloneSpec`。
真正的瓶颈（模型 API 速率）由两个信号量统一控制：全局 `max_parallel_tasks`
与单分身 `max_concurrency`，所以"多开 worker"不会把上游打爆。

**调度机制**（三层）：

```
优先级队列  →  worker 池  →  每分身信号量  →  带超时/重试的执行
 (priority)    (全局并发)     (单角色并发)     (timeout + retry)
```

* 优先级数字越小越先执行（交互=0，常规后台=5，定时巡检=8），保证交互不被后台饿死；
* 超时**不重试**（超时说明负载已满，重试只会加剧雪崩），仅 `FAILED` 重试；
* **内置极简 cron 解析器**（`分 时 日 月 周`，支持 `*` `*/n` `a,b` `a-b`），
  不引入 APScheduler。每 15 秒 tick 一次，**定时器只负责投递、不负责执行**，
  所以长任务不会堵住调度心跳。

### 4.4 情绪系统 —— `app/emotion.py`

**为什么用 PAD 而不是离散标签**：离散标签无法表达强度、无法插值、无法自然衰减。
PAD 把情绪放进连续三维空间，于是三件事都变成算术：

```
刺激:  state += gain × intensity × effect        # 一次事件 = 一个位移向量
衰减:  α = 1 − 0.5^(Δt / half_life)              # 指数回归，渐近不越界
       x ← x + (baseline − x) × α
语气:  state → style_directive() / voice_params()  # 两个纯函数映射
```

三维符号组合出 Mehrabian 八象限：

| (+P,+A,+D) | (+P,+A,−D) | (+P,−A,+D) | (+P,−A,−D) | (−P,+A,+D) | (−P,+A,−D) | (−P,−A,+D) | (−P,−A,−D) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 热情 exuberant | 亲和 dependent | 松弛 relaxed | 温和 docile | 冷峻 disdainful | 紧绷 anxious | 冷淡 hostile | 倦怠 bored |

三个关键设计决定（都是跑测试时踩出来的）：

1. **判据是"相对基线的偏移"而不是绝对值**。基线本来就偏正向（P=+0.10），
   按绝对值判会让情绪系统永远偏向正向象限；偏移量小于 `label_epsilon` 才判中性。
2. **负性偏差 `negativity_bias = 1.6`**（Baumeister et al., 2001）：同等强度下负面
   事件心理冲击大于正面事件。不设这个偏差，正向刺激会持续占优，情绪长期卡在正向。
3. **正常回复不产生正向情绪事件**。顺利应答是默认预期，不是值得高兴的事 ——
   最初版本每轮都记一次 `task_success`，结果三四轮内就顶到 clamp 并永久卡在
   exuberant，语气调节彻底失效。现在只有"出错/超时"和"后台分身任务结果"才影响情绪。

情绪如何真正改变输出：`style_directive()` 生成一段注入主脑的指令，
`voice_params()` 推导 TTS 语速（唤醒度越高越快）。
强度低于阈值时返回空串 —— 情绪不该每轮都扰动 prompt，否则语气指令会变成噪声。

### 4.5 多模态路由 —— `app/media_router.py`

声明式路由表，把"附件类型"映射到"模型能力"：

| 附件类型 | 路由目标 | MCP 工具 | 模型端点 |
| --- | --- | --- | --- |
| `IMAGE` | 视觉理解 | `media_tools/vision_describe` | `VISION_*` |
| `VOICE` / `AUDIO` | 语音转文本 | `media_tools/stt_transcribe` | `STT_*` |
| 输出侧 | 文本转语音 | `media_tools/tts_synthesize` | `TTS_*` |
| `DOCUMENT` | 不路由（落盘） | — | — |

* **三个模态各自独立配置端点**，可以识图用 A 家、语音用 B 家；
* `ingest()` 返回 `(notes, merged_text)`：`merged_text` 是语音转写结果，
  用它替代空文本，让主流程像处理纯文本一样处理语音；
* **同模态回复**：用户用语音提问就用语音回答，降低认知切换成本；
* Telegram `sendVoice` 只接受 OGG/Opus，`synthesize()` 内部用 ffmpeg 转码；
  没有 ffmpeg 就降级返回 mp3 走 `sendAudio` —— 功能不中断，只是气泡样式不同。

### 4.6 工具挂载 —— `app/mcp_hub.py` + `mcp_servers/media_tools_server.py`

**Tool 规范**（`media-tools` server 的对外契约）：

| 工具名 | 用途 | 输入 | 输出 |
| --- | --- | --- | --- |
| `vision_describe` | 图片理解 | `image_path`, `prompt?`, `max_chars?` | `{ok, model, text, elapsed_ms}` |
| `stt_transcribe` | 语音转文本 | `audio_path`, `language?`, `prompt?` | `{ok, model, text, language, duration_sec}` |
| `tts_synthesize` | 文本转语音 | `text`, `out_path?`, `voice?`, `speed?`, `fmt?` | `{ok, model, voice, path, bytes}` |
| `media_probe` | 媒体嗅探 | `path` | `{ok, suffix, bytes, mime, duration_sec}` |

加 `mempalace` server（29 个工具）后，模型侧一共能看到：

```
mcp__media_tools__vision_describe    mcp__media_tools__stt_transcribe
mcp__media_tools__tts_synthesize     mcp__media_tools__media_probe
mcp__mempalace__mempalace_search     mcp__mempalace__mempalace_add_drawer
mcp__mempalace__mempalace_kg_add     ...（共 33 个）
```

**错误约定**：工具内部**永不抛异常**，一律返回 `{"ok": false, "error": "..."}`。
因为 MCP 的 `isError` 会中断模型的工具调用循环，而"这张图读不出来"属于**业务结果**
而不是协议故障 —— 让模型看到错误并自行决定如何回复，比让整轮对话崩掉更合理。

**调用流程**：

```
Codex 主脑 ──┐
             ├─▶ [MCP stdio] ─▶ media_tools_server ─▶ OpenAI 兼容端点 (vision/stt/tts)
应用路由   ──┘                 └▶ mempalace.mcp_server ─▶ Chroma + SQLite
```

**并发正确性 —— 为什么每个 server 要有独立 worker task**：
MCP 官方 SDK 的 `ClientSession` 建在 anyio 的 task group 上，跨 task 调用会报
`Attempted to exit cancel scope in a different task`。常见做法是"全局 session + 一把锁"，
但那只是把并发退化成串行，且超时取消时仍会炸。

本实现用**每 server 一个专属 worker task**：worker 在自己的上下文里 `async with`
整个 stdio 生命周期，外部调用者只往 `asyncio.Queue` 投请求并 `await` 一个 Future。
所有 MCP IO 都发生在创建它们的那个 task 里，anyio 约束天然满足；不同 server 之间完全并行。

`call_qualified()` 宽容解析三种写法（`mcp__s__t` / `s__t` / 裸 `t`），
因为工具名有三个来源（模型输出、本应用路由代码、Codex 配置文件），
让它们共用同一个入口比要求所有调用方记住同一种拼法更不容易出错。

---

## 5. 一轮对话的完整数据流

```
Telegram 入站
   │
   ├─1─ 情绪评估  emotion.appraise_text(用户原话)      ← 先评估再回复，情绪才能影响本轮
   ├─2─ 多模态路由 media.ingest(附件)                  ← 图片→vision，语音→stt
   ├─3─ 记忆召回  memory.build_context(用户意图)        ← L0/L1 + 语义召回 + KG 事实
   ├─4─ 情绪指令  emotion.style_directive()            ← PAD → 语气要求
   ├─5─ 主脑推理  brain.reply(文本, 记忆, 情绪, 附件)   ← Codex thread（有状态，可 resume）
   ├─6─ 结果回灌  emotion（仅失败/超时，成功不回灌）
   ├─7─ 语音合成  media.synthesize()                   ← 仅当用户用语音提问
   ├─8─ 出站发送  channel.send()
   └─9─ 异步落库  asyncio.create_task(_persist)        ← 落在回复之后，不计入用户等待
```

**三段式 prompt 装配**（`CodexBrain._compose_input`）—— 按变化频率拆成三层：

| 内容 | 走哪条通道 | 变化频率 |
| --- | --- | --- |
| 人格与能力边界 | `base_instructions`（建线程时） | 几乎不变 |
| 工具使用规则 | `developer_instructions` | 极少变 |
| 记忆上下文 / 情绪指令 | run 输入里的 `<context>` 块 | 每轮都变 |

高频变化的内容不污染低频的指令层，Codex 服务端也能更好地复用线程状态。

---

## 6. 指令集

| 指令 | 作用 |
| --- | --- |
| `/status` | 各模块健康状态（主脑 / 工具 / 记忆 / 调度） |
| `/emotion` | 当前 PAD 值、象限、TTS 参数、最近情绪事件 |
| `/memory <内容>` | 语义检索长期记忆 |
| `/task <描述>` | 派发分身后台执行，完成后主动通知 |
| `/voice on\|off` | 切换语音回复 |
| `/reset` | 开新线程（长期记忆不变） |

---

## 7. 部署步骤

### 7.1 依赖

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 7.2 初始化记忆宫殿

```bash
pip install mempalace
mempalace init ~/.mempalace/palace
mempalace status
```

### 7.3 把工具挂载进 Codex

```bash
python scripts/setup_codex_mcp.py --print   # 审一遍生成内容
python scripts/setup_codex_mcp.py           # 写入 ~/.codex/config.toml
```

脚本的设计取舍：

* **只追加不重写** —— 已存在的 `[mcp_servers.x]` 段落跳过，你手写的其它配置一个字节不动；
  改前自动备份为 `config.toml.bak`；
* **密钥不落盘** —— API Key 用 `env_vars` 从环境变量转发，绝不写进 `config.toml`；
* 写入后在 Codex 里执行 `/mcp` 查看 server 状态。

也可以手工等价配置：

```toml
[mcp_servers.media_tools]
command = "python"
args = ["-m", "mcp_servers.media_tools_server"]
cwd = "/path/to/rookie-agent"
enabled = true
startup_timeout_sec = 20
tool_timeout_sec = 180
env_vars = ["VISION_API_KEY", "STT_API_KEY", "TTS_API_KEY", "VISION_BASE_URL", "STT_BASE_URL", "TTS_BASE_URL"]
```

### 7.4 配置并启动

```bash
cp .env.example .env       # 填 TELEGRAM_BOT_TOKEN / OPENAI_API_KEY / 各模态端点
python main.py --health-check        # 装配 + 自检后退出（0=健康 1=不健康 2=配置阻塞）
python main.py --dry-run --console   # 先验证装配
python main.py --console             # 终端对话
python main.py                       # 接 Telegram
```

启动参数：`--console`（终端渠道）、`--dry-run`（不调真实模型）、`--health-check`（自检后退出）、
`--log-level DEBUG`。

### 7.5 Linux 物理机 / 云主机部署（systemd 托管）

**不要**用 `nohup python main.py &` —— 那样 `systemctl stop` / 重启 / 崩溃自愈全都没有。
本仓库带一个惰性部署套件：

```bash
./deploy/install.sh          # 系统依赖 + venv + 目录 + .env + systemd unit（幂等，可重复跑）
nano .env                    # 填密钥
./deploy/verify.sh           # 部署验收：含 SIGTERM 优雅停机与孤儿进程检查
sudo systemctl enable --now rookie-agent
journalctl -u rookie-agent -f
```

`deploy/` 里的东西各管一件事：

| 文件 | 作用 |
| --- | --- |
| `install.sh` | 一键引导（apt 依赖 → venv → 目录 → `.env` → unit），会拒绝不满足的前提 |
| `verify.sh` | 验收脚本，专抓"不报错只静默失能"的故障 |
| `probe_runtime_api.py` | 运行时 API 探针：逐个校验**代码真正调用的外部符号**（34 项），抓"包还在但子模块/参数被删"的静默破坏 |
| `rookie-agent.service.in` | systemd unit 模板（`install.sh` 负责渲染占位符） |
| `env.production.example` | 生产环境配置模板（含每项的作用与坑） |
| `requirements.lock.txt` | 首次成功安装后由 `pip freeze` 生成，下次用它而不是范围约束 |

> ⚠️ **镜像源**：默认走阿里云。**不要用清华源** —— `pypi.tuna.tsinghua.edu.cn`
> 上不存在 `openai-codex`，会报 `Could not find a version ... (from versions: none)`，
> 看起来像包不存在，实际是源的问题。`install.sh` 内置了回退到 `pypi.org` 的逻辑。

完整运行手册（路径规划、权限、备份、升级回滚、故障排查）见
**[`docs/deployment-linux.md`](docs/deployment-linux.md)**。

### 7.6 Codex 登录

`CodexBrain` 自动复用已有 Codex 登录态。首次可：

```python
from openai_codex import Codex
with Codex() as codex:
    print(codex.login_chatgpt().auth_url)         # 浏览器登录
    # 或 device-code / API Key
```

### 7.7 自定义 Codex 端点

想指向自建网关 / 公司中转 / 本地推理服务，在 `.env` 里填三项即可：

```bash
AGENT_CODEX_BASE_URL=https://your-endpoint.example.com/v1
AGENT_CODEX_BASE_URL_API_KEY=...        # 留空则复用 OPENAI_API_KEY
AGENT_CODEX_MODEL=<该端点认识的模型名>
```

> 🔴 **端点必须实现 OpenAI Responses API 且支持 SSE 流式。**
> 请求打到 `{base_url}/responses`，请求头带 `accept: text/event-stream`。
>
> Codex CLI 0.154.0 已经把 **Chat Completions 协议整个移除**（实测）：
> `wire_api="chat"` → `is no longer supported`；
> `wire_api="chat_completions"` → `unknown variant, expected 'responses'`。
>
> 所以**只提供 `/v1/chat/completions` 的第三方中转配了也用不了** ——
> 配置能过、服务能起，只是每轮对话都失败。选端点前先跑：
>
> ```bash
> python scripts/probe_codex_endpoint.py --show-events
> ```

实现细节：SDK 的 `CodexConfig` 里**没有** `base_url` 字段，端点属于 Codex CLI 的
`model_providers` 配置，本项目通过 SDK 的 `config_overrides`
（拼成 `codex --config k=v`）传下去 —— 见 `app/config.py` 的
`codex_provider_overrides()`。密钥走子进程环境变量而非命令行参数，
避免出现在 `ps` 输出里。

---

## 8. 验证结果

`python scripts/smoke_test.py` —— 用桩替换工具层、渠道层与模型，验证架构与流程本身：

```
通过 42/42

场景 1｜对话流水线（情绪 + 记忆 + 主脑）
  输入            P       A       D      象限
  中性提问       0.10    0.00    0.00   neutral
  被赞扬         0.49    0.13    0.07   exuberant
  被指责         0.01    0.40   -0.06   anxious      ← P 从 +0.49 掉到 +0.01
  紧急+亲昵      0.11    0.80    0.07   exuberant
  玩笑           0.36    0.80    0.14   exuberant

场景 2｜衰减      半衰期后位移恰好走完一半（实测 +0.2960 / 期望 +0.2960）
场景 3｜多模态    图片→vision ✅  语音→stt ✅  语音提问→tts 语音回复 ✅
场景 4｜记忆层    语义召回 ✅  MCP 通道 ✅  KG 降级不抛异常 ✅  outbox 自愈 ✅
场景 5｜分身      3/3 任务完成 ✅  worker 池 ✅  cron 解析 ✅  非法表达式拒绝 ✅
场景 6｜指令集    /help /emotion /memory /status /task 全部有响应 ✅
场景 7｜自定义端点 base_url→CLI 覆盖项 ✅  锁 responses 协议 ✅  密钥不进 argv ✅  保留 id 被拒 ✅
```

---

## 9. 目录结构

```
rookie-agent/
├── main.py                          # 启动入口：依赖注入的唯一地方
├── app/
│   ├── config.py                    # 集中配置（frozen dataclass + 极简 .env）+ 启动前安全校验
│   ├── contracts.py                 # ★ 模块边界契约：数据 Schema + Protocol
│   ├── memory.py                    # ② 记忆层    MemoryGateway
│   ├── emotion.py                   # ④ 情绪系统  PadEmotionEngine
│   ├── media_router.py              # ⑤ 多模态    MediaRouter
│   ├── mcp_hub.py                   # ⑥ 工具挂载  McpHub
│   ├── channel_telegram.py          # ① 渠道      TelegramChannel
│   ├── channel_console.py           # ① 渠道      ConsoleChannel（第二个实现）
│   ├── brain.py                     # 主脑        CodexBrain（+ 命令 trace 出参）
│   ├── clones.py                    # ③ 分身      CloneSystem + CronExpr
│   ├── command_audit.py             # 执行层      命令抽取 / 脱敏 / 落库
│   ├── local_exec.py                # 执行层      白名单命令 + 输出捕获
│   ├── process_manager.py           # 执行层      长驻进程 start/logs/stop
│   └── orchestrator.py              # 编排层
├── mcp_servers/
│   └── media_tools_server.py        # 自建 MCP server（vision/stt/tts/probe）
├── scripts/
│   ├── setup_codex_mcp.py           # 把工具挂载进 ~/.codex/config.toml
│   ├── probe_sandbox.py             # 沙箱边界探测（不需要 Codex 账号）
│   ├── probe_codex_endpoint.py      # 自定义 Codex 端点自检（Responses + SSE）
│   ├── smoke_test.py                # 架构冒烟测试（42 项）
│   └── smoke_exec.py                # 执行层专项测试（65 项）
├── deploy/                          # Linux 部署套件（无容器化）
│   ├── install.sh                   # 一键引导：apt 依赖 → venv → 目录 → .env → unit
│   ├── verify.sh                    # 部署验收：含 SIGTERM 优雅停机与孤儿进程检查
│   ├── probe_runtime_api.py         # 运行时 API 探针（34 项，抓"包在但 API 变了"）
│   ├── rookie-agent.service.in      # systemd unit 模板
│   ├── env.production.example       # 生产环境配置模板
│   └── requirements.lock.txt        # 首次安装后由 pip freeze 生成（版本锁）
├── docs/
│   ├── deployment-linux.md          # ★ Linux 部署运行手册（路径/权限/备份/排障）
│   ├── linux-deployment-readiness.md # v0.1.0 Linux 上线可行性评估
│   ├── shell-tool-design.md         # 终端执行能力设计方案（含已核实的 SDK 事实）
│   └── (sandbox-probe-report.md)    # 本机生成、不入库：含绝对路径与平台安全画像
├── requirements.txt
└── .env.example
```

---

## 10. 已知边界与扩展点

| 项 | 现状 | 若需扩展 |
| --- | --- | --- |
| 情绪评估 | 加权关键词 + 提问句式，零延迟零成本 | 换成返回 `EmotionEvent` 列表的小模型调用，契约不变 |
| 实体抽取 | 用 Codex `output_schema` 结构化输出（默认关闭，多一次调用） | 开 `AGENT_EXTRACT_FACTS=true` |
| 事实抽取线程 | 每次抽取开一条 `ephemeral` 线程 | 可加批量抽取合并调用 |
| MCP 断线重连 | worker 断开后不自动重启 | 在 `_ServerWorker._run` 外层加退避重连 |
| 会话映射 | SQLite 单机 | 换 Redis 即可多实例 |
| 单机分身上限 | 受模型 API 速率限制 | 加分布式队列（Redis Streams）即可跨机 |

---

## 11. 执行能力：三条路径

**先说结论：Codex 自带 shell 执行，不需要我们自己造执行器。** 核实依据见
`docs/shell-tool-design.md` §0 —— app-server 的 `ThreadItem` 里就有 `commandExecution`
（定义为 "Sandboxed shell command"），Codex 跑过的每条命令都会出现在 `TurnResult.items` 里。

于是系统里存在**三条独立**的执行路径，约束完全不同 —— 这是最容易搞混的地方：

| 谁在跑 | 用什么 | 约束 | 默认 |
| --- | --- | --- | --- |
| **模型** | Codex 内置 shell 工具 | Codex 的 OS 级沙箱（macOS Seatbelt / Linux bwrap+seccomp / Windows 原生）+ `approval_mode` | 开启 |
| **我们的代码** | `LocalExecutor`（白名单命令） | **不受 Codex 沙箱约束** → 只允许登记过的命令 | 开启 |
| **我们的代码** | `ProcessManager`（长驻进程） | **不受任何沙箱约束**，权限等于当前进程 | **关闭** |

### 11.1 命令审计（零新增工具）

不拦截、不包装 —— 只是把 Codex 本来就产出的条目筛出来：

```python
trace: list[CommandTrace] = []
answer = await brain.reply(..., trace=trace)   # 出参，列表归调用方 → 天然无竞态
await auditor.record(session_id=..., traces=trace, file_changes=changed)
```

用**出参**而不是改返回类型，是为了不破坏 `BrainPort` 既有签名，
同时避免"实现方保存上一轮状态"那种跨会话竞态。

落库前**强制脱敏**（`sk-` / `ghp_` / `Bearer` / `token=` / `password=`）——
命令里经常带凭据，原样入库等于把密钥永久留在磁盘上。用 `/audit <关键词>` 检索。

### 11.2 内部执行入口（非模型消费方）

`LocalExecutor` 只暴露 `run_named()`，命令必须先登记进 `NAMED_COMMANDS`
（`git_status` / `git_last_commit` / `git_diff_stat` / `python_version` / `platform_probe`）。

**这不是安全边界**，而是阻止内部代码在演进中悄悄长成任意执行入口 ——
加命令必须显式登记，这个动作本身会让人停下来想清楚。
orchestrator 每 5 分钟用它跑一次 `git_status` 探针（不经过模型、不产生对话），
结果缓存给 `/status` 展示。

输出捕获的四条纪律（都是实测踩出来的）：

* **双阈值** —— 返回级截断 + 进程级上限。只做返回级截断不够，
  `yes` / `cat /dev/urandom` 会在截断之前先把内存吃穿；
* **并发读双流** —— 只读 stdout 不读 stderr 会因管道缓冲区写满而**死锁**；
* **ANSI 剥离** —— 否则日志不可读还白烧 token；
* **编码兜底链** —— UTF-16 BOM → UTF-8 → GBK → latin-1（Windows 中文控制台是 GBK）。

### 11.3 长驻进程

Codex 只有一次性命令，没有 Hermes 的 `process`（start/logs/kill）—— 
这是四个缺口里**唯一值得新建模块**的一个。`ProcessManager` 提供
`start` / `logs` / `stop` / `list` / `stop_all` / `reap`，带日志环形缓冲（按字节预算裁旧）
与进程组终止（POSIX `killpg` / Windows `taskkill /T`）。

```
/proc start --name web npm run dev    →    /proc logs web 80    →    /proc stop web
```

一个实测发现的坑：**读取端在管道 EOF 就返回，而 `returncode` 由 transport 回调设置，
两者有时序差**。不 `await proc.wait()` 就会拿到 `None`，把"命令成功"误判成"退出码未知"，
还会让已退出的进程一直被算作"运行中"，导致并发计数虚高、后续 start 被误拒。

### 11.4 启动前的 fail-closed 校验

`TelegramConfig.is_allowed()` 的语义是**空白名单 = 不限制**。而现在执行能力开着 ——
两者叠加就等于"任何人都能让这台机器跑命令"。所以 `validate_security()` 会**拒绝启动**：

```
已启用执行能力，但 TELEGRAM_ALLOWED_CHAT_IDS 为空  →  拒绝启动（exit 2）
```

确实要绕过只能显式加 `--allow-insecure`（仅开发环境）。

### 11.5 验证

```bash
python scripts/smoke_test.py    # 架构层 42/42
python scripts/smoke_exec.py    # 执行层 65/65（含真实进程 start/logs/stop 全链路）
```
