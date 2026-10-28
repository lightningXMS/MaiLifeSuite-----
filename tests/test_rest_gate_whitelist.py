"""rest_gate 群闸门 selected 模式受控白名单回归。

覆盖合并新增的 `rest_gate.group_whitelist`：
- selected 模式下白名单内群受闸门管辖、名单外群不受管辖；
- 上游兼容路径（社交白名单 + 夜间静音）仍然生效；
- 配置校验器清洗（非数字报错、去重保序、分隔符输入）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLifeSuite.config import MaiLifeSuiteSettings
from MaiLifeSuite.life.rest_gate import RestGate


def make_gate(group_mode: str = "selected", whitelist: list[str] | None = None,
              social_groups: list[dict] | None = None) -> RestGate:
    config = MaiLifeSuiteSettings()
    config.rest_gate.group_mode = group_mode
    if whitelist is not None:
        config.rest_gate.group_whitelist = whitelist
    if social_groups is not None:
        config.social.groups = social_groups
    return RestGate(None, config, None, None, logging.getLogger("test"))


import logging


class GroupWhitelistCase(IsolatedAsyncioTestCase):
    def test_selected_mode_whitelisted_group_is_governed(self) -> None:
        gate = make_gate("selected", whitelist=["701", "702"])
        self.assertTrue(gate.group_gate_enabled_for("701"))
        self.assertTrue(gate.group_gate_enabled_for("702"))

    def test_selected_mode_unlisted_group_is_not_governed(self) -> None:
        gate = make_gate("selected", whitelist=["701"])
        self.assertFalse(gate.group_gate_enabled_for("999"))
        self.assertFalse(gate.group_gate_enabled_for(""))

    def test_all_mode_governs_every_group(self) -> None:
        gate = make_gate("all", whitelist=[])
        self.assertTrue(gate.group_gate_enabled_for("701"))
        self.assertTrue(gate.group_gate_enabled_for("anything"))

    def test_social_allowlist_compatibility_path_kept(self) -> None:
        """上游兼容：社交白名单中开启夜间静音的群在 selected 模式仍受管辖。"""
        social_group_enabled = {"group_id": "801", "enabled": True, "rest_gate_enabled": True}
        social_group_disabled = {"group_id": "802", "enabled": True, "rest_gate_enabled": False}
        gate = make_gate("selected", whitelist=[], social_groups=[social_group_enabled, social_group_disabled])
        self.assertTrue(gate.group_gate_enabled_for("801"))
        self.assertFalse(gate.group_gate_enabled_for("802"))

    def test_decide_group_blocks_whitelisted_group_in_window(self) -> None:
        """端到端行为：selected+白名单群在群夜窗内被静默，名单外群放行。"""
        gate = make_gate("selected", whitelist=["701"])
        gate.config.rest_gate.group_enabled = True
        gate.config.rest_gate.gate_mode = "time_window"
        # 群夜窗默认 22:30-08:00；把 now 固定在 23:00 落入窗内。
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 10, 28, 23, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        blocked, reason = gate.decide_group("在吗", now, "701")
        self.assertFalse(blocked)
        allowed, reason2 = gate.decide_group("在吗", now, "999")
        self.assertTrue(allowed)
        self.assertEqual(reason2, "group_not_enabled")

    def test_validator_rejects_non_numeric_and_dedupes(self) -> None:
        import warnings
        from pydantic import ValidationError
        cfg = MaiLifeSuiteSettings()
        with self.assertRaises(ValidationError):
            cfg.rest_gate.group_whitelist = ["abc群"]
        cfg.rest_gate.group_whitelist = ["701", " 701 ", "702"]
        self.assertEqual(cfg.rest_gate.group_whitelist, ["701", "702"])
        cfg.rest_gate.group_whitelist = "801, 802 801"
        self.assertEqual(cfg.rest_gate.group_whitelist, ["801", "802"])


if __name__ == "__main__":
    import unittest
    unittest.main()
