# Finishes the AWDAX -> VORA rename of the two folders VS Code kept open.
# 1. Close Visual Studio Code (and any terminal inside these folders). 2. Run:
#    powershell -ExecutionPolicy Bypass -File "D:\otherAndGames\SIDE PROJECTS\AWDAX_SIH\AwdaxP\scripts\finish-folder-rename.ps1"
$projects = 'D:\otherAndGames\SIDE PROJECTS'
Set-Location C:\
Rename-Item -LiteralPath "$projects\AWDAX_SIH\AwdaxP" -NewName 'Vora' -ErrorAction Stop
Rename-Item -LiteralPath "$projects\AWDAX_SIH" -NewName 'VORA_SIH' -ErrorAction Stop
# the browser path in .env follows the new folder names
$envFile = "$projects\VORA_SIH\Vora\.env"
(Get-Content -LiteralPath $envFile) | ForEach-Object {
    if ($_ -like 'VORA_BROWSER_BINARY=*') { $_ -replace 'AWDAX_SIH/AwdaxP/', 'VORA_SIH/Vora/' } else { $_ }
} | Set-Content -LiteralPath $envFile
Write-Output "Done. Open D:\otherAndGames\SIDE PROJECTS\VORA_SIH in VS Code."
