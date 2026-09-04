from pathlib import Path
import hashlib
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "robot_skills" / "projector_control"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


class MeetingViewerContractTests(unittest.TestCase):
    def test_approved_slides_are_bundled_without_conversion(self) -> None:
        self.assertEqual(
            sha256(SKILL / "assets" / "test-1.jpg"),
            "1a15506352fe68dffdf8e61df85703e93e27e370ada8b8db6cf46b5a7880d542",
        )
        self.assertEqual(
            sha256(SKILL / "assets" / "test-2.jpg"),
            "84ea835ad85b415edf8e228790b9c93fe53c3f2eebba13df7d612745863c8e83",
        )

    def test_one_activity_preloads_and_switches_both_slides(self) -> None:
        source = (
            SKILL
            / "android_meeting_viewer"
            / "src"
            / "com"
            / "adjacentgarden"
            / "meeting"
            / "MeetingSlidesActivity.java"
        ).read_text(encoding="utf-8")
        self.assertIn("BitmapFactory.decodeResource", source)
        self.assertIn("R.drawable.meeting_slide_1", source)
        self.assertIn("R.drawable.meeting_slide_2", source)
        self.assertIn("SLIDE_INTERVAL_MS = 3000L", source)
        self.assertIn("imageView.setImageBitmap", source)
        self.assertIn("ACTION_PAUSE", source)
        self.assertIn("ACTION_RESUME", source)
        self.assertIn("handler.removeCallbacks(advanceSlide)", source)
        self.assertNotIn("GalleryActivity", source)

    def test_viewer_hides_both_android_system_bars(self) -> None:
        source = (
            SKILL
            / "android_meeting_viewer"
            / "src"
            / "com"
            / "adjacentgarden"
            / "meeting"
            / "MeetingSlidesActivity.java"
        ).read_text(encoding="utf-8")
        self.assertIn("SYSTEM_UI_FLAG_IMMERSIVE_STICKY", source)
        self.assertIn("SYSTEM_UI_FLAG_HIDE_NAVIGATION", source)
        self.assertIn("controller.hide(WindowInsets.Type.systemBars())", source)

    def test_helper_does_not_restart_gallery_between_slides(self) -> None:
        helper = (SKILL / "system" / "robot-meeting-projection-v2").read_text(
            encoding="utf-8"
        )
        self.assertIn("com.adjacentgarden.meeting/.MeetingSlidesActivity", helper)
        self.assertIn("1a15506352fe68dffdf8e61df85703e93e27e370ada8b8db6cf46b5a7880d542", helper)
        self.assertIn("84ea835ad85b415edf8e228790b9c93fe53c3f2eebba13df7d612745863c8e83", helper)
        self.assertNotIn("while true", helper)
        self.assertNotIn("input tap", helper)
        self.assertNotIn("am start -S -n \"$GALLERY", helper)
        self.assertIn("[q]wen_meeting_image_loop[.]sh", helper)
        self.assertIn("kill -CONT", helper)
        self.assertIn("kill -KILL", helper)
        self.assertIn("am broadcast -a \"$VIEWER_PAUSE_ACTION\"", helper)
        self.assertIn("am broadcast -a \"$VIEWER_RESUME_ACTION\"", helper)
        self.assertNotIn("kill -STOP", helper)
        loop_stop = helper[helper.index("loop-stop)"):helper.index("loop-pause)")]
        self.assertIn('am force-stop "$VIDEO_PACKAGE"', loop_stop)

    def test_existing_control_surface_is_preserved(self) -> None:
        helper = (SKILL / "system" / "robot-meeting-projection-v2").read_text(
            encoding="utf-8"
        )
        for action in ("loop-start", "loop-stop", "loop-pause", "loop-resume", "loop-status"):
            self.assertIn(action, helper)

    def test_projector_routes_pause_and_stop_to_distinct_commands(self) -> None:
        source = (SKILL / "run.py").read_text(encoding="utf-8")
        turn_off = source[source.index("def turn_projector_off"):source.index("def start_meeting_presentation")]
        meeting_scroll = source[source.index("def meeting_scroll"):source.index("def hold_meeting_presentation")]
        self.assertIn('"meeting_presentation_stop_command"', turn_off)
        self.assertNotIn('"meeting_presentation_pause_command"', turn_off)
        self.assertIn('"meeting_presentation_pause_command"', meeting_scroll)


if __name__ == "__main__":
    unittest.main()
