# ============================================================
# AD9910 Control Service - Quick Start (PowerShell)
# Web:  http://LAN-IP:18867
# RPyC: port 18868
# The Analog Devices USB DLLs are 32-bit, so 32-bit Python is required.
# Set AD9910_PYTHON to override the Python executable if necessary.
# ============================================================

$ErrorActionPreference = 'Stop'
Write-Host 'AD9910 start script revision: 2026-11-10-4 (ps1)'

# ---- Select the Python interpreter ----
$py = $null
if ($env:AD9910_PYTHON) {
    $py = $env:AD9910_PYTHON
} elseif (Test-Path 'C:\Program Files (x86)\Python310-32\python.exe') {
    $py = 'C:\Program Files (x86)\Python310-32\python.exe'
} else {
    $py = 'python'
}

# ---- Verify 32-bit interpreter ----
& $py -c "import struct, sys; sys.exit(0 if struct.calcsize('P') == 4 else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host 'ERROR: AD9910 requires a 32-bit Python interpreter.'
    Write-Host "Selected interpreter: $py"
    Read-Host 'Press Enter to exit'
    exit 1
}

# ---- Verify required packages ----
& $py -c "import numpy, tornado, rpyc"
if ($LASTEXITCODE -ne 0) {
    Write-Host 'ERROR: Required Python packages are missing from:'
    Write-Host "  $py"
    Write-Host 'Install them with:'
    Write-Host "  & '$py' -m pip install numpy tornado rpyc"
    Read-Host 'Press Enter to exit'
    exit 1
}

# ---- Locate the project root ----
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..\..')).Path
if (-not (Test-Path (Join-Path $projectRoot 'devices\__init__.py'))) {
    Write-Host 'ERROR: Cannot find the project root from this script location.'
    Write-Host "Script: $PSCommandPath"
    Write-Host "Calculated project root: $projectRoot"
    Read-Host 'Press Enter to exit'
    exit 1
}
Set-Location $projectRoot
$env:PYTHONPATH = "$projectRoot;$env:PYTHONPATH"
Write-Host "Project root: $(Get-Location)"
Write-Host "Python: $py"

# ---- Clock settings (must match the ADI evaluation software) ----
# External Clock = 1000 MHz, Divider Disable = unchecked -> System Clock = 500 MHz
# (the chip's REFCLK /2 input divider stays enabled, so SYSCLK = REF_CLK / 2).
# Every FTW is computed from SYSCLK, so these two must describe the real hardware.
# Override without editing this file:
#   set AD9910_REFCLK=1000
#   set AD9910_DIVIDER_DISABLE=1   (only if Divider Disable is checked; measured
#                                   to stop the DDS output on these boards)
$refclk = if ($env:AD9910_REFCLK) { $env:AD9910_REFCLK } else { '1000' }
$clockArgs = @('--refclk', $refclk)
$divState = 'enabled (/2) -> SYSCLK ' + ([double]$refclk / 2) + ' MHz'
if ($env:AD9910_DIVIDER_DISABLE -eq '1') {
    $clockArgs += '--divider-disable'
    $divState = 'bypassed -> SYSCLK ' + $refclk + ' MHz'
}
Write-Host "DDS clock: REF_CLK $refclk MHz, divider $divState"

# ---- Start the service ----
& $py -m devices.generators.adiclockevals.control_service.app `
    --config-dir devices/generators/adiclockevals/config `
    --web-host 0.0.0.0 --web-port 18867 `
    --rpyc-host 0.0.0.0 --rpyc-port 18868 `
    --config initial @clockArgs

Write-Host ''
Write-Host 'Service exited.'
Read-Host 'Press Enter to exit'
