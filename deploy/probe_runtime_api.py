#!/usr/bin/env python3
"""运行时 API 探针 —— 验证「已安装的第三方库」与「代码实际用法」是否一致。

--------------------------------------------------------------------------
为什么需要这个东西
--------------------------------------------------------------------------
``import <包>`` 成功**不等于**代码能用。第三方库的大版本升级最常见的
破坏方式不是删包，而是：

* 删掉某个子模块（mcp 2.x 删了 ``mcp.server.fastmcp``）；
* 把某个类改名（FastMCP → MCPServer）；
* 改掉某个函数参数名（``response_format`` → ``format`` 之类）；
* 收紧枚举取值（``ApprovalMode`` 少一个成员）。

这些破坏在 ``pip install`` 阶段全是静默的，装完 "OK"，一跑就崩。
本探针把代码里**真正 import / 调用的每一个外部符号**列出来逐个验证 ——
版本号只是代理指标，符号存在性才是真命题。

还有一类更隐蔽的问题：本项目对 telegram / mcp 都做了
``try: import ... except: _XXX_AVAILABLE = False`` 的降级包装。
于是 ``import app.channel_telegram`` 永远成功，哪怕 PTB 完全不可用。
为此本探针**额外校验这些降级标志位为 True**，防止"静默降级"
被当成"通过"。

--------------------------------------------------------------------------
用法
--------------------------------------------------------------------------
    python deploy/probe_runtime_api.py          # 退出码 0 = 全绿
    python deploy/probe_runtime_api.py -v       # 额外打印堆栈与通过项备注

不需要任何密钥、不需要网络、不会启动 MCP server 或 Codex 进程。
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import sys
import traceback
from typing import Any, Callable

APP_DIR = pathlib.Path(__file__).resolve().parent.parent
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

_RESULTS: list[tuple[str, bool, str]] = []


# =========================================================================== #
# 基础设施
# =========================================================================== #


def probe(label: str, fn: Callable[[], Any]) -> None:
    """跑一个探针。fn 正常返回即通过；抛异常或返回 False 即失败。

    Args:
        label: 人类可读的探针名，打印在结果表里。
        fn: 无参可调用对象。
    """
    try:
        result = fn()
        if result is False:
            _RESULTS.append((label, False, "显式返回 False"))
        else:
            _RESULTS.append((label, True, result if isinstance(result, str) else ""))
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        _RESULTS.append((label, False, detail))
        if VERBOSE:
            traceback.print_exc()


def need_attrs(module_name: str, *attrs: str) -> Callable[[], str]:
    """生成"模块存在且含指定属性"的探针函数。"""

    def _check() -> str:
        mod = importlib.import_module(module_name)
        missing = [a for a in attrs if not hasattr(mod, a)]
        if missing:
            ver = getattr(mod, "__version__", "?")
            raise AttributeError(
                f"{module_name} 缺少属性: {', '.join(missing)}（实际版本 {ver}）"
            )
        return ", ".join(attrs)

    return _check


def _enum_members(enum_cls: Any, expected: tuple[str, ...], label: str) -> str:
    """校验 str-Enum 的**取值**是否存在。

    坑：``ApprovalMode`` 是 ``str`` 的子类，``hasattr`` 会把 str 自带的
    方法（capitalize / format / ...）也算成"存在"，所以不能只做 hasattr。
    这里直接对成员做 ``isinstance`` 检查 —— 项目记忆里已标定过这一条。
    """
    missing = [n for n in expected if not isinstance(getattr(enum_cls, n, None), enum_cls)]
    if missing:
        actual = [m.name for m in enum_cls]
        raise ValueError(f"{label} 缺少取值 {missing}；实际可用：{actual}")
    return ", ".join(expected)


# =========================================================================== #
# 探针实现（全部先定义，后调用）
# =========================================================================== #


def check_approval() -> str:
    from openai_codex import ApprovalMode

    return _enum_members(ApprovalMode, ("auto_review", "deny_all"), "ApprovalMode")


def check_sandbox() -> str:
    from openai_codex import Sandbox

    return _enum_members(
        Sandbox, ("full_access", "read_only", "workspace_write"), "Sandbox"
    )


def check_async_codex_sig() -> str:
    from openai_codex import AsyncCodex

    params = list(inspect.signature(AsyncCodex.__init__).parameters)
    if "config" not in params:
        raise ValueError(f"AsyncCodex.__init__ 参数变了：{params}（代码依赖 config=）")
    return "config="


def check_stdio_params_sig() -> str:
    from mcp import StdioServerParameters

    params = set(inspect.signature(StdioServerParameters).parameters)
    needed = {"command", "args", "env", "cwd"}
    missing = needed - params
    if missing:
        raise ValueError(f"StdioServerParameters 缺参数：{sorted(missing)}")
    return ", ".join(sorted(needed))


def check_ptb_lifecycle() -> str:
    """校验 PTB 的手动生命周期 API。

    坑：轮询方法挂在 **``Updater``** 上（``app.updater.start_polling()``），
    不是 ``Application`` 上。PTB 只有 ``Application.run_polling()`` 这个
    "一站式"入口，而本项目刻意不用它 —— 要手动 ``initialize()`` /
    ``updater.start_polling()``，好和 asyncio 主循环共存、并让 SIGTERM
    能走到 ``shutdown()``。第一版探针断言错了对象，误报过一次。
    """
    from telegram.ext import Application, Updater

    app_need = ("builder", "initialize", "shutdown", "updater")
    missing = [n for n in app_need if not hasattr(Application, n)]
    if missing:
        raise AttributeError(f"Application 缺少 {missing}")

    updater_need = ("start_polling", "stop")
    missing_u = [n for n in updater_need if not hasattr(Updater, n)]
    if missing_u:
        raise AttributeError(f"Updater 缺少 {missing_u}")
    return "Application.initialize/shutdown + Updater.start_polling/stop"


def check_openai_audio_api() -> str:
    """校验 vision / stt / tts 三条路径的参数名。

    这三个调用全靠**关键字传参**，参数改名 = 运行期 TypeError，
    而它被 try/except 兜着，只会变成"工具返回 ok:false"，
    在部署阶段极难察觉。所以这里必须在安装阶段就验死。
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key="probe-not-used")
    checks = {
        "chat.completions.create": (client.chat.completions.create, {"model", "messages"}),
        "audio.transcriptions.create": (
            client.audio.transcriptions.create,
            {"model", "file", "language", "prompt"},
        ),
        "audio.speech.create": (
            client.audio.speech.create,
            {"model", "voice", "input", "speed", "response_format"},
        ),
    }
    for path, (fn, needed) in checks.items():
        missing = needed - set(inspect.signature(fn).parameters)
        if missing:
            raise ValueError(f"{path} 缺参数 {sorted(missing)}")
    return f"{len(checks)} 个 API 参数齐备"


def _guard_flag(module_name: str, flag: str) -> str:
    """校验模块内的降级标志位为 True（防止静默降级被算作通过）。"""
    mod = importlib.import_module(module_name)
    value = getattr(mod, flag, None)
    if value is not True:
        err = getattr(mod, "_PTB_IMPORT_ERROR", None) or getattr(
            mod, "_MCP_IMPORT_ERROR", "未知原因"
        )
        raise RuntimeError(f"{module_name}.{flag} = {value!r}（导入失败：{err}）")
    return f"{flag}=True"


# =========================================================================== #
# 探针清单
# =========================================================================== #

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# --- 1. openai-codex：主脑。地基，缺一个符号等于完全不可用 ---
probe("openai_codex 顶层符号", need_attrs(
    "openai_codex",
    "AsyncCodex", "Codex", "Sandbox", "ApprovalMode", "CodexConfig", "TurnResult",
))
probe("openai_codex.ApprovalMode 取值", check_approval)
probe("openai_codex.Sandbox 取值", check_sandbox)
probe("openai_codex.AsyncCodex 构造签名", check_async_codex_sig)

# --- 2. mcp：客户端与服务端两条路径都要验 ---
probe("mcp 客户端符号", need_attrs("mcp", "ClientSession", "StdioServerParameters"))
probe("mcp.client.stdio.stdio_client", need_attrs("mcp.client.stdio", "stdio_client"))
probe("mcp.server.fastmcp.FastMCP〔2.x 已删除〕", need_attrs("mcp.server.fastmcp", "FastMCP"))
probe("StdioServerParameters 参数", check_stdio_params_sig)

# --- 3. python-telegram-bot：消息渠道 ---
probe("telegram 顶层符号", need_attrs("telegram", "Bot", "Message", "Update"))
probe("telegram.constants.ChatAction", need_attrs("telegram.constants", "ChatAction"))
probe("telegram.error.TelegramError", need_attrs("telegram.error", "TelegramError"))
probe("telegram.ext 符号", need_attrs(
    "telegram.ext", "Application", "ContextTypes", "MessageHandler", "filters",
))
probe("Application/Updater 生命周期方法", check_ptb_lifecycle)

# --- 4. openai：多模态 ---
probe("openai.AsyncOpenAI", need_attrs("openai", "AsyncOpenAI"))
probe("openai 音频/对话 API 参数", check_openai_audio_api)

# --- 5. 记忆层 ---
probe("mempalace 可导入", lambda: importlib.import_module("mempalace") and "ok")
probe("chromadb 可导入", lambda: importlib.import_module("chromadb") and "ok")

# --- 6. 本项目模块真的 import 一遍（覆盖面最广的一条）---
for _mod in (
    "app.contracts",
    "app.config",
    "app.brain",
    "app.memory",
    "app.mcp_hub",
    "app.orchestrator",
    "app.channel_console",
    "app.channel_telegram",
    "app.clones",
    "app.emotion",
    "app.media_router",
    "app.local_exec",
    "app.process_manager",
    "app.command_audit",
    "mcp_servers.media_tools_server",
):
    probe(f"import {_mod}", lambda n=_mod: importlib.import_module(n) and "ok")

# --- 7. 降级标志位必须是 True（不能被 try/except 悄悄吞掉）---
probe("无静默降级：telegram 可用", lambda: _guard_flag("app.channel_telegram", "_PTB_AVAILABLE"))
probe("无静默降级：mcp 可用", lambda: _guard_flag("app.mcp_hub", "_MCP_AVAILABLE"))


# =========================================================================== #
# 结果
# =========================================================================== #


def main() -> int:
    """打印结果表并返回退出码（0 = 全绿）。"""
    width = max(len(label) for label, _, _ in _RESULTS)
    failed = 0
    print()
    print("  运行时 API 探针 —— 已安装的库 vs 代码实际用法")
    print("  " + "─" * (width + 10))
    for label, passed, note in _RESULTS:
        mark = "\033[32m✓\033[0m" if passed else "\033[31m✗\033[0m"
        suffix = f"  {note}" if note and (not passed or VERBOSE) else ""
        print(f"  {mark} {label:<{width}}{suffix}")
        if not passed:
            failed += 1
    total = len(_RESULTS)
    print("  " + "─" * (width + 10))
    if failed:
        print(f"  \033[31m{failed}/{total} 项未通过\033[0m —— 已安装的库与代码用法不兼容")
    else:
        print(f"  \033[32m{total}/{total} 项全部通过\033[0m")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
