"""Clock-paced recording independent of the slower visual control loop."""
import math
import queue
import threading
import time
import cv2


class PetVideoError(RuntimeError):
    """An optional video artifact failed, not the pet detector/controller."""


def record_frames(cap, writer, first_frame, duration, fps, callback=None):
    latest = queue.Queue(maxsize=1)
    stop = threading.Event()
    done = threading.Event()
    errors = []
    stats = {"written_frames": 0, "analysis_frames": 0, "missed_frames": 0}
    started = time.monotonic()
    deadline = started + duration

    def capture():
        try:
            for index in range(math.ceil(duration * fps)):
                if stop.wait(max(0.0, started + index / fps - time.monotonic())):
                    break
                if time.monotonic() >= deadline:
                    break
                ok, frame = (True, first_frame) if index == 0 else cap.read()
                if not ok or frame is None:
                    stats["missed_frames"] += 1
                    continue
                # The writer owns unannotated camera frames. Analysis receives
                # a separate copy and may drop old analysis frames, not video.
                if frame.shape[:2] != first_frame.shape[:2]:
                    frame = cv2.resize(frame, (first_frame.shape[1], first_frame.shape[0]))
                writer.write(frame)
                stats["written_frames"] += 1
                try:
                    latest.get_nowait()
                except queue.Empty:
                    pass
                latest.put_nowait(frame.copy())
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=capture, name="pet-video-recorder", daemon=True)
    worker.start()
    try:
        while not done.is_set() or not latest.empty():
            if time.monotonic() >= deadline:
                break
            try:
                frame = latest.get(timeout=min(.1, max(.001, deadline-time.monotonic())))
            except queue.Empty:
                continue
            if callback is not None:
                # Keep motor control on the caller thread; no callback can
                # issue a late motion command after this function returns.
                callback(frame)
                stats["analysis_frames"] += 1
    finally:
        stop.set()
        worker.join()
    stats["recording_elapsed_sec"] = round(time.monotonic()-started, 3)
    if errors:
        raise PetVideoError(f"post_detection_video_capture_failed:{errors[0]}")
    return stats
