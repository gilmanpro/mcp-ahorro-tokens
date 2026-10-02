# Sondear e invocar un MCP por JSON-RPC sin activarlo — protocolo y troubleshooting Windows

Detalle técnico detrás de `scripts/probe_mcp.py` (tools/list) y
`scripts/call_mcp.py` (tools/call). Léelo cuando alguno de los dos falle o
cuando quieras entender por qué NO debes sondear a mano con curl/PowerShell.

## Índice

- [Por qué sondear en vez de activar](#por-qué-sondear-en-vez-de-activar)
- [El problema de sondear a mano en Windows](#el-problema-de-sondear-a-mano-en-windows)
- [Flujo remoto (Streamable HTTP) verificado](#flujo-remoto-streamable-http-verificado)
- [Invocar una tool sin activar el MCP (tools/call) — call_mcp.py](#invocar-una-tool-sin-activar-el-mcp-toolscall--call_mcppy)
- [Flujo local (stdio) verificado](#flujo-local-stdio-verificado)
- [Secretos](#secretos)
- [Exit codes](#exit-codes-probe_mcppy-call_mcppy-y-audit_mcppy)
- [Apéndice: resources/list, prompts/list y el patrón de sesión única](#apéndice-resourceslist-promptslint-y-el-patron-de-sesion-unica)

## Por qué sondear en vez de activar

Las tools de un MCP se **inyectan en el system prompt al arrancar la sesión**.
Activar un MCP a mitad de sesión con `toggle_mcp.py on` hace que
`opencode mcp list` lo muestre `connected`, pero el agente **no puede llamar
sus tools hasta reiniciar la sesión** (o abrir una nueva). Por eso, para
responder preguntas informativas ("¿qué tools tiene el MCP X?", "¿cuántas tools
me costaría activarlo?") NO hay que activar nada: se pregunta directamente al
servidor por JSON-RPC con `python scripts/probe_mcp.py <nombre>`, sin tocar la
config y sin gastar el coste de contexto de la activación.

## El problema de sondear a mano en Windows (lo que motivó el script)

En una sesión real se gastaron ~10 tool calls intentando `tools/list` manual:

- **PowerShell 5.1 corrompe las comillas del JSON** al pasarlo como argumento a
  `curl.exe`: el servidor responde `"JSON invalido"`. Los backticks, comillas
  anidadas y el pipe de PS destruyen el payload antes de salir del shell.
- **`Invoke-WebRequest` falla por TLS**: el .NET Framework viejo de PS 5.1 no
  negocia el TLS del endpoint aunque `curl.exe` funcione en la misma máquina.
  No concluyas que el servidor está caído: es el cliente.
- **cmd con `/v:on`**: la expansión diferida de variables dentro de un `for` o
  un bloque requiere `!TOK!`, no `%TOK%`; con `%TOK%` el JSON llega vacío o
  sin sustituir.
- **Servidores stateless**: algunos no devuelven `Mcp-Session-Id` en
  `initialize` y sencillamente ignoran la notificación `initialized`. No es un
  error: reintenta/continúa sin cabecera de sesión.

`probe_mcp.py` encapsula todo esto (urllib + json + subprocess, solo stdlib).

## Flujo remoto (Streamable HTTP) verificado

POST a la `url` de la config con `Content-Type: application/json` y
`Accept: application/json, text/event-stream`:

1. `initialize` (id 1, con `protocolVersion`, `capabilities: {}`, `clientInfo`).
   - Capturar el header de respuesta **`Mcp-Session-Id`** si viene (el script
     imprime solo sus 8 primeros caracteres; el id de sesión tampoco es secreto,
     pero no hace falta).
   - HTTP 401 = secreto/Authorization inválido; 404 en la raíz puede indicar
     que falta un sufijo `/mcp` en la url.
2. `notifications/initialized` (sin `id`) con la cabecera de sesión si existe.
   Suele responder 200/202 con cuerpo vacío; que no responda cuerpo es normal.
3. `tools/list` (id 2) con la cabecera de sesión. **Si da 400/404 y había
   sesión capturada, reintenta SIN la cabecera** (servidor stateless).
4. Respuesta puede venir como **JSON puro o SSE**: líneas `data: {...}`; el
   objeto útil es el que trae `result` o `error`.

## Invocar una tool sin activar el MCP (`tools/call`) — `call_mcp.py`

Para una **acción puntual** (enviar un correo, crear un registro) no hace falta
activar el servidor ni reiniciar la sesión: tras el mismo handshake de arriba se
envía `tools/call` y se imprime su resultado. `call_mcp.py` importa los helpers
de `probe_mcp.py` (resolución de secretos, parsing SSE/JSON, proceso stdio), así
que ambos flujos comparten tolerancias (SSE, stateless, `npx -y` lento).

Request (id 3, después de `tools/list` con id 2 para validar que la tool existe):

```json
{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
 "params": {"name": "send-email", "arguments": {"to": "...", "subject": "...", "from": "..."}}}
```

Respuesta útil en `result`:

- `content[]`: array de bloques. Los de `type: "text"` traen el payload en
  `text`, que **puede ser JSON serializado** (el script lo imprime tal cual;
  si necesitas parsearlo, vuelca stdout a un archivo UTF-8). También existen
  `image`/`audio`/`resource`, que el script resume sin volcar base64.
- `structuredContent` (opcional): el mismo resultado ya como objeto JSON; el
  script lo imprime formateado si viene.
- `isError: true`: **la tool respondió, pero falló su ejecución** (p. ej.
  validación de argumentos). El JSON-RPC llega como `result`, no como `error`;
  `call_mcp.py` imprime el contenido del error y sale con 1. Un `error`
  JSON-RPC (method not found, etc.) es otro caso y sale igualmente con 1.

Errores reales que hay que conocer (lección de la sesión con `resend`):

- **Schema incompleto:** el `inputSchema` publicado de `send-email` omitía el
  parámetro requerido `from`; el servidor respondió `isError` con
  `from: expected string, received undefined`. El **primer intento fallido por
  validación no llegó a la API** (no envió nada), así que reintentar es seguro
  y no duplica. Técnica de descubrimiento: llamar una tool de **solo lectura**
  relacionada (`list-domains`) para obtener el valor válido real y reintentar
  con él.
- Validación previa: `call_mcp.py` comprueba el nombre contra `tools/list`
  ANTES del `tools/call`; si no existe, imprime las tools disponibles y sale
  con 2 (evita esperar el error del servidor, que en resend-mcp además llega
  como `error` JSON-RPC "Tool ... not found").
- **Argumentos:** en Windows el quoting JSON inline (`--args`) es frágil; usa
  `--args-file payload.json` (el script lee UTF-8, tolera BOM). No registres
  ni imprimas los payloads con secretos: el script solo muestra el resultado
  de la tool.
- Sin `--list` ni tool: `--list` deja el script usable como sustituto cómodo
  de `probe_mcp.py` (handshake + `tools/list`, exit 0).

## Flujo local (stdio) verificado

El proceso se lanza con `command` + `args` de la config (resolviendo el
ejecutable con `shutil.which` — en Windows `npx` es `npx.cmd` y
`CreateProcess` no lo encuentra sin eso) y `environment` fusionado con el del
sistema. Los mensajes JSON-RPC van **newline-delimited** por stdin/stdout
(`Content-Length` no aplica en stdio MCP moderno):

1. `initialize` (id 1) → esperar línea con `"id":1`.
2. `notifications/initialized` (sin id) → no se espera respuesta.
3. `tools/list` (id 2) → esperar línea con `"id":2`.
4. **Matar el proceso SIEMPRE** (terminate → kill), incluso si algo falla.

Notas:

- `npx -y <paquete>` en la **primera ejecución descarga el paquete**: puede
  superar el timeout de 60 s; sube con `--timeout 180` si es el primer sondeo.
- Si el proceso muere pronto (falta una API key, paquete inexistente), el
  script incluye el final de stderr en el error. Si la API key está definida
  como `{file:./.secrets/...}` y el archivo no existe, el script lo dice por
  ruta, NUNCA por contenido.
- Verificado en real: `resend` (`npx -y resend-mcp`) responde `tools/list` con
  106 tools en ~3 s cuando el paquete ya está cacheado.

## Secretos

Las referencias `{file:./ruta}` se resuelven **en runtime**, relativas al
directorio de la config (`~/.config/opencode` para la global), y el valor
resuelto solo viaja en cabeceras HTTP o variables de entorno del proceso hijo.
**Nunca lo imprimas** (ni en stdout, ni en stderr, ni en el reporte).

## Exit codes (probe_mcp.py, call_mcp.py y audit_mcp.py)

| Code | Significado |
|---|---|
| 0 | tools listadas correctamente (probe; call con `--list`) o tool invocada con exito (`isError` false en call); en audit, auditoria y lote sin fallos |
| 1 | fallo de sondio/invocacion (red, HTTP, proceso, error JSON-RPC, timeout, o la tool respondio `isError: true`; en audit, tambien una tool inexistente en el lote) |
| 2 | error de uso/config (config inexistente, servidor inexistente, JSON de argumentos roto, tool no encontrada en `tools/list`, `--from-cache` sin cache valida y sin `--refresh`) |

## Apéndice: resources/list, prompts/list y el patron de sesion unica

Leccion de una auditoria real de 5 servidores (wsl-port y context7 remotos
HTTP/SSE; chrome-devtools, playwright y resend locales stdio via `npx`;
203 tools en total).

### Las otras dos listas del protocolo

`tools/list` no es el unico inventario. Tras el mismo handshake
(`initialize` → `notifications/initialized`) el resultado de `initialize`
trae `capabilities` (`{"tools": {...}, "resources": {...}, "prompts": {...}}`)
y `serverInfo` (`{"name": ..., "version": ...}`), que antes probe/call
ignoraban y obligaban a hacer JSON-RPC manual. Las peticiones:

```json
{"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}}
{"jsonrpc": "2.0", "id": 4, "method": "prompts/list",   "params": {}}
```

- `resources/list` → `result.resources[]` con `uri`, `name`, `description`,
  `mimeType` opcionales. `resources/templates/list` existe ademas si la
  capability `resources` declara `"subscribe"`/listas de plantillas.
- `prompts/list` → `result.prompts[]` con `name`, `description`, `arguments[]`.
- **Muchos servidores no los soportan**: responden error JSON-RPC **-32601**
  (Method not found) o directamente no declaran la capability. Eso NO es un
  fallo de la auditoria: `McpSession.list_resources()/list_prompts()` devuelven
  `None` y `audit_mcp.py` pinta `no soportado (-32601)` sin afectar al exit code.
  En la auditoria real: context7 declaraba capabilities con listas vacías
  (0 resources, 0 prompts) y resend no declaraba ninguna (solo `tools`).
- Los demas metodos de esas familias (`resources/read`, `prompts/get`) siguen
  el mismo patron de peticion; no hace falta sondearlos a mano con curl:
  `audit_mcp.py --call ...` usa `tools/call`, y para leer un recurso usa el
  JSON-RPC manual SOLO si de verdad lo necesitas (mismas reglas de quoting y
  SSE de arriba).

### Patron de sesion unica (por que existe audit_mcp.py)

El coste real de `probe_mcp.py`/`call_mcp.py` por ejecucion:

| Transporte | Coste por arranque | Efecto en 20 llamadas |
|---|---|---|
| local stdio via `npx` | ~15-20 s (Node + carga del paquete; primera vez, descarga) | 5-7 min solo en arranques |
| remoto Streamable HTTP | 1 handshake (initialize) por sesion | N veces el round-trip del handshake |

Un servidor MCP es un **proceso/sesion con estado**: tras el handshake admite
N peticiones con ids incrementales hasta que se cierra el pipe (stdio) o la
sesion HTTP (`Mcp-Session-Id`). `McpSession` en `scripts/mcp_common.py`
encapsula eso:

```python
with McpSession(server, config_dir, timeout=120) as s:
    s.start()                      # initialize + notifications/initialized
    tools = s.list_tools()         # ids 2,3,4... automaticos
    for plan in llamadas:          # 1 arranque, N tools/call en orden
        s.call_tool(plan["tool"], plan["args"])
                                   # close() MATA el proceso hijo SIEMPRE
```

`audit_mcp.py` expone el patron por CLI: auditoria completa, `--call`
repetido y `--batch plan.json` comparten **una sola** sesion. Regla practica:
**más de una operacion contra el mismo servidor → una sola ejecucion de
`audit_mcp.py`**, nunca un bucle de `call_mcp.py`.

Ademas, cada `tools/list` en vivo alimenta la cache `.cache\<nombre>.json`
(serverInfo, capabilities, tools con `inputSchema`, resources, prompts,
timestamp): con `--from-cache` el inventario se lee del disco en 0 s sin
tocar el servidor — la auditoria de 106 tools de resend ya no vuelve a costar
ni arranque ni red.
