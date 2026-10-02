---
name: mcp-ahorro-tokens
description: "Activar y desactivar MCPs de OpenCode bajo demanda para ahorrar tokens: un MCP conectado inyecta sus tools en cada mensaje y solo funciona al arrancar la sesion. Enciende solo lo pedido; descubre con opencode mcp list. Para INSPECCIONAR sin activar: probe_mcp.py. Para ACCION PUNTUAL sin activar ni reiniciar: tools/call de call_mcp.py, coste cero de prompt. Para AUDITAR un MCP, PROBAR TODAS SUS TOOLS o hacer VARIAS LLAMADAS/LOTE al mismo servidor: audit_mcp.py (1 arranque, N operaciones; el npx local tarda 15-20 s por relanzamiento). Schemas ya sondeados: --from-cache, 0 s 0 red. Usar SIEMPRE que el usuario pida activar, apagar, usar o auditar un MCP, o pregunte que tools tiene, y AL TERMINAR una tarea que uso un MCP para desactivarlo y avisar. Cubre toggle/probe/call/audit_mcp.py, cache, whitelist y aviso de reinicio. Regla de oro: inspeccionar sondeando, auditoria y lote en una sola sesion con audit_mcp.py, accion puntual tools/call, encender solo lo pedido, apagar al acabar, nunca apagar los permanentes."
license: MIT
compatibility: opencode
metadata:
  audience: all
  workflow: token-optimization
---

# MCP a demanda — ahorro de tokens

Cada servidor MCP **conectado** añade sus tools (con nombres, descripciones y schemas completos) al prompt de sistema del agente **en cada mensaje**. Un MCP con 40 tools puede costar varios miles de tokens por turno, aunque la conversacion no lo use. Por eso: los MCPs se encienden para la tarea que los necesita y se apagan cuando termina.

## Arbol de decision

**Esta skill es agnostica del inventario**: NO asuma que existen MCPs concretos (ni resend, ni wsl-port, ni ningun otro). El conjunto de servidores disponibles se **descubre en tiempo de ejecucion** con cualquiera de estos metodos:

- `opencode mcp list` (muestra `connected`, `disabled` o `failed` por servidor).
- Leyendo la seccion `mcp` de `~/.config/opencode/opencode.json` (global) y del `opencode.json` del proyecto.
- `python scripts/toggle_mcp.py status` (lista los definidos en la config con su estado).

1. **¿Es una pregunta INFORMATIVA sobre un MCP?** ("¿que tools tiene X?", "¿cuantas tools me costaria activarlo?", "¿existe la tool Y?") → **NO actives nada**: sondealo con `python scripts/probe_mcp.py <nombre>` (vease la seccion Inspeccionar). **Si ya lo sondeaste hace poco, no lo relances**: `python scripts/probe_mcp.py <nombre> --from-cache` responde desde la cache local (0 s, 0 red, 0 tokens de arranque; >24 h de antiguedad → refresca con `--refresh` o quita el flag). Activar solo se justifica si el usuario va a **USAR** las tools de forma interactiva y repetida.
2. **¿El usuario pide una ACCION PUNTUAL con un MCP?** ("enviame un correo", "creame ese registro" — una o pocas llamadas, y no necesitas sus tools durante toda la sesion) → **NO lo actives**: invoca la tool directamente con `python scripts/call_mcp.py <nombre> <tool> --args-file ...` (tools/call por JSON-RPC: coste cero de prompt, sin reinicio de sesion). Vease la seccion "Usar un MCP sin activarlo".
3. **¿VARIAS llamadas al mismo MCP o AUDITORIA completa?** ("audita el MCP X", "prueba todas sus tools", "necesito 3-4 datos de resend") → **`python scripts/audit_mcp.py <nombre>`**: hace **1 solo arranque y N operaciones** en la misma sesion (handshake + serverInfo + capabilities + tools/resources/prompts + lote de `tools/call`). Encadenar `call_mcp.py` pagaria el relanzamiento del servidor **por llamada** (~15-20 s en locales `npx`). Vease la seccion "Auditar y llamar en lote".
4. **¿El usuario pidio un MCP EXPLICITAMENTE para uso interactivo?** Solo dos casos validos:
   - **Nombra el MCP** ("usa resend", "conecta el MCP de WSL") → ese es el candidato.
   - **La tarea es inequivocamente de un MCP concreto** y exige varias llamadas encadenadas o exploracion de tools en la sesion → busca en el inventario descubierto el servidor que encaje, por su nombre o por las tools que expone.
   - Si el usuario no pidio ningun MCP, **no enciendas nada**: la mayoria de tareas se resuelven con skills, CLIs y herramientas normales, sin coste de MCP.
5. **¿Ningun MCP del inventario encaja con lo pedido?** Informa al usuario ("no hay ningun MCP configurado para X; puedes anadirlo con `opencode mcp add`") en vez de encender uno parecido o improvisar. Nunca enciendas un MCP "que podria servir".
6. **¿El MCP elegido esta ya activo?** Comprueba con `opencode mcp list`.
7. **Si esta apagado y lo necesitas de forma interactiva:** activalo (ver Como activar/desactivar), AVISA al usuario de que lo activaste **y de que sus tools no estaran disponibles en esta sesion hasta reiniciarla** (se inyectan al arrancar), y haz la tarea (en sesion nueva, o con lo que la sesion actual permita). Para una sola accion puntual recuerda la via 2: `call_mcp.py`, sin activar ni reiniciar; para varias, la via 3: `audit_mcp.py`.
8. **Al terminar la tarea:** desactivalo, y explica al usuario en el reporte: "Se desactivo el MCP X para ahorrar tokens; dime si quieres dejarlo permanente."
9. **Excepcion permanente:** si el usuario marco un MCP como permanente ("dejalo siempre encendido"), no lo apagues sin pedir permiso. Registra esa preferencia durante la sesion.

## Inspeccionar las tools de un MCP (sin activarlo)

Preguntarle al servidor directamente por JSON-RPC deja el inventario intacto y no gasta un solo token de activacion:

```bat
python scripts\probe_mcp.py <nombre> [--config <ruta>] [--timeout <seg>] [--from-cache]
```

- Lista nombre + descripcion de cada tool y el TOTAL, tanto para servidores **remotos** (handshake Streamable HTTP: initialize → initialized → tools/list; tolera SSE y servidores stateless sin `Mcp-Session-Id`) como **locales** (lanza el `command` por stdio newline-delimited y lo mata al terminar; `npx -y` en la primera ejecucion puede tardar: sube `--timeout` a 90 o mas).
- Cada sondio en vivo **escribe la cache de schemas** en `.cache\<nombre>.json` (serverInfo, capabilities, tools con inputSchema, timestamp). Con `--from-cache` la respuesta sale del disco **sin arrancar el servidor** (0 s, 0 red); si la cache no existe o supera `--max-age` horas (defecto 24) avisa y sale con 2 — refresca con `--refresh` o sin el flag.
- ¿Auditoria completa o ademas alguna llamada? Usa `audit_mcp.py` (seccion "Auditar y llamar en lote"): misma cache, 1 arranque, N operaciones.
- Resuelve las referencias `{file:./.secrets/...}` de secretos **en runtime y nunca las imprime**.
- Exit code: 0 listo, 1 fallo de sondio, 2 error de config/servidor inexistente o `--from-cache` sin cache valida (sin `--refresh`).
- **Si despues el usuario quiere USAR esas tools:** distingue el tipo de uso.
  - **Uso puntual** (una o pocas llamadas, p. ej. enviar un correo) → `call_mcp.py` (ver siguiente seccion): sin activar, sin reiniciar, sin coste de prompt.
  - **Uso interactivo repetido** en la sesion (el agente va a llamar muchas tools del MCP una y otra vez) → `toggle_mcp.py on <nombre>` + recordarle que en la sesion actual no estaran disponibles hasta reiniciar (o abrir sesion nueva).

Detalle del protocolo y por que NO se debe sondear a mano con curl/PowerShell en Windows: lee `references/probe-jsonrpc.md`.

## Usar un MCP sin activarlo (tools/call directo)

Hermano de probe_mcp.py que da un paso mas: tras el handshake hace `tools/call` de UNA tool y muestra su resultado, sin tocar la config ni gastar tokens de prompt:

```bat
:: Listar tools (equivalente a probe_mcp.py, comodo desde el mismo script)
python scripts\call_mcp.py <nombre> --list --timeout 90
python scripts\call_mcp.py <nombre> --list --from-cache   :: sin arrancar el servidor

:: Invocar una tool: los argumentos SIEMPRE por archivo UTF-8 (quoting inline
:: en cmd/PowerShell corrompe el JSON)
python scripts\call_mcp.py <nombre> <tool> --args-file payload.json --timeout 90
```

- **Cuando conviene:** acciones puntuales (enviar un correo, crear un registro, listar dominios) donde NO necesitas las tools del MCP declaradas en tu prompt durante toda la sesion. Ideal para servidores con decenas o cientos de tools (activarlos pagaria decenas de miles de tokens por mensaje).
- **Limite: UNA llamada por ejecucion.** Cada ejecucion repite el handshake y, en servidores locales, **relanza el proceso completo (~15-20 s con `npx`)**; el servidor ademas no "recuerda" nada entre llamadas. Si necesitas **varias llamadas al mismo MCP o auditarlo**, NO encadenes este script: usa `audit_mcp.py` (1 arranque, N operaciones). Debes conocer o consultar antes el schema de la tool (`--list --from-cache` si ya lo sondeaste, y `references/probe-jsonrpc.md` para el schema completo), y el servidor debe estar **definido en la config** (aunque este `disabled`) para que el script resuelva su `command`/`url` y secretos.
- Valida la tool contra `tools/list` antes de llamarla: si no existe, imprime la lista completa y sale con 2. JSON de argumentos invalido → 2; fallo de red/proceso o `isError:true` de la tool → 1; exito → 0.
- Nunca imprimas los argumentos ni los secretos resueltos; solo el resultado de la tool.

## Auditar y llamar en lote (sesion unica) — audit_mcp.py

`probe_mcp.py` y `call_mcp.py` relanzan el servidor en CADA ejecucion: en locales `npx` son ~15-20 s de arranque por llamada, y cada listado reimprime todas las tools (resend: 106 tools → miles de tokens en tu contexto). `audit_mcp.py` hace **1 arranque y N operaciones** en una sola sesion y su salida es **compacta por defecto** (numero de tools y nombres; descripciones solo con `--verbose`).

```bat
:: Auditoria completa: handshake + serverInfo + capabilities + tools/list +
:: resources/list + prompts/list (lo no soportado se marca -32601 sin fallar)
:: y escribe la cache .cache\<nombre>.json
python scripts\audit_mcp.py resend --timeout 120

:: Varias llamadas en UN solo arranque: --call es repetible, cada --args-file
:: se asigna por orden al --call que no trae argumentos
python scripts\audit_mcp.py context7 --calls-only ^
  --call resolve-library-id --args-file p1.json ^
  --call resolve-library-id --args-file p2.json

:: Lote desde un plan JSON (misma sesion, tabla OK/ERROR con outputs truncados
:: a --max-output lineas)
python scripts\audit_mcp.py context7 --batch plan.json --calls-only --max-output 8

:: Ver el ultimo snapshot sin arrancar el servidor (0 s, 0 red)
python scripts\audit_mcp.py resend --from-cache
```

Plan de ejemplo `plan.json` (lista de llamadas; `args` inline o `args_file` relativo al plan):

```json
[
  {"tool": "resolve-library-id", "args": {"libraryName": "Next.js", "query": "routing"}},
  {"tool": "resolve-library-id", "args_file": "payload2.json"}
]
```

- **Remotos y locales** se soportan igual que probe/call (Streamable HTTP con SSE/stateless; stdio newline-delimited matando el proceso al cerrar). Secretos `{file:...}` resueltos en runtime, nunca impresos.
- Flags para saltarse secciones: `--calls-only` (solo llamadas), `--no-tools`, `--no-resources`, `--no-prompts`; `--refresh` si `--from-cache` encuentra la cache vieja (> `--max-age` horas, defecto 24).
- Salida por defecto sin `--verbose`: `SERVER / CAPS / TOOLS N — nombres / RESOURCES / PROMPTS / CALLS:` una linea por llamada (`OK tool — preview` o `ERROR tool — motivo corto`). `--from-cache` NO puede combinarse con `--call`/`--batch` (invocar exige hablar con el servidor).
- Exit codes: 0 todo OK; 1 algun fallo de llamada/sondeo (`isError:true`, tool inexistente, red/proceso — lo "no soportado" NO es fallo); 2 error de config/uso.
- La auditoria real que motivo este script: 5 servidores (2 remotos HTTP/SSE, 3 locales npx stdio), 203 tools; con sesiones unicas por servidor el coste de arranque paso de ~15-20 s por operacion a **uno solo por servidor**.

## Lecciones de una sesion real (resend / send-email)

- **El schema publicado puede omitir parametros requeridos:** `send-email` no listaba `from` en su schema y el servidor respondio `from: expected string, received undefined`. Solucion: sondear tools de **solo lectura** relacionadas (p. ej. `list-domains`) para descubrir los valores validos (el unico dominio verificado) y reintentar. El intento fallido por validacion **NO llego a la API**: no hay riesgo de duplicados al reintentar.
- **Servidores gigantes:** resend expone 106 tools; activarlo inyectaria decenas de miles de tokens por mensaje. Para una accion puntual, `call_mcp.py`; para varias, `audit_mcp.py --batch` (un solo arranque); si necesitas varias tools a menudo, conecta el servidor y oculta todo menos un whitelist (ver Alternativa en Como activar/desactivar).

## Como activar / desactivar

El metodo verificado contra el binario instalado (opencode 2.x) es editar la configuracion `~/.config/opencode/opencode.json`. El binario acepta **dos sintaxis** (usa la que ya tenga el archivo del usuario):

- Formato anidado (el del opencode.json global de esta maquina): clave `mcp.servers.<nombre>` con flag `disabled` (`true` = apagado).
- Formato plano (doc oficial estable): clave `mcp.<nombre>` con flag `enabled` (`false` = apagado; por defecto es `true`).

Sintaxis completa, ejemplos y el CLI `opencode mcp`: lee `references/sintaxis-opencode-mcp.md` ANTES de editar la config si no la recuerdas. No leas ese archivo para tareas que solo usan `opencode mcp list`.

**Recomendado — usa el script** (edita el JSON sin romper estructura ni resolver secretos):

```bat
:: Estado actual de TODOS los servidores definidos (descubre el inventario aqui)
python scripts\toggle_mcp.py status

:: Apagar un servidor tras terminar su tarea (escribe disabled: true)
python scripts\toggle_mcp.py off <nombre>

:: Encender un servidor explicitamente pedido por el usuario
python scripts\toggle_mcp.py on <nombre>
```

Ruta del script dentro de esta skill: `scripts/toggle_mcp.py` (por defecto opera sobre `~/.config/opencode/opencode.json`; usa `--config` para otro archivo). Despues de cualquier cambio, **verifica** con `opencode mcp list`.

**Alternativa intermedia para un MCP con MUCHAS tools que quieres tener conectadas sin pagarlas en el prompt**: ocultarlas todas con un patron y **whitelistear explicitamente** solo las 2-3 que usas, en opencode.json:

```json
"mcp": { "servers": { "resend": { "disabled": false } } },
"tools": {
  "resend_*": false,
  "resend_send-email": true,
  "resend_list-domains": true
}
```

Con un servidor de 106 tools (resend) esto deja de inyectar decenas de miles de tokens por mensaje a pagar solo las 2 tools whitelisteadas, y el servidor queda vivo para otras integraciones. Orden de preferencia: **apagar** ahorra mas (ni siquiera se conecta) → **accion puntual unica**: `call_mcp.py` sin tocar nada → **uso repetido de 2-3 tools**: patron `_*: false` + whitelist → **uso intensivo de muchas tools**: activar el servidor entero.

## Regla de oro (flujo completo)

```
inspeccionar (¿que tools tiene?) → probe_mcp.py → responder — sin tocar la config
  ¿ya lo sondeaste hace poco? → probe_mcp.py <nombre> --from-cache (0 s, 0 red)

accion puntual (enviar un correo, UNA llamada) → call_mcp.py <nombre> <tool>
--args-file payload.json → reportar resultado — sin activar, sin reiniciar

auditoria o VARIAS llamadas/LOTE al mismo MCP → audit_mcp.py <nombre>
(--batch plan.json o --call repetido) → 1 arranque, N operaciones → tabla compacta

uso interactivo repetido: detectar necesidad → opencode mcp list → toggle on
(avisar + tools llegan en sesion nueva/reinicio) → hacer la tarea
→ toggle off (avisar + explicar ahorro de tokens) → verificar con opencode mcp list
```

- Si el usuario encendio un MCP el mismo y pide "apagalo al terminar", cumplelo en la misma respuesta final.
- Si la sesion va a seguir usando el MCP en los proximos minutos (varias tareas seguidas), mantenlo encendido y apaga al cerrar el lote de trabajo, no entre cada tarea.
- Reporta SIEMPRE el cambio de estado en tu respuesta: "MCP <nombre> activado para la tarea / desactivado al terminar para ahorrar tokens".

## Anti-patrones

- **Encadenar varios `call_mcp.py` contra un MCP local `npx`**: cada ejecucion relanza el servidor y paga ~15-20 s de arranque POR LLAMADA. Usa `audit_mcp.py --batch plan.json` (o `--call` repetido): **1 solo arranque, N llamadas** en la misma sesion.
- **Volver a sondear en vivo lo que ya esta en la cache**: tras un probe/audit, los schemas quedan en `.cache\<nombre>.json`; relanzar el servidor para ver lo mismo gasta 15-20 s (local) o red inutilmente. Usa `--from-cache` (y recuerda `--max-age`/`--refresh` si necesitas datos frescos).
- **Imprimir todas las tools sin necesidad**: `--list` de un servidor de 106 tools (resend) vuelca miles de tokens en el contexto. Usa la salida compacta de `audit_mcp.py` (numero + nombres) o `--from-cache`, y `--verbose` solo cuando haga falta el detalle.
- **Dejar MCPs encendidos "por si acaso"**: cada mensaje posterior paga los tokens de sus tools. Apaga al terminar.
- **Sondear con PowerShell/curl inline y JSON entrecomillado a mano**: PS 5.1 corrompe las comillas del payload ("JSON invalido") y `Invoke-WebRequest` falla por TLS (.NET viejo) aunque curl funcione. Usa siempre `probe_mcp.py`.
- **Imprimir secretos "para depurar"**: los valores de `{file:./.secrets/...}` se resuelven en runtime; nunca los muestres en la conversacion ni en logs.
- **Activar un MCP solo para responder "¿que tools tiene?"**: es una pregunta informativa → `probe_mcp.py`, sin tocar la config.
- **Activar un MCP (y pagar sus tools en cada mensaje + reiniciar sesion) para una sola llamada puntual**: usa `call_mcp.py <nombre> <tool> --args-file ...` — coste cero de prompt.
- **Confiar ciegamente en el schema publicado**: puede omitir parametros requeridos (leccion real: `send-email` sin `from`). Ante un error de validacion, consulta tools de solo lectura relacionadas para descubrir el valor valido y reintenta.
- **Apagar en medio de una tarea** (entre tool calls): la sesion puede depender de la conexion; apaga solo al entregar el resultado.
- **Apagar sin avisar o sin confirmar lo "permanente"**: el usuario puede estar esperando que el MCP siga disponible. Pregunta una vez y respeta la respuesta.
- **Reformatear la config entera**: no cambies el formato que usa el archivo del usuario (anidado vs plano); edita solo el flag del servidor.
- **Poner `disabled: true` y `enabled: false` a la vez**: usa el flag que corresponda al formato del bloque.
- **Asumir que un servidor no existe porque no aparece en `opencode mcp list`**: list solo muestra los definidos en la config; puede haber config de proyecto que anada otros.
- **Manipular secretos**: los tokens van en `{file:./.secrets/...}` (referencias que los scripts preservan y resuelven solo en runtime). Nunca copies el valor de un secreto a la config, a un comando ni a la conversacion — tampoco al imprimir el resultado de un sondio.

## Problemas comunes

| Sintoma | Causa | Solucion |
|---|---|---|
| Cambié el flag y sigue conectado | Config no recargada | Reinicia la sesion/opencode; verifica con `opencode mcp list` |
| Active un MCP a mitad de sesion y sus tools no aparecen | Las tools se inyectan en el prompt al **arrancar** la sesion | `opencode mcp list` dira `connected` pero no podras llamarlas: reinicia la sesion o abre una nueva. Si solo querias ver que tools hay, usa `probe_mcp.py` sin activar |
| `JSON invalido` al sondear con curl desde PowerShell | PS 5.1 corrompe las comillas del payload al pasarlo a curl.exe | No sondees a mano: usa `python scripts/probe_mcp.py <nombre>` |
| `Invoke-WebRequest` falla con error TLS pero curl funciona | El .NET viejo de PS 5.1 no negocia ese TLS | Usa curl o probe_mcp.py (urllib); no concluyas que el servidor esta caido |
| El servidor no devuelve `Mcp-Session-Id` | Servidor stateless: no exige sesion | No es un error; probe_mcp.py reintenta tools/list sin la cabecera si hiciera falta |
| `failed: Connection closed` | Comando local roto o falta dependencia | Revisa el `command` del servidor; prueba el comando a mano |
| MCP no aparece en list | No esta en la config activa | Buscalo en opencode.json global y de proyecto; `opencode mcp add <nombre>` para crearlo |
| El script dice "no existe" | Nombre mal escrito | Ejecuta `toggle_mcp.py status` que lista los existentes |
| `call_mcp.py` devuelve error de validacion tipo "campo requerido undefined" aunque el schema no liste ese campo | Schema publicado incompleto por el servidor | Sondea tools de solo lectura relacionadas (p. ej. `list-domains` en resend) para descubrir los valores validos, anade el campo faltante al payload y reintenta; el intento fallido por validacion suele quedarse en el servidor y NO llega a la API (sin duplicados) |
| `--args '{...}'` inline falla con "JSON no valido" | cmd/PowerShell corrompen las comillas del JSON | Pon los argumentos en un archivo UTF-8 y usa `--args-file payload.json` |
| La primera llamada a un MCP local (`npx -y ...`) expira | `npx` descarga el paquete en el primer arranque | Sube `--timeout 90` (o mas) en probe_mcp.py, call_mcp.py y audit_mcp.py |
| Cada llamada a un MCP local tarda 15-20 s y se repite | probe/call relanzan el proceso en cada ejecucion | Agrupa en `audit_mcp.py --batch` (1 arranque, N llamadas) o lee schemas con `--from-cache` |
