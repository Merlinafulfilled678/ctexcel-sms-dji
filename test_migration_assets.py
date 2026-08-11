from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DRIVER_ROOT = ROOT / "drivers" / "Quectel-Ports-30.0.65.2"


class MigrationAssetTests(unittest.TestCase):
    def test_requirements_match_the_validated_runtime(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            requirements,
            [
                "Flask==3.1.3",
                "pyserial==3.5",
                "requests==2.34.2",
            ],
        )

    def test_bundled_driver_hashes_match_the_verified_package(self) -> None:
        expected = {
            "qcser.cat": "84511642502CF1398C6B859303C1AD87FA1BEF6CCA65CDA2CCF1D741D1004F2D",
            "qcser.inf": "ECD9EBD5337D32B6ED9CB0AE5599BFA3DFD77EE375723F82ECC46EF732D5F037",
            "serial/amd64/qcusbser.sys": "4FFB594F274B597740DBE1BC698492D4D447E294188339370BE12A2C764DBD9A",
        }
        paths = {
            relative_path: DRIVER_ROOT / Path(relative_path)
            for relative_path in expected
        }
        present = {name for name, path in paths.items() if path.is_file()}
        if not present:
            self.skipTest("optional Quectel driver payload is not in the source checkout")
        self.assertEqual(present, set(expected), "driver payload is incomplete")
        for relative_path, expected_hash in expected.items():
            path = paths[relative_path]
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest().upper()
            self.assertEqual(actual_hash, expected_hash, relative_path)

    def test_migration_scripts_do_not_pin_current_machine_ids(self) -> None:
        binder = (ROOT / "tools" / "Bind-DjiAtPort.ps1").read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r'VID_2CA3&PID_4006&MI_02\\[^"\'\s]+', binder, re.I)
        )
        self.assertIsNone(re.search(r"\boem\d+\.inf\b", binder, re.I))
        self.assertIn("DjiDeviceDiscovery.ps1", binder)

        serial_tools = [
            "Inspect-DjiStoredSms.ps1",
            "Read-DjiModemState.ps1",
            "Receive-DjiSmsProbe.ps1",
            "Set-DjiCarrierProfile.ps1",
        ]
        for name in serial_tools:
            text = (ROOT / "tools" / name).read_text(encoding="utf-8")
            self.assertNotIn("[string]$PortName = 'COM14'", text, name)
            self.assertIn("Resolve-DjiAtPortName", text, name)

    def test_migration_entrypoints_and_guidance_are_present(self) -> None:
        required = [
            ROOT / "MIGRATION.md",
            ROOT / "新电脑迁移检查.bat",
            ROOT / "tools" / "DjiDeviceDiscovery.ps1",
            ROOT / "tools" / "Test-NewPcReadiness.ps1",
            ROOT / "drivers" / "README.md",
        ]
        for path in required:
            self.assertTrue(path.is_file(), str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
