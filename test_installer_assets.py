from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "installer" / "assets"
WHEELS = ASSETS / "wheels"
BOOTSTRAP = ROOT / "installer" / "bootstrap" / "InstallerBootstrap.cs"
MANIFEST = ROOT / "installer" / "bootstrap" / "installer.manifest"
BUILDER = ROOT / "tools" / "Build-MigrationInstaller.ps1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


class InstallerAssetTests(unittest.TestCase):
    def test_official_prerequisite_hashes_are_exact(self) -> None:
        expected = {
            "python-3.14.5-amd64.exe": "F9C09F5ED6F796FD1A8BC5DDFA41715A494B453C4781F0E35D5077CF9FA58F6D",
            "PowerShell-7.6.4-win-x64.msi": "D11942DF52FD12470169797ABFA4781D9480EFDC81000BA4FA55A5B921ED8DD0",
        }
        paths = {name: ASSETS / name for name in expected}
        present = {name for name, path in paths.items() if path.is_file()}
        if not present:
            self.skipTest("optional offline prerequisite payload is not in source checkout")
        self.assertEqual(present, set(expected), "offline prerequisite payload is incomplete")
        for name, expected_hash in expected.items():
            path = paths[name]
            self.assertEqual(sha256(path), expected_hash, name)

    def test_offline_wheelhouse_is_locked_and_hash_checked(self) -> None:
        lock = (WHEELS / "requirements-lock.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lock,
            [
                "blinker==1.9.0",
                "certifi==2026.7.22",
                "charset-normalizer==3.4.9",
                "click==8.4.2",
                "colorama==0.4.6",
                "Flask==3.1.3",
                "idna==3.18",
                "itsdangerous==2.2.0",
                "Jinja2==3.1.6",
                "MarkupSafe==3.0.3",
                "pyserial==3.5",
                "requests==2.34.2",
                "urllib3==2.7.0",
                "Werkzeug==3.1.8",
            ],
        )
        self.assertEqual(
            sha256(WHEELS / "requirements-lock.txt"),
            "DEFC42BE24A12A9C36722F9EF34723C740C3A45C2CA60568D76817D231A036E7",
        )

        manifest_lines = (WHEELS / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(manifest_lines), 14)
        manifest_names: set[str] = set()
        expected_by_name: dict[str, str] = {}
        for line in manifest_lines:
            expected_hash, name = line.split("  ", 1)
            manifest_names.add(name)
            expected_by_name[name] = expected_hash

        wheel_paths = list(WHEELS.glob("*.whl"))
        if not wheel_paths:
            return
        self.assertEqual(manifest_names, {path.name for path in wheel_paths})
        for path in wheel_paths:
            self.assertEqual(sha256(path), expected_by_name[path.name], path.name)

    def test_installer_enforces_safety_and_repair_boundaries(self) -> None:
        source = BOOTSTRAP.read_text(encoding="utf-8")
        builder = BUILDER.read_text(encoding="utf-8")

        self.assertIn("--no-index", source)
        self.assertIn("Bind-DjiAtPort.ps1", source)
        self.assertIn("Test-NewPcReadiness.ps1", source)
        self.assertIn("VID_2CA3&PID_4006&MI_02", source)
        self.assertIn("archive.jsonl", source)
        self.assertIn("mutableFiles", source)
        self.assertIn("FileSystemRights.Modify", source)
        self.assertIn('drive.DriveFormat, "NTFS"', source)
        self.assertIn('"autostart\\\": false', source)
        self.assertNotIn("/api/send", source)
        self.assertNotIn("AT+CGACT", source)
        self.assertNotIn("NETOPEN", source)
        self.assertNotIn("CurrentVersion\\\\Run", source)
        self.assertNotIn("schtasks", source.lower())

        self.assertIn("app.log", builder)
        self.assertIn("runtime-python.txt", builder)
        self.assertIn("runtime-pythonw.txt", builder)
        self.assertIn("Stop-VerifiedDjiServiceForSnapshot", builder)
        self.assertIn("ctexcel", builder)
        self.assertIn("dji_qdc507", builder)
        self.assertIn("$serviceInstance.root_fingerprint", builder)
        self.assertNotIn("Get-CimInstance Win32_Process", builder)

    def test_runtime_binding_is_used_by_startup_and_readiness(self) -> None:
        startup = (ROOT / "启动短信工具.bat").read_text(encoding="utf-8")
        readiness = (ROOT / "tools" / "Test-NewPcReadiness.ps1").read_text(encoding="utf-8")
        self.assertIn("runtime-pythonw.txt", startup)
        self.assertIn("pyw -3.14", startup)
        self.assertIn("runtime-python.txt", readiness)
        self.assertIn("$pythonSource", readiness)

    @unittest.skipUnless(Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe").is_file(), "Framework csc unavailable")
    def test_bootstrap_compiles_with_inbox_framework_compiler(self) -> None:
        csc = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
        stub = """
namespace CTExcelSmsDjiInstaller
{
    internal static class PayloadInfo
    {
        internal const string PythonFileName = "python.exe";
        internal const string PythonSha256 = "00";
        internal const string PowerShellFileName = "pwsh.msi";
        internal const string PowerShellSha256 = "00";
        internal const string ProjectSha256 = "00";
        internal const string WheelsSha256 = "00";
        internal const string BuiltUtc = "test";
        internal const int ProjectFileCount = 1;
        internal const int WheelCount = 1;
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="ctexcel-installer-compile-") as temporary:
            temporary_path = Path(temporary)
            stub_path = temporary_path / "PayloadInfo.cs"
            output_path = temporary_path / "compile-test.exe"
            stub_path.write_text(stub, encoding="utf-8")
            command = [
                str(csc),
                "/nologo",
                "/target:winexe",
                "/platform:x64",
                "/optimize+",
                "/codepage:65001",
                "/reference:System.dll",
                "/reference:System.Core.dll",
                "/reference:System.Drawing.dll",
                "/reference:System.IO.Compression.dll",
                "/reference:System.IO.Compression.FileSystem.dll",
                "/reference:System.Security.dll",
                "/reference:System.Windows.Forms.dll",
                f"/win32manifest:{MANIFEST}",
                f"/out:{output_path}",
                str(BOOTSTRAP),
                str(stub_path),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue(output_path.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
