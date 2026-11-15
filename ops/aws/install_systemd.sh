#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

REPO_DIR="${DEFAULT_REPO_DIR}"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
ON_CALENDAR="*-*-* 04:30:00 Asia/Seoul"
CHECK_ONLY=false

usage() {
    cat <<'EOF'
Usage:
  sudo bash ops/aws/install_systemd.sh [options]

Options:
  --repo-dir PATH       Absolute repository path (default: detected path)
  --service-user USER   Linux account that owns and runs the ETL
  --calendar VALUE      systemd OnCalendar value
                        (default: *-*-* 04:30:00 Asia/Seoul)
  --check-only          Validate prerequisites without changing systemd
  -h, --help            Show this help
EOF
}

while (($# > 0)); do
    case "$1" in
        --repo-dir)
            REPO_DIR="${2:?--repo-dir requires a value}"
            shift 2
            ;;
        --service-user)
            SERVICE_USER="${2:?--service-user requires a value}"
            shift 2
            ;;
        --calendar)
            ON_CALENDAR="${2:?--calendar requires a value}"
            shift 2
            ;;
        --check-only)
            CHECK_ONLY=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ "${REPO_DIR}" != /* ]]; then
    echo "--repo-dir must be an absolute Linux path" >&2
    exit 2
fi
if [[ "${REPO_DIR}" =~ [[:space:]] ]]; then
    echo "Repository paths containing whitespace are not supported" >&2
    exit 2
fi
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "Linux service user does not exist: ${SERVICE_USER}" >&2
    exit 2
fi

SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"
PYTHON_PATH="${REPO_DIR}/.venv/bin/python"
ENV_PATH="${REPO_DIR}/.env"
REQUIRED_FILES=(
    "${REPO_DIR}/requirements.txt"
    "${REPO_DIR}/etl/run_policy_pipeline.py"
    "${REPO_DIR}/etl/run_web_pipeline.py"
    "${REPO_DIR}/etl/check_pipeline_health.py"
    "${SCRIPT_DIR}/youthlink-policy-pipeline.service.template"
    "${SCRIPT_DIR}/youthlink-web-pipeline.service.template"
    "${SCRIPT_DIR}/youthlink-policy-pipeline.timer.template"
)

for required_file in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "${required_file}" ]]; then
        echo "Required file is missing: ${required_file}" >&2
        exit 1
    fi
done
if [[ ! -x "${PYTHON_PATH}" ]]; then
    echo "Python virtual environment is missing: ${PYTHON_PATH}" >&2
    echo "Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
if [[ ! -f "${ENV_PATH}" ]]; then
    echo "Environment file is missing: ${ENV_PATH}" >&2
    echo "Copy .env.example to .env, fill it, and run: chmod 600 .env" >&2
    exit 1
fi

run_as_service_user() {
    if [[ "$(id -u)" -eq 0 && "$(id -un)" != "${SERVICE_USER}" ]]; then
        runuser -u "${SERVICE_USER}" -- "$@"
    else
        if [[ "$(id -un)" != "${SERVICE_USER}" ]]; then
            echo "Run validation as ${SERVICE_USER}, or run the installer with sudo" >&2
            return 1
        fi
        "$@"
    fi
}

run_as_service_user "${PYTHON_PATH}" -c \
    "import psycopg, dotenv, requests, openai, lxml" \
    || {
        echo "The virtual environment does not contain all required packages" >&2
        exit 1
    }

run_as_service_user "${PYTHON_PATH}" -c '
import sys
from dotenv import dotenv_values

required = (
    "DATABASE_URL",
    "YOUTH_API_URL",
    "YOUTH_API_KEY",
    "LITELLM_API_KEY",
    "LITELLM_BASE_URL",
    "LITELLM_EMBEDDING_MODEL",
    "LITELLM_CHAT_MODEL",
)
values = dotenv_values(sys.argv[1])
missing = [name for name in required if not str(values.get(name, "")).strip()]
if missing:
    print("Missing required environment names: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
' "${ENV_PATH}"

if command -v stat >/dev/null 2>&1; then
    ENV_MODE="$(stat -c '%a' "${ENV_PATH}")"
    if [[ "${ENV_MODE}" != "600" && "${ENV_MODE}" != "400" ]]; then
        echo "${ENV_PATH} must not be readable by other users (run: chmod 600 .env)" >&2
        exit 1
    fi
fi

echo "preflight=ok"
echo "repo=${REPO_DIR}"
echo "service_user=${SERVICE_USER}"
echo "calendar=${ON_CALENDAR}"

if [[ "${CHECK_ONLY}" == true ]]; then
    exit 0
fi
if [[ "$(id -u)" -ne 0 ]]; then
    echo "Installing system services requires sudo" >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemd is not available on this server" >&2
    exit 1
fi

escape_sed_replacement() {
    printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

REPO_ESCAPED="$(escape_sed_replacement "${REPO_DIR}")"
USER_ESCAPED="$(escape_sed_replacement "${SERVICE_USER}")"
GROUP_ESCAPED="$(escape_sed_replacement "${SERVICE_GROUP}")"
PYTHON_ESCAPED="$(escape_sed_replacement "${PYTHON_PATH}")"
CALENDAR_ESCAPED="$(escape_sed_replacement "${ON_CALENDAR}")"

render_unit() {
    local source_path="$1"
    local destination_path="$2"
    sed \
        -e "s|@REPO_DIR@|${REPO_ESCAPED}|g" \
        -e "s|@SERVICE_USER@|${USER_ESCAPED}|g" \
        -e "s|@SERVICE_GROUP@|${GROUP_ESCAPED}|g" \
        -e "s|@PYTHON_PATH@|${PYTHON_ESCAPED}|g" \
        -e "s|@ON_CALENDAR@|${CALENDAR_ESCAPED}|g" \
        "${source_path}" > "${destination_path}"
    chmod 0644 "${destination_path}"
}

render_unit \
    "${SCRIPT_DIR}/youthlink-policy-pipeline.service.template" \
    "/etc/systemd/system/youthlink-policy-pipeline.service"
render_unit \
    "${SCRIPT_DIR}/youthlink-web-pipeline.service.template" \
    "/etc/systemd/system/youthlink-web-pipeline.service"
render_unit \
    "${SCRIPT_DIR}/youthlink-policy-pipeline.timer.template" \
    "/etc/systemd/system/youthlink-policy-pipeline.timer"

systemd-analyze verify \
    /etc/systemd/system/youthlink-policy-pipeline.service \
    /etc/systemd/system/youthlink-web-pipeline.service \
    /etc/systemd/system/youthlink-policy-pipeline.timer
systemctl daemon-reload
systemctl enable --now youthlink-policy-pipeline.timer
systemctl reset-failed youthlink-policy-pipeline.service youthlink-web-pipeline.service || true

echo "installed=ok"
systemctl list-timers youthlink-policy-pipeline.timer --no-pager
