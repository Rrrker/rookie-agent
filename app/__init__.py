"""Codex Agent System —— 可分层的长期记忆型多模态 Agent。

模块依赖方向（严格单向，不允许反向 import）::

    contracts / config      ← 所有模块都依赖，且只依赖这两者
        ↑
    memory / emotion / mcp_hub / media_router
        ↑
    brain / clones
        ↑
    orchestrator
        ↑
    main

任何模块之间的调用都必须经过 contracts.py 里定义的 Protocol 与 dataclass，
实现类之间互不 import —— 这样任何一个模块都能被替换成 Mock。
"""

__version__ = "0.1.0"
