from pathlib import Path


ROOT = Path(__file__).resolve().parent
IMMERSIVE = "settings put global policy_control 'immersive.full=*'"


def test_every_video_projection_entrypoint_enables_immersive_fullscreen():
    entrypoints = {
        "fitness": ROOT / "projector_control/system/robot-start-exercise-projection",
        "movie": ROOT / "media_player/system/robot-media-player",
    }

    for name, path in entrypoints.items():
        source = path.read_text(encoding="utf-8")
        assert IMMERSIVE in source, f"{name} does not enforce Android immersive fullscreen"


def test_ppt_entrypoint_enables_immersive_fullscreen():
    source = (ROOT / "projector_control/system/robot-meeting-projection-v2").read_text(encoding="utf-8")
    activity = (
        ROOT
        / "projector_control/android_meeting_viewer/src/com/adjacentgarden/meeting/MeetingSlidesActivity.java"
    ).read_text(encoding="utf-8")
    assert "settings put global policy_control 'immersive.full=*'" in source
    assert "/sdcard/Pictures/test-1.jpg" in source
    assert "/sdcard/Pictures/test-2.jpg" in source
    assert "meeting-image-viewer.apk" in source
    assert "com.adjacentgarden.meeting/.MeetingSlidesActivity" in source
    assert "input tap" not in source
    assert "VideoPlayActivity" not in source
    assert "video/mp4" not in source
    assert "loop-pause" in source
    assert "loop-resume" in source
    assert "kill -STOP" not in source
    assert "am broadcast" in source
    assert "SYSTEM_UI_FLAG_IMMERSIVE_STICKY" in activity
    assert "SYSTEM_UI_FLAG_HIDE_NAVIGATION" in activity
    assert "controller.hide(WindowInsets.Type.systemBars())" in activity
    assert "R.drawable.meeting_slide_1" in activity
    assert "R.drawable.meeting_slide_2" in activity


def test_short_video_entrypoints_hide_controls_immediately():
    entrypoints = (
        ROOT / "projector_control/system/robot-start-exercise-projection",
        ROOT / "media_player/system/robot-media-player",
    )
    for path in entrypoints:
        assert "sleep 1.0; input tap 960 500" in path.read_text(encoding="utf-8")


def test_welcome_image_is_upside_down_and_immersive_fullscreen():
    source = (ROOT / "welcome_projection/system/robot-welcome-projection").read_text(encoding="utf-8")
    assert IMMERSIVE in source
    assert "user_rotation 2" in source
    assert "user-rotation lock 2" in source
    assert "/sdcard/Pictures/welcome_home.png" in source
    assert "welcome-image-viewer.apk" in source
    assert "com.adjacentgarden.welcome/.WelcomeImageActivity" in source
    assert "VideoPlayActivity" not in source
