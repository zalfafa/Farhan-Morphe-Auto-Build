import unittest

from tools.resolve_version import collect_target_versions, parse_version_tuple, pick_supported_version


class ResolveVersionTests(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(parse_version_tuple("1.95.104"), (1, 95, 104))
        self.assertEqual(parse_version_tuple("7.92.0.977185651"), (7, 92, 0, 977185651))

    def test_collect_targets(self):
        data = {
            "patches": [{
                "compatiblePackages": [{
                    "packageName": "com.example.app",
                    "targets": [
                        {"version": "1.0.0"},
                        {"version": "2.0.0", "isExperimental": True},
                    ],
                }]
            }]
        }
        self.assertEqual(
            collect_target_versions(data, "com.example.app"),
            [("1.0.0", False), ("2.0.0", True)],
        )

    def test_collect_targets_hushgram_layout(self):
        data = {
            "patches": [{
                "compatiblePackages": {"com.example.app": ["3.0.0"]},
                "compatibility": [{
                    "packageName": "com.example.app",
                    "targets": [
                        {"version": "3.0.0", "experimental": False},
                        {"version": "4.0.0", "experimental": True},
                    ],
                }],
            }]
        }
        self.assertEqual(
            collect_target_versions(data, "com.example.app"),
            [("3.0.0", False), ("4.0.0", True)],
        )

    def test_experimental_flag_from_any_entry_wins(self):
        data = {
            "patches": [{
                "compatiblePackages": {"com.example.app": ["5.0.0"]},
                "compatibility": [{
                    "packageName": "com.example.app",
                    "targets": [{"version": "5.0.0", "experimental": True}],
                }],
            }]
        }
        targets = collect_target_versions(data, "com.example.app")
        self.assertEqual(targets, [("5.0.0", True)])
        self.assertEqual(pick_supported_version(targets), ("5.0.0", True))

    def test_prefers_highest_stable(self):
        self.assertEqual(
            pick_supported_version([("1.0.0", False), ("2.0.0", True), ("1.5.0", False)]),
            ("1.5.0", False),
        )

    def test_allows_experimental(self):
        self.assertEqual(
            pick_supported_version([("1.0.0", False), ("2.0.0", True)], allow_experimental=True),
            ("2.0.0", True),
        )


if __name__ == "__main__":
    unittest.main()
