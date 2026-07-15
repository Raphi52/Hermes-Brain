function Get-AmitelBrainHookCommands([string]$Wrapper) {
    $prefix = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "
    return [pscustomobject]@{
        Quoted = $prefix + '"' + $Wrapper + '"'
        Unquoted = $prefix + $Wrapper
    }
}

function Test-AmitelBrainHookCommand([string]$Actual, [string]$Wrapper) {
    $commands = Get-AmitelBrainHookCommands $Wrapper
    return [string]::Equals($Actual, $commands.Quoted, [StringComparison]::Ordinal) -or
        [string]::Equals($Actual, $commands.Unquoted, [StringComparison]::Ordinal)
}
