[CmdletBinding()]
param(
    [string]$BrainRoot = "",
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA "hermes" }),
    [string]$StateRoot = $(Join-Path $env:LOCALAPPDATA "AmitelBrain"),
    [string]$RuntimePython = "",
    [switch]$SkipDependencies,
    [switch]$SkipIndex,
    [switch]$SkipUserEnvironment
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot "integrations\windows\hook-command.ps1")
if ([string]::IsNullOrWhiteSpace($BrainRoot)) { $BrainRoot = $PSScriptRoot }
$repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).ProviderPath -replace '\\', '/'
$resolvedBrain = (Resolve-Path -LiteralPath $BrainRoot).ProviderPath -replace '\\', '/'
$knowledge = "$resolvedBrain/knowledge"
if (-not (Test-Path -LiteralPath $knowledge)) { throw "knowledge/ introuvable sous $resolvedBrain" }

$venvRoot = Join-Path $StateRoot ".venv"
$runtimeRoot = Join-Path $StateRoot "tooling"
$python = if ($RuntimePython) { (Resolve-Path -LiteralPath $RuntimePython).ProviderPath } else { Join-Path $venvRoot "Scripts\python.exe" }
$configPath = Join-Path $StateRoot "config.json"
$installedWrapper = Join-Path $StateRoot "amitel-brain-hook.ps1"
New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null

if (-not $SkipDependencies) {
    if (-not (Test-Path -LiteralPath $python)) {
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if ($uv) {
            & $uv.Source venv --python 3.11 $venvRoot
            if ($LASTEXITCODE -ne 0) { throw "Échec de création du venv avec uv" }
        } else {
            $py = Get-Command py -ErrorAction SilentlyContinue
            if ($py) {
                & $py.Source -3.11 -m venv $venvRoot
                if ($LASTEXITCODE -ne 0) { throw "Échec de création du venv avec py" }
            } else {
                $systemPython = Get-Command python -ErrorAction SilentlyContinue
                if (-not $systemPython) { throw "Installez uv ou Python 3.11, puis relancez install.ps1" }
                & $systemPython.Source -m venv $venvRoot
                if ($LASTEXITCODE -ne 0) { throw "Échec de création du venv avec python" }
            }
        }
    }
    if (-not (Test-Path -LiteralPath $python)) { throw "Le venv local n'a pas été créé: $python" }
    $requirements = "$repoRoot/tooling/requirements.txt"
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        & $uv.Source pip install --python $python -r $requirements
        if ($LASTEXITCODE -ne 0) { throw "Échec d'installation des dépendances avec uv" }
    } else {
        & $python -m pip install -r $requirements
        if ($LASTEXITCODE -ne 0) { throw "Échec d'installation des dépendances avec pip" }
    }
}
if (-not (Test-Path -LiteralPath $python)) { throw "Runtime absent: $python" }

New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
# TOUS les modules Python du tooling sont copies, pas une liste nommee a la main. Cette liste
# etait figee a 7 fichiers alors que le serveur en importe desormais une vingtaine
# (brain_trace, brain_singleton, brain_curate, brain_validate, brain_attention...) : une
# reinstallation produisait un serveur qui plantait a l import, ou pire, ecrasait une copie
# saine par une copie amputee. Constate le 2026-09-02.
$runtimeFiles = @(Get-ChildItem -LiteralPath "$repoRoot/tooling" -Filter "*.py" -File | ForEach-Object { $_.Name })
if ($runtimeFiles.Count -lt 1) { throw "aucun module Python trouve dans $repoRoot/tooling" }
# brain_server.py reste verifie NOMMEMENT : son absence doit echouer fort, pas silencieusement.
if ($runtimeFiles -notcontains "brain_server.py") { throw "brain_server.py introuvable dans le clone local" }
foreach ($runtimeFile in $runtimeFiles) {
    $source = "$repoRoot/tooling/$runtimeFile"
    Copy-Item -LiteralPath $source -Destination (Join-Path $runtimeRoot $runtimeFile) -Force
}
$indexScript = Join-Path $runtimeRoot "brain_index.py"
Copy-Item -LiteralPath "$repoRoot/integrations/windows/amitel-brain-hook.ps1" -Destination $installedWrapper -Force
$config = [ordered]@{
    brain_root = $resolvedBrain
    code_root = ($runtimeRoot -replace '\\', '/')
    python = ($python -replace '\\', '/')
    installed_from = $repoRoot
    installed_at = [DateTime]::UtcNow.ToString("o")
}
[IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 10), $utf8NoBom)
if (-not $SkipUserEnvironment) {
    [Environment]::SetEnvironmentVariable("AMITEL_BRAIN_ROOT", $resolvedBrain, "User")
    [Environment]::SetEnvironmentVariable("AMITEL_BRAIN_CODE_ROOT", ($runtimeRoot -replace '\\', '/'), "User")
    [Environment]::SetEnvironmentVariable("AMITEL_BRAIN_PYTHON", ($python -replace '\\', '/'), "User")
}
$env:AMITEL_BRAIN_ROOT = $resolvedBrain
$env:AMITEL_BRAIN_CODE_ROOT = ($runtimeRoot -replace '\\', '/')
$env:AMITEL_BRAIN_PYTHON = ($python -replace '\\', '/')

function Read-JsonObject([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        if (-not [string]::IsNullOrWhiteSpace($raw)) { return ($raw | ConvertFrom-Json) }
    }
    return (New-Object PSObject)
}

function Ensure-Property($Object, [string]$Name, $Value) {
    if ($null -eq $Object.PSObject.Properties[$Name]) {
        $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value
    }
}

function Write-JsonWithBackup([string]$Path, $Object) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if (Test-Path -LiteralPath $Path) {
        $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmssfff")
        Copy-Item -LiteralPath $Path -Destination "$Path.amitel-brain.$stamp.bak" -Force
    }
    [IO.File]::WriteAllText($Path, ($Object | ConvertTo-Json -Depth 30), $utf8NoBom)
}

function Add-UserPromptHook([string]$Path, [string]$Command, [string]$Wrapper) {
    $settings = Read-JsonObject $Path
    Ensure-Property $settings "hooks" (New-Object PSObject)
    Ensure-Property $settings.hooks "UserPromptSubmit" @()
    $present = $false
    foreach ($group in @($settings.hooks.UserPromptSubmit)) {
        foreach ($hook in @($group.hooks)) {
            if (Test-AmitelBrainHookCommand ([string]$hook.command) $Wrapper) { $present = $true }
        }
    }
    if (-not $present) {
        $entry = [pscustomobject]@{
            matcher = ""
            hooks = @([pscustomobject]@{ type = "command"; command = $Command; timeout = 15 })
        }
        $settings.hooks.UserPromptSubmit = @($settings.hooks.UserPromptSubmit) + @($entry)
        Write-JsonWithBackup $Path $settings
    }
}

$hookCommands = Get-AmitelBrainHookCommands $installedWrapper
$command = $hookCommands.Quoted
Add-UserPromptHook (Join-Path $env:USERPROFILE ".claude\settings.json") $command $installedWrapper
$codexHome = Join-Path $env:USERPROFILE ".codex"
Add-UserPromptHook (Join-Path $codexHome "hooks.json") $command $installedWrapper

$codexTrusted = $false
$codexCommand = Get-Command codex -ErrorAction SilentlyContinue
if ($codexCommand) {
    $codexTrustOutput = (& $python "$repoRoot/tooling/codex_trust_hook.py" --codex $codexCommand.Source --codex-home $codexHome --cwd $repoRoot --wrapper $installedWrapper 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "Le hook Codex a été installé mais son approbation ciblée a échoué: $codexTrustOutput" }
    $codexTrusted = $true
} else {
    Write-Warning "Codex CLI absent: hook copié mais non approuvé. Relancez install.ps1 après installation de Codex."
}

$pluginSource = "$repoRoot/integrations/hermes-amitel-brain"
$pluginTarget = Join-Path $HermesHome "plugins\hermes-amitel-brain"
New-Item -ItemType Directory -Force -Path $pluginTarget | Out-Null
Copy-Item -Path "$pluginSource\*" -Destination $pluginTarget -Recurse -Force

$hermesEnabled = $false
$hermesCommand = Get-Command hermes -ErrorAction SilentlyContinue
if ($hermesCommand) {
    $oldHermesHome = $env:HERMES_HOME
    $env:HERMES_HOME = $HermesHome
    try {
        $pluginList = (& $hermesCommand.Source plugins list --plain --no-bundled 2>&1 | Out-String)
        $alreadyEnabled = $LASTEXITCODE -eq 0 -and $pluginList -match '(?m)^enabled\s+\S+\s+\S+\s+hermes-amitel-brain\s*$'
        if (-not $alreadyEnabled) {
            $enableOutput = ("n" | & $hermesCommand.Source plugins enable hermes-amitel-brain 2>&1 | Out-String)
            if ($LASTEXITCODE -ne 0) { throw "Le plugin Hermes a été copié mais son activation a échoué: $enableOutput" }
        }
        $hermesEnabled = $true
    } finally {
        if ($null -eq $oldHermesHome) {
            Remove-Item Env:HERMES_HOME -ErrorAction SilentlyContinue
        } else {
            $env:HERMES_HOME = $oldHermesHome
        }
    }
} else {
    Write-Warning "Hermes CLI absent: plugin copié mais non activé. Lancez 'hermes plugins enable hermes-amitel-brain' après installation de Hermes."
}

if (-not $SkipIndex) {
    $oldPythonPath = $env:PYTHONPATH
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    try {
        & $python $indexScript --knowledge $knowledge --out "$resolvedBrain/tooling/index"
        if ($LASTEXITCODE -ne 0) { throw "Échec de l'indexation" }
    } finally {
        if ($null -ne $oldPythonPath) { $env:PYTHONPATH = $oldPythonPath }
    }
}

$result = [ordered]@{
    brain_root = $resolvedBrain
    code_root = ($runtimeRoot -replace '\\', '/')
    python = ($python -replace '\\', '/')
    claude_hook = (Join-Path $env:USERPROFILE ".claude\settings.json")
    codex_hook = (Join-Path $env:USERPROFILE ".codex\hooks.json")
    codex_trusted = $codexTrusted
    hermes_plugin = $pluginTarget
    hermes_enabled = $hermesEnabled
    restart_required = @("Hermes", "Claude Code", "Codex")
}
$result | ConvertTo-Json -Depth 10
