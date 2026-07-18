#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
OS_NAME=${OPTION_SCANNER_OS:-$(uname -s)}
INSTALL_DIR=${HOME}/.local/share/option-scanner
CONFIG_DIR=${HOME}/.config/option-scanner
STATE_DIR=${HOME}/.local/state/option-scanner
INSTALL_SERVICE=1
START_SERVICE=0
UNINSTALL=0
PURGE=0

usage() {
  cat <<'EOF'
用法：./install.sh [选项]

  --prefix DIR       代码安装目录（默认 ~/.local/share/option-scanner）
  --config-dir DIR   配置目录（默认 ~/.config/option-scanner）
  --state-dir DIR    缓存与日志目录（默认 ~/.local/state/option-scanner）
  --no-service       只复制文件，不安装 systemd/LaunchAgent
  --start            安装后立即启用并启动服务
  --no-start         安装服务但暂不启动（默认）
  --uninstall        停止服务并删除已安装代码，保留配置和状态
  --purge            与 --uninstall 同用，同时删除配置和状态
  -h, --help         显示帮助

重新运行安装脚本即可升级；现有配置和扫描状态不会被覆盖。
EOF
}

while (($#)); do
  case "$1" in
    --prefix)
      INSTALL_DIR=${2:?--prefix 缺少目录}; shift 2 ;;
    --config-dir)
      CONFIG_DIR=${2:?--config-dir 缺少目录}; shift 2 ;;
    --state-dir)
      STATE_DIR=${2:?--state-dir 缺少目录}; shift 2 ;;
    --no-service)
      INSTALL_SERVICE=0; shift ;;
    --start)
      START_SERVICE=1; shift ;;
    --no-start)
      START_SERVICE=0; shift ;;
    --uninstall)
      UNINSTALL=1; shift ;;
    --purge)
      PURGE=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "未知选项：$1" >&2; usage >&2; exit 2 ;;
  esac
done

if ((PURGE && !UNINSTALL)); then
  echo "--purge 必须与 --uninstall 一起使用。" >&2
  exit 2
fi

CONFIG_FILE=${CONFIG_DIR}/option-scanner.env
RUNNER=${INSTALL_DIR}/run_scanner.sh
SYSTEMD_UNIT=${HOME}/.config/systemd/user/option-scanner.service
LAUNCHD_LABEL=com.local.option-scanner
LAUNCHD_PLIST=${HOME}/Library/LaunchAgents/${LAUNCHD_LABEL}.plist

remove_installed_files() {
  rm -f \
    "${INSTALL_DIR}/option_alert_daemon.py" \
    "${INSTALL_DIR}/exchange_adapters.py" \
    "${INSTALL_DIR}/option_scanner_core.py" \
    "${INSTALL_DIR}/scanner_http.py" \
    "${INSTALL_DIR}/README.md" \
    "${INSTALL_DIR}/run_scanner.sh"
  rm -rf "${INSTALL_DIR}/__pycache__"
  rmdir "${INSTALL_DIR}" 2>/dev/null || true
}

if ((UNINSTALL)); then
  if [[ ${OS_NAME} == Linux && -f ${SYSTEMD_UNIT} ]]; then
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user disable --now option-scanner.service >/dev/null 2>&1 || true
    fi
    rm -f "${SYSTEMD_UNIT}"
    command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload || true
  elif [[ ${OS_NAME} == Darwin && -f ${LAUNCHD_PLIST} ]]; then
    launchctl bootout "gui/$(id -u)/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
    rm -f "${LAUNCHD_PLIST}"
  fi
  remove_installed_files
  if ((PURGE)); then
    rm -rf "${CONFIG_DIR}" "${STATE_DIR}"
  fi
  echo "已卸载 option-scanner。"
  ((PURGE)) || echo "已保留配置 ${CONFIG_DIR} 和状态 ${STATE_DIR}。"
  exit 0
fi

PYTHON_BIN=${PYTHON_BIN:-$(command -v python3 || true)}
if [[ -z ${PYTHON_BIN} ]]; then
  echo "未找到 python3，请先安装 Python 3.9+。" >&2
  exit 1
fi
"${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "Python 版本过低，需要 Python 3.9+。" >&2
  exit 1
}

REQUIRED_FILES=(
  option_alert_daemon.py
  exchange_adapters.py
  option_scanner_core.py
  scanner_http.py
  option-scanner.env.example
  README.md
)
for name in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f ${SOURCE_DIR}/${name} ]]; then
    echo "安装包缺少 ${name}。" >&2
    exit 1
  fi
done

mkdir -p "${INSTALL_DIR}" "${CONFIG_DIR}" "${STATE_DIR}"
for name in option_alert_daemon.py exchange_adapters.py option_scanner_core.py scanner_http.py README.md; do
  install -m 0644 "${SOURCE_DIR}/${name}" "${INSTALL_DIR}/${name}"
done
if [[ ! -f ${CONFIG_FILE} ]]; then
  install -m 0600 "${SOURCE_DIR}/option-scanner.env.example" "${CONFIG_FILE}"
  if [[ ${OS_NAME} == Darwin && -f /etc/ssl/cert.pem ]]; then
    printf '\nSSL_CERT_FILE=/etc/ssl/cert.pem\n' >>"${CONFIG_FILE}"
  fi
  echo "已创建配置：${CONFIG_FILE}"
else
  echo "保留现有配置：${CONFIG_FILE}"
fi

{
  echo '#!/usr/bin/env bash'
  echo 'set -euo pipefail'
  printf 'INSTALL_DIR=%q\n' "${INSTALL_DIR}"
  printf 'DEFAULT_CONFIG=%q\n' "${CONFIG_FILE}"
  printf 'DEFAULT_STATE=%q\n' "${STATE_DIR}"
  printf 'PYTHON_BIN=%q\n' "${PYTHON_BIN}"
  cat <<'EOF'
CONFIG_FILE=${OPTION_SCANNER_CONFIG:-${DEFAULT_CONFIG}}
STATE_DIR=${OPTION_SCANNER_STATE_DIR:-${DEFAULT_STATE}}
if [[ -f ${CONFIG_FILE} ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
  set +a
fi

: "${SCANNER_EXCHANGE:=all}"
: "${SCANNER_UNDERLYING:=}"
: "${SCANNER_INTERVAL:=60}"
: "${SCANNER_CATALOG_TTL:=21600}"
: "${SCANNER_ONCE:=0}"
: "${SCANNER_WORKERS:=8}"
: "${SCANNER_RATE_LIMIT:=8}"
: "${SCANNER_LIMIT:=20}"
: "${SCANNER_ALERT_LIMIT:=20}"
: "${SCANNER_MAX_LOG_MB:=100}"
: "${SCANNER_NOTIFY:=0}"

mkdir -p "${STATE_DIR}"
ARGS=(
  "${PYTHON_BIN}" -u "${INSTALL_DIR}/option_alert_daemon.py"
  --exchange "${SCANNER_EXCHANGE}"
  --interval "${SCANNER_INTERVAL}"
  --catalog-ttl "${SCANNER_CATALOG_TTL}"
  --cache "${STATE_DIR}/option_catalog.json"
  --log "${STATE_DIR}/option_alerts.jsonl"
  --max-log-mb "${SCANNER_MAX_LOG_MB}"
  --workers "${SCANNER_WORKERS}"
  --rate-limit "${SCANNER_RATE_LIMIT}"
  --limit "${SCANNER_LIMIT}"
  --alert-limit "${SCANNER_ALERT_LIMIT}"
)
[[ -n ${SCANNER_UNDERLYING} ]] && ARGS+=(--underlying "${SCANNER_UNDERLYING}")
[[ ${SCANNER_ONCE} == 1 ]] && ARGS+=(--once)
if [[ ${SCANNER_NOTIFY} == 1 ]]; then
  ARGS+=(--notify)
else
  ARGS+=(--dry-run)
fi
[[ -n ${SSL_CERT_FILE:-} ]] && export SSL_CERT_FILE
exec "${ARGS[@]}"
EOF
} >"${RUNNER}"
chmod 0755 "${RUNNER}"

"${PYTHON_BIN}" -m py_compile \
  "${INSTALL_DIR}/scanner_http.py" \
  "${INSTALL_DIR}/option_scanner_core.py" \
  "${INSTALL_DIR}/exchange_adapters.py" \
  "${INSTALL_DIR}/option_alert_daemon.py"

install_linux_service() {
  mkdir -p "$(dirname -- "${SYSTEMD_UNIT}")"
  cat >"${SYSTEMD_UNIT}" <<EOF
[Unit]
Description=Option surface scanner and Feishu alerts
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart="${RUNNER}"
WorkingDirectory="${INSTALL_DIR}"
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemd unit 已生成，但未找到 systemctl；可手动运行 ${RUNNER}。" >&2
    return
  fi
  systemctl --user daemon-reload
  if ((START_SERVICE)); then
    systemctl --user enable option-scanner.service >/dev/null
    systemctl --user restart option-scanner.service
    echo "systemd 用户服务已启动。"
  else
    echo "systemd 用户服务已安装但未启动。"
  fi
}

xml_escape() {
  "${PYTHON_BIN}" -c 'import html, sys; print(html.escape(sys.argv[1], quote=True), end="")' "$1"
}

install_macos_service() {
  local runner_xml install_xml stdout_xml stderr_xml
  runner_xml=$(xml_escape "${RUNNER}")
  install_xml=$(xml_escape "${INSTALL_DIR}")
  stdout_xml=$(xml_escape "${STATE_DIR}/service.stdout.log")
  stderr_xml=$(xml_escape "${STATE_DIR}/service.stderr.log")
  mkdir -p "$(dirname -- "${LAUNCHD_PLIST}")"
  cat >"${LAUNCHD_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>${runner_xml}</string></array>
  <key>WorkingDirectory</key><string>${install_xml}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>${stdout_xml}</string>
  <key>StandardErrorPath</key><string>${stderr_xml}</string>
</dict>
</plist>
EOF
  if ((START_SERVICE)); then
    launchctl bootout "gui/$(id -u)/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "${LAUNCHD_PLIST}"
    echo "macOS LaunchAgent 已启动。"
  else
    echo "macOS LaunchAgent 已安装但未启动。"
  fi
}

if ((INSTALL_SERVICE)); then
  case "${OS_NAME}" in
    Linux) install_linux_service ;;
    Darwin) install_macos_service ;;
    *) echo "不支持自动安装 ${OS_NAME} 服务；可手动运行 ${RUNNER}。" >&2 ;;
  esac
fi

echo
echo "安装完成："
echo "  代码：${INSTALL_DIR}"
echo "  配置：${CONFIG_FILE}"
echo "  状态：${STATE_DIR}"
echo "  手动运行：${RUNNER}"
echo "修改配置后重新运行 ./install.sh，或重启对应用户服务。"
