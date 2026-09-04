from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "robot_skills" / "realtime_information" / "run.py"
SPEC = importlib.util.spec_from_file_location("realtime_information_run", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RealtimeLocationTests(unittest.TestCase):
    def test_conversational_weather_prefix_does_not_override_home(self) -> None:
        self.assertIsNone(
            MODULE.location_from_query(
                "我等会儿要出门了，今天的天气适合穿什么衣服？",
                "weather",
            )
        )
        self.assertIsNone(MODULE.location_from_query("今天天气怎么样？", "weather"))

    def test_explicit_weather_locations_are_preserved(self) -> None:
        self.assertEqual(MODULE.location_from_query("北京今天天气怎么样？", "weather"), "北京")
        self.assertEqual(MODULE.location_from_query("公司今天天气怎么样？", "weather"), "公司")
        self.assertEqual(
            MODULE.location_from_query("北京市顺义区现在的天气怎么样？", "weather"),
            "北京市顺义区",
        )

    def test_saved_company_keeps_its_own_district(self) -> None:
        config = MODULE.load_config()
        client = MODULE.Client(config)
        company = MODULE.saved_place_location(client, "公司")
        self.assertIsNotNone(company)
        self.assertEqual(company["district_id"], "110113")
        self.assertEqual(MODULE.baidu_district_id(client, company, "unused"), "110113")

    def test_default_home_is_beijing_chaoyang(self) -> None:
        config = MODULE.load_config()
        client = MODULE.Client(config)
        args = argparse.Namespace(
            latitude=None,
            longitude=None,
            coordinate_system=None,
            location=None,
            query="我等会儿要出门了，今天的天气适合穿什么衣服？",
            action="weather",
        )
        location = MODULE.resolve_location(client, args)
        self.assertEqual(location["name"], "请配置家庭地址")
        self.assertAlmostEqual(location["latitude"], 0.0)
        self.assertAlmostEqual(location["longitude"], 0.0)

    def test_low_confidence_noclass_baidu_result_is_rejected(self) -> None:
        config = MODULE.load_config()
        client = MODULE.Client(config)
        client.config = json.loads(json.dumps(config))
        client.config["endpoints"]["open_meteo_geocoding"] = "open-meteo-test"
        responses = [
            ({
                "status": 0,
                "result": {
                    "location": {"lat": 45.0, "lng": 114.0},
                    "confidence": 50,
                    "level": "NoClass",
                },
            }, False),
            ({"results": []}, False),
        ]
        with mock.patch.object(client, "json_request", side_effect=responses):
            with self.assertRaisesRegex(MODULE.QueryError, "location_not_found"):
                MODULE.geocode(client, "我等会儿要出门了")


if __name__ == "__main__":
    unittest.main()
