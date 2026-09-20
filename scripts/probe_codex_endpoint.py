#!/usr/bin/env python3
"""自定义 Codex 端点自检 —— 验证 {base_url}/responses 是否真的可用。

--------------------------------------------------------------------------
为什么需要它
--------------------------------------------------------------------------
「OpenAI 兼容」是个被滥用的说法。Codex CLI 0.154.0 **只认 Responses API**，
并且要求 **SSE 流式**响应：

* 实测 ``wire_api = "chat"`` → ``is no longer supported``（Chat Completions 协议已被移除）
* 实测 ``wire_api = "chat_completions"`` → ``unknown variant``（只接受 ``responses``）
* 实测请求头带 ``accept: text/event-stream``，非流式端点会报
  ``stream closed before response.completed``

问题是：市面上大量第三方中转只实现了 ``/v1/chat/completions``。
它们会**接受**你的配置（不报错），然后在第一次对话时超时或返回怪错误 ——
这类"配置看起来没问题、就是不能用"的故障排查成本极高。

本脚本直接发一个最小的 Responses 请求，按响应形态给出明确诊断。

--------------------------------------------------------------------------
用法
--------------------------------------------------------------------------
    python scripts/probe_codex_endpoint.py                 # 读 .env 里的配置
    python scripts/probe_codex_endpoint.py --show-events   # 额外打印 SSE 事件名

退出码：0 = 端点可用 / 1 = 端点有问题 / 2 = 没配自定义端点
**不会打印你的 API Key**（只显示前 6 位）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许直接 `python scripts/probe_codex_endpoint.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402

TIMEOUT = 30.0


def _mask(key: str | None) -> str:
    """脱敏显示密钥。"""
    if not key:
        return "(未配置)"
    return key[:6] + "…" + f"({len(key)} 字符)"


def _report_http(status: int, body: str) -> None:
    """按 HTTP 状态码给出针对性的诊断。"""
    print(f"  HTTP {status}")
    if status == 401 or status == 403:
        print("  ✗ 鉴权失败。检查 AGENT_CODEX_BASE_URL_API_KEY（或 OPENAI_API_KEY）是否正确、是否有余额/权限。")
    elif status == 404:
        print("  ✗ 404 —— 端点没有 /responses 这个路径。")
        print("    最可能的原因：这是个只提供 Chat Completions 的中转。")
        print("    Codex 只支持 Responses API，这类端点用不了（换端点，或让中转方开 Responses）。")
    elif status == 400:
        print("  ✗ 400 —— 请求被拒。可能是模型名不对（检查 AGENT_CODEX_MODEL），")
        print("    或该端点不支持 Responses 请求体格式。")
    elif status >= 500:
        print("  ✗ 服务端错误，通常是上游不可用或端点内部异常。")
    snippet = body.strip().replace("\n", " ")[:400]
    if snippet:
        print(f"  响应体：{snippet}")


def probe(base_url: str, api_key: str | None, model: str, show_events: bool) -> int:
    """发一个最小的 Responses 请求并判定端点形态。

    Args:
        base_url: 端点根地址（例如 https://x.example.com/v1）。
        api_key: 密钥；None 时不发 Authorization 头（本地推理服务可能不需要）。
        model: 模型名。
        show_events: 是否打印收到的 SSE 事件名。

    Returns:
        退出码：0 可用 / 1 有问题。
    """
    try:
        import httpx
    except ImportError:
        print("  缺少 httpx。请先 pip install -r requirements.txt")
        return 1

    url = base_url.rstrip("/") + "/responses"
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "input": "ping",
        "stream": True,
        "max_output_tokens": 16,
    }

    print(f"  请求：POST {url}")
    print(f"  模型：{model}")
    print(f"  密钥：{_mask(api_key)}")
    print()

    event_names: list[str] = []
    content_type = ""
    status = 0
    body_head = ""
    got_completed = False

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            with client.stream("POST", url, headers=headers, json=payload) as resp:
                status = resp.status_code
                content_type = resp.headers.get("content-type", "")
                if status != 200:
                    body_head = resp.read().decode("utf-8", "replace")[:800]
                    _report_http(status, body_head)
                    return 1

                # 200：按 SSE 读取，找 response.completed
                for raw in resp.iter_lines():
                    line = raw.strip()
                    if line.startswith("event:"):
                        name = line[6:].strip()
                        event_names.append(name)
                        if name == "response.completed":
                            got_completed = True
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                        if not body_head:
                            body_head = data
                        if '"response.completed"' in data or '"status":"completed"' in data:
                            got_completed = True
                    if got_completed:
                        break
    except httpx.ConnectError as exc:
        print(f"  ✗ 连不上：{exc}")
        print("    检查 AGENT_CODEX_BASE_URL 的域名/端口是否正确，以及本机能否出网。")
        return 1
    except httpx.TimeoutException:
        print(f"  ✗ {TIMEOUT:.0f}s 超时。端点可能不接受流式请求，或上游很慢。")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 请求异常：{exc.__class__.__name__}: {exc}")
        return 1

    print(f"  HTTP {status}")
    print(f"  Content-Type: {content_type or '(无)'}")
    if show_events and event_names:
        print(f"  SSE 事件：{', '.join(event_names[:12])}")
        if len(event_names) > 12:
            print(f"           …另有 {len(event_names) - 12} 个")
    print()

    ok = True
    if "text/event-stream" in content_type:
        print("  ✓ 是 SSE 流式响应（Codex 要求）")
    else:
        print(f"  ✗ Content-Type 不是 text/event-stream —— Codex 需要流式响应")
        ok = False

    if got_completed:
        print("  ✓ 收到 response.completed —— Responses API 语义正确")
    elif event_names:
        print(f"  ! 收到 SSE 事件但没看到 response.completed（可能是别的流式协议）")
        print(f"    首个事件：{event_names[0]}")
        ok = False
    else:
        print("  ! 没有解析到 SSE 事件；响应可能不是标准 Responses 流")
        snippet = body_head.strip()[:200]
        if snippet:
            print(f"    首个 data：{snippet}")
        ok = False

    return 0 if ok else 1


def main() -> int:
    """入口。"""
    ap = argparse.ArgumentParser(description="自定义 Codex 端点自检")
    ap.add_argument("--show-events", action="store_true", help="打印收到的 SSE 事件名")
    args = ap.parse_args()

    cfg = load_config()
    base_url = cfg.codex.base_url
    if not base_url:
        print()
        print("  未配置自定义端点（AGENT_CODEX_BASE_URL 为空）。")
        print("  当前 Codex 主脑走官方 openai provider：")
        print(f"    模型   {cfg.codex.model}")
        print(f"    凭据   {'OPENAI_API_KEY' if cfg.codex.api_key else '机器上的 Codex 登录态 (~/.codex)'}")
        print()
        print("  要测自定义端点，先在 .env 里填：")
        print("    AGENT_CODEX_BASE_URL=https://your-endpoint.example.com/v1")
        print("    AGENT_CODEX_BASE_URL_API_KEY=...")
        print("    AGENT_CODEX_MODEL=<该端点的模型名>")
        print()
        return 2

    print()
    print("  自定义 Codex 端点自检")
    print("  " + "─" * 60)
    key = cfg.codex.base_url_api_key or cfg.codex.api_key
    rc = probe(base_url, key, cfg.codex.model, args.show_events)
    print("  " + "─" * 60)
    if rc == 0:
        print("  ✓ 端点可用。现在可以正常启动 agent 了。")
    else:
        print("  ✗ 端点不可用（见上面的诊断）。修好之前 agent 每轮对话都会失败。")
    print()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
