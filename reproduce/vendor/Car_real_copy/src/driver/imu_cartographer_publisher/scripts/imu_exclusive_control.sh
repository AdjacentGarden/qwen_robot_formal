#!/bin/sh
set -eu

DEVICE="${IMU_I2C_DEVICE:-4-006a}"
DRIVER="${IMU_KERNEL_DRIVER:-st_lsm6dsx_i2c}"
DRIVER_DIR="/sys/bus/i2c/drivers/${DRIVER}"
DEVICE_DRIVER="/sys/bus/i2c/devices/${DEVICE}/driver"
PROXY_SERVICE="iio-sensor-proxy.service"

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "imu_exclusive_control must run as root" >&2
        exit 1
    fi
}

acquire() {
    require_root
    # A plain stop is insufficient because this static D-Bus service can be
    # activated again. The runtime mask lasts only until reboot.
    systemctl mask --runtime --now "${PROXY_SERVICE}" >/dev/null

    if [ -L "${DEVICE_DRIVER}" ]; then
        bound_driver="$(basename "$(readlink -f "${DEVICE_DRIVER}")")"
        if [ "${bound_driver}" != "${DRIVER}" ]; then
            echo "refusing to unbind ${DEVICE}: unexpected driver ${bound_driver}" >&2
            exit 1
        fi
        printf '%s' "${DEVICE}" > "${DRIVER_DIR}/unbind"
    fi

    if [ -L "${DEVICE_DRIVER}" ]; then
        echo "failed to unbind ${DEVICE} from ${DRIVER}" >&2
        exit 1
    fi
    echo "exclusive IMU ownership prepared: ${DEVICE} is unbound; ${PROXY_SERVICE} is runtime-masked"
}

release() {
    require_root
    if [ ! -L "${DEVICE_DRIVER}" ] && [ -e "${DRIVER_DIR}/bind" ]; then
        printf '%s' "${DEVICE}" > "${DRIVER_DIR}/bind" || true
    fi
    systemctl unmask --runtime "${PROXY_SERVICE}" >/dev/null || true
    systemctl start "${PROXY_SERVICE}" || true
    echo "exclusive IMU ownership released"
}

status() {
    if [ -L "${DEVICE_DRIVER}" ]; then
        echo "kernel_driver=$(readlink -f "${DEVICE_DRIVER}")"
    else
        echo "kernel_driver=unbound"
    fi
    echo "iio_sensor_proxy=$(systemctl is-active "${PROXY_SERVICE}" 2>/dev/null || true)"
}

case "${1:-}" in
    acquire) acquire ;;
    release) release ;;
    status) status ;;
    *) echo "usage: $0 {acquire|release|status}" >&2; exit 2 ;;
esac
