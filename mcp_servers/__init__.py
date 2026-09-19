"""自建 MCP 工具服务包。

本包每个模块都是一个**独立可执行**的 MCP server 进程，
以 stdio 方式被拉起（由应用自己 或 Codex 主脑 分别启动）。

刻意不 import ``app.*`` —— 工具服务是独立的部署单元，
只认环境变量，这样它能被任何 MCP 宿主（Codex / Claude Code / Cursor）复用。
"""
