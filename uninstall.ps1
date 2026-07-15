[CmdletBinding()]
param(
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA "hermes" }),
    [string]$StateRoot = $(Join-Path $env:LOCALAPPDATA "AmitelBrain")
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot "integrations\windows\hook-command.ps1")

function Remove-BrainHook([string]$Path, [string]$Wrapper) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $settings = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $settings.hooks -or $null -eq $settings.hooks.UserPromptSubmit) { return }
    $changed = $false
    $groups = @()
    foreach ($group in @($settings.hooks.UserPromptSubmit)) {
        $kept = @()
        foreach ($hook in @($group.hooks)) {
            if (Test-AmitelBrainHookCommand ([string]$hook.command) $Wrapper) {
                $changed = $true
            } else {
                $kept += $hook
            }
        }
        if ($kept.Count -gt 0) {
            $group.hooks = $kept
            $groups += $group
        }
    }
    if ($changed) {
        $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmssfff")
        Copy-Item -LiteralPath $Path -Destination "$Path.amitel-brain.$stamp.bak" -Force
        $settings.hooks.UserPromptSubmit = $groups
        [IO.File]::WriteAllText($Path, ($settings | ConvertTo-Json -Depth 30), $utf8NoBom)
    }
}

$installedWrapper = Join-Path $StateRoot "amitel-brain-hook.ps1"
Remove-BrainHook (Join-Path $env:USERPROFILE ".claude\settings.json") $installedWrapper
Remove-BrainHook (Join-Path $env:USERPROFILE ".codex\hooks.json") $installedWrapper

$plugin = Join-Path $HermesHome "plugins\hermes-amitel-brain"
$hermesCommand = Get-Command hermes -ErrorAction SilentlyContinue
if ($hermesCommand) {
    $oldHermesHome = $env:HERMES_HOME
    $env:HERMES_HOME = $HermesHome
    try {
        & $hermesCommand.Source plugins disable hermes-amitel-brain | Out-Null
    } finally {
        if ($null -eq $oldHermesHome) {
            Remove-Item Env:HERMES_HOME -ErrorAction SilentlyContinue
        } else {
            $env:HERMES_HOME = $oldHermesHome
        }
    }
}
if (Test-Path -LiteralPath $plugin) { Remove-Item -LiteralPath $plugin -Recurse -Force }

$configPath = Join-Path $StateRoot "config.json"
$config = $null
if (Test-Path -LiteralPath $configPath) {
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
if ($config) {
    if ([Environment]::GetEnvironmentVariable("AMITEL_BRAIN_ROOT", "User") -eq [string]$config.brain_root) {
        [Environment]::SetEnvironmentVariable("AMITEL_BRAIN_ROOT", $null, "User")
    }
    if ([Environment]::GetEnvironmentVariable("AMITEL_BRAIN_CODE_ROOT", "User") -eq [string]$config.code_root) {
        [Environment]::SetEnvironmentVariable("AMITEL_BRAIN_CODE_ROOT", $null, "User")
    }
    if ([Environment]::GetEnvironmentVariable("AMITEL_BRAIN_PYTHON", "User") -eq [string]$config.python) {
        [Environment]::SetEnvironmentVariable("AMITEL_BRAIN_PYTHON", $null, "User")
    }
}
Remove-Item -LiteralPath (Join-Path $StateRoot "amitel-brain-hook.ps1") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $configPath -Force -ErrorAction SilentlyContinue

[ordered]@{
    claude_hook_removed = $true
    codex_hook_removed = $true
    hermes_plugin_removed = (-not (Test-Path -LiteralPath $plugin))
    runtime_preserved = (Join-Path $StateRoot ".venv")
    restart_required = @("Hermes", "Claude Code", "Codex")
} | ConvertTo-Json -Depth 10
