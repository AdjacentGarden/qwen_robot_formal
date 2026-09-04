#!/usr/bin/env python3
import argparse
import time

from ros_robot_controller.ros_robot_controller_sdk import Board


def main():
    parser = argparse.ArgumentParser(
        description='Single-board S0 duplex test: send motor speed and read feedback on one Board instance.'
    )
    parser.add_argument('--device', default='/dev/ttyS0')
    parser.add_argument('--baudrate', type=int, default=115200)
    parser.add_argument('--duration', type=float, default=10.0)
    parser.add_argument('--send-rate', type=float, default=20.0)
    parser.add_argument('--speed', type=float, default=25.0)
    parser.add_argument('--switch-period', type=float, default=2.0)
    args = parser.parse_args()

    board = Board(device=args.device, baudrate=args.baudrate, timeout=0.1)
    board.enable_reception(True)
    print(f'[INFO] start duplex test on {args.device} @ {args.baudrate}')
    print('[INFO] do not run ros_robot_controller or motor_speed_reader at the same time on the same serial device')

    period = 1.0 / max(1.0, args.send_rate)
    start = time.monotonic()

    try:
        while time.monotonic() - start < args.duration:
            elapsed = time.monotonic() - start
            direction = 1.0 if int(elapsed / max(0.1, args.switch_period)) % 2 == 0 else -1.0

            right_target = args.speed * direction
            left_target = -args.speed * direction
            board.set_motor_speed([[1, right_target], [2, left_target]])

            feedback = board.get_motor_speed()
            if feedback is None:
                print(f'[T={elapsed:6.2f}s] tx right={right_target:7.2f} left={left_target:7.2f} | rx none')
            else:
                left_fb, right_fb = feedback
                print(
                    f'[T={elapsed:6.2f}s] tx right={right_target:7.2f} left={left_target:7.2f} '
                    f'| rx left={left_fb:7.2f} right={right_fb:7.2f}'
                )
            time.sleep(period)
    except KeyboardInterrupt:
        print('\n[INFO] interrupted by user')
    finally:
        board.set_motor_speed([[1, 0.0], [2, 0.0]])
        board.close()
        print('[INFO] stop motors and close serial')


if __name__ == '__main__':
    main()
