from __future__ import annotations

from pathlib import Path
import unittest

from tools.system_mode.adapters import (
    AdapterError,
    GenericInGuestAdapter,
    HostImagePlan,
    built_in_descriptors,
)


class AdapterContractTest(unittest.TestCase):
    def test_in_guest_adapter_requires_a_supported_doctor_report(self) -> None:
        adapter = GenericInGuestAdapter()
        self.assertTrue(adapter.supports({"assessment": {"verdict": "supported"}}))
        self.assertFalse(adapter.supports({"assessment": {"verdict": "blocked"}}))

    def test_host_plan_requires_three_distinct_paths(self) -> None:
        plan = HostImagePlan(
            source_image=Path("/vendor/live.img"),
            working_copy=Path("/vendor/live.img"),
            external_backup=Path("/backup/live.img"),
            restore_command=("restore", "live.img"),
            target_stopped=True,
        )
        with self.assertRaisesRegex(AdapterError, "distinct paths"):
            plan.validate()

    def test_host_plan_requires_a_stopped_target_and_restore_command(self) -> None:
        plan = HostImagePlan(
            source_image=Path("/vendor/live.img"),
            working_copy=Path("/work/copy.img"),
            external_backup=Path("/backup/live.img"),
            restore_command=(),
            target_stopped=False,
        )
        with self.assertRaisesRegex(AdapterError, "stopped target"):
            plan.validate()

    def test_tier_b_is_host_only_and_never_folded_into_the_apk_adapter(self) -> None:
        descriptors = {item.adapter_id: item for item in built_in_descriptors()}
        tier_a = descriptors["generic-in-guest"]
        tier_b = descriptors["generic-host-image"]
        self.assertEqual("in_guest", tier_a.execution)
        self.assertEqual("host_image", tier_b.execution)
        self.assertFalse(tier_a.requires_stopped_target)
        self.assertTrue(tier_b.requires_stopped_target)


if __name__ == "__main__":
    unittest.main()
