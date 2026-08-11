[CmdletBinding()]
param(
    [ValidateSet('ListOnly', 'Install')]
    [string]$Mode = 'ListOnly',

    [string]$InstanceId,

    [string]$InfPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'DjiDeviceDiscovery.ps1')

if (-not $InstanceId) {
    $InstanceId = (Get-DjiMi02Device).InstanceId
}
if (-not $InfPath) {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $InfPath = Join-Path $projectRoot 'drivers\Quectel-Ports-30.0.65.2\qcser.inf'
}

$expectedPrefix = 'USB\VID_2CA3&PID_4006&MI_02\'
$expectedDescription = 'Quectel USB AT Port'
$expectedProvider = 'Quectel Incorporated'
$expectedVersion = '30.0.65.2'

if (-not $InstanceId.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to touch a non-MI_02 device: $InstanceId"
}

$resolvedInf = [IO.Path]::GetFullPath($InfPath)
if (-not (Test-Path -LiteralPath $resolvedInf -PathType Leaf)) {
    throw "Driver INF not found: $resolvedInf"
}
if ([IO.Path]::GetFileName($resolvedInf) -ine 'qcser.inf') {
    throw "Expected qcser.inf, received: $resolvedInf"
}

$driverRoot = Split-Path -Parent $resolvedInf
$expectedHashes = [ordered]@{
    'qcser.cat' = '84511642502CF1398C6B859303C1AD87FA1BEF6CCA65CDA2CCF1D741D1004F2D'
    'qcser.inf' = 'ECD9EBD5337D32B6ED9CB0AE5599BFA3DFD77EE375723F82ECC46EF732D5F037'
    'serial\amd64\qcusbser.sys' = '4FFB594F274B597740DBE1BC698492D4D447E294188339370BE12A2C764DBD9A'
}
foreach ($relativePath in $expectedHashes.Keys) {
    $filePath = Join-Path $driverRoot $relativePath
    if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
        throw "Bundled driver file not found: $filePath"
    }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $filePath).Hash
    if ($actualHash -ne $expectedHashes[$relativePath]) {
        throw "Bundled driver hash mismatch: $relativePath"
    }
}

$catalogPath = Join-Path $driverRoot 'qcser.cat'
$catalogSignature = Get-AuthenticodeSignature -LiteralPath $catalogPath
if (
    $catalogSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
    $catalogSignature.SignerCertificate.Subject -notmatch 'Microsoft Windows Hardware Compatibility Publisher'
) {
    throw 'Bundled Quectel driver catalog signature is not valid Microsoft WHCP.'
}

if ($Mode -eq 'Install') {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Install mode requires an elevated administrator process.'
    }
}

if (-not ('DjiAtPortBinder' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class DjiAtPortBinder
{
    private const uint SPDIT_CLASSDRIVER = 0x00000001;
    private const uint DI_ENUMSINGLEINF = 0x00010000;
    private const int ERROR_NO_MORE_ITEMS = 259;
    private static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

    [StructLayout(LayoutKind.Sequential)]
    private struct SP_DEVINFO_DATA
    {
        public uint cbSize;
        public Guid ClassGuid;
        public uint DevInst;
        public IntPtr Reserved;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct SP_DEVINSTALL_PARAMS
    {
        public uint cbSize;
        public uint Flags;
        public uint FlagsEx;
        public IntPtr hwndParent;
        public IntPtr InstallMsgHandler;
        public IntPtr InstallMsgHandlerContext;
        public IntPtr FileQueue;
        public IntPtr ClassInstallReserved;
        public uint Reserved;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string DriverPath;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct SP_DRVINFO_DATA
    {
        public uint cbSize;
        public uint DriverType;
        public IntPtr Reserved;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
        public string Description;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
        public string MfgName;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
        public string ProviderName;

        public System.Runtime.InteropServices.ComTypes.FILETIME DriverDate;
        public ulong DriverVersion;
    }

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern IntPtr SetupDiCreateDeviceInfoList(IntPtr ClassGuid, IntPtr hwndParent);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetupDiOpenDeviceInfoW(
        IntPtr DeviceInfoSet,
        string DeviceInstanceId,
        IntPtr hwndParent,
        uint OpenFlags,
        ref SP_DEVINFO_DATA DeviceInfoData);

    [DllImport("setupapi.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetupDiSetSelectedDevice(
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetupDiGetDeviceInstallParamsW(
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData,
        ref SP_DEVINSTALL_PARAMS DeviceInstallParams);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetupDiSetDeviceInstallParamsW(
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData,
        ref SP_DEVINSTALL_PARAMS DeviceInstallParams);

    [DllImport("setupapi.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetupDiBuildDriverInfoList(
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData,
        uint DriverType);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetupDiEnumDriverInfoW(
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData,
        uint DriverType,
        uint MemberIndex,
        ref SP_DRVINFO_DATA DriverInfoData);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetupDiSetSelectedDriverW(
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData,
        ref SP_DRVINFO_DATA DriverInfoData);

    [DllImport("setupapi.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetupDiDestroyDriverInfoList(
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData,
        uint DriverType);

    [DllImport("setupapi.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetupDiDestroyDeviceInfoList(IntPtr DeviceInfoSet);

    [DllImport("newdev.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DiInstallDevice(
        IntPtr hwndParent,
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData,
        ref SP_DRVINFO_DATA DriverInfoData,
        uint Flags,
        [MarshalAs(UnmanagedType.Bool)] out bool NeedReboot);

    private static void ThrowLastError(string operation)
    {
        int error = Marshal.GetLastWin32Error();
        throw new Win32Exception(error, operation + " failed with Win32 error " + error + ".");
    }

    private static string FormatVersion(ulong value)
    {
        return String.Format(
            "{0}.{1}.{2}.{3}",
            (value >> 48) & 0xffff,
            (value >> 32) & 0xffff,
            (value >> 16) & 0xffff,
            value & 0xffff);
    }

    private static void OpenSingleInfDriverList(
        string instanceId,
        string infPath,
        out IntPtr deviceInfoSet,
        out SP_DEVINFO_DATA deviceInfoData)
    {
        deviceInfoSet = SetupDiCreateDeviceInfoList(IntPtr.Zero, IntPtr.Zero);
        if (deviceInfoSet == INVALID_HANDLE_VALUE)
            ThrowLastError("SetupDiCreateDeviceInfoList");

        deviceInfoData = new SP_DEVINFO_DATA();
        deviceInfoData.cbSize = (uint)Marshal.SizeOf(typeof(SP_DEVINFO_DATA));

        if (!SetupDiOpenDeviceInfoW(deviceInfoSet, instanceId, IntPtr.Zero, 0, ref deviceInfoData))
            ThrowLastError("SetupDiOpenDeviceInfo");

        if (!SetupDiSetSelectedDevice(deviceInfoSet, ref deviceInfoData))
            ThrowLastError("SetupDiSetSelectedDevice");

        SP_DEVINSTALL_PARAMS parameters = new SP_DEVINSTALL_PARAMS();
        parameters.cbSize = (uint)Marshal.SizeOf(typeof(SP_DEVINSTALL_PARAMS));
        parameters.DriverPath = String.Empty;

        if (!SetupDiGetDeviceInstallParamsW(deviceInfoSet, ref deviceInfoData, ref parameters))
            ThrowLastError("SetupDiGetDeviceInstallParams");

        parameters.Flags |= DI_ENUMSINGLEINF;
        parameters.DriverPath = infPath;

        if (!SetupDiSetDeviceInstallParamsW(deviceInfoSet, ref deviceInfoData, ref parameters))
            ThrowLastError("SetupDiSetDeviceInstallParams");

        if (!SetupDiBuildDriverInfoList(deviceInfoSet, ref deviceInfoData, SPDIT_CLASSDRIVER))
            ThrowLastError("SetupDiBuildDriverInfoList");
    }

    private static List<SP_DRVINFO_DATA> EnumerateDrivers(
        IntPtr deviceInfoSet,
        ref SP_DEVINFO_DATA deviceInfoData)
    {
        List<SP_DRVINFO_DATA> drivers = new List<SP_DRVINFO_DATA>();
        for (uint index = 0; ; index++)
        {
            SP_DRVINFO_DATA driver = new SP_DRVINFO_DATA();
            driver.cbSize = (uint)Marshal.SizeOf(typeof(SP_DRVINFO_DATA));

            if (!SetupDiEnumDriverInfoW(
                    deviceInfoSet,
                    ref deviceInfoData,
                    SPDIT_CLASSDRIVER,
                    index,
                    ref driver))
            {
                int error = Marshal.GetLastWin32Error();
                if (error == ERROR_NO_MORE_ITEMS)
                    break;
                throw new Win32Exception(error, "SetupDiEnumDriverInfo failed with Win32 error " + error + ".");
            }

            drivers.Add(driver);
        }

        return drivers;
    }

    public static string[] ListCandidates(string instanceId, string infPath)
    {
        IntPtr deviceInfoSet = INVALID_HANDLE_VALUE;
        SP_DEVINFO_DATA deviceInfoData = new SP_DEVINFO_DATA();
        bool builtList = false;

        try
        {
            OpenSingleInfDriverList(instanceId, infPath, out deviceInfoSet, out deviceInfoData);
            builtList = true;
            List<SP_DRVINFO_DATA> drivers = EnumerateDrivers(deviceInfoSet, ref deviceInfoData);
            List<string> result = new List<string>();

            for (int index = 0; index < drivers.Count; index++)
            {
                SP_DRVINFO_DATA driver = drivers[index];
                result.Add(String.Join("\t", new string[] {
                    index.ToString(),
                    driver.Description ?? String.Empty,
                    driver.MfgName ?? String.Empty,
                    driver.ProviderName ?? String.Empty,
                    FormatVersion(driver.DriverVersion)
                }));
            }

            return result.ToArray();
        }
        finally
        {
            if (builtList)
                SetupDiDestroyDriverInfoList(deviceInfoSet, ref deviceInfoData, SPDIT_CLASSDRIVER);
            if (deviceInfoSet != INVALID_HANDLE_VALUE)
                SetupDiDestroyDeviceInfoList(deviceInfoSet);
        }
    }

    public static bool InstallExact(
        string instanceId,
        string infPath,
        string expectedDescription,
        string expectedProvider,
        string expectedVersion)
    {
        IntPtr deviceInfoSet = INVALID_HANDLE_VALUE;
        SP_DEVINFO_DATA deviceInfoData = new SP_DEVINFO_DATA();
        bool builtList = false;

        try
        {
            OpenSingleInfDriverList(instanceId, infPath, out deviceInfoSet, out deviceInfoData);
            builtList = true;
            List<SP_DRVINFO_DATA> drivers = EnumerateDrivers(deviceInfoSet, ref deviceInfoData);
            SP_DRVINFO_DATA selected = new SP_DRVINFO_DATA();
            int matches = 0;

            foreach (SP_DRVINFO_DATA driver in drivers)
            {
                if (String.Equals(driver.Description, expectedDescription, StringComparison.OrdinalIgnoreCase) &&
                    String.Equals(driver.ProviderName, expectedProvider, StringComparison.OrdinalIgnoreCase) &&
                    String.Equals(FormatVersion(driver.DriverVersion), expectedVersion, StringComparison.Ordinal))
                {
                    selected = driver;
                    matches++;
                }
            }

            if (matches != 1)
                throw new InvalidOperationException("Expected exactly one AT driver candidate, found " + matches + ".");

            if (!SetupDiSetSelectedDriverW(deviceInfoSet, ref deviceInfoData, ref selected))
                ThrowLastError("SetupDiSetSelectedDriver");

            bool needReboot;
            if (!DiInstallDevice(
                    IntPtr.Zero,
                    deviceInfoSet,
                    ref deviceInfoData,
                    ref selected,
                    0,
                    out needReboot))
                ThrowLastError("DiInstallDevice");

            return needReboot;
        }
        finally
        {
            if (builtList)
                SetupDiDestroyDriverInfoList(deviceInfoSet, ref deviceInfoData, SPDIT_CLASSDRIVER);
            if (deviceInfoSet != INVALID_HANDLE_VALUE)
                SetupDiDestroyDeviceInfoList(deviceInfoSet);
        }
    }
}
'@
}

$candidates = [DjiAtPortBinder]::ListCandidates($InstanceId, $resolvedInf)
Write-Output "TargetInstance=$InstanceId"
Write-Output "DriverInf=$resolvedInf"
Write-Output 'CandidateFormat=Index<TAB>Description<TAB>Manufacturer<TAB>Provider<TAB>Version'
$candidates | ForEach-Object { Write-Output "Candidate=$_" }

$expectedPattern = "`t$([regex]::Escape($expectedDescription))`t.*`t$([regex]::Escape($expectedProvider))`t$([regex]::Escape($expectedVersion))$"
$matchingCandidates = @($candidates | Where-Object { $_ -match $expectedPattern })
if ($matchingCandidates.Count -ne 1) {
    throw "Expected exactly one verified AT candidate; found $($matchingCandidates.Count)."
}

if ($Mode -eq 'ListOnly') {
    Write-Output 'Result=LIST_ONLY_OK'
    exit 0
}

$needReboot = [DjiAtPortBinder]::InstallExact(
    $InstanceId,
    $resolvedInf,
    $expectedDescription,
    $expectedProvider,
    $expectedVersion)

Write-Output 'Result=INSTALL_OK'
Write-Output "NeedReboot=$needReboot"
