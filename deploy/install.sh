#!/usr/bin/env bash
# =============================================================================
# rookie-agent 一键部署脚本（Linux，无容器化）
#
# 设计原则：
#   1. **就地部署**：脚本从 deploy/ 目录运行，项目根 = 脚本上一级目录。
#      venv、var/、.env 全部落在项目内，不往别处散落文件。
#   2. **幂等**：可反复执行。已存在的 venv / .env 不会被覆盖，只补缺。
#   3. **会拒绝**：Python 版本过低、缺关键二进制、系统不支持时**直接失败并说明原因**，
#      而不是装一半留个坏掉的现场。
#
# 用法：
#   ./deploy/install.sh                       # 默认安装
#   ./deploy/install.sh --no-apt              # 跳过 apt（依赖已就绪时）
#   ./deploy/install.sh --index-url https://pypi.org/simple
#   APP_USER=agent ./deploy/install.sh        # 指定服务运行账号
# =============================================================================
set -euo pipefail

# --------------------------------------------------------------------------- #
# 参数与常量
# --------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_USER="${APP_USER:-$(id -un)}"
APP_GROUP="${APP_GROUP:-$(id -gn "${APP_USER}" 2>/dev/null || id -gn)}"
SERVICE_NAME="${SERVICE_NAME:-rookie-agent}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

# 国内默认走**阿里云**镜像。刻意不用清华：tuna 上不存在 openai-codex
# （https://pypi.tuna.tsinghua.edu.cn/simple/openai-codex/ 返回 404），
# pip 会直接报 "from versions: none"，装到 requirements.txt 第 9 行就整体失败。
# 阿里云已同步到 0.154.0，与 requirements.txt 里锁定的版本一致。
# 任一源失败会自动回退到官方源再试一次（见 4/7 步）。
INDEX_URL="${INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
PYPI_FALLBACK="https://pypi.org/simple"
DO_APT=1
INSTALL_UNIT=1
WRITE_LOCK=1

PY_MIN_MAJOR=3
PY_MIN_MINOR=10          # openai-codex 硬要求

APT_PACKAGES=(
  python3-venv python3-dev build-essential
  git curl ca-certificates
  procps                 # pgrep/pkill —— 验收脚本要靠它检查孤儿进程
  bubblewrap             # Codex 的 Linux 沙箱依赖，缺了会 fail-closed
  ffmpeg                 # TTS → OGG/Opus 转码（缺了只是降级，不阻塞）
  sqlite3
)

HR="────────────────────────────────────────────────────────────────────────"
info()  { printf '  \033[36m·\033[0m %s\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()   { printf '\n  \033[31m✗ %s\033[0m\n\n' "$*" >&2; exit 1; }
step()  { printf '\n%s\n%s\n%s\n' "$HR" "$1" "$HR"; }

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-apt)     DO_APT=0; shift ;;
    --no-unit)    INSTALL_UNIT=0; shift ;;
    --no-lock)    WRITE_LOCK=0; shift ;;
    --index-url)  INDEX_URL="$2"; shift 2 ;;
    -h|--help)    usage ;;
    *) die "未知参数：$1（用 --help 查看用法）" ;;
  esac
done

printf '\n%s\n  rookie-agent 部署\n  APP_DIR=%s\n  APP_USER=%s\n%s\n' \
  "$HR" "$APP_DIR" "$APP_USER" "$HR"

[[ -f "${APP_DIR}/main.py" ]] \
  || die "在 ${APP_DIR} 找不到 main.py。请把脚本放在项目的 deploy/ 目录下运行。"

# --------------------------------------------------------------------------- #
# 1. 平台预检
# --------------------------------------------------------------------------- #
step "1/7  平台预检"

[[ "$(uname -s)" == "Linux" ]] \
  || die "本脚本只支持 Linux（当前 $(uname -s)）。Windows/macOS 请直接用 python main.py。"

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|aarch64) ok "架构 ${ARCH}（Codex CLI 与 onnxruntime 都有对应 wheel）" ;;
  *) warn "架构 ${ARCH} 未验证：Codex CLI 的预编译包与 chromadb/onnxruntime 可能没有对应 wheel" ;;
esac

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  info "发行版：${PRETTY_NAME:-unknown}"
  case "${ID:-}" in
    ubuntu|debian) ok "apt 系发行版，依赖安装走 apt" ;;
    *) warn "非 apt 系发行版（ID=${ID:-unknown}）：请手动安装 bubblewrap / ffmpeg / python3-venv" ;;
  esac
fi

# --------------------------------------------------------------------------- #
# 2. 选定 Python 解释器
# --------------------------------------------------------------------------- #
step "2/7  选定 Python 解释器"

# 顺序刻意是"从稳到新"：二进制 wheel（onnxruntime / torch）对新版 Python 的支持
# 通常滞后半年到一年，用 3.12 这类成熟版本能避开"装不上"的坑。
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3.12 python3.11 python3.13 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then PYTHON_BIN="$candidate"; break; fi
  done
fi
[[ -n "$PYTHON_BIN" ]] || die "找不到 python3。请先安装：apt install python3 python3-venv"

PY_VER="$("$PYTHON_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
PY_MAJOR="${PY_VER%%.*}"; PY_MINOR="${PY_VER##*.}"
if (( PY_MAJOR < PY_MIN_MAJOR || (PY_MAJOR == PY_MIN_MAJOR && PY_MINOR < PY_MIN_MINOR) )); then
  die "$PYTHON_BIN 是 Python ${PY_VER}，低于要求的 ${PY_MIN_MAJOR}.${PY_MIN_MINOR}（openai-codex 的硬要求）。
     解决：安装更高版本解释器后重试，例如
       sudo apt install python3.12 python3.12-venv
     或（推荐，不需要动系统 Python）：
       curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.12
       PYTHON_BIN=\"\$(uv python find 3.12)\" $0"
fi
ok "使用 ${PYTHON_BIN}（Python ${PY_VER}）"
(( PY_MINOR >= 14 )) && warn "Python ${PY_VER} 较新，部分二进制 wheel 可能尚未发布；若下面 pip 安装失败，请改用 3.12"

# --------------------------------------------------------------------------- #
# 3. 系统依赖
# --------------------------------------------------------------------------- #
step "3/7  系统依赖"

if (( DO_APT )); then
  if ! command -v apt-get >/dev/null 2>&1; then
    warn "没有 apt-get，跳过系统依赖安装（请自行保证 bubblewrap / ffmpeg / python3-venv 存在）"
  else
    missing=()
    for pkg in "${APT_PACKAGES[@]}"; do
      dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if (( ${#missing[@]} == 0 )); then
      ok "apt 依赖已齐（${#APT_PACKAGES[@]} 个包）"
    else
      info "需要安装：${missing[*]}"
      sudo apt-get update -qq
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
      ok "apt 依赖安装完成"
    fi
  fi
else
  warn "已跳过 apt（--no-apt）"
fi

check_bin() {
  # $1=二进制名  $2=apt 包名  $3=缺失时的后果说明
  if command -v "$1" >/dev/null 2>&1; then
    ok "$1: $(command -v "$1")"
  else
    warn "缺少 $2（二进制 $1）：$3"
  fi
}

# 注意：apt 包名是 bubblewrap，但它提供的**二进制叫 bwrap**。
# 拿包名去 command -v 会永远判为"缺失"——首次部署时就是这么误报的，
# 害得人以为沙箱依赖没装。这里一律检查真实二进制名。
check_bin bwrap  bubblewrap "Codex 的 Linux 沙箱依赖，缺了会 fail-closed，agent 将无法执行任何工具调用。务必安装。"
check_bin ffmpeg ffmpeg     "语音回复会降级为发送音频文件而非语音气泡（功能不中断）。"

# --------------------------------------------------------------------------- #
# 4. 虚拟环境与 Python 依赖
# --------------------------------------------------------------------------- #
step "4/7  虚拟环境与依赖"

VENV_DIR="${APP_DIR}/.venv"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  info "创建 venv：${VENV_DIR}"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  ok "venv 已创建"
else
  ok "venv 已存在，复用"
fi
VENV_PY="${VENV_DIR}/bin/python"

info "升级打包工具"
"$VENV_PY" -m pip install --quiet --upgrade pip setuptools wheel

# 镜像回退：国内镜像对部分包存在同步缺口（例如 tuna 至今没有 openai-codex，
# 阿里云也没有当天刚发布的版本）。一次解析失败不代表包不存在，
# 换官方源往往就好了 —— 所以这里给两次机会，而不是直接 die。
PIP_OK=0
for idx in "$INDEX_URL" "$PYPI_FALLBACK"; do
  (( PIP_OK )) && break
  info "安装 requirements.txt（索引：${idx}）"
  if "$VENV_PY" -m pip install -r "${APP_DIR}/requirements.txt" -i "$idx"; then
    PIP_OK=1
    if [[ "$idx" != "$INDEX_URL" ]]; then
      warn "已使用回退源安装：${idx}（首选源 ${INDEX_URL} 不可用）"
    fi
  elif [[ "$idx" != "$PYPI_FALLBACK" ]]; then
    warn "在 ${INDEX_URL} 解析失败，回退到 ${PYPI_FALLBACK} 重试"
  fi
done
if (( ! PIP_OK )); then
  die "依赖安装失败（已依次尝试 ${INDEX_URL} 与 ${PYPI_FALLBACK}）。常见原因：
     1) 所选镜像缺少锁定版本 → 换 --index-url，或确认 requirements.txt
        里每个锁定版本在该源上存在（pip 的报错会点名是哪一个）；
     2) Python ${PY_VER} 上某些二进制 wheel 尚未发布 → 用 uv 装 3.12 后重试：
          curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.12
     3) 需要编译 C 扩展 → 确认已装 build-essential / python3-dev。"
fi

# 关键 import 自检：装上了不等于能跑（openai-codex 目前是 beta，API 面随版本变化）
info "关键依赖自检"
"$VENV_PY" - <<'PY' || die "关键依赖自检失败：请检查上面的报错。openai-codex 处于 beta，务必锁版本。"
import sys, importlib
missing = []
for mod in ("openai_codex", "mcp", "telegram", "openai", "chromadb", "mempalace"):
    try:
        importlib.import_module(mod)
    except Exception as exc:
        missing.append(f"{mod} ({exc})")
if missing:
    print("  缺失：" + "; ".join(missing))
    sys.exit(1)
import openai_codex
print("  openai-codex OK")
try:
    from openai_codex import ApprovalMode, Sandbox
    print("  ApprovalMode:", [m for m in dir(ApprovalMode) if not m.startswith("_")])
    print("  Sandbox     :", [m for m in dir(Sandbox) if not m.startswith("_")])
except Exception as exc:
    print("  ⚠ 无法内省 ApprovalMode/Sandbox:", exc)
PY
ok "关键依赖自检通过"

# 运行时 API 探针：装上了 != 能用。上面只验证了 import 成功，
# 这里逐个验证**代码真正调用的那些符号**是否存在——因为第三方库的大版本
# 升级经常是"包还在、某个子模块/参数没了"（mcp 2.x 删掉 mcp.server.fastmcp
# 就是活生生的例子），那种破坏 import 自检是照不出来的。
if [[ -f "${APP_DIR}/deploy/probe_runtime_api.py" ]]; then
  "$VENV_PY" "${APP_DIR}/deploy/probe_runtime_api.py" \
    || die "运行时 API 探针未通过。上面每个 MISS 都指明了缺什么，
     请据此收紧 requirements.txt 里对应包的版本上限后重新部署。"
fi

if (( WRITE_LOCK )); then
  "$VENV_PY" -m pip freeze > "${APP_DIR}/deploy/requirements.lock.txt"
  ok "已生成版本锁 deploy/requirements.lock.txt（下次部署请用它，别再用范围约束）"
fi

# --------------------------------------------------------------------------- #
# 5. 数据目录
# --------------------------------------------------------------------------- #
step "5/7  数据目录"

DATA_DIR="${APP_DIR}/var"
mkdir -p "${DATA_DIR}"/{media,clones,exec,procs}
mkdir -p "${HOME}/.mempalace" "${HOME}/.codex"
ok "运行数据：${DATA_DIR}（media / clones / exec / procs）"
ok "长期记忆：${HOME}/.mempalace"
ok "Codex 配置：${HOME}/.codex"

# --------------------------------------------------------------------------- #
# 6. 配置文件
# --------------------------------------------------------------------------- #
step "6/7  配置文件"

ENV_FILE="${APP_DIR}/.env"
if [[ -f "$ENV_FILE" ]]; then
  ok ".env 已存在，保持不动（避免覆盖你的密钥）"
else
  if [[ -f "${APP_DIR}/deploy/env.production.example" ]]; then
    cp "${APP_DIR}/deploy/env.production.example" "$ENV_FILE"
    ok "已从模板生成 .env —— **必须填写密钥后才能启动**"
  else
    warn "找不到 deploy/env.production.example，跳过 .env 生成"
  fi
fi
if [[ -f "$ENV_FILE" ]]; then
  chmod 600 "$ENV_FILE"
  ok ".env 权限设为 600（内含 Telegram token 与 API Key）"
fi

# --------------------------------------------------------------------------- #
# 7. systemd 服务
# --------------------------------------------------------------------------- #
step "7/7  systemd 服务"

if (( INSTALL_UNIT )); then
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "没有 systemctl，跳过服务安装（可用前台运行：${VENV_PY} ${APP_DIR}/main.py）"
  else
    TMP_UNIT="$(mktemp)"
    sed -e "s|@APP_DIR@|${APP_DIR}|g" \
        -e "s|@APP_USER@|${APP_USER}|g" \
        -e "s|@APP_GROUP@|${APP_GROUP}|g" \
        "${APP_DIR}/deploy/rookie-agent.service.in" > "$TMP_UNIT"
    sudo install -m 0644 "$TMP_UNIT" "$UNIT_PATH"
    rm -f "$TMP_UNIT"
    sudo systemctl daemon-reload
    ok "服务单元已安装：${UNIT_PATH}"
    info "刻意**没有** enable/start —— 请先填好 .env，再执行下面的命令"
  fi
else
  warn "已跳过服务安装（--no-unit）"
fi

# --------------------------------------------------------------------------- #
# 摘要
# --------------------------------------------------------------------------- #
cat <<EOF

${HR}
  部署完成
${HR}

  项目目录   ${APP_DIR}
  虚拟环境   ${VENV_PY}
  数据目录   ${DATA_DIR}
  配置文件   ${ENV_FILE}

  下一步：
    1) 填密钥（必填项见文件内注释）：
         nano ${ENV_FILE}
    2) 装配自检（不需要 Telegram token，也不需要 Codex 登录）：
         ${VENV_PY} ${APP_DIR}/main.py --health-check --dry-run
    3) 部署验收（含 SIGTERM 优雅停机检查，全自动）：
         ${APP_DIR}/deploy/verify.sh
    4) 探测本机沙箱真实边界（结论不可跨平台外推，必须在目标机跑）：
         cd ${APP_DIR} && ${VENV_PY} scripts/probe_sandbox.py
    5) 前台试跑一遍（Ctrl+C 退出）：
         cd ${APP_DIR} && ${VENV_PY} main.py --console
    6) 交给 systemd 托管：
         sudo systemctl enable --now ${SERVICE_NAME}
         systemctl status ${SERVICE_NAME}
         journalctl -u ${SERVICE_NAME} -f

  完整验收清单见 docs/deployment-linux.md
EOF
