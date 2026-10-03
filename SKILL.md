---
name: mcp-ahorro-tokens
description: "Activa y desactiva servidores MCP de OpenCode bajo demanda para ahorrar tokens: un MCP conectado inyecta sus tools en cada mensaje aunque no se usen. Al cargar la skill, off --all --bg los apaga todos (--except protege los permanentes); enciende solo lo pedido y apaga al terminar avisando. Inspeccionar sin activar: probe_mcp.py (--from-cache, sin red); accion puntual: call_mcp.py; lote/auditoria en una sola sesion: audit_mcp.py (--all recorre el inventario; --out vuelca la salida completa de cada llamada; --show-tools [--tool X] lee catalogo e inputSchema desde la cache). Auto-refresco de cache al iniciar opencode via plugin, cero tokens. Usar SIEMPRE que el usuario pida activar, apagar, conectar o usar un MCP, apagarlos todos, preguntar que tools tiene o cuanto cuesta, auditar tools o hacer un lote, leer schemas de la cache, autoconfigurar los MCP al arrancar opencode o ahorrar tokens, aunque no mencione la skill. NO usar para crear un servidor MCP (usa mcp-builder)."
license: MIT
compatibility: opencode
metadata:
  audience: all
  workflow: token-optimization
---

# MCP a demanda — ahorro de tokens

Cada servidor MCP **conectado** añade los nombres, descripciones y schemas completos de sus tools al prompt del agente **en cada mensaje**, aunque la conversacion nunca las use: un servidor de 106 tools (resend) inyectaria decenas de miles de tokens por turno. Principio: encender solo lo que la tarea necesita, apagar cuando termina y, siempre que se pueda, usar el servidor **sin encenderlo**.

Todos los comandos suponen cwd la carpeta de esta skill (`~\.agents\skills\mcp-ahorro-tokens`); desde otro cwd, antepon la ruta completa a `scripts\...`.

**Regla de temporales**: todo residuo que generan estos scripts (planes, resultados de lote con `--out`, logs del `off --all --bg` y del refresco de cache) vive **dentro de `.tmp\` de la skill** — nunca en `%TEMP%` ni fuera del proyecto. Ojo: `.tmp\` **NO esta en el `.gitignore`** (el guard local de la skill solo cubre `.cache/` y `__pycache__/`), asi que sus residuos quedan **untracked** en el repo de skills: JAMAS anadirlos con `git add -A` ni `git add .tmp` — en este repo el commit siempre se hace con rutas explicitas.

## Accion al iniciar la skill

Lo primero al cargar esta skill, antes de cualquier otra decision, es apagar todos los MCPs para que la sesion arranque a coste cero — **en segundo plano, sin bloquear el flujo**:

```bat
python scripts\toggle_mcp.py off --all --bg
```

Retorna de inmediato (exit 0): el script se relanza detached, se auto-verifica releyendo la config desde disco con un reintento de escritura, y deja el resultado en `.tmp\mcp-off-all.log` de la skill (resumen por servidor y linea final VERIFICADO/FALLO; `--log <ruta>` admite otra ruta explicita). Es idempotente (los ya apagados se reportan pero no se tocan). Informa al usuario en una linea ("Se han apagado los MCPs al iniciar para ahorrar tokens; dime si quieres dejar alguno permanente"). Si ya indico MCPs permanentes: `off --all --bg --except nombre1,nombre2`.

Si la tarea posterior necesita certeza inmediata (no confiar en el bg): `python scripts\toggle_mcp.py verify` — lectura instantanea del JSON, una linea por servidor y exit 0 = todos apagados (1 = alguno ENCENDIDO; `--bg-log` adjunta la ultima linea del log como evidencia). `off --all` a secas también vale (verifica integrado; `--no-verify` lo omite).

## Arbol de decision — una herramienta por necesidad

La skill es agnostica del inventario: no asuma que existe ningun MCP. Se descubre en tiempo de ejecucion con `opencode mcp list` (estados `connected` / `disabled` / `failed`), con `python scripts\toggle_mcp.py status` o leyendo la seccion `mcp` de `~/.config/opencode/opencode.json` y del `opencode.json` del proyecto.

| Necesidad | Valor por defecto | Salida si no encaja |
|---|---|---|
| Pregunta informativa ("que tools tiene?", "cuanto costaria activarlo?", "existe la tool Y?") | `python scripts\probe_mcp.py <nombre>` — no toca la config. Si ya lo sondeaste hace poco: `--from-cache` (0 s, 0 red). Si hace falta el **inputSchema** de una tool concreto: `python scripts\audit_mcp.py <nombre> --show-tools --tool <X>` (desde cache, sin red, sin exigir frescura) | Si despues hay que usar las tools: fila "uso interactivo" |
| Accion puntual (enviar un correo, crear un registro: una o pocas llamadas, sin necesitar las tools en el prompt) | `python scripts\call_mcp.py <nombre> <tool> --args-file payload.json --timeout 90` | Si seran varias llamadas al mismo servidor: fila "auditoria o lote" |
| Auditoria, probar todas las tools, varias llamadas o lote al mismo MCP | `python scripts\audit_mcp.py <nombre>` (1 arranque, N operaciones). Con `--out .tmp\resultados` cada resultado se vuelca COMPLETO a un archivo y stdout queda compacto. Re-auditar todo el inventario: `--all --timeout 120` | Leer los schemas del ultimo snapshot sin tocar el servidor: `--from-cache` o `--show-tools` |
| Uso interactivo repetido pedido explicitamente ("usa resend", "conecta el MCP de WSL") | `python scripts\toggle_mcp.py on <nombre>` + avisar de que las tools llegan al reiniciar la sesion | Si no quiere reiniciar: haz la tarea con `call_mcp.py` (1 llamada) o `audit_mcp.py` (lote) sin activar |
| Ningun MCP del inventario encaja | Informa: no hay servidor configurado para X; puede anadirse con `opencode mcp add` | No enciendas uno "que podria servir" |

Si el usuario no pidio ningun MCP, no enciendas nada: la mayoria de tareas se resuelven con skills, CLIs y herramientas normales.

## Al terminar la tarea

- `python scripts\toggle_mcp.py off <nombre>` y refleja el cambio en la respuesta final: "Se desactivo el MCP X para ahorrar tokens; dime si quieres dejarlo permanente".
- Excepcion: un MCP que el usuario marco como permanente no se apaga sin pedir permiso; registra esa preferencia durante la sesion.
- Si la sesion va a encadenar varias tareas con el mismo MCP en los proximos minutos, apaga al cerrar el lote de trabajo, no entre tarea y tarea.
- Verifica el estado resultante con `opencode mcp list`.

## probe_mcp.py — inspeccionar sin activar

```bat
python scripts\probe_mcp.py <nombre> [--config <ruta>] [--timeout <seg>] [--from-cache] [--refresh]
```

- Lista nombre + descripcion de cada tool y el total, en remotos (Streamable HTTP: tolera SSE y servidores stateless sin `Mcp-Session-Id`) y locales (stdio; el primer `npx -y` descarga el paquete: sube `--timeout` a 90 o mas).
- Cada sondio en vivo escribe la cache de schemas `.cache\<nombre>.json` (serverInfo, capabilities, tools con inputSchema, timestamp). `--from-cache` responde desde el disco sin arrancar el servidor; si la cache falta o supera `--max-age` horas (defecto 24) avisa y sale con 2: refresca con `--refresh` o quita el flag.
- Resuelve las referencias de secretos `{file:./.secrets/...}` en runtime y nunca las imprime. Exit codes: 0 listo, 1 fallo de sondio, 2 error de config, servidor inexistente o cache invalida.

## call_mcp.py — accion puntual sin activar ni reiniciar

```bat
python scripts\call_mcp.py <nombre> --list --timeout 90
python scripts\call_mcp.py <nombre> <tool> --args-file payload.json --timeout 90
```

- Los argumentos SIEMPRE por archivo UTF-8 (`payload.json`): el quoting inline en cmd/PowerShell corrompe el JSON. Ejemplo real para `resend` / `send-email`:

```json
{"to": "cliente@ejemplo.com", "from": "no-reply@tudominio.com", "subject": "Aviso", "text": "Cuerpo del mensaje"}
```

- **Una llamada por ejecucion**: cada una repite el handshake y, en locales, relanza el proceso completo (~15-20 s con `npx`); el servidor no recuerda nada entre llamadas. Si necesitas varias, pasa a `audit_mcp.py`.
- Valida la tool contra `tools/list` antes de llamarla (si no existe, imprime la lista y sale con 2); JSON de argumentos roto → 2; fallo de red/proceso o `isError: true` de la tool → 1; exito → 0.
- Debes conocer el schema de la tool (`--list --from-cache` si ya la sondeaste) y el servidor debe estar **definido** en la config (aunque este `disabled`) para que el script resuelva su `command`/`url` y secretos.

## audit_mcp.py — auditoria y lote en una sola sesion

`probe_mcp.py` y `call_mcp.py` relanzan el servidor en cada ejecucion y reimprimen el catalogo; `audit_mcp.py` hace **1 arranque y N operaciones** con salida compacta por defecto (numero de tools y nombres; descripciones solo con `--verbose`). La auditoria que lo motivo: 5 servidores (2 remotos HTTP/SSE, 3 locales `npx`), 203 tools; el coste de arranque paso de ~15-20 s por operacion a uno solo por servidor.

```bat
:: Auditoria completa: handshake + serverInfo + capabilities + tools/resources/
:: prompts (lo no soportado se marca -32601 sin fallar) y escribe la cache
python scripts\audit_mcp.py resend --timeout 120

:: Varias llamadas en UN arranque: --call es repetible; cada --args-file se
:: asigna por orden al --call que no trae argumentos
python scripts\audit_mcp.py context7 --calls-only --call resolve-library-id --args-file p1.json --call resolve-library-id --args-file p2.json

:: Lote desde un plan JSON con SALIDA COMPLETA en archivos (FLUJO RAPIDO RECOMENDADO):
:: stdout solo estado OK/ERROR + preview de 140 chars + ruta; el detalle integro
:: queda en .tmp\resultados\<NN>_<tool>.json — YA NO hace falta re-llamar tool
:: por tool con call_mcp.py para leer el resultado legible
python scripts\audit_mcp.py wsl-port --calls-only --batch .tmp\plan_status.json --out .tmp\resultados --timeout 120

:: (alternativa sin archivos: --max-output 0 vuelca el resultado COMPLETO en
:: stdout, sin truncar lineas ni ancho, sin necesidad de --verbose)
python scripts\audit_mcp.py wsl-port --calls-only --call status --max-output 0

:: Catalogo de tools o inputSchema de UNA tool desde la cache: sin red, sin
:: re-sondeo, funciona con el servidor apagado y no exige cache fresca
python scripts\audit_mcp.py wsl-port --show-tools
python scripts\audit_mcp.py wsl-port --show-tools --tool wsl_exec

:: Ultimo snapshot sin arrancar el servidor; --all audita TODO el inventario
python scripts\audit_mcp.py resend --from-cache
python scripts\audit_mcp.py --all --timeout 120
```

Plan `plan.json` (lista de llamadas; `args` inline o `args_file` relativo al plan):

```json
[
  {"tool": "resolve-library-id", "args": {"libraryName": "Next.js", "query": "routing"}},
  {"tool": "resolve-library-id", "args_file": "payload2.json"}
]
```

- Flags para saltarse secciones: `--calls-only`, `--no-tools`, `--no-resources`, `--no-prompts`; `--refresh` si `--from-cache` encuentra la cache vieja. `--from-cache` y `--all` no se combinan con `--call`/`--batch` (invocar exige hablar con el servidor; `--all` recorre el inventario el solo). `--show-tools` es de solo inspeccion: incompatible con `--call`/`--batch`/`--all`/`--from-cache`; no toca la config ni la red (si no hay cache, sale con 2 sugiriendo el sondeo o `refrescar_cache.py`).
- **Truncados y --out**: la preview de la tabla OK/ERROR siempre se corta a 140 chars (ANCHO_PREVIEW) para stdout compacto; `--max-output N` (con `--verbose`) corta el detalle a N lineas de 200 chars (ANCHO_LINEA); `--max-output 0` = detalle COMPLETO en stdout sin truncar. Para el resultado integro de un lote usa `--out .tmp\resultados`: escribe `<NN>_<tool>.json` (JSON formateado si el texto es JSON; raw si es texto; tambien se vuelca el texto de los `isError`) y la lectura humana se hace DESPUES sobre esos archivos — el lote sigue siendo 1 solo arranque.
- `--timeout` aplica POR SERVIDOR en `--all`: los locales `npx` tardan, usa 120. Un servidor que falla se anota FALLO y el lote continua.
- Exit codes: 0 todo OK; 1 algun fallo de llamada/sondeo (`isError: true`, tool inexistente, red/proceso — lo "no soportado" no es fallo); 2 error de config/uso. Con `--all`: 1 si algun servidor fallo, 2 si `--all` lleva `--call`/`--batch` o nombre de servidor. Con `--show-tools`: 2 si no hay cache, la tool pedida no existe o se combina con llamadas.

## toggle_mcp.py — activar, desactivar y whitelist

```bat
python scripts\toggle_mcp.py status
python scripts\toggle_mcp.py on <nombre>
python scripts\toggle_mcp.py off <nombre>
python scripts\toggle_mcp.py off --all [--except n1,n2] [--bg] [--no-verify]
python scripts\toggle_mcp.py verify [--except n1,n2] [--bg-log]
```

`off --all` verifica por defecto (relee el JSON escrito, reintenta una vez, imprime VERIFICADO o sale con 1); `--bg` hace eso mismo detached escribiendo en `.tmp\mcp-off-all.log` de la skill (regla de temporales: dentro del proyecto, nunca %TEMP%; `--log <ruta>` para otra ruta explicita) y retorna al instante; `verify` confirma el estado leyendo solo (exit 0/1/2) y `--bg-log` muestra la ultima linea de ese mismo log (defecto `.tmp\mcp-off-all.log`; admite ruta explicita). Por defecto opera sobre `~/.config/opencode/opencode.json` (`--config` para otro archivo) y luego verifica con `opencode mcp list`. El binario acepta dos formatos de config: anidado (`mcp.servers.<nombre>` + `disabled`) y plano oficial (`mcp.<nombre>` + `enabled`). Lee `references/sintaxis-opencode-mcp.md` ANTES de editar la config a mano.

## Auto-refresco al iniciar opencode (plugin en segundo plano, cero tokens)

La cache de schemas (`.cache\<server>.json`) es lo que permite `--from-cache`, `--show-tools` y `probe_mcp.py --from-cache` **sin red**. Si esta vieja (>24 h con `--from-cache`), cada primer sondeo paga arranque de servidor. `refrescar_cache.py` la mantiene fresca **por fuera de opencode**: no abre ni reinicia sesiones, no escribe la config, no cambia `enabled`/`disabled` de nadie y no inyecta tools en el prompt — coste de tokens CERO (usa el mismo handshake JSON-RPC de `audit_mcp.py` contra cada servidor definido; los locales `npx` se arrancan y se matan solos; `--timeout` por servidor, defecto 60; un servidor caido se anota FALLO y el lote continua; NUNCA imprime secretos — las URLs con credenciales se enmascaran). El resumen se appendea a `.tmp\refresco_cache.log` (unico rastro cuando corre sin consola).

**Via recomendada: el plugin global `mcp-refresco-cache.js`** (en `~\.config\opencode\plugins\`; en Windows `%USERPROFILE%\.config\opencode\plugins\`). Opencode descubre y carga automaticamente los archivos de esa carpeta al iniciar el server (doc oficial https://opencode.ai/docs/plugins: "Files in these directories are automatically loaded at startup"), y el init del plugin lanza en SEGUNDO PLANO el refresco con `pythonw` real (`detached` + `windowsHide` + sin `await`: el arranque de opencode jamas se bloquea; tarda ~9-11 s por su cuenta con los 5 servidores). El plugin NO registra ningun hook => cero inyeccion en prompts/sesiones => **cero tokens**, y no toca la seccion `mcp` de la config (no enciende ni apaga nada).

- **Throttle y anti-doble-lanzamiento**: marcador `.tmp\ultimo_refresco.json` (epoch + resumen + `estado`). Si el ultimo refresco EXITOSO tiene menos de **6 horas** (constante `THROTTLE_HORAS` al inicio del plugin; env `MCP_REFRESH_THROTTLE_HOURS`), no relanza; el estado `"en-progreso"` con TTL de 30 min evita dos lanzamientos concurrentes. Se eligio el marcador (y no la edad de `.cache\*.json`) porque la edad de la cache no distingue refresco OK de FALLO ni permite lock.
- **pythonw REAL (hallazgo critico de Windows)**: el `pythonw.exe` del PATH que resuelve primero es el ALIAS de la Microsoft Store (`...\WindowsApps\pythonw.exe`), un stub que abriria la Tienda en lugar de ejecutar. El plugin —igual que `registrar_tarea.ps1`— resuelve `pythonw.exe` **junto al `sys.executable` del python real** y descarta rutas `WindowsApps` o de 0 bytes; ultimo fallback: `python.exe` con `windowsHide`/`detached`.
- **Resiliencia**: skill o script inexistentes, python no resuelto o excepcion inesperada => escribe 1 linea a `.tmp\plugin_refresco.log` y NUNCA rompe el arranque de opencode (try/catch total).
- **Como verificar que funciono**: `.tmp\plugin_refresco.log` (lineas `LANZADO` / `OMITIDO: throttle` / `refresco termino`), marcador `.tmp\ultimo_refresco.json`, `python scripts\refrescar_cache.py --verificar` (lectura local: edad y n° de tools por servidor) y el crecimiento de `.tmp\refresco_cache.log`. Ojo: los plugins se cargan AL INICIAR el server — un opencode que ya estaba abierto no ejecuta un plugin recien creado hasta el siguiente arranque.
- **Como desactivarlo** (lo mas simple primero): mover o borrar `mcp-refresco-cache.js` de `~/.config\opencode\plugins\`; o cambiar la constante `DESACTIVADO` del inicio del plugin a `true`; o lanzar opencode con la env `MCP_REFRESH_DISABLED=1`.

```bat
:: Refresco manual de TODOS los servidores definidos (~9-11 s con 5 servidores)
python scripts\refrescar_cache.py

:: Solo algunos, o ver el estado de la cache local sin tocar la red
python scripts\refrescar_cache.py --solo wsl-port context7
python scripts\refrescar_cache.py --verificar

:: Ejercitar la logica del plugin sin reiniciar opencode (llama la funcion exportada;
:: ajusta la ruta del file:/// al home de tu maquina)
node -e "import('file:///<home>/.config/opencode/plugins/mcp-refresco-cache.js').then(m=>m.lanzarRefresco().then(r=>console.log(JSON.stringify(r))))"
```

### Alternativa OPCIONAL sin opencode: tarea programada de Windows

`registrar_tarea.ps1` / `quitar_tarea.ps1` crean/quitan la tarea `MCP-AhorroTokens-RefrescoCache` en Task Scheduler (trigger ONLOGON, usuario actual, sin admin) que ejecuta `pythonw` con el mismo script. Usala SOLO si la maquina no tiene opencode; **la via recomendada es el plugin de arriba** (cero componentes del SO, mismo efecto). Detalles verificados en Windows 11: usa el modulo ScheduledTasks (`Register-ScheduledTask -Force`, idempotente) porque `schtasks /Create` no fija `WorkingDirectory` y porque el trigger de logon debe ligarse al usuario (`-AtLogOn -User <dominio>\<usuario>`): un LogonTrigger "de cualquier usuario" exige admin y falla con Acceso denegado 0x80070005. El interprete se detecta igual que el plugin (pythonw real junto a `sys.executable`, nunca el alias de la Store). La confirmacion final es `schtasks /Query`; `quitar_tarea.ps1` es idempotente (si no existe, sale con 0 sin tocar nada).

```bat
powershell -ExecutionPolicy Bypass -File scripts\registrar_tarea.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\registrar_tarea.ps1
:: opcional: -IntervaloMin N anade repeticion cada N minutos (por defecto solo al iniciar sesion)

:: Confirmar y quitar
schtasks /Query /TN MCP-AhorroTokens-RefrescoCache /FO LIST
powershell -ExecutionPolicy Bypass -File scripts\quitar_tarea.ps1
```

Alternativa intermedia para un servidor con muchas tools que quieres conectado sin pagarlas en el prompt: oculta todas con el patron `"<servidor>_*": false` en `"tools"` de opencode.json y whitelistea solo las 2-3 que usas (ejemplo JSON en la referencia). Orden de preferencia: apagar (ni se conecta) → accion puntual `call_mcp.py` → uso repetido de 2-3 tools (whitelist) → uso intensivo de muchas tools (activar el servidor entero).

## Anti-patrones

- Empezar una tarea con MCPs encendidos que nadie pidio: cada mensaje posterior paga sus tools; `off --all --bg` al cargar la skill garantiza coste cero sin bloquear el flujo (el script se auto-verifica) y `--except` protege los permanentes.
- Encadenar varios `call_mcp.py` contra un MCP local `npx`: cada ejecucion relanza el servidor y paga ~15-20 s por llamada; `audit_mcp.py --batch plan.json` (o `--call` repetido) hace 1 solo arranque con N llamadas.
- Re-llamar tool por tool con `call_mcp.py` para "ver el resultado completo" de un lote truncado: anula la promesa de 1 arranque. Desde el lote usa `audit_mcp.py ... --out .tmp\resultados` (salida integra por llamada en archivos) o `--max-output 0` (detalle completo en stdout).
- Programar un `python -c` artesanal para leer `.cache\<server>.json`: `audit_mcp.py <servidor> --show-tools [--tool <X>]` ya imprime el catalogo y el inputSchema desde la cache, sin red y con el servidor apagado.
- Dejar logs o resultados fuera del proyecto (%TEMP%, Downloads...): toda residuo de estos scripts vive en `.tmp\` de la skill (`mcp-off-all.log`, `refresco_cache.log`, `resultados\`).
- Fiarte de `pythonw.exe` del PATH para el auto-refresco (plugin o tarea programada): el de `WindowsApps` es el ALIAS de la Microsoft Store (un stub que abriria la Tienda en vez de ejecutar, a veces de 0 bytes); tanto `mcp-refresco-cache.js` como `registrar_tarea.ps1` resuelven el pythonw REAL junto al `sys.executable` y descartan rutas `WindowsApps` — si lanzas el refresco a mano, usa esa ruta absoluta.
- Esperar que el plugin refresque en la sesion de opencode que YA esta abierta: `~/.config\opencode\plugins\` se descubre al iniciar el server; un plugin recien creado o editado no actua hasta el proximo arranque — para probarlo sin reiniciar, llama su `lanzarRefresco()` con node o ejecuta `refrescar_cache.py`.
- Volver a sondear en vivo lo que ya esta en la cache: relanza 15-20 s (local) o red inutilmente para los mismos schemas; usa `--from-cache` y `--refresh` solo si necesitas datos frescos.
- Imprimir catalogos de tools sin necesidad: `--list` de un servidor de 106 tools vuelca miles de tokens en el contexto; usa la salida compacta de `audit_mcp.py` o `--from-cache`, y `--verbose` solo cuando haga falta el detalle.
- Sondear a mano con curl o PowerShell: PS 5.1 corrompe las comillas del JSON ("JSON invalido") y `Invoke-WebRequest` falla por TLS aunque curl funcione; `probe_mcp.py` encapsula ambos flujos.
- Activar un MCP para responder "que tools tiene": era una pregunta informativa y pagas tools en cada mensaje mas reinicio de sesion; responde con `probe_mcp.py` sin tocar la config.
- Activar un MCP para una sola llamada puntual: pagas las tools todo el resto de la sesion y un reinicio sin necesidad; usa `call_mcp.py <nombre> <tool> --args-file ...`, cero tokens de prompt y sin reiniciar.
- Confiar ciegamente en el schema publicado: puede omitir parametros requeridos (leccion real: `send-email` no listaba `from` y el servidor lo exijo). Ante un error de validacion, sondea tools de solo lectura relacionadas (`list-domains`) para descubrir el valor valido y reintenta: el intento fallido por validacion no llega a la API, no hay duplicados.
- Apagar un MCP en medio de una tarea (entre tool calls): la sesion puede depender de la conexion; apaga solo al entregar el resultado.
- Apagar sin avisar o apagar un MCP permanente: el usuario puede esperar que siga disponible; reporta el cambio de estado en cada respuesta final y pregunta la preferencia una vez, y respetala.
- Reformatear la config entera o mezclar flags: cambia el formato del archivo y puede romper secretos; usa solo el flag del bloque (`disabled` en anidado, `enabled` en plano, nunca ambos) o mejor `toggle_mcp.py`, que preserva estructura y secretos.
- Copiar el valor de un secreto a la config, a un comando o a la conversacion: queda impreso en logs y prompts de forma permanente. Los tokens van en `{file:./.secrets/...}`, resueltos en runtime y **nunca impresos** — esto si es critico.
- Descartar un servidor "porque no existe" al no aparecer en `opencode mcp list`: list solo muestra los definidos en la config; la config de proyecto puede anadir otros.

## Referencias — cuando cargar cada una

- El servidor responde algo inesperado a nivel de protocolo, o quieres entender el JSON-RPC (handshake remoto, stdio local, SSE, stateless, `tools/call`, resources/prompts, patron de sesion unica, porque no sondear a mano): lee `references/probe-jsonrpc.md`. No la leas para ejecutar un comando que ya funciono.
- Vas a editar `opencode.json` a mano, o dudas de la sintaxis (anidado vs plano), el CLI `opencode mcp` o el whitelist de tools: lee `references/sintaxis-opencode-mcp.md`. No la leas para tareas que solo usan `opencode mcp list` o `toggle_mcp.py`.
- Un sintoma sin respuesta aqui (tools que no aparecen a mitad de sesion, `JSON invalido`, timeout de `npx`, "campo requerido undefined", servidor ausente de list): lee `references/troubleshooting.md`, tabla sintoma → causa → solucion con comando.
