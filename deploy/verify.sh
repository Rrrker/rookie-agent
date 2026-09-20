#!/usr/bin/env bash
# =============================================================================
# rookie-agent 部署验收脚本
#
# 不碰任何外部服务、不需要 Telegram token、不需要 Codex 登录。
# 它专门验证那些**不会报错、只会静默失能**的部署问题：
#
#   · MCP 工具数为 0（解释器路径写错 → 服务起来了但识图/STT/TTS/记忆全废）
#   · SIGTERM 不收尾（systemctl stop 后 MCP 子进程变孤儿、outbox 不补投）
#   · 路径跟着 CWD 跑（systemd 下 CWD=/ → 写进系统 /var 或直接启动失败）
#   · bubblewrap 缺失（Codex 沙箱 fail-closed → agent 活着但不干活）
#
# 用法：
#   ./deploy/verify.sh              # 全量
#   ./deploy/verify.sh --quick      # 跳过两个较慢的测试套件
#
# 退出码：0 全通过 / 1 有失败项
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVICE_NAME="${SERVICE_NAME:-rookie-agent}"
VENV_PY="${APP_DIR}/.venv/bin/python"
ENV_FILE="${APP_DIR}/.env"
DATA_DIR="${APP_DIR}/var"

QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

PASS=0; FAIL=0; WARN=0
HR="────────────────────────────────────────────────────────────────────────"
pass() { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; WARN=$((WARN+1)); }
head_() { printf '\n%s\n%s\n%s\n' "$HR" "$1" "$HR"; }

cd "$APP_DIR" || { echo "无法进入 $APP_DIR"; exit 1; }

printf '\n%s\n  rookie-agent 部署验收\n  APP_DIR=%s\n%s\n' "$HR" "$APP_DIR" "$HR"

# --------------------------------------------------------------------------- #
head_ "1. 平台与系统依赖"
# --------------------------------------------------------------------------- #
[[ "$(uname -s)" == "Linux" ]] && pass "操作系统 Linux" || fail "非 Linux（$(uname -s)）"
pass "架构 $(uname -m)"

# 注意：apt 包名是 bubblewrap，但它提供的**二进制叫 bwrap**。
# 按包名去 command -v 会永远判为缺失（install.sh 里曾同款误报）。
if command -v bwrap >/dev/null 2>&1; then
  pass "bubblewrap(bwrap): $(command -v bwrap)"
else
  fail "缺少 bubblewrap —— Codex 的 Linux 沙箱会 fail-closed，agent 将无法执行任何工具调用
        修复： sudo apt install bubblewrap"
fi

if command -v ffmpeg >/dev/null 2>&1; then
  pass "ffmpeg: $(command -v ffmpeg)"
else
  warn "缺少 ffmpeg —— 语音回复降级为音频文件（不影响功能）"
fi

# --------------------------------------------------------------------------- #
head_ "2. 虚拟环境与依赖"
# --------------------------------------------------------------------------- #
if [[ -x "$VENV_PY" ]]; then
  pass "venv: $("$VENV_PY" -c 'import sys;print(sys.version.split()[0], sys.executable)')"
else
  fail "找不到 venv 解释器：${VENV_PY}
        修复： ./deploy/install.sh"
fi

if [[ -x "$VENV_PY" ]]; then
  IMPORT_ERR="$("$VENV_PY" - <<'PY' 2>&1
import importlib, sys
bad = []
for m in ("openai_codex", "mcp", "telegram", "openai", "chromadb", "mempalace"):
    try:
        importlib.import_module(m)
    except Exception as exc:
        bad.append(f"{m}: {exc}")
print("; ".join(bad))
sys.exit(1 if bad else 0)
PY
)"
  if [[ $? -eq 0 ]]; then
    pass "关键依赖全部可导入（openai_codex / mcp / telegram / openai / chromadb / mempalace）"
  else
    fail "依赖导入失败：${IMPORT_ERR}"
  fi

  APPROVAL_ERR="$("$VENV_PY" - <<'PY' 2>&1
from openai_codex import ApprovalMode
names = {m for m in dir(ApprovalMode) if not m.startswith("_")}
need = {"auto_review", "deny_all"}
print("members=" + ",".join(sorted(names)))
raise SystemExit(0 if need <= names else 1)
PY
)"
  if [[ $? -eq 0 ]]; then
    pass "ApprovalMode 内省符合预期（${APPROVAL_ERR#members=}）"
  else
    fail "ApprovalMode 成员与代码假设不符：${APPROVAL_ERR}
        说明 openai-codex 版本已变化（它是 beta，API 面会变）。请锁回 deploy/requirements.lock.txt 的版本。"
  fi

  # 运行时 API 探针：比"import 成功"强得多 —— 能查出 mcp 2.x 那种
  # "包还在、子模块被删"的静默破坏（import 自检照不出来）。
  if [[ -f "${APP_DIR}/deploy/probe_runtime_api.py" ]]; then
    PROBE_OUT="$("$VENV_PY" "${APP_DIR}/deploy/probe_runtime_api.py" 2>&1)"
    if [[ $? -eq 0 ]]; then
      pass "运行时 API 探针：$(printf '%s\n' "$PROBE_OUT" | grep -oE '[0-9]+/[0-9]+ 项全部通过' | tail -1)"
    else
      fail "运行时 API 探针未通过（已装库与代码用法不兼容）"
      printf '%s\n' "$PROBE_OUT" | grep '✗' | head -20 | sed 's/^/        /'
    fi
  fi
fi

# --------------------------------------------------------------------------- #
head_ "3. 目录与权限"
# --------------------------------------------------------------------------- #
for sub in "" media clones exec procs; do
  d="${DATA_DIR}/${sub}"
  if [[ -d "$d" ]] && [[ -w "$d" ]]; then
    pass "可写：${d}"
  else
    fail "不可写或不存在：${d}（修复： mkdir -p \"$d\"）"
  fi
done

if [[ -f "$ENV_FILE" ]]; then
  pass "配置文件存在：${ENV_FILE}"
  PERM="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo '?')"
  if [[ "$PERM" == "600" ]]; then
    pass ".env 权限 600"
  else
    warn ".env 权限是 ${PERM}，建议收紧：chmod 600 \"$ENV_FILE\""
  fi
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    warn "TELEGRAM_BOT_TOKEN 为空 —— 服务会降级到控制台渠道（不会崩，但收不到 Telegram 消息）"
  else
    pass "TELEGRAM_BOT_TOKEN 已填写"
  fi
  if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] && [[ -z "${TELEGRAM_ALLOWED_CHAT_IDS:-}" ]]; then
    fail "TELEGRAM_ALLOWED_CHAT_IDS 为空 —— fail-closed 会拒绝启动（exit 2）。
        空白名单 = 不限制会话，叠加已开启的执行能力等于对所有人开放命令执行。"
  fi
  [[ -n "${OPENAI_API_KEY:-}" ]] \
    && pass "OPENAI_API_KEY 已填写" \
    || warn "OPENAI_API_KEY 为空 —— 需要机器上已有 Codex 登录态（~/.codex/auth.json）"
else
  fail "配置文件不存在：${ENV_FILE}（修复： ./deploy/install.sh 或 cp deploy/env.production.example .env）"
fi

# --------------------------------------------------------------------------- #
head_ "4. 路径锚定（不跟随 CWD）"
# --------------------------------------------------------------------------- #
# 在 / 目录下加载配置，检查数据目录是否仍指向项目内 —— 这正是 systemd 的场景。
PATH_PROBE="$("$VENV_PY" - <<PY 2>&1
import sys
sys.path.insert(0, "${APP_DIR}")
from app.config import load_config, project_root
cfg = load_config("${ENV_FILE}")
print(f"{cfg.data_dir}|{cfg.memory.outbox_path}|{cfg.memory.mcp_command}|{project_root()}")
PY
)"
if [[ "$PATH_PROBE" == *"${APP_DIR}"* ]] && [[ "$PATH_PROBE" == *"/var"* ]]; then
  pass "数据目录锚定项目根：$(echo "$PATH_PROBE" | cut -d'|' -f1)"
  pass "outbox 锚定项目根：$(echo "$PATH_PROBE" | cut -d'|' -f2)"
else
  fail "数据/outbox 路径没有锚定项目根：${PATH_PROBE}"
fi

MCP_CMD="$(echo "$PATH_PROBE" | cut -d'|' -f3)"
if [[ "$MCP_CMD" == *".venv"* ]]; then
  pass "MCP 解释器指向 venv：${MCP_CMD}"
elif [[ -x "$MCP_CMD" ]]; then
  warn "MCP 解释器是 ${MCP_CMD}（不是 venv 路径）。能跑，但请确认它装了 mcp/mempalace。"
else
  fail "MCP 解释器不存在：${MCP_CMD} —— 两个 MCP server 都会起不来（tool_count 将是 0）"
fi

# --------------------------------------------------------------------------- #
head_ "5. 离线测试套件"
# --------------------------------------------------------------------------- #
if (( QUICK )); then
  warn "已跳过（--quick）"
else
  for suite in smoke_test smoke_exec; do
    OUT="$("$VENV_PY" "scripts/${suite}.py" 2>&1)"
    RC=$?
    SUMMARY="$(printf '%s\n' "$OUT" | grep -oE '通过 [0-9]+/[0-9]+' | tail -1)"
    if [[ $RC -eq 0 ]]; then
      pass "${suite}: ${SUMMARY:-全通过}"
    else
      fail "${suite}: ${SUMMARY:-失败}"
      printf '%s\n' "$OUT" | grep -E '✗|❌|Error|Traceback' | head -20 | sed 's/^/        /'
    fi
  done
fi

# --------------------------------------------------------------------------- #
head_ "6. 装配自检（MCP 工具是否真的挂上了）"
# --------------------------------------------------------------------------- #
HC_LOG="$(mktemp)"
"$VENV_PY" main.py --health-check --dry-run --log-level WARNING > "$HC_LOG" 2>&1
HC_RC=$?
# 不用 `... | head -1` 取值：head 提前退出会给上游 grep 发 SIGPIPE，
# 在 pipefail 下管道状态不可靠（偶发丢结果）。JSON 报告里 tool_count 只出现一次，
# 两级 grep 串起来自然就一个数，不需要截断。
TOOL_COUNT="$(grep -oE '"tool_count": *[0-9]+' "$HC_LOG" | grep -oE '[0-9]+' || true)"
if [[ $HC_RC -eq 0 ]] && [[ -n "$TOOL_COUNT" ]] && (( TOOL_COUNT > 0 )); then
  pass "健康检查通过：tool_count=${TOOL_COUNT}"
  grep -oE '"healthy": *[a-z]+' "$HC_LOG" | sort | uniq -c | sed 's/^/        /'
else
  fail "健康检查未通过（exit=${HC_RC}, tool_count=${TOOL_COUNT:-无}）"
  grep -E '\"error\"|启动前检查未通过|\[FAIL\]' "$HC_LOG" | head -10 | sed 's/^/        /'
fi
rm -f "$HC_LOG"

# --------------------------------------------------------------------------- #
head_ "7. 优雅停机（SIGTERM 后不留孤儿进程）"
# --------------------------------------------------------------------------- #
# 这一段是 `systemctl stop` 的等价物。用 --dry-run 起一个真实实例：
# 它照样会拉起真正的 MCP 子进程（这是最容易泄漏的东西），但不需要 Codex 凭据。
STOP_LOG="$(mktemp)"
"$VENV_PY" main.py --dry-run --console --log-level INFO > "$STOP_LOG" 2>&1 < /dev/null &
MAIN_PID=$!

CHILDREN=""
for _ in $(seq 1 40); do
  CHILDREN="$(pgrep -P "$MAIN_PID" 2>/dev/null | tr '\n' ' ')"
  [[ -n "${CHILDREN// /}" ]] && break
  sleep 0.5
done
sleep 3   # 给 MCP server 一点时间完成握手

if [[ -z "${CHILDREN// /}" ]]; then
  warn "未能观察到 MCP 子进程（可能启动过慢），停机测试仍会继续"
else
  pass "已拉起子进程：${CHILDREN}"
fi

kill -TERM "$MAIN_PID" 2>/dev/null
for _ in $(seq 1 60); do
  kill -0 "$MAIN_PID" 2>/dev/null || break
  sleep 0.5
done

if kill -0 "$MAIN_PID" 2>/dev/null; then
  fail "收到 SIGTERM 后 30 秒仍未退出 —— 优雅停机未生效"
  kill -KILL "$MAIN_PID" 2>/dev/null
else
  pass "主进程已被 SIGTERM 干净停止"
fi

if grep -q "优雅关闭" "$STOP_LOG"; then
  pass "日志出现「优雅关闭」，说明 orchestrator.stop() 真的执行了"
else
  fail "日志没有「优雅关闭」—— SIGTERM 处理器没跑，MCP 子进程/长驻进程会变孤儿、outbox 不会补投"
  tail -15 "$STOP_LOG" | sed 's/^/        /'
fi

ORPHANS=""
for pid in $CHILDREN; do
  kill -0 "$pid" 2>/dev/null && ORPHANS="${ORPHANS}${pid} "
done
if [[ -z "${ORPHANS// /}" ]]; then
  pass "没有残留的 MCP 子进程"
else
  fail "残留子进程：${ORPHANS}（这些就是泄漏的 MCP server）"
fi
rm -f "$STOP_LOG"

# --------------------------------------------------------------------------- #
head_ "8. systemd 服务（若已安装）"
# --------------------------------------------------------------------------- #
# ⚠️ 这里**不能**写成 `systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"`。
# 原因是个经典陷阱：grep -q 一命中就立刻关掉管道，systemctl 随即收到 SIGPIPE(141)；
# 在本脚本的 `set -o pipefail` 下，整条管道会被判为失败 ——
# 于是服务明明装好了却报"未安装"。首次部署时就是这样误报的。
# 改为直接看 unit 文件（最可靠，且不依赖 systemd 在 SSH 会话里可达）。
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
UNIT_KNOWN=0
if [[ -f "$UNIT_FILE" ]]; then
  UNIT_KNOWN=1
elif command -v systemctl >/dev/null 2>&1 && systemctl cat "$SERVICE_NAME" >/dev/null 2>&1; then
  UNIT_KNOWN=1
fi

if (( UNIT_KNOWN )); then
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    pass "${SERVICE_NAME} 正在运行"
    systemctl show -p MainPID --value "$SERVICE_NAME" | sed 's/^/        MainPID=/'
  else
    warn "${SERVICE_NAME} 已安装但未运行：systemctl status ${SERVICE_NAME}"
  fi
  systemctl is-enabled --quiet "$SERVICE_NAME" \
    && pass "${SERVICE_NAME} 已设为开机自启" \
    || warn "${SERVICE_NAME} 未设为自启：sudo systemctl enable ${SERVICE_NAME}"
else
  warn "${SERVICE_NAME}.service 未安装（修复： ./deploy/install.sh）"
fi
# --------------------------------------------------------------------------- #
printf '\n%s\n  验收结果：通过 %d / 失败 %d / 提醒 %d\n%s\n\n' "$HR" "$PASS" "$FAIL" "$WARN" "$HR"
if (( FAIL > 0 )); then
  printf '  \033[31m✗ 有 %d 项未通过，暂不要交给 systemd 托管。\033[0m\n\n' "$FAIL"
  exit 1
fi
printf '  \033[32m✓ 全部通过。\033[0m\n\n'
exit 0
