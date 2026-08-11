using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace CTExcelSmsDjiInstaller
{
    internal static class Program
    {
        [STAThread]
        public static int Main(string[] args)
        {
            string resultPath = GetValueArgument(args, "--result=");
            if (HasArgument(args, "--self-test"))
                return InstallerEngine.RunSelfTest(resultPath);

            if (!IsAdministrator())
                return RelaunchElevated();

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            InstallerForm form = new InstallerForm();
            Application.Run(form);
            return form.ExitCode;
        }

        private static bool HasArgument(string[] args, string expected)
        {
            return args.Any(delegate(string value)
            {
                return String.Equals(value, expected, StringComparison.OrdinalIgnoreCase);
            });
        }

        private static string GetValueArgument(string[] args, string prefix)
        {
            foreach (string value in args)
            {
                if (value.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                    return value.Substring(prefix.Length);
            }
            return null;
        }

        private static bool IsAdministrator()
        {
            WindowsIdentity identity = WindowsIdentity.GetCurrent();
            WindowsPrincipal principal = new WindowsPrincipal(identity);
            return principal.IsInRole(WindowsBuiltInRole.Administrator);
        }

        private static int RelaunchElevated()
        {
            try
            {
                ProcessStartInfo info = new ProcessStartInfo();
                info.FileName = Assembly.GetExecutingAssembly().Location;
                info.Arguments = "--elevated";
                info.UseShellExecute = true;
                info.Verb = "runas";
                Process process = Process.Start(info);
                if (process == null)
                    throw new InvalidOperationException("Windows did not start the elevated installer process.");
                process.WaitForExit();
                return process.ExitCode;
            }
            catch (Win32Exception ex)
            {
                string detail = ex.NativeErrorCode == 1223
                    ? "你取消了管理员权限确认，安装尚未开始。"
                    : "无法申请管理员权限：" + ex.Message;
                MessageBox.Show(detail, "CTExcel 短信工具安装器", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 5;
            }
            catch (Exception ex)
            {
                MessageBox.Show("无法启动安装器：" + ex.Message, "CTExcel 短信工具安装器", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 5;
            }
        }
    }

    internal sealed class InstallerForm : Form
    {
        private readonly Label titleLabel;
        private readonly Label stepLabel;
        private readonly Label privacyLabel;
        private readonly ProgressBar progressBar;
        private readonly RichTextBox logBox;
        private readonly Button openFolderButton;
        private readonly Button closeButton;
        private bool running;
        private string installedPath;

        internal int ExitCode { get; private set; }

        internal InstallerForm()
        {
            Text = "CTExcel 短信工具 · DJI 迁移安装器";
            ClientSize = new Size(760, 520);
            MinimumSize = new Size(700, 480);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(247, 249, 252);
            Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Regular, GraphicsUnit.Point);

            titleLabel = new Label();
            titleLabel.Text = "正在准备 CTExcel 短信工具";
            titleLabel.Font = new Font("Microsoft YaHei UI", 17F, FontStyle.Bold, GraphicsUnit.Point);
            titleLabel.ForeColor = Color.FromArgb(30, 42, 60);
            titleLabel.AutoSize = true;
            titleLabel.Location = new Point(26, 22);

            privacyLabel = new Label();
            privacyLabel.Text = "离线安装 · 只绑定 DJI AT 串口 · 不发短信 · 不创建开机启动";
            privacyLabel.ForeColor = Color.FromArgb(75, 91, 112);
            privacyLabel.AutoSize = true;
            privacyLabel.Location = new Point(29, 62);

            stepLabel = new Label();
            stepLabel.Text = "等待开始…";
            stepLabel.Font = new Font("Microsoft YaHei UI", 10F, FontStyle.Bold, GraphicsUnit.Point);
            stepLabel.ForeColor = Color.FromArgb(28, 103, 196);
            stepLabel.AutoEllipsis = true;
            stepLabel.Location = new Point(29, 96);
            stepLabel.Size = new Size(700, 24);

            progressBar = new ProgressBar();
            progressBar.Location = new Point(29, 126);
            progressBar.Size = new Size(702, 18);
            progressBar.Style = ProgressBarStyle.Marquee;
            progressBar.MarqueeAnimationSpeed = 28;

            logBox = new RichTextBox();
            logBox.Location = new Point(29, 160);
            logBox.Size = new Size(702, 292);
            logBox.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            logBox.ReadOnly = true;
            logBox.BackColor = Color.White;
            logBox.BorderStyle = BorderStyle.FixedSingle;
            logBox.Font = new Font("Consolas", 9F, FontStyle.Regular, GraphicsUnit.Point);
            logBox.DetectUrls = false;

            openFolderButton = new Button();
            openFolderButton.Text = "打开安装目录";
            openFolderButton.Enabled = false;
            openFolderButton.Size = new Size(126, 34);
            openFolderButton.Location = new Point(469, 468);
            openFolderButton.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
            openFolderButton.Click += OpenFolderButtonClick;

            closeButton = new Button();
            closeButton.Text = "关闭";
            closeButton.Enabled = false;
            closeButton.Size = new Size(126, 34);
            closeButton.Location = new Point(605, 468);
            closeButton.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
            closeButton.Click += delegate { Close(); };

            Controls.Add(titleLabel);
            Controls.Add(privacyLabel);
            Controls.Add(stepLabel);
            Controls.Add(progressBar);
            Controls.Add(logBox);
            Controls.Add(openFolderButton);
            Controls.Add(closeButton);

            AcceptButton = closeButton;
            ExitCode = 1;
        }

        protected override void OnShown(EventArgs e)
        {
            base.OnShown(e);
            running = true;
            Thread worker = new Thread(WorkerMain);
            worker.IsBackground = true;
            worker.SetApartmentState(ApartmentState.STA);
            worker.Start();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (running)
            {
                e.Cancel = true;
                MessageBox.Show("环境或驱动正在安装，请等待当前步骤结束。", Text, MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            base.OnFormClosing(e);
        }

        private void WorkerMain()
        {
            InstallerEngine engine = null;
            try
            {
                engine = new InstallerEngine(ReportProgress);
                InstallResult result = engine.Install();
                installedPath = result.InstallPath;
                BeginInvoke((MethodInvoker)delegate
                {
                    running = false;
                    ExitCode = 0;
                    titleLabel.Text = "安装完成";
                    titleLabel.ForeColor = Color.FromArgb(26, 127, 80);
                    stepLabel.Text = result.WarningCount == 0
                        ? "全部检查通过，可以关闭安装器。"
                        : "安装成功；有 " + result.WarningCount + " 项非阻断提醒，请查看日志。";
                    progressBar.Style = ProgressBarStyle.Continuous;
                    progressBar.Value = 100;
                    openFolderButton.Enabled = true;
                    closeButton.Enabled = true;
                    MessageBox.Show(
                        "CTExcel 短信工具已经安装到：\n" + result.InstallPath +
                        "\n\n桌面快捷方式已按可用情况创建。程序没有自动启动，也没有创建开机启动。" +
                        "\n首次启动前请确认旧电脑上的 Telegram Bot 服务仍保持停止。",
                        Text,
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Information);
                });
            }
            catch (Exception ex)
            {
                string logPath = engine == null ? "未创建" : engine.LogPath;
                BeginInvoke((MethodInvoker)delegate
                {
                    running = false;
                    ExitCode = 2;
                    titleLabel.Text = "安装未完成";
                    titleLabel.ForeColor = Color.FromArgb(184, 45, 45);
                    stepLabel.Text = "失败原因：" + ex.Message;
                    progressBar.Style = ProgressBarStyle.Continuous;
                    progressBar.Value = 0;
                    closeButton.Enabled = true;
                    MessageBox.Show(
                        "安装失败：\n" + ex.Message + "\n\n详细日志：\n" + logPath,
                        Text,
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error);
                });
            }
            finally
            {
                if (engine != null)
                    engine.Dispose();
            }
        }

        private void ReportProgress(string step, string message)
        {
            if (IsDisposed)
                return;
            BeginInvoke((MethodInvoker)delegate
            {
                if (!String.IsNullOrEmpty(step))
                    stepLabel.Text = step;
                if (!String.IsNullOrEmpty(message))
                {
                    logBox.AppendText(message + Environment.NewLine);
                    logBox.SelectionStart = logBox.TextLength;
                    logBox.ScrollToCaret();
                }
            });
        }

        private void OpenFolderButtonClick(object sender, EventArgs e)
        {
            if (String.IsNullOrEmpty(installedPath) || !Directory.Exists(installedPath))
                return;
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = "explorer.exe";
            info.Arguments = InstallerEngine.QuoteArgument(installedPath);
            info.UseShellExecute = true;
            Process.Start(info);
        }
    }

    internal sealed class InstallResult
    {
        internal string InstallPath;
        internal int WarningCount;
    }

    internal sealed class ProcessResult
    {
        internal int ExitCode;
        internal string StandardOutput;
        internal string StandardError;

        internal string CombinedOutput
        {
            get { return (StandardOutput + Environment.NewLine + StandardError).Trim(); }
        }
    }

    internal sealed class InstallerEngine : IDisposable
    {
        private const string ProjectResource = "CTExcel.ProjectZip";
        private const string WheelsResource = "CTExcel.WheelsZip";
        private const string PythonResource = "CTExcel.PythonInstaller";
        private const string PowerShellResource = "CTExcel.PowerShellInstaller";
        private const string MarkerName = ".ctexcel-dji-install.json";
        private readonly Action<string, string> reporter;
        private readonly StreamWriter logWriter;
        private readonly object logLock = new object();
        private readonly List<string> warnings = new List<string>();
        private string currentStep;
        private string workRoot;

        internal string LogPath { get; private set; }

        internal InstallerEngine(Action<string, string> progressReporter)
        {
            reporter = progressReporter;
            LogPath = Path.Combine(
                Path.GetTempPath(),
                "CTExcel-SMS-DJI-install-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Process.GetCurrentProcess().Id + ".log");
            logWriter = new StreamWriter(LogPath, false, new UTF8Encoding(false));
            logWriter.AutoFlush = true;
            Log("Package built UTC: " + PayloadInfo.BuiltUtc);
            Log("Package project files: " + PayloadInfo.ProjectFileCount);
        }

        public void Dispose()
        {
            lock (logLock)
            {
                logWriter.Dispose();
            }
        }

        internal InstallResult Install()
        {
            if (!IsAdministrator())
                throw new InvalidOperationException("安装进程没有管理员权限。请重新运行并允许 UAC。 ");

            workRoot = Path.Combine(Path.GetTempPath(), "CTExcel-SMS-DJI-install-work-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(workRoot);

            try
            {
                Step("1/9  校验内置离线资源");
                PayloadPaths payload = ExtractAndVerifyPayloads(workRoot);

                Step("2/9  检查 Windows 与安全边界");
                ValidateOperatingSystem();
                EnsureServicePortFree();
                EnsureDjiDeviceConnected();
                EnsureNoCellularNetworkAdapter();

                Step("3/9  检查或安装 PowerShell 7");
                string pwsh = EnsurePowerShell(payload.PowerShellInstaller);

                Step("4/9  检查或安装 Python 3.14 x64");
                string python = EnsurePython(payload.PythonInstaller);

                Step("5/9  离线安装 Python 依赖");
                InstallPythonPackages(python, payload.WheelsDirectory);

                Step("6/9  检查或绑定 DJI AT 串口驱动");
                EnsureDjiAtDriver(pwsh, payload.ProjectDirectory);

                Step("7/9  安装短信工具及私人迁移数据");
                string installPath = SelectInstallPath();
                InstallProject(payload.ProjectDirectory, installPath, python);

                Step("8/9  运行新电脑只读验收");
                RunReadinessCheck(pwsh, installPath);

                Step("9/9  创建手动启动入口");
                TryCreateDesktopShortcut(installPath);

                Log("INSTALLATION_COMPLETED path=" + installPath);
                Report(currentStep, "完成。详细日志：" + LogPath);
                return new InstallResult { InstallPath = installPath, WarningCount = warnings.Count };
            }
            catch (Exception ex)
            {
                Log("INSTALLATION_FAILED step=" + currentStep);
                Log(ex.ToString());
                if (ex is InstallerFailure)
                    throw;
                throw new InstallerFailure(currentStep + "：" + ex.Message, ex);
            }
            finally
            {
                SafeDeleteWorkRoot(workRoot);
            }
        }

        internal static int RunSelfTest(string resultPath)
        {
            if (String.IsNullOrWhiteSpace(resultPath))
                resultPath = Path.Combine(Path.GetTempPath(), "CTExcel-SMS-DJI-self-test-" + Guid.NewGuid().ToString("N") + ".txt");

            string root = Path.Combine(Path.GetTempPath(), "CTExcel-SMS-DJI-self-test-work-" + Guid.NewGuid().ToString("N"));
            InstallerEngine engine = null;
            string selfTestLog = null;
            try
            {
                Directory.CreateDirectory(root);
                engine = new InstallerEngine(null);
                selfTestLog = engine.LogPath;
                engine.ExtractAndVerifyPayloads(root);
                File.WriteAllText(resultPath, "SELF_TEST_OK " + PayloadInfo.BuiltUtc, new UTF8Encoding(false));
                return 0;
            }
            catch (Exception ex)
            {
                try
                {
                    File.WriteAllText(resultPath, "SELF_TEST_FAIL " + ex.GetType().Name + ": " + ex.Message, new UTF8Encoding(false));
                }
                catch
                {
                }
                return 9;
            }
            finally
            {
                if (engine != null)
                    engine.Dispose();
                if (!String.IsNullOrEmpty(selfTestLog))
                {
                    try { File.Delete(selfTestLog); }
                    catch { }
                }
                SafeDeleteWorkRoot(root);
            }
        }

        private PayloadPaths ExtractAndVerifyPayloads(string root)
        {
            string payloadRoot = Path.Combine(root, "payload");
            Directory.CreateDirectory(payloadRoot);
            string projectZip = Path.Combine(payloadRoot, "project.zip");
            string wheelsZip = Path.Combine(payloadRoot, "wheels.zip");
            string pythonInstaller = Path.Combine(payloadRoot, PayloadInfo.PythonFileName);
            string powerShellInstaller = Path.Combine(payloadRoot, PayloadInfo.PowerShellFileName);

            ExtractResource(ProjectResource, projectZip, PayloadInfo.ProjectSha256);
            ExtractResource(WheelsResource, wheelsZip, PayloadInfo.WheelsSha256);
            ExtractResource(PythonResource, pythonInstaller, PayloadInfo.PythonSha256);
            ExtractResource(PowerShellResource, powerShellInstaller, PayloadInfo.PowerShellSha256);

            if (!VerifyAuthenticode(pythonInstaller))
                throw new InstallerFailure("内置 Python 安装器的数字签名无效。文件不会运行。 ");
            if (!VerifyAuthenticode(powerShellInstaller))
                throw new InstallerFailure("内置 PowerShell MSI 的数字签名无效。文件不会运行。 ");
            Log("Official prerequisite hashes and Authenticode signatures are valid.");

            ValidateZip(projectZip, new string[]
            {
                "app.py",
                "config.json",
                "archive.jsonl",
                "state.json",
                "tg_state.json",
                "static/index.html",
                "tools/Bind-DjiAtPort.ps1",
                "tools/Test-NewPcReadiness.ps1",
                "drivers/Quectel-Ports-30.0.65.2/qcser.inf"
            }, PayloadInfo.ProjectFileCount, false);
            ValidateZip(wheelsZip, new string[] { "requirements-lock.txt" }, PayloadInfo.WheelCount, true);

            string projectDirectory = Path.Combine(root, "project");
            string wheelsDirectory = Path.Combine(root, "wheels");
            ExtractZipSafely(projectZip, projectDirectory);
            ExtractZipSafely(wheelsZip, wheelsDirectory);

            return new PayloadPaths
            {
                ProjectDirectory = projectDirectory,
                WheelsDirectory = wheelsDirectory,
                PythonInstaller = pythonInstaller,
                PowerShellInstaller = powerShellInstaller
            };
        }

        private void ExtractResource(string resourceName, string destination, string expectedHash)
        {
            Assembly assembly = Assembly.GetExecutingAssembly();
            using (Stream source = assembly.GetManifestResourceStream(resourceName))
            {
                if (source == null)
                    throw new InstallerFailure("安装包缺少内置资源：" + resourceName);
                using (FileStream target = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    source.CopyTo(target);
            }
            string actual = ComputeSha256(destination);
            if (!String.Equals(actual, expectedHash, StringComparison.OrdinalIgnoreCase))
                throw new InstallerFailure("内置资源校验失败：" + resourceName + "，SHA-256 不匹配。 ");
            Log("Verified resource " + resourceName + " SHA256=" + actual);
        }

        private static string ComputeSha256(string path)
        {
            using (SHA256 algorithm = SHA256.Create())
            using (FileStream stream = File.OpenRead(path))
                return BitConverter.ToString(algorithm.ComputeHash(stream)).Replace("-", String.Empty);
        }

        private static void ValidateZip(string zipPath, string[] requiredEntries, int expectedCount, bool countWheelsOnly)
        {
            using (ZipArchive archive = ZipFile.OpenRead(zipPath))
            {
                HashSet<string> names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                int counted = 0;
                foreach (ZipArchiveEntry entry in archive.Entries)
                {
                    string normalized = entry.FullName.Replace('\\', '/').TrimStart('/');
                    if (normalized.Contains("../") || normalized.StartsWith("..", StringComparison.Ordinal) || Path.IsPathRooted(entry.FullName))
                        throw new InstallerFailure("压缩载荷含不安全路径：" + entry.FullName);
                    names.Add(normalized);
                    if (countWheelsOnly)
                    {
                        if (normalized.EndsWith(".whl", StringComparison.OrdinalIgnoreCase))
                            counted++;
                    }
                    else if (!String.IsNullOrEmpty(entry.Name))
                    {
                        counted++;
                    }
                }

                foreach (string required in requiredEntries)
                {
                    if (!names.Contains(required.Replace('\\', '/')))
                        throw new InstallerFailure("压缩载荷缺少文件：" + required);
                }
                if (counted != expectedCount)
                    throw new InstallerFailure("压缩载荷文件数量异常。预期 " + expectedCount + "，实际 " + counted + "。 ");
            }
        }

        private static void ExtractZipSafely(string zipPath, string destination)
        {
            Directory.CreateDirectory(destination);
            string destinationPrefix = Path.GetFullPath(destination).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            using (ZipArchive archive = ZipFile.OpenRead(zipPath))
            {
                foreach (ZipArchiveEntry entry in archive.Entries)
                {
                    string targetPath = Path.GetFullPath(Path.Combine(destination, entry.FullName));
                    if (!targetPath.StartsWith(destinationPrefix, StringComparison.OrdinalIgnoreCase))
                        throw new InstallerFailure("拒绝解压越界路径：" + entry.FullName);

                    if (String.IsNullOrEmpty(entry.Name))
                    {
                        Directory.CreateDirectory(targetPath);
                        continue;
                    }
                    string parent = Path.GetDirectoryName(targetPath);
                    if (!Directory.Exists(parent))
                        Directory.CreateDirectory(parent);
                    entry.ExtractToFile(targetPath, true);
                }
            }
        }

        private void ValidateOperatingSystem()
        {
            if (!Environment.Is64BitOperatingSystem)
                throw new InstallerFailure("需要 64 位 Windows，当前系统不是 x64。 ");
            Version version = Environment.OSVersion.Version;
            if (version.Major < 10)
                throw new InstallerFailure("只支持 Windows 10/11 x64；当前系统版本为 " + version + "。 ");
            if (version.Build < 22000)
                AddWarning("当前是 Windows build " + version.Build + "；仅 Windows 11 做过现场验证。 ");
            else
                Report(currentStep, "Windows 11 x64 build " + version.Build + "：通过");
        }

        private void EnsureServicePortFree()
        {
            TcpListener listener = null;
            try
            {
                listener = new TcpListener(IPAddress.Loopback, 7597);
                listener.Start();
            }
            catch (SocketException)
            {
                throw new InstallerFailure("本机端口 7597 已被占用。请关闭旧短信工具或占用该端口的程序后重试。 ");
            }
            finally
            {
                if (listener != null)
                    listener.Stop();
            }
            Report(currentStep, "端口 7597 空闲：通过");
        }

        private void EnsureDjiDeviceConnected()
        {
            string pnputil = Path.Combine(Environment.SystemDirectory, "pnputil.exe");
            ProcessResult result = RunProcess(pnputil, "/enum-devices /connected /deviceids", false);
            if (result.ExitCode != 0)
                throw ProcessFailure("无法枚举已连接设备", pnputil, result);

            MatchCollection matches = Regex.Matches(
                result.CombinedOutput,
                @"USB\\VID_2CA3&PID_4006&MI_02\\[^\s]+",
                RegexOptions.IgnoreCase);
            HashSet<string> ids = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (Match match in matches)
                ids.Add(match.Value.Trim());
            if (ids.Count == 0)
                throw new InstallerFailure("没有检测到 DJI QDC507 的 MI_02 接口。请插入大疆 4G 模块，等待设备枚举完成后重试。 ");
            if (ids.Count != 1)
                throw new InstallerFailure("检测到 " + ids.Count + " 个 DJI MI_02 接口；为避免绑定错设备，安装器已停止。只保留一个模块后重试。 ");
            Report(currentStep, "DJI QDC507 MI_02：已连接");
        }

        private void EnsureNoCellularNetworkAdapter()
        {
            string pnputil = Path.Combine(Environment.SystemDirectory, "pnputil.exe");
            ProcessResult result = RunProcess(pnputil, "/enum-devices /connected /class Net", false);
            if (result.ExitCode != 0)
                throw ProcessFailure("无法核验 WWAN 安全状态", pnputil, result);
            if (Regex.IsMatch(result.CombinedOutput, "DJI|Quectel|QDC507|SimTech|SIM7600", RegexOptions.IgnoreCase))
                throw new InstallerFailure("检测到 DJI/Quectel/SimTech 蜂窝网卡。为避免产生移动数据流量，安装器不会继续；请先安全移除或禁用该 WWAN 网卡。 ");
            Report(currentStep, "未检测到相关 WWAN 网卡：通过");
        }

        private string EnsurePowerShell(string installerPath)
        {
            string existing = FindPowerShell7();
            if (existing != null)
            {
                Report(currentStep, "已存在 PowerShell 7：" + existing);
                return existing;
            }

            string msiexec = Path.Combine(Environment.SystemDirectory, "msiexec.exe");
            string msiLog = Path.Combine(workRoot, "PowerShell-7.6.4-install.log");
            string arguments = "/i " + QuoteArgument(installerPath) +
                " /qn /norestart ADD_PATH=1 REGISTER_MANIFEST=1 ENABLE_PSREMOTING=0 USE_MU=0 ENABLE_MU=0 /L*v " + QuoteArgument(msiLog);
            ProcessResult result = RunProcess(msiexec, arguments, true);
            if (result.ExitCode != 0 && result.ExitCode != 3010)
            {
                AppendExternalLog(msiLog);
                throw ProcessFailure("PowerShell 7.6.4 MSI 安装失败", msiexec, result);
            }
            if (result.ExitCode == 3010)
                AddWarning("PowerShell 安装要求重启 Windows；本次会继续验收，完成后仍建议重启。 ");
            RefreshProcessPath();
            existing = FindPowerShell7();
            if (existing == null)
                throw new InstallerFailure("PowerShell MSI 返回成功，但没有找到可运行的 pwsh.exe。MSI 日志已写入总日志。 ");
            Report(currentStep, "PowerShell 7.6.4 安装完成");
            return existing;
        }

        private string FindPowerShell7()
        {
            List<string> candidates = new List<string>();
            string programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
            candidates.Add(Path.Combine(programFiles, "PowerShell", "7", "pwsh.exe"));
            string onPath = ResolveOnPath("pwsh.exe");
            if (onPath != null)
                candidates.Add(onPath);

            foreach (string candidate in candidates.Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (!File.Exists(candidate))
                    continue;
                ProcessResult result = RunProcess(candidate, "--version", false);
                Match match = Regex.Match(result.CombinedOutput, @"PowerShell\s+(\d+)(?:\.(\d+))?", RegexOptions.IgnoreCase);
                int major;
                if (result.ExitCode == 0 && match.Success && Int32.TryParse(match.Groups[1].Value, out major) && major >= 7)
                    return candidate;
            }
            return null;
        }

        private string EnsurePython(string installerPath)
        {
            string existing = FindPython314();
            if (existing != null)
            {
                Report(currentStep, "已存在 Python 3.14 x64：" + existing);
                return existing;
            }

            string pythonLog = Path.Combine(workRoot, "Python-3.14.5-install.log");
            string arguments = "/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1 Include_launcher=1 InstallLauncherAllUsers=1 Include_test=0 AssociateFiles=0 Shortcuts=0 /log " + QuoteArgument(pythonLog);
            ProcessResult result = RunProcess(installerPath, arguments, true);
            if (result.ExitCode != 0 && result.ExitCode != 3010)
            {
                AppendExternalLog(pythonLog);
                throw ProcessFailure("Python 3.14.5 x64 安装失败", installerPath, result);
            }
            if (result.ExitCode == 3010)
                AddWarning("Python 安装要求重启 Windows；本次会继续验收，完成后仍建议重启。 ");
            RefreshProcessPath();
            existing = FindPython314();
            if (existing == null)
                throw new InstallerFailure("Python 安装器返回成功，但没有找到 Python 3.14 x64。安装日志已并入总日志。 ");
            Report(currentStep, "Python 3.14.5 x64 安装完成");
            return existing;
        }

        private string FindPython314()
        {
            List<string> candidates = new List<string>();
            string programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            candidates.Add(Path.Combine(programFiles, "Python314", "python.exe"));
            candidates.Add(Path.Combine(localAppData, "Programs", "Python", "Python314", "python.exe"));
            string onPath = ResolveOnPath("python.exe");
            if (onPath != null)
                candidates.Add(onPath);

            string launcher = ResolveOnPath("py.exe");
            if (launcher != null)
            {
                ProcessResult launcherResult = RunProcess(
                    launcher,
                    "-3.14 -c " + QuoteArgument("import sys; print(sys.executable)"),
                    false);
                if (launcherResult.ExitCode == 0)
                {
                    string launchedPath = LastNonEmptyLine(launcherResult.StandardOutput);
                    if (!String.IsNullOrEmpty(launchedPath))
                        candidates.Add(launchedPath.Trim());
                }
            }

            foreach (string candidate in candidates.Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (IsPython314X64(candidate))
                    return Path.GetFullPath(candidate);
            }
            return null;
        }

        private bool IsPython314X64(string python)
        {
            if (String.IsNullOrWhiteSpace(python) || !File.Exists(python))
                return false;
            string code = "import sys; print('%d.%d|%d|%s' % (sys.version_info[0], sys.version_info[1], 64 if sys.maxsize > 2**32 else 32, sys.executable))";
            ProcessResult result = RunProcess(python, "-c " + QuoteArgument(code), false);
            if (result.ExitCode != 0)
                return false;
            string line = LastNonEmptyLine(result.StandardOutput);
            return line != null && line.StartsWith("3.14|64|", StringComparison.Ordinal);
        }

        private void InstallPythonPackages(string python, string wheelDirectory)
        {
            string lockPath = Path.Combine(wheelDirectory, "requirements-lock.txt");
            if (!File.Exists(lockPath))
                throw new InstallerFailure("离线依赖锁文件缺失：" + lockPath);
            string arguments = "-m pip install --disable-pip-version-check --no-index --find-links " +
                QuoteArgument(wheelDirectory) + " --requirement " + QuoteArgument(lockPath);
            ProcessResult result = RunProcess(python, arguments, true);
            if (result.ExitCode != 0)
                throw ProcessFailure("离线 Python 依赖安装失败", python, result);

            string verifyCode =
                "import flask, serial, requests; import importlib.metadata as m; " +
                "expected={'Flask':'3.1.3','pyserial':'3.5','requests':'2.34.2'}; " +
                "actual={k:m.version(k) for k in expected}; " +
                "assert actual==expected, (actual, expected); print('PYTHON_PACKAGES_OK')";
            ProcessResult verify = RunProcess(python, "-c " + QuoteArgument(verifyCode), true);
            if (verify.ExitCode != 0 || !verify.StandardOutput.Contains("PYTHON_PACKAGES_OK"))
                throw ProcessFailure("Python 依赖版本验收失败", python, verify);
            Report(currentStep, "Flask、pyserial、requests 及传递依赖：通过");
        }

        private void EnsureDjiAtDriver(string pwsh, string projectDirectory)
        {
            string tools = Path.Combine(projectDirectory, "tools");
            string discovery = Path.Combine(tools, "DjiDeviceDiscovery.ps1");
            string binder = Path.Combine(tools, "Bind-DjiAtPort.ps1");
            ProcessResult probe = ProbeValidatedDriver(pwsh, discovery);
            if (probe.ExitCode == 0 && probe.StandardOutput.Contains("DRIVER_READY"))
            {
                Report(currentStep, "Quectel AT 驱动 30.0.65.2 已就绪，无需重复安装");
                return;
            }
            if (probe.ExitCode != 3)
                throw ProcessFailure("读取当前 DJI AT 驱动状态失败", pwsh, probe);

            ProcessResult listOnly = RunProcess(
                pwsh,
                "-NoProfile -File " + QuoteArgument(binder) + " -Mode ListOnly",
                true);
            if (listOnly.ExitCode != 0 || !listOnly.StandardOutput.Contains("Result=LIST_ONLY_OK"))
                throw ProcessFailure("随包驱动候选校验失败，未执行绑定", pwsh, listOnly);

            ProcessResult install = RunProcess(
                pwsh,
                "-NoProfile -File " + QuoteArgument(binder) + " -Mode Install",
                true);
            if (install.ExitCode != 0 || !install.StandardOutput.Contains("Result=INSTALL_OK"))
                throw ProcessFailure("DJI MI_02 驱动绑定失败", pwsh, install);

            bool needReboot = install.StandardOutput.IndexOf("NeedReboot=True", StringComparison.OrdinalIgnoreCase) >= 0;
            ProcessResult finalProbe = null;
            for (int attempt = 0; attempt < 10; attempt++)
            {
                Thread.Sleep(1200);
                finalProbe = ProbeValidatedDriver(pwsh, discovery);
                if (finalProbe.ExitCode == 0 && finalProbe.StandardOutput.Contains("DRIVER_READY"))
                {
                    Report(currentStep, "Quectel AT 驱动 30.0.65.2 绑定完成");
                    if (needReboot)
                        AddWarning("驱动报告需要重启；安装结束后请重启 Windows，再首次启动短信工具。 ");
                    return;
                }
            }

            string advice = needReboot
                ? "驱动安装已返回成功，但设备要求重启。请重启 Windows 后重新运行本安装器完成验收。"
                : "驱动安装已返回成功，但 AT 串口未在 12 秒内重新出现。请拔插 DJI 模块后重新运行本安装器。";
            throw new InstallerFailure(advice + " 最后探测结果：" + Tail(finalProbe == null ? String.Empty : finalProbe.CombinedOutput, 8));
        }

        private ProcessResult ProbeValidatedDriver(string pwsh, string discoveryScript)
        {
            string script =
                "$ErrorActionPreference='Stop'\n" +
                ". " + PowerShellSingleQuoted(discoveryScript) + "\n" +
                "$d=Get-DjiMi02Device\n" +
                "if(-not $d.IsQuectelAtPort -or -not $d.PortName -or -not $d.DriverName){Write-Output 'DRIVER_NEEDS_UPDATE';exit 3}\n" +
                "$p=Join-Path $env:SystemRoot 'System32\\pnputil.exe'\n" +
                "$o=@(& $p /enum-drivers /class Ports 2>&1)\n" +
                "if($LASTEXITCODE -ne 0){throw 'pnputil driver query failed'}\n" +
                "$blocks=[regex]::Split(($o -join \"`n\"),'(?:\\r?\\n){2,}')\n" +
                "$b=@($blocks|Where-Object{$_ -match [regex]::Escape($d.DriverName)})|Select-Object -First 1\n" +
                "if($b -match '(?i)Quectel Incorporated' -and $b -match '30\\.0\\.65\\.2'){Write-Output ('DRIVER_READY|'+$d.PortName);exit 0}\n" +
                "Write-Output 'DRIVER_NEEDS_UPDATE';exit 3\n";
            string encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(script));
            return RunProcess(pwsh, "-NoProfile -EncodedCommand " + encoded, true);
        }

        private static string SelectInstallPath()
        {
            try
            {
                DriveInfo drive = new DriveInfo("D:\\");
                if (drive.IsReady && drive.DriveType == DriveType.Fixed &&
                    String.Equals(drive.DriveFormat, "NTFS", StringComparison.OrdinalIgnoreCase))
                    return Path.Combine(drive.RootDirectory.FullName, "CTExcel-SMS-DJI");
            }
            catch
            {
            }
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CTExcel-SMS-DJI");
        }

        private void InstallProject(string source, string destination, string python)
        {
            bool repair = false;
            if (Directory.Exists(destination) && Directory.EnumerateFileSystemEntries(destination).Any())
            {
                if (!File.Exists(Path.Combine(destination, MarkerName)))
                    throw new InstallerFailure("目标目录已存在且不是本安装器创建的目录：" + destination + "。请先改名或移走该目录，安装器不会覆盖未知文件。 ");
                repair = true;
                Report(currentStep, "检测到已有安装，将修复程序文件并保留目标电脑上的配置与短信状态");
            }
            else
            {
                Directory.CreateDirectory(destination);
            }
            GrantCurrentUserModify(destination);

            HashSet<string> mutableFiles = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                "config.json",
                "archive.jsonl",
                "state.json",
                "tg_state.json"
            };

            string sourcePrefix = Path.GetFullPath(source).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            foreach (string sourceFile in Directory.GetFiles(source, "*", SearchOption.AllDirectories))
            {
                string fullSource = Path.GetFullPath(sourceFile);
                if (!fullSource.StartsWith(sourcePrefix, StringComparison.OrdinalIgnoreCase))
                    throw new InstallerFailure("项目复制发现越界文件：" + sourceFile);
                string relative = fullSource.Substring(sourcePrefix.Length);
                string target = Path.Combine(destination, relative);
                if (repair && mutableFiles.Contains(relative) && File.Exists(target))
                    continue;
                string parent = Path.GetDirectoryName(target);
                if (!Directory.Exists(parent))
                    Directory.CreateDirectory(parent);
                File.Copy(fullSource, target, true);
            }

            string pythonw = Path.Combine(Path.GetDirectoryName(python), "pythonw.exe");
            if (!File.Exists(pythonw))
                throw new InstallerFailure("Python 3.14 存在，但同目录缺少 pythonw.exe：" + pythonw);
            File.WriteAllText(Path.Combine(destination, "runtime-python.txt"), python, new UTF8Encoding(false));
            File.WriteAllText(Path.Combine(destination, "runtime-pythonw.txt"), pythonw, new UTF8Encoding(false));

            string marker =
                "{\n" +
                "  \"package_built_utc\": \"" + PayloadInfo.BuiltUtc + "\",\n" +
                "  \"project_sha256\": \"" + PayloadInfo.ProjectSha256 + "\",\n" +
                "  \"last_installed_utc\": \"" + DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ") + "\",\n" +
                "  \"autostart\": false\n" +
                "}\n";
            File.WriteAllText(Path.Combine(destination, MarkerName), marker, new UTF8Encoding(false));
            Report(currentStep, (repair ? "修复完成：" : "首次安装完成：") + destination);
        }

        private static void GrantCurrentUserModify(string directory)
        {
            SecurityIdentifier user = WindowsIdentity.GetCurrent().User;
            if (user == null)
                throw new InstallerFailure("无法识别当前 Windows 用户，不能安全设置安装目录写入权限。 ");
            DirectorySecurity security = Directory.GetAccessControl(directory);
            FileSystemAccessRule rule = new FileSystemAccessRule(
                user,
                FileSystemRights.Modify,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None,
                AccessControlType.Allow);
            security.AddAccessRule(rule);
            Directory.SetAccessControl(directory, security);
        }

        private void RunReadinessCheck(string pwsh, string installPath)
        {
            string script = Path.Combine(installPath, "tools", "Test-NewPcReadiness.ps1");
            ProcessResult result = RunProcess(
                pwsh,
                "-NoProfile -File " + QuoteArgument(script),
                true);
            if (result.ExitCode != 0)
                throw ProcessFailure("新电脑只读验收失败", pwsh, result);

            Match summary = Regex.Match(result.CombinedOutput, @"SUMMARY\s+PASS=(\d+)\s+WARN=(\d+)\s+FAIL=(\d+)", RegexOptions.IgnoreCase);
            if (!summary.Success)
                throw new InstallerFailure("只读验收脚本返回成功，但没有输出 SUMMARY。详见日志：" + LogPath);
            int warningCount = Int32.Parse(summary.Groups[2].Value);
            int failureCount = Int32.Parse(summary.Groups[3].Value);
            if (failureCount != 0)
                throw new InstallerFailure("只读验收报告 FAIL=" + failureCount + "。详见日志：" + LogPath);
            if (warningCount > 0)
                AddWarning("只读验收包含 " + warningCount + " 项提醒；常见原因是新电脑的 Telegram 本机代理尚未启动。 ");
            Report(currentStep, "只读验收通过：" + summary.Value);
        }

        private void TryCreateDesktopShortcut(string installPath)
        {
            string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
            string shortcutPath = Path.Combine(desktop, "CTExcel短信工具-DJI.lnk");
            if (File.Exists(shortcutPath))
            {
                Report(currentStep, "桌面已有同名快捷方式，未覆盖：" + shortcutPath);
                return;
            }

            object shell = null;
            object shortcut = null;
            try
            {
                Type shellType = Type.GetTypeFromProgID("WScript.Shell");
                if (shellType == null)
                    throw new InvalidOperationException("WScript.Shell is unavailable.");
                shell = Activator.CreateInstance(shellType);
                shortcut = shellType.InvokeMember(
                    "CreateShortcut",
                    BindingFlags.InvokeMethod,
                    null,
                    shell,
                    new object[] { shortcutPath });
                Type shortcutType = shortcut.GetType();
                shortcutType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { Path.Combine(installPath, "启动短信工具.bat") });
                shortcutType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { installPath });
                shortcutType.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "手动启动 CTExcel DJI 短信工具" });
                shortcutType.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
                Report(currentStep, "已创建桌面手动启动快捷方式；未创建任何开机启动项");
            }
            catch (Exception ex)
            {
                AddWarning("程序已安装，但桌面快捷方式创建失败：" + ex.Message + "。可直接双击安装目录中的“启动短信工具.bat”。 ");
            }
            finally
            {
                if (shortcut != null && Marshal.IsComObject(shortcut))
                    Marshal.FinalReleaseComObject(shortcut);
                if (shell != null && Marshal.IsComObject(shell))
                    Marshal.FinalReleaseComObject(shell);
            }
        }

        private ProcessResult RunProcess(string fileName, string arguments, bool logOutput)
        {
            Log("RUN " + fileName + " " + arguments);
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = fileName;
            info.Arguments = arguments;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            info.StandardOutputEncoding = Encoding.UTF8;
            info.StandardErrorEncoding = Encoding.UTF8;

            using (Process process = new Process())
            {
                process.StartInfo = info;
                try
                {
                    process.Start();
                }
                catch (Exception ex)
                {
                    throw new InstallerFailure("无法启动进程 " + fileName + "：" + ex.Message, ex);
                }
                Task<string> stdout = process.StandardOutput.ReadToEndAsync();
                Task<string> stderr = process.StandardError.ReadToEndAsync();
                process.WaitForExit();
                Task.WaitAll(stdout, stderr);
                ProcessResult result = new ProcessResult
                {
                    ExitCode = process.ExitCode,
                    StandardOutput = stdout.Result ?? String.Empty,
                    StandardError = stderr.Result ?? String.Empty
                };
                Log("EXIT " + result.ExitCode + " " + fileName);
                if (!String.IsNullOrWhiteSpace(result.CombinedOutput))
                {
                    Log(result.CombinedOutput);
                    if (logOutput)
                        Report(currentStep, Tail(result.CombinedOutput, 18));
                }
                return result;
            }
        }

        private InstallerFailure ProcessFailure(string operation, string executable, ProcessResult result)
        {
            string detail = Tail(result.CombinedOutput, 16);
            if (String.IsNullOrWhiteSpace(detail))
                detail = "进程没有返回文本。";
            return new InstallerFailure(
                operation + "。程序：" + executable + "；退出码：" + result.ExitCode + "；详情：" + detail + "；完整日志：" + LogPath);
        }

        private void AppendExternalLog(string path)
        {
            if (!File.Exists(path))
                return;
            try
            {
                string text = File.ReadAllText(path, Encoding.UTF8);
                Log("EXTERNAL INSTALLER LOG " + path + Environment.NewLine + Tail(text, 120));
            }
            catch (Exception ex)
            {
                Log("Could not read external installer log: " + ex.Message);
            }
        }

        private void Step(string value)
        {
            currentStep = value;
            Log("STEP " + value);
            Report(value, value);
        }

        private void AddWarning(string value)
        {
            warnings.Add(value);
            Log("WARNING " + value);
            Report(currentStep, "提醒：" + value);
        }

        private void Report(string step, string message)
        {
            if (reporter != null)
                reporter(step, message);
        }

        private void Log(string value)
        {
            lock (logLock)
            {
                logWriter.WriteLine("[{0:yyyy-MM-dd HH:mm:ss.fff}] {1}", DateTime.Now, value);
            }
        }

        internal static string QuoteArgument(string value)
        {
            if (value == null)
                return "\"\"";
            return "\"" + value.Replace("\"", "\\\"") + "\"";
        }

        private static string PowerShellSingleQuoted(string value)
        {
            return "'" + value.Replace("'", "''") + "'";
        }

        private static string LastNonEmptyLine(string value)
        {
            if (value == null)
                return null;
            string[] lines = value.Split(new string[] { "\r\n", "\n" }, StringSplitOptions.RemoveEmptyEntries);
            return lines.Length == 0 ? null : lines[lines.Length - 1].Trim();
        }

        private static string Tail(string value, int maxLines)
        {
            if (String.IsNullOrWhiteSpace(value))
                return String.Empty;
            string[] lines = value.Split(new string[] { "\r\n", "\n" }, StringSplitOptions.RemoveEmptyEntries);
            int start = Math.Max(0, lines.Length - maxLines);
            return String.Join(Environment.NewLine, lines.Skip(start).ToArray()).Trim();
        }

        private static string ResolveOnPath(string executable)
        {
            string path = Environment.GetEnvironmentVariable("PATH") ?? String.Empty;
            foreach (string rawDirectory in path.Split(Path.PathSeparator))
            {
                string directory = rawDirectory.Trim().Trim('"');
                if (directory.Length == 0)
                    continue;
                try
                {
                    string candidate = Path.Combine(directory, executable);
                    if (File.Exists(candidate))
                        return Path.GetFullPath(candidate);
                }
                catch
                {
                }
            }
            return null;
        }

        private static void RefreshProcessPath()
        {
            string machine = Environment.GetEnvironmentVariable("Path", EnvironmentVariableTarget.Machine) ?? String.Empty;
            string user = Environment.GetEnvironmentVariable("Path", EnvironmentVariableTarget.User) ?? String.Empty;
            Environment.SetEnvironmentVariable("PATH", machine + Path.PathSeparator + user, EnvironmentVariableTarget.Process);
        }

        private static bool IsAdministrator()
        {
            WindowsIdentity identity = WindowsIdentity.GetCurrent();
            WindowsPrincipal principal = new WindowsPrincipal(identity);
            return principal.IsInRole(WindowsBuiltInRole.Administrator);
        }

        private static void SafeDeleteWorkRoot(string path)
        {
            if (String.IsNullOrWhiteSpace(path) || !Directory.Exists(path))
                return;
            try
            {
                string tempPrefix = Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
                string full = Path.GetFullPath(path);
                string name = Path.GetFileName(full);
                bool expectedName = name.StartsWith("CTExcel-SMS-DJI-install-work-", StringComparison.Ordinal) ||
                    name.StartsWith("CTExcel-SMS-DJI-self-test-work-", StringComparison.Ordinal);
                if (full.StartsWith(tempPrefix, StringComparison.OrdinalIgnoreCase) && expectedName)
                    Directory.Delete(full, true);
            }
            catch
            {
            }
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct WINTRUST_FILE_INFO
        {
            public UInt32 cbStruct;
            public IntPtr pcwszFilePath;
            public IntPtr hFile;
            public IntPtr pgKnownSubject;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct WINTRUST_DATA
        {
            public UInt32 cbStruct;
            public IntPtr pPolicyCallbackData;
            public IntPtr pSIPClientData;
            public UInt32 dwUIChoice;
            public UInt32 fdwRevocationChecks;
            public UInt32 dwUnionChoice;
            public IntPtr pFile;
            public UInt32 dwStateAction;
            public IntPtr hWVTStateData;
            public IntPtr pwszURLReference;
            public UInt32 dwProvFlags;
            public UInt32 dwUIContext;
        }

        [DllImport("wintrust.dll", ExactSpelling = true, PreserveSig = true, SetLastError = true)]
        private static extern UInt32 WinVerifyTrust(
            IntPtr hwnd,
            [MarshalAs(UnmanagedType.LPStruct)] Guid pgActionID,
            IntPtr pWVTData);

        private static bool VerifyAuthenticode(string path)
        {
            IntPtr filePathPointer = IntPtr.Zero;
            IntPtr fileInfoPointer = IntPtr.Zero;
            IntPtr dataPointer = IntPtr.Zero;
            try
            {
                filePathPointer = Marshal.StringToCoTaskMemUni(path);
                WINTRUST_FILE_INFO fileInfo = new WINTRUST_FILE_INFO();
                fileInfo.cbStruct = (UInt32)Marshal.SizeOf(typeof(WINTRUST_FILE_INFO));
                fileInfo.pcwszFilePath = filePathPointer;
                fileInfo.hFile = IntPtr.Zero;
                fileInfo.pgKnownSubject = IntPtr.Zero;
                fileInfoPointer = Marshal.AllocCoTaskMem(Marshal.SizeOf(typeof(WINTRUST_FILE_INFO)));
                Marshal.StructureToPtr(fileInfo, fileInfoPointer, false);

                WINTRUST_DATA data = new WINTRUST_DATA();
                data.cbStruct = (UInt32)Marshal.SizeOf(typeof(WINTRUST_DATA));
                data.dwUIChoice = 2;
                data.fdwRevocationChecks = 0;
                data.dwUnionChoice = 1;
                data.pFile = fileInfoPointer;
                data.dwStateAction = 0;
                data.dwProvFlags = 0x00001000;
                data.dwUIContext = 0;
                dataPointer = Marshal.AllocCoTaskMem(Marshal.SizeOf(typeof(WINTRUST_DATA)));
                Marshal.StructureToPtr(data, dataPointer, false);

                Guid action = new Guid("00AAC56B-CD44-11d0-8CC2-00C04FC295EE");
                UInt32 result = WinVerifyTrust(new IntPtr(-1), action, dataPointer);
                return result == 0;
            }
            finally
            {
                if (dataPointer != IntPtr.Zero)
                    Marshal.FreeCoTaskMem(dataPointer);
                if (fileInfoPointer != IntPtr.Zero)
                    Marshal.FreeCoTaskMem(fileInfoPointer);
                if (filePathPointer != IntPtr.Zero)
                    Marshal.FreeCoTaskMem(filePathPointer);
            }
        }

        private sealed class PayloadPaths
        {
            internal string ProjectDirectory;
            internal string WheelsDirectory;
            internal string PythonInstaller;
            internal string PowerShellInstaller;
        }
    }

    internal sealed class InstallerFailure : Exception
    {
        internal InstallerFailure(string message)
            : base(message)
        {
        }

        internal InstallerFailure(string message, Exception innerException)
            : base(message, innerException)
        {
        }
    }
}
