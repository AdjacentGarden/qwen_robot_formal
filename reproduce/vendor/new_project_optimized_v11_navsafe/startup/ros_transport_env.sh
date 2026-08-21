#!/usr/bin/env bash
# This file is sourced by every V8 ROS process. It never edits /dev/shm and it
# can be rolled back by setting V8_DDS_TRANSPORT=system_default.

PROJECT="/home/test/new_project_optimized_v11_navsafe"
TRANSPORT_CONFIG="$PROJECT/config/ros_transport.env"
if [ -z "${V8_DDS_TRANSPORT+x}" ] && [ -f "$TRANSPORT_CONFIG" ]; then
    # shellcheck disable=SC1090
    source "$TRANSPORT_CONFIG"
fi

case "${V8_DDS_TRANSPORT:-udp}" in
    cyclone)
        export RMW_IMPLEMENTATION="rmw_cyclonedds_cpp"
        unset FASTRTPS_DEFAULT_PROFILES_FILE
        unset FASTDDS_DEFAULT_PROFILES_FILE
        export CYCLONEDDS_URI="file://$PROJECT/config/cyclonedds_local.xml"
        ;;
    udp)
        export RMW_IMPLEMENTATION="rmw_fastrtps_cpp"
        profile="$PROJECT/config/fastdds_udp_only.xml"
        export FASTRTPS_DEFAULT_PROFILES_FILE="$profile"
        export FASTDDS_DEFAULT_PROFILES_FILE="$profile"
        unset CYCLONEDDS_URI
        ;;
    system_default)
        export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"
        unset FASTRTPS_DEFAULT_PROFILES_FILE
        unset FASTDDS_DEFAULT_PROFILES_FILE
        unset CYCLONEDDS_URI
        ;;
    *)
        echo "Unsupported V8_DDS_TRANSPORT=${V8_DDS_TRANSPORT}" >&2
        return 2 2>/dev/null || exit 2
        ;;
esac
