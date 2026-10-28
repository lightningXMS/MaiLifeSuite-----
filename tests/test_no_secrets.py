# -*- coding: utf-8 -*-
"""敏感信息检查测试化：当前树整合扫描回归 + 规则匹配单元测试。

单元测试全部使用虚构字符串（tvly-dev-FAKE... 等），不写入任何真实 Key 或账号；
真实标识只保存在 tools/check_secrets.py 的 FORBIDDEN 规则里（扫描时自排除）。
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import check_secrets


class NoSecretsCase(unittest.TestCase):
    """整合扫描：当前 git 跟踪树必须干净（含配置备份 / 环境文件名检测）。"""

    def test_tracked_files_contain_no_forbidden_identifiers(self) -> None:
        failures = check_secrets.scan_tree()
        self.assertEqual(failures, [], "跟踪文件中存在敏感标识:\n" + "\n".join(failures))


class ScannerRuleTests(unittest.TestCase):
    """规则匹配单元测试：虚构字符串命中即失败，干净文本必须通过。"""

    def test_fictional_tavily_dev_key_is_flagged(self) -> None:
        hits = check_secrets.find_forbidden('api_keys = ["tvly-dev-FAKE000000000example"]')
        self.assertTrue(any("tvly-dev-" in hit for hit in hits), hits)

    def test_fictional_tavily_production_key_is_flagged(self) -> None:
        hits = check_secrets.find_forbidden('api_keys = ["tvly-FAKEPROD0000000example"]')
        self.assertTrue(any("生产前缀" in hit for hit in hits), hits)

    def test_clean_text_has_no_hits(self) -> None:
        self.assertEqual(check_secrets.find_forbidden("api_keys = []\nadmin_qq = []\n"), [])

    def test_custom_fictional_rules_flag_secret_and_fail_check(self) -> None:
        rules = [("虚构泄漏标识", "FAKE-LEAK-TOKEN-0001")]
        self.assertEqual(
            check_secrets.find_forbidden("token = FAKE-LEAK-TOKEN-0001", rules),
            ["含虚构泄漏标识 FAKE-LEAK-TOKEN-0001"],
        )

    def test_rules_track_real_tavily_prefixes_not_the_typo(self) -> None:
        patterns = {pat for _name, pat in check_secrets.FORBIDDEN}
        self.assertIn("tvly-dev-", patterns)
        self.assertIn("tvly-", patterns)
        self.assertNotIn("t4y-dev", patterns)

    def test_backup_and_env_filenames_are_flagged(self) -> None:
        hits = check_secrets.find_forbidden_backup_paths([
            "config_back/config.toml.backup-20261028",
            "config.toml.backup-20261028",
            "data/.env",
            "plugin.py",
        ])
        self.assertEqual(len(hits), 3, hits)
        self.assertFalse(any("plugin.py" in hit for hit in hits))


if __name__ == "__main__":
    from unittest import main
    main()
