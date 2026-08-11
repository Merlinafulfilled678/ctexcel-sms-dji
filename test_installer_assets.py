from __future__ import annotations

import hashlib
import json
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
PUBLIC_BUILDER = ROOT / "tools" / "Build-PublicInstaller.ps1"
PACKAGING_MODULE = ROOT / "tools" / "InstallerPackaging.psm1"

PUBLIC_PAYLOAD_FILES = {
    "CONTRIBUTING.md",
    "DJI-QDC507-CTEXCEL.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "app.py",
    "carrier_profile.py",
    "config.example.json",
    "config_store.py",
    "diag_readonly.py",
    "drivers/README.md",
    "modem_profile.py",
    "requirements.txt",
    "static/index.html",
    "tg_bot.py",
    "tools/DjiDeviceDiscovery.ps1",
    "tools/Test-NewPcReadiness.ps1",
    "tools/Uninstall-CtExcelSmsDji.ps1",
    "卸载短信工具.bat",
    "启动短信工具.bat",
}

PUBLIC_PAYLOAD_FORBIDDEN = {
    "config.json",
    "archive.jsonl",
    "state.json",
    "tg_state.json",
    "app.log",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


class InstallerAssetTests(unittest.TestCase):
    def test_public_builder_describes_an_explicit_secret_free_payload(self) -> None:
        completed = subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(PUBLIC_BUILDER),
                "-DescribePayload",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        description = json.loads(completed.stdout)
        self.assertEqual(description["package_kind"], "public")
        self.assertFalse(description["includes_private_data"])
        self.assertFalse(description["includes_driver"])
        payload_files = {value.replace("\\", "/") for value in description["payload_files"]}
        self.assertEqual(payload_files, PUBLIC_PAYLOAD_FILES)
        self.assertTrue(PUBLIC_PAYLOAD_FORBIDDEN.isdisjoint(payload_files))
        self.assertFalse(any(name.startswith("SPEC") for name in payload_files))
        self.assertFalse(any(".bak." in name for name in payload_files))

    def test_public_builder_stages_only_the_declared_payload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ctexcel-public-stage-") as temporary:
            stage = Path(temporary) / "project"
            completed = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(PUBLIC_BUILDER),
                    "-StagePayloadDirectory",
                    str(stage),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            description = json.loads(completed.stdout)
            self.assertEqual(description["package_kind"], "public")
            staged = {
                path.relative_to(stage).as_posix()
                for path in stage.rglob("*")
                if path.is_file()
            }
            self.assertEqual(staged, PUBLIC_PAYLOAD_FILES)
            for relative in PUBLIC_PAYLOAD_FILES:
                self.assertEqual((stage / relative).read_bytes(), (ROOT / relative).read_bytes())

    def test_public_payload_validator_rejects_a_token_without_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ctexcel-public-secret-") as temporary:
            stage = Path(temporary) / "project"
            staged = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(PUBLIC_BUILDER),
                    "-StagePayloadDirectory",
                    str(stage),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            secret = "123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWX"
            readme = stage / "README.md"
            readme.write_text(
                readme.read_text(encoding="utf-8") + "\n" + secret,
                encoding="utf-8",
            )
            validated = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(PUBLIC_BUILDER),
                    "-ValidatePayloadDirectory",
                    str(stage),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertNotEqual(validated.returncode, 0)
            self.assertNotIn(secret, validated.stdout + validated.stderr)
            self.assertIn("README.md", validated.stdout + validated.stderr)

    def test_uninstaller_describes_a_validated_backup_plan_without_mutation(self) -> None:
        uninstaller = ROOT / "tools" / "Uninstall-CtExcelSmsDji.ps1"
        with tempfile.TemporaryDirectory(prefix="ctexcel-uninstall-plan-") as temporary:
            install_path = Path(temporary) / "CTExcel-SMS-DJI"
            install_path.mkdir()
            (install_path / ".ctexcel-dji-install.json").write_text(
                '{"autostart": false}\n', encoding="utf-8"
            )
            private_files = {
                "config.json": "{}\n",
                "archive.jsonl": "",
                "state.json": "{}\n",
                "tg_state.json": "{}\n",
            }
            for name, content in private_files.items():
                (install_path / name).write_text(content, encoding="utf-8")

            completed = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(uninstaller),
                    "-DescribePlan",
                    "-InstallPath",
                    str(install_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            plan = json.loads(completed.stdout)
            self.assertEqual(Path(plan["install_path"]).resolve(), install_path.resolve())
            self.assertEqual(set(plan["private_files_present"]), set(private_files))
            self.assertTrue(plan["will_backup_private_data"])
            self.assertTrue(plan["will_remove_install_directory"])
            for name, content in private_files.items():
                self.assertEqual((install_path / name).read_text(encoding="utf-8"), content)

    def test_public_and_migration_builders_share_the_packaging_seam(self) -> None:
        module = PACKAGING_MODULE.read_text(encoding="utf-8")
        self.assertIn("function New-CtExcelInstallerExecutable", module)
        for builder_path in (PUBLIC_BUILDER, BUILDER):
            builder = builder_path.read_text(encoding="utf-8")
            self.assertIn("InstallerPackaging.psm1", builder, builder_path.name)
            self.assertIn("New-CtExcelInstallerExecutable", builder, builder_path.name)

    @unittest.skipUnless(
        (ASSETS / "python-3.14.5-amd64.exe").is_file()
        and (ASSETS / "PowerShell-7.6.4-win-x64.msi").is_file()
        and Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe").is_file(),
        "optional offline compiler payload is not in source checkout",
    )
    def test_public_builder_builds_a_self_tested_executable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ctexcel-public-build-") as temporary:
            output = Path(temporary) / "dist"
            completed = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(PUBLIC_BUILDER),
                    "-OutputDirectory",
                    str(output),
                    "-Version",
                    "0.9.0-beta",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            executable = output / "CTExcel-SMS-DJI-Setup-v0.9.0-beta.exe"
            self.assertEqual(Path(result["path"]), executable)
            self.assertEqual(result["package_kind"], "public")
            self.assertFalse(result["includes_private_data"])
            self.assertFalse(result["includes_driver"])
            self.assertTrue(executable.is_file())
            self.assertEqual(result["sha256"], sha256(executable))
            sums = output / "SHA256SUMS.txt"
            self.assertEqual(
                sums.read_text(encoding="utf-8"),
                f"{result['sha256']}  {executable.name}\n",
            )

            result_path = Path(temporary) / "self-test.txt"
            self_test = subprocess.run(
                [str(executable), "--self-test", f"--result={result_path}"],
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(self_test.returncode, 0)
            self_test_text = result_path.read_text(encoding="utf-8")
            self.assertRegex(self_test_text, r"^SELF_TEST_OK public ")
            self.assertIn("device=optional private=false driver=false", self_test_text)

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
        self.assertIn('installPathLabel.Text = "安装目录";', source)
        self.assertNotIn("安装目录（可修改）", source)
        self.assertIn("AutoScaleMode = AutoScaleMode.Dpi", source)
        self.assertIn("AutoScaleDimensions = new SizeF(96F, 96F)", source)
        self.assertIn("FolderBrowserDialog", source)
        self.assertIn("ValidateInstallPath", source)
        self.assertIn("DriveType.Fixed", source)
        self.assertIn("FileAttributes.ReparsePoint", source)
        self.assertIn("不能直接安装到磁盘根目录", source)
        self.assertIn('"autostart\\\": false', source)
        self.assertNotIn("/api/send", source)
        self.assertNotIn("AT+CGACT", source)
        self.assertNotIn("NETOPEN", source)
        self.assertNotIn("CurrentVersion\\\\Run", source)
        self.assertNotIn("schtasks", source.lower())
        self.assertIn("配置并启动", source)
        self.assertIn("Shell.Application", source)

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
        internal const string PackageKind = "public";
        internal const bool IncludesPrivateData = false;
        internal const bool IncludesDriver = false;
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
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue(output_path.is_file())

    @unittest.skipUnless(Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe").is_file(), "Framework csc unavailable")
    def test_installer_manifest_makes_the_process_dpi_aware(self) -> None:
        csc = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
        probe = """
using System;
using System.Runtime.InteropServices;

internal static class DpiProbe
{
    [DllImport("user32.dll")]
    private static extern bool IsProcessDPIAware();

    public static int Main()
    {
        bool aware = IsProcessDPIAware();
        Console.WriteLine(aware ? "DPI_AWARE" : "DPI_UNAWARE");
        return aware ? 0 : 3;
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="ctexcel-dpi-probe-") as temporary:
            temporary_path = Path(temporary)
            source_path = temporary_path / "DpiProbe.cs"
            executable = temporary_path / "DpiProbe.exe"
            source_path.write_text(probe, encoding="utf-8")
            compiled = subprocess.run(
                [
                    str(csc),
                    "/nologo",
                    "/target:exe",
                    "/platform:x64",
                    "/codepage:65001",
                    f"/win32manifest:{MANIFEST}",
                    f"/out:{executable}",
                    str(source_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            checked = subprocess.run(
                [str(executable)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertEqual(checked.stdout.strip(), "DPI_AWARE")

    @unittest.skipUnless(Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe").is_file(), "Framework csc unavailable")
    def test_installer_layout_does_not_overlap_at_the_current_system_dpi(self) -> None:
        csc = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
        stub = """
using System;
using System.Reflection;
using System.Windows.Forms;

namespace CTExcelSmsDjiInstaller
{
    internal static class PayloadInfo
    {
        internal const string PackageKind = "public";
        internal const bool IncludesPrivateData = false;
        internal const bool IncludesDriver = false;
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

    internal static class LayoutHarness
    {
        private static Control Field(InstallerForm form, string name)
        {
            return (Control)typeof(InstallerForm).GetField(name, BindingFlags.Instance | BindingFlags.NonPublic).GetValue(form);
        }

        public static int Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (InstallerForm form = new InstallerForm())
            {
                form.CreateControl();
                form.PerformLayout();
                Control title = Field(form, "titleLabel");
                Control privacy = Field(form, "privacyLabel");
                Control pathLabel = Field(form, "installPathLabel");
                Control pathBox = Field(form, "installPathTextBox");
                Control step = Field(form, "stepLabel");
                Control progress = Field(form, "progressBar");
                Control log = Field(form, "logBox");
                Control start = Field(form, "startInstallButton");
                Console.WriteLine(
                    "CLIENT={0}x{1} TITLE={2},{3} PRIVACY={4},{5} PATH={6},{7} STEP={8},{9} LOG={10},{11} START={12},{13}",
                    form.ClientSize.Width, form.ClientSize.Height,
                    title.Top, title.Bottom, privacy.Top, privacy.Bottom,
                    pathLabel.Top, pathBox.Bottom, step.Top, progress.Bottom,
                    log.Top, log.Bottom, start.Top, start.Bottom);
                if (title.Bottom > privacy.Top)
                    return 10;
                if (privacy.Bottom > pathLabel.Top)
                    return 11;
                if (pathLabel.Bottom > pathBox.Top)
                    return 12;
                if (pathBox.Bottom > step.Top)
                    return 13;
                if (step.Bottom > progress.Top)
                    return 14;
                if (progress.Bottom > log.Top)
                    return 15;
                if (log.Bottom > start.Top)
                    return 16;
                if (start.Bottom > form.ClientSize.Height)
                    return 17;
                return 0;
            }
        }
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="ctexcel-layout-probe-") as temporary:
            temporary_path = Path(temporary)
            source_path = temporary_path / "LayoutHarness.cs"
            executable = temporary_path / "LayoutHarness.exe"
            source_path.write_text(stub, encoding="utf-8")
            compiled = subprocess.run(
                [
                    str(csc),
                    "/nologo",
                    "/target:exe",
                    "/platform:x64",
                    "/codepage:65001",
                    "/main:CTExcelSmsDjiInstaller.LayoutHarness",
                    "/reference:System.dll",
                    "/reference:System.Core.dll",
                    "/reference:System.Drawing.dll",
                    "/reference:System.IO.Compression.dll",
                    "/reference:System.IO.Compression.FileSystem.dll",
                    "/reference:System.Security.dll",
                    "/reference:System.Windows.Forms.dll",
                    f"/win32manifest:{MANIFEST}",
                    f"/out:{executable}",
                    str(BOOTSTRAP),
                    str(source_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            checked = subprocess.run(
                [str(executable)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    @unittest.skipUnless(Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe").is_file(), "Framework csc unavailable")
    def test_install_path_validation_accepts_safe_targets_and_rejects_unsafe_ones(self) -> None:
        csc = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
        stub = """
namespace CTExcelSmsDjiInstaller
{
    internal static class PayloadInfo
    {
        internal const string PackageKind = "public";
        internal const bool IncludesPrivateData = false;
        internal const bool IncludesDriver = false;
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

    internal static class PathHarness
    {
        public static int Main(string[] args)
        {
            try
            {
                System.Console.WriteLine(InstallerEngine.ValidateInstallPath(args[0]));
                return 0;
            }
            catch (System.Exception ex)
            {
                System.Console.Error.WriteLine(ex.Message);
                return 2;
            }
        }
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="ctexcel-installer-path-") as temporary:
            temporary_path = Path(temporary)
            stub_path = temporary_path / "PathHarness.cs"
            output_path = temporary_path / "path-test.exe"
            stub_path.write_text(stub, encoding="utf-8")
            command = [
                str(csc),
                "/nologo",
                "/target:exe",
                "/platform:x64",
                "/optimize+",
                "/codepage:65001",
                "/main:CTExcelSmsDjiInstaller.PathHarness",
                "/reference:System.dll",
                "/reference:System.Core.dll",
                "/reference:System.Drawing.dll",
                "/reference:System.IO.Compression.dll",
                "/reference:System.IO.Compression.FileSystem.dll",
                "/reference:System.Security.dll",
                "/reference:System.Windows.Forms.dll",
                f"/out:{output_path}",
                str(BOOTSTRAP),
                str(stub_path),
            ]
            compiled = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)

            safe_target = temporary_path / "safe-target"
            run_options = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}

            safe = subprocess.run([str(output_path), str(safe_target)], **run_options)
            self.assertEqual(safe.returncode, 0, safe.stdout + safe.stderr)
            self.assertEqual(Path(safe.stdout.strip()).resolve(), safe_target.resolve())

            repair_target = temporary_path / "repair-target"
            repair_target.mkdir()
            (repair_target / ".ctexcel-dji-install.json").write_text("{}\n", encoding="utf-8")
            repair = subprocess.run([str(output_path), str(repair_target)], **run_options)
            self.assertEqual(repair.returncode, 0, repair.stdout + repair.stderr)

            occupied = temporary_path / "occupied"
            occupied.mkdir()
            (occupied / "other.txt").write_text("keep\n", encoding="utf-8")
            rejected = subprocess.run([str(output_path), str(occupied)], **run_options)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("不是本安装器创建", rejected.stderr)

            relative = subprocess.run([str(output_path), "relative-path"], **run_options)
            self.assertNotEqual(relative.returncode, 0)
            self.assertIn("绝对路径", relative.stderr)

            drive_root = Path(temporary_path.anchor)
            root_result = subprocess.run([str(output_path), str(drive_root)], **run_options)
            self.assertNotEqual(root_result.returncode, 0)
            self.assertIn("根目录", root_result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
