#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import sys

SKILL_NAME = "fan_control"
DISABLED_REASON = "fan_control / feeder_control / light_control 的旧家电控制实现已删除，等待重新实现；当前不会执行任何硬件控制。"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"{SKILL_NAME} is disabled.")
    parser.add_argument("args", nargs="*")
    parser.parse_args(argv)
    print(json.dumps({
        "ok": False,
        "skill": SKILL_NAME,
        "disabled": True,
        "message": DISABLED_REASON,
    }, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())
