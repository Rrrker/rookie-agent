"""启动入口 —— 依赖注入的唯一地方。

整个系统里**只有这个文件知道所有具体实现类**。
其余每个模块都只 import :mod:`app.contracts`。
想换掉任何一层（把 MemPalace 换成 pgvector、把 Codex 换成别的 runtime），
只改这里的装配代码，不动模块。

用法::

    python main.py                    # 使用 .env 配置启动
    python main.py --console          # 用终端代替 Telegram（无需 token）
    python main.py --dry-run          # 不调用真实模型，验证装配与流程
    python main.py --allow-insecure   # 显式跳过安全检查（仅开发用）
    python main.py --log-level DEBUG
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.brain import CodexBrain
from app.clones import CloneSystem
from app.command_audit import CommandAuditor
from app.config import AppConfig, load_config, validate_security
from app.emotion import PadEmotionEngine
from app.local_exec import LocalExecutor
from app.mcp_hub import McpHub
from app.media_router import MediaRouter
from app.memory import MemoryGateway
from app.orchestrator import Orchestrator, default_clones
from app.process_manager import ProcessManager

logger = logging.getLogger("app")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    for noisy in ("telegram", "httpx", "httpcore", "openai", "chromadb", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_channel(cfg: AppConfig, *, force_console: bool) -> object:
    """选渠道实现。这里体现了 ChannelPort 的价值：上层完全无感。"""
    if force_console or not cfg.telegram.token:
        if not force_console:
            logger.warning("未配置 TELEGRAM_BOT_TOKEN，自动降级到控制台渠道")
        from app.channel_console import ConsoleChannel

        return ConsoleChannel()
    from app.channel_telegram import TelegramChannel

    return TelegramChannel(cfg.telegram)


def build_orchestrator(cfg: AppConfig, *, force_console: bool = False) -> Orchestrator:
    """组装整个系统。顺序体现依赖方向：底层先建，上层后建。"""
    # 1) 工具层最先建 —— 它没有依赖，且被多层消费
    hub = McpHub.from_app_config(cfg.memory, cfg.media)

    # 2) 依赖工具层的模块
    memory = MemoryGateway(cfg.memory, hub)
    media = MediaRouter(cfg.media, hub)

    # 3) 工具层反向注入记忆（打破 memory <-> hub 的构造环）
    hub.bind_memory(memory)

    # 4) 无外部依赖的状态型模块
    emotion = PadEmotionEngine(cfg.emotion)

    # 5) 主脑依赖工具层
    brain = CodexBrain(cfg.codex, hub, session_db=cfg.data_dir / "sessions.sqlite3")

    # 6) 分身系统依赖"执行器"端口（由主脑实现）
    clones = CloneSystem(cfg.scheduler, brain, default_clones())

    # 7) 执行层三件套
    #    executor —— 内部白名单命令（不受 Codex 沙箱约束，所以只给白名单）
    #    processes —— 长驻进程（不受任何沙箱约束，默认关闭）
    #    auditor  —— 把 Codex 自己跑过的命令写进长期记忆
    executor = LocalExecutor(cfg.exec)
    processes = ProcessManager(cfg.process)
    auditor = CommandAuditor(memory, cfg.exec, wing=cfg.memory.default_wing)

    # 8) 渠道、编排
    channel = build_channel(cfg, force_console=force_console)
    return Orchestrator(
        cfg,
        channel=channel,
        hub=hub,
        memory=memory,
        emotion=emotion,
        media=media,
        brain=brain,
        clones=clones,
        executor=executor,
        processes=processes,
        auditor=auditor,
    )


async def amain(args: argparse.Namespace) -> int:
    cfg = load_config(args.env_file)
    if args.dry_run:
        cfg = _with_dry_run(cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    # 安全检查要在"装配之前"做：fail-closed，别等启动到一半才发现配置放开了执行能力
    problems, notices = validate_security(cfg)
    for notice in notices:
        logger.warning("安全提示：%s", notice)
    if problems:
        for problem in problems:
            logger.error("安全检查未通过：%s", problem)
        if not args.allow_insecure:
            logger.error(
                "已拒绝启动（fail-closed）。修正配置后重试；"
                "确实要绕过请显式加 --allow-insecure（仅限开发环境）。"
            )
            return 2
        logger.warning("--allow-insecure 已指定，继续启动。风险自负。")

    orchestrator = build_orchestrator(cfg, force_console=args.console)

    try:
        await orchestrator.start()
    except Exception:
        logger.exception("启动失败")
        return 1

    logger.info("系统已启动。按 Ctrl+C 退出。")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("正在关闭…")
        await orchestrator.stop()
    return 0


def _with_dry_run(cfg: AppConfig) -> AppConfig:
    """把配置切到 dry_run（frozen dataclass → 用 dataclasses.replace 重建）。"""
    from dataclasses import replace

    return replace(cfg, codex=replace(cfg.codex, dry_run=True))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex Agent System")
    parser.add_argument("--console", action="store_true", help="用终端渠道代替 Telegram")
    parser.add_argument("--dry-run", action="store_true", help="不调用真实模型")
    parser.add_argument("--env-file", default=".env", help="配置文件路径")
    parser.add_argument("--log-level", default=None, help="覆盖 AGENT_LOG_LEVEL")
    parser.add_argument(
        "--allow-insecure",
        action="store_true",
        help="显式跳过启动前的安全检查（仅开发环境使用）",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    cfg = load_config(args.env_file)
    configure_logging(args.log_level or cfg.log_level)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
