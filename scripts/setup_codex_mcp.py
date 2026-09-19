"""把 MCP 工具挂载进 Codex —— 生成 ``~/.codex/config.toml`` 的 MCP 段。

==============================================================================
为什么必须有这个脚本
==============================================================================
Codex Python SDK **不提供工具注册接口**。想给主脑挂工具，唯一的路是把
server 写进 Codex 自己的配置文件。于是"工具挂载"这件事天然有两个消费方：

* 应用自己（``app/mcp_hub.py``，进程内直连 MCP server）
* Codex 主脑（Codex 自己拉起 MCP server，模型看到 ``mcp__<key>__<tool>``）

两边必须配置一致，否则会出现"应用能用的工具主脑看不到"这种诡异现象。
本脚本从 :class:`app.mcp_hub.McpServerSpec` 生成配置 —— **同一份定义，两个消费方**。

==============================================================================
安全设计
==============================================================================
* **只追加，不重写**。已存在的 ``[mcp_servers.x]`` 段落会被跳过，你手写的
  其它配置一个字节都不会被动；
* **密钥不落盘**。API Key 用 ``env_vars`` 从环境变量转发，
  绝不写进 ``config.toml``；
* ``--print`` 只打印不写入，方便先审一遍。

用法::

    python scripts/setup_codex_mcp.py --print      # 只看会生成什么
    python scripts/setup_codex_mcp.py              # 写入 ~/.codex/config.toml
    python scripts/setup_codex_mcp.py --force      # 覆盖已存在的同名段落
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config  # noqa: E402
from app.mcp_hub import McpServerSpec  # noqa: E402


def codex_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home).expanduser() if codex_home else Path.home() / ".codex") / "config.toml"


def build_specs(cfg) -> list[McpServerSpec]:
    """从应用配置派生 Codex 侧配置 —— 单一事实来源。"""
    python_bin = sys.executable

    return [
        McpServerSpec(
            key="mempalace",
            command=python_bin,
            args=("-m", "mempalace.mcp_server"),
            cwd=PROJECT_ROOT,
            startup_timeout_sec=30.0,
            tool_timeout_sec=60.0,
        ),
        McpServerSpec(
            key="media_tools",
            command=python_bin,
            args=("-m", "mcp_servers.media_tools_server"),
            cwd=PROJECT_ROOT,
            startup_timeout_sec=20.0,
            tool_timeout_sec=180.0,
        ),
    ]


def render_block(spec: McpServerSpec, cfg) -> str:
    """生成一个段落。密钥用 env_vars 转发，不写明文。"""
    lines = [
        "",
        f"# ---- {spec.key} ----（由 scripts/setup_codex_mcp.py 生成，可手工调整）",
        f"[mcp_servers.{spec.key}]",
        f'command = "{spec.command.replace(chr(92), "/")}"',
        "args = [" + ", ".join(f'"{a}"' for a in spec.args) + "]",
        f'cwd = "{spec.cwd.as_posix()}"' if spec.cwd else "",
        "enabled = true",
        f"startup_timeout_sec = {spec.startup_timeout_sec:g}",
        f"tool_timeout_sec = {spec.tool_timeout_sec:g}",
        "# 非密钥配置",
    ]

    if spec.key == "media_tools":
        lines.append(
            'env = { '
            f'"VISION_MODEL" = "{cfg.media.vision_model}", '
            f'"STT_MODEL" = "{cfg.media.stt_model}", '
            f'"TTS_MODEL" = "{cfg.media.tts_model}", '
            f'"TTS_VOICE" = "{cfg.media.tts_voice}" }}'
        )
        lines.append(
            "# 密钥从环境变量转发 —— 不要把 API Key 写进这个文件"
        )
        lines.append(
            'env_vars = ["VISION_API_KEY", "STT_API_KEY", "TTS_API_KEY", '
            '"VISION_BASE_URL", "STT_BASE_URL", "TTS_BASE_URL", "FFMPEG_BIN"]'
        )
    elif spec.key == "mempalace":
        lines.append(f'env = {{ "MEMPALACE_PALACE_PATH" = "{cfg.memory.palace_path.as_posix()}" }}')

    return "\n".join(line for line in lines if line != "") + "\n"


def section_exists(text: str, key: str) -> bool:
    return re.search(rf"^\s*\[mcp_servers\.{re.escape(key)}\]\s*$", text, re.MULTILINE) is not None


def strip_section(text: str, key: str) -> str:
    """删掉某个 ``[mcp_servers.key]`` 段落（直到下一个同级 section 之前）。"""
    pattern = re.compile(
        rf"(?:^# ---- {re.escape(key)} ----.*?\n)?"
        rf"^\s*\[mcp_servers\.{re.escape(key)}\]\s*\n"
        rf"(?:(?!^\s*\[).*\n)*",
        re.MULTILINE,
    )
    return pattern.sub("", text)


def main() -> int:
    parser = argparse.ArgumentParser(description="把 MCP 工具挂载进 Codex 配置")
    parser.add_argument("--print", dest="dry", action="store_true", help="只打印不写入")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的同名段落")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    cfg = load_config(PROJECT_ROOT / args.env_file)
    specs = build_specs(cfg)
    config_path = codex_config_path()

    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""

    to_add: list[str] = []
    for spec in specs:
        block = render_block(spec, cfg)
        if section_exists(existing, spec.key):
            if not args.force:
                print(f"[跳过] [mcp_servers.{spec.key}] 已存在（--force 可覆盖）")
                continue
            existing = strip_section(existing, spec.key)
            print(f"[覆盖] [mcp_servers.{spec.key}]")
        else:
            print(f"[新增] [mcp_servers.{spec.key}]")
        to_add.append(block)

    payload = existing.rstrip() + "\n" + "".join(to_add)
    header = (
        "\n# ===== 以下 MCP 段由 scripts/setup_codex_mcp.py 生成 =====\n"
    )

    if args.dry:
        print("\n" + "-" * 70)
        print(f"目标文件：{config_path}")
        print("-" * 70)
        print("".join(to_add))
        return 0

    if not to_add:
        print("无需改动。")
        return 0

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        backup = config_path.with_suffix(".toml.bak")
        shutil.copy2(config_path, backup)
        print(f"已备份原配置到 {backup}")

    config_path.write_text(header + payload + "\n", encoding="utf-8")
    print(f"\n已写入 {config_path}")

    python_bin = sys.executable.replace("\\", "/")
    print(
        "\n下一步：\n"
        "  1) 重启 Codex（或 Codex App）让它重新读取配置；\n"
        "  2) 在 Codex 里执行 /mcp 查看 server 状态；\n"
        f"  3) 确认 MCP server 用的是同一个解释器：{python_bin}\n"
        "  4) 模型侧看到的工具名形如 mcp__media_tools__vision_describe"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
