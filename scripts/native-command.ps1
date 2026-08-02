function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    & $Command
    $ExitCode = $LASTEXITCODE
    if ($null -ne $ExitCode -and $ExitCode -ne 0) {
        throw "$Label failed with exit code $ExitCode."
    }
}
