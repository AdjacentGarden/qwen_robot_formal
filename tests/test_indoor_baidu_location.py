import argparse
import importlib.util
import json
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "robot_skills" / "realtime_information" / "run.py"
SPEC = importlib.util.spec_from_file_location("realtime_information_run_test", MODULE_PATH)
run = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = run
SPEC.loader.exec_module(run)


class IndoorLocationTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((MODULE_PATH.parent / "config.json").read_text(encoding="utf-8"))
        self.client = run.Client(self.config)
        self.args = argparse.Namespace(
            action="indoor_location", query="", location=None, latitude=None,
            longitude=None, coordinate_system="wgs84", radius=None, limit=None,
        )

    def test_default_and_external_routing(self):
        indoor = ["你现在在哪里", "机器人当前所在位置", "你在客厅还是书房", "现在在哪个房间", "你在家里哪个区域"]
        external = ["你现在在哪个城市", "GPS定位在哪里", "告诉我经纬度", "你在哪个街道", "外部地理位置是什么"]
        for text in indoor:
            self.assertEqual(run.location_action_for_query(text), "indoor_location", text)
        for text in external:
            self.assertEqual(run.location_action_for_query(text), "external_location", text)

    def test_room_classification(self):
        self.assertEqual(run.classify_indoor_room(self.config, 0.05, -0.05)["display_name"], "餐厅")
        self.assertEqual(run.classify_indoor_room(self.config, -2.15, 0.12)["display_name"], "客厅")
        self.assertEqual(run.classify_indoor_room(self.config, 0.1, 2.95)["display_name"], "书房")
        self.assertEqual(run.classify_indoor_room(self.config, 8.0, 8.0)["name"], "unknown")

    def test_indoor_query_uses_map_pose(self):
        payload = run.indoor_location_query(
            self.client,
            self.args,
            pose_provider=lambda _config: {"available": True, "x": -2.2, "y": 0.1, "yaw": 0.0},
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "indoor_location")
        self.assertEqual(payload["source"], "map_tf")
        self.assertEqual(payload["result"]["room"]["display_name"], "客厅")

    def test_missing_map_never_falls_back_to_external_location(self):
        payload = run.indoor_location_query(
            self.client,
            self.args,
            pose_provider=lambda _config: {"available": False, "error": "test_no_tf"},
        )
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["source"], "map_tf_unavailable")
        self.assertNotIn("请配置家庭地址", payload["message"])

    def test_external_location_is_configured_and_disclaims_gps(self):
        self.args.action = "external_location"
        self.args.query = "你现在在哪个城市"
        payload = run.external_location_query(self.client, self.args)
        self.assertEqual(payload["action"], "external_location")
        self.assertIn("请配置家庭地址", payload["message"])
        self.assertIn("不代表", payload["message"])

    def test_legacy_location_auto_routes(self):
        self.args.action = "location"
        self.args.query = "你现在在哪里"
        indoor = run.location_query(
            self.client,
            self.args,
        )
        self.assertEqual(indoor["action"], "indoor_location")
        self.args.query = "你现在在哪个城市"
        external = run.location_query(self.client, self.args)
        self.assertEqual(external["action"], "external_location")


if __name__ == "__main__":
    unittest.main()
