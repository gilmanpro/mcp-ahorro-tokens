<#
.SYNOPSIS
  Registra la tarea 'MCP-AhorroTokens-RefrescoCache': refresco de la cache de
  schemas MCP al iniciar sesion de Windows, sin opencode y sin gastar tokens.

.DESCRIPTION
  Crea (idempotente; sobreescribe si ya existe) una tarea de Task Scheduler
  con trigger ONLOGON (al iniciar sesion el usuario actual; NO requiere admin)
  que ejecuta pythonw.exe (o python.exe si pythonw no existe; se detecta en el
  PATH) con la ruta absoluta de scripts\refrescar_cache.py y WorkingDirectory
  la raiz de la skill.

  La tarea NO toca opencode ni cambia el estado enabled/disabled de los
  servidores MCP: solo sondea cada servidor y actualiza .cache\<server>.json;
  el resumen queda en .tmp\refresco_cache.log (dentro del proyecto).

  -IntervaloMin N  anade ademas repeticion cada N minutos (por defecto NO:
  solo al iniciar sesion).
  -DryRun          imprime el comando schtasks /Create equivalente y el comando
  real de PowerShell SIN tocar Task Scheduler.

  Motor elegido: modulo ScheduledTasks (Register-ScheduledTask -Force), porque
  schtasks /Create no permite fijar WorkingDirectory. Al final se confirma con
  schtasks /Query (lo pide el plan de trabajo).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\registrar_tarea.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File scripts\registrar_tarea.ps1
  powershell -ExecutionPolicy Bypass -File scripts\registrar_tarea.ps1 -IntervaloMin 30
#>
[CmdletBinding()]
param(
    [string]$TaskName = "MCP-AhorroTokens-RefrescoCache",
    [int]$IntervaloMin = 0,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"

$SkillRoot = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $SkillRoot "scripts\refrescar_cache.py"
if (-not (Test-Path -LiteralPath $Script)) {
    Write-Error "No existe el script a ejecutar: $Script"
    exit 1
}

# Detectar interprete SIN caer en el alias 0-byte de la Microsoft Store
# (WindowsApps\pythonw.exe es un stub que abriria la tienda en la tarea).
# 1) python real -> pythonw.exe junto a su sys.executable (ventana oculta).
$RealPyw = $null
try {
    $RealPyw = & python -c "import sys,os; d=os.path.dirname(sys.executable); p=os.path.join(d,'pythonw.exe'); print(p if os.path.isfile(p) and os.path.getsize(p) > 0 else '')" 2>$null
} catch {
    $RealPyw = $null
}
if ($RealPyw) {
    $Exe = $RealPyw.Trim()
} else {
    # 2) pythonw.exe del PATH que NO sea alias 0-byte; si no, python.exe real.
    $Pyw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($Pyw -and (Get-Item -LiteralPath $Pyw.Source).Length -gt 0) {
        $Exe = $Pyw.Source
    } else {
        $Py = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $Py -or (Get-Item -LiteralPath $Py.Source).Length -eq 0) {
            Write-Error "No se detecto un python/pythonw real (los alias 0-byte de WindowsApps no valen)."
            exit 1
        }
        $Exe = $Py.Source
    }
}

# Comando equivalente en schtasks (informativo: schtasks no fija WorkingDirectory).
# Sintaxis cmd real: el valor de /TR va entrecomillado y las comillas internas
# se escapan con barra invertida.
$Tr = '"{0}" "{1}"' -f $Exe, $Script
$TrEsc = $Tr.Replace('"', '\"')
$schtasksCmd = "schtasks /Create /TN `"{0}`" /SC ONLOGON /TR `"{1}`" /F" -f $TaskName, $TrEsc
$psCmd = "Register-ScheduledTask -TaskName `"{0}`" -Trigger (New-ScheduledTaskTrigger -AtLogOn -User `"{1}\{2}`") " -f $TaskName, $env:USERDOMAIN, $env:USERNAME
$psCmd += "-Action (New-ScheduledTaskAction -Execute `"{0}`" -Argument '`"{1}`"' -WorkingDirectory `"{2}`") " -f $Exe, $Script, $SkillRoot
$psCmd += "-Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew) -Force"

Write-Host "Skill root : $SkillRoot"
Write-Host "Script     : $Script"
Write-Host "Ejecutable : $Exe"
Write-Host "Tarea      : $TaskName (trigger ONLOGON, usuario actual, sin admin)"
if ($IntervaloMin -gt 0) {
    Write-Host "Repeticion : cada $IntervaloMin min (ademas del logon)"
} else {
    Write-Host "Repeticion : no (-IntervaloMin N para anadirla)"
}
Write-Host ""
Write-Host "Comando schtasks /Create equivalente:"
Write-Host "  $schtasksCmd"
Write-Host "Comando real (modulo ScheduledTasks):"
Write-Host "  $psCmd"

if ($DryRun) {
    Write-Host ""
    Write-Host "[DryRun] No se toco Task Scheduler."
    exit 0
}

# Trigger de logon DEL USUARIO ACTUAL: -User es obligatorio para registrar sin
# admin (un LogonTrigger "de cualquier usuario" exige elevacion y falla con
# "Acceso denegado" 0x80070005).
$triggers = @(New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME")
if ($IntervaloMin -gt 0) {
    $triggers += New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervaloMin) `
        -RepetitionDuration (New-TimeSpan -Days 36500)
}
$action = New-ScheduledTaskAction -Execute $Exe -Argument ('"{0}"' -f $Script) -WorkingDirectory $SkillRoot
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Trigger $triggers -Action $action `
    -Settings $settings -Force | Out-Null

Write-Host ""
Write-Host "Confirmacion (schtasks /Query /TN $TaskName /FO LIST):"
schtasks /Query /TN $TaskName /FO LIST
if ($LASTEXITCODE -ne 0) {
    Write-Error "schtasks /Query no encontro la tarea; revisa el Programador de tareas."
    exit 1
}
Write-Host ""
Write-Host "OK: la cache MCP se refrescara al iniciar sesion (sin opencode, cero tokens)."
exit 0
