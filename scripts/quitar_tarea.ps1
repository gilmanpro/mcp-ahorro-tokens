<#
.SYNOPSIS
  Quita la tarea 'MCP-AhorroTokens-RefrescoCache' creada por registrar_tarea.ps1.

.DESCRIPTION
  Elimina la tarea de refresco de cache MCP del Task Scheduler de forma
  idempotente: si la tarea no existe, sale con 0 sin tocar nada. No borra
  logs ni cache: solo desactiva el auto-refresco al iniciar sesion.
  -DryRun imprime lo que ejecutaria sin tocar Task Scheduler.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\quitar_tarea.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName = "MCP-AhorroTokens-RefrescoCache",
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"

$existe = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existe) {
    Write-Host "La tarea '$TaskName' no existe: nada que quitar."
    exit 0
}

Write-Host "Equivalente schtasks: schtasks /Delete /TN `"$TaskName`" /F"
Write-Host "Real (modulo ScheduledTasks): Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"

if ($DryRun) {
    Write-Host "[DryRun] No se toco Task Scheduler."
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Error "La tarea '$TaskName' sigue registrada."
    exit 1
}

Write-Host ""
Write-Host "Confirmacion (schtasks /Query debe reportar que no existe):"
# Con EAP=Stop, el stderr esperado de schtasks ("no puede encontrar el archivo")
# lanzaria NativeCommandError y cortaria el script: bajarlo a Continue para
# leer el exit code real como confirmacion.
$ErrorActionPreference = "Continue"
schtasks /Query /TN $TaskName /FO LIST 2>&1 | Out-Host
$queryExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($queryExit -ne 0) {
    Write-Host "OK: la tarea '$TaskName' fue eliminada (schtasks ya no la encuentra)."
    exit 0
}
Write-Error "schtasks /Query todavia ve la tarea; eliminacion incompleta."
exit 1
