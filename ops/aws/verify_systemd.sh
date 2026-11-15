#!/usr/bin/env bash
set -uo pipefail

POLICY_SERVICE="youthlink-policy-pipeline.service"
WEB_SERVICE="youthlink-web-pipeline.service"
POLICY_TIMER="youthlink-policy-pipeline.timer"
FAILED=0

fail() {
    echo "check_error=$1" >&2
    FAILED=1
}

property() {
    systemctl show "$1" --property="$2" --value 2>/dev/null
}

if ! command -v systemctl >/dev/null 2>&1; then
    echo "check_error=systemd_not_available" >&2
    exit 1
fi

echo "verification=youthlink_aws_systemd"
echo "systemd_version=$(systemctl --version | head -n 1)"

for unit in "${POLICY_SERVICE}" "${WEB_SERVICE}" "${POLICY_TIMER}"; do
    load_state="$(property "${unit}" LoadState)"
    echo "${unit}.load_state=${load_state:-unknown}"
    if [[ "${load_state}" != "loaded" ]]; then
        fail "${unit}_not_loaded"
    fi
done

timer_enabled="$(systemctl is-enabled "${POLICY_TIMER}" 2>/dev/null || true)"
timer_active="$(systemctl is-active "${POLICY_TIMER}" 2>/dev/null || true)"
timer_next="$(property "${POLICY_TIMER}" NextElapseUSecRealtime)"
echo "timer.enabled=${timer_enabled:-unknown}"
echo "timer.active=${timer_active:-unknown}"
echo "timer.next=${timer_next:-unknown}"
if [[ "${timer_enabled}" != "enabled" ]]; then
    fail "timer_not_enabled"
fi
if [[ "${timer_active}" != "active" ]]; then
    fail "timer_not_active"
fi

for unit in "${POLICY_SERVICE}" "${WEB_SERVICE}"; do
    active_state="$(property "${unit}" ActiveState)"
    result="$(property "${unit}" Result)"
    exit_status="$(property "${unit}" ExecMainStatus)"
    started_at="$(property "${unit}" ExecMainStartTimestamp)"
    finished_at="$(property "${unit}" ExecMainExitTimestamp)"
    echo "${unit}.active_state=${active_state:-unknown}"
    echo "${unit}.result=${result:-unknown}"
    echo "${unit}.exit_status=${exit_status:-unknown}"
    echo "${unit}.started_at=${started_at:-never}"
    echo "${unit}.finished_at=${finished_at:-never}"

    if [[ -z "${started_at}" ]]; then
        fail "${unit}_has_not_run"
    elif [[ "${active_state}" == "active" || "${active_state}" == "activating" ]]; then
        fail "${unit}_is_still_running"
    elif [[ -z "${finished_at}" ]]; then
        fail "${unit}_has_not_finished"
    elif [[ "${result}" != "success" || "${exit_status}" != "0" ]]; then
        fail "${unit}_last_run_failed"
    fi
done

repo_dir="$(property "${POLICY_SERVICE}" WorkingDirectory)"
service_user="$(property "${POLICY_SERVICE}" User)"
python_path="${repo_dir}/.venv/bin/python"
health_script="${repo_dir}/etl/check_pipeline_health.py"

echo "service_user=${service_user:-unknown}"
echo "repo_dir=${repo_dir:-unknown}"

if [[ -z "${repo_dir}" || ! -x "${python_path}" || ! -f "${health_script}" ]]; then
    fail "runtime_path_invalid"
else
    echo "database_health_begin"
    if [[ "$(id -u)" -eq 0 && "$(id -un)" != "${service_user}" ]]; then
        (
            cd "${repo_dir}" || exit 1
            runuser -u "${service_user}" -- \
                "${python_path}" etl/check_pipeline_health.py --json
        ) || fail "database_health_failed"
    elif [[ "$(id -un)" == "${service_user}" ]]; then
        (
            cd "${repo_dir}" || exit 1
            "${python_path}" etl/check_pipeline_health.py --json
        ) || fail "database_health_failed"
    else
        fail "run_with_sudo_or_as_service_user"
    fi
    echo "database_health_end"
fi

if [[ "${FAILED}" -ne 0 ]]; then
    echo "verification_result=FAILED"
    echo "next_step=journalctl -u youthlink-policy-pipeline.service -u youthlink-web-pipeline.service -n 100 --no-pager"
    exit 1
fi

echo "verification_result=OK"
