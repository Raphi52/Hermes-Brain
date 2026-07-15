[CmdletBinding()]
param()

$ErrorActionPreference = "SilentlyContinue"
$stateRoot = $PSScriptRoot
$configPath = Join-Path $stateRoot "config.json"
if (-not (Test-Path -LiteralPath $configPath)) { exit 0 }

try {
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $python = [string]$config.python
    $brainRoot = [string]$config.brain_root
    $codeRoot = [string]$config.code_root
    $hook = Join-Path $codeRoot "brain_hook.py"
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $hook)) { exit 0 }

    $payload = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($payload)) { exit 0 }

    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $python
    $start.Arguments = '"' + ($hook -replace '"', '\"') + '"'
    $start.WorkingDirectory = Split-Path -Parent $hook
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.EnvironmentVariables.Remove("PYTHONPATH")
    $start.EnvironmentVariables["AMITEL_BRAIN_ROOT"] = $brainRoot
    $start.EnvironmentVariables["AMITEL_BRAIN_CODE_ROOT"] = $codeRoot
    $start.EnvironmentVariables["AMITEL_BRAIN_PYTHON"] = $python

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    [void]$process.Start()
    $process.StandardInput.Write($payload)
    $process.StandardInput.Close()
    $outputTask = $process.StandardOutput.ReadToEndAsync()
    $errorTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(12000)) {
        try { $process.Kill() } catch {}
        exit 0
    }
    $output = $outputTask.GetAwaiter().GetResult()
    [void]$errorTask.GetAwaiter().GetResult()
    if (-not [string]::IsNullOrWhiteSpace($output)) {
        [Console]::Out.Write($output)
    }
} catch {
    # Fail-open: recall must never block the user's prompt.
}
exit 0
