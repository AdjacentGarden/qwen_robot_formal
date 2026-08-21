#!/usr/bin/env bash
set -euo pipefail

PROJECT="/home/test/new_project_optimized_v11_navsafe"
CAR="/home/test/Car_real_copy_v11_navsafe"
UNIT="v11-navsafe-navigation.service"
RUNTIME="$PROJECT/runtime/startup"
LOCK="$RUNTIME/navigation_recovery.lock"
HEALTH="$PROJECT/startup/nav2_health_check.py"

mkdir -p "$RUNTIME"
exec 9>"$LOCK"
if ! flock -w 5 9; then
    printf '{"ok":false,"error":"navigation_recovery_lock_timeout"}\n'
    exit 11
fi

set +u
source "$PROJECT/startup/ros_transport_env.sh"
source /opt/ros/humble/setup.bash
source "$CAR/install/setup.bash"
set -u

if ! systemctl --user cat "$UNIT" >/dev/null 2>&1; then
    printf '{"ok":false,"error":"navigation_unit_not_loaded","unit":"%s"}\n' "$UNIT"
    exit 12
fi

started=$(date +%s)
systemctl --user restart "$UNIT"

deadline=$((SECONDS + 90))
stable=0
checks=0
while (( SECONDS < deadline )); do
    state=$(systemctl --user show "$UNIT" --property=ActiveState --value 2>/dev/null || true)
    if [[ "$state" == "failed" || "$state" == "inactive" || -z "$state" ]]; then
        printf '{"ok":false,"error":"navigation_unit_stopped_during_recovery","state":"%s"}\n' "$state"
        exit 13
    fi
    checks=$((checks + 1))
    if timeout 7 python3 "$HEALTH" --timeout 4 >/dev/null 2>&1; then
        stable=$((stable + 1))
        if (( stable >= 3 )); then
            elapsed=$(( $(date +%s) - started ))
            printf '{"ok":true,"unit":"%s","stable_checks":%d,"checks":%d,"elapsed_sec":%d}\n' \
                "$UNIT" "$stable" "$checks" "$elapsed"
            exit 0
        fi
    else
        stable=0
    fi
    sleep 0.3
done

printf '{"ok":false,"error":"navigation_recovery_health_timeout","checks":%d}\n' "$checks"
exit 14
