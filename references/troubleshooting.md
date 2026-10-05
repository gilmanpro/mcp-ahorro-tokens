# Troubleshooting: sintoma → causa → solucion

Tabla de fallos reales observados en sesion con los scripts de esta skill
(`probe_mcp.py`, `call_mcp.py`, `audit_mcp.py`, `toggle_mcp.py`) y con la
configuracion MCP de opencode. Leer cuando un sintoma aparezca; no hace falta
cargarla para ejecutar comandos que ya responden bien. Detalle del protocolo
JSON-RPC detras de varias filas: `probe-jsonrpc.md`.

| Sintoma | Causa | Solucion |
|---|---|---|
| Cambié el flag y sigue conectado | Config no recargada | Reinicia la sesion/opencode; verifica con `opencode mcp list` |
| Active un MCP a mitad de sesion y sus tools no aparecen | Las tools se inyectan en el prompt al **arrancar** la sesion | `opencode mcp list` dira `connected` pero no podras llamarlas: reinicia la sesion o abre una nueva. Si solo querias ver que tools hay, usa `python scripts\probe_mcp.py <nombre>` sin activar |
| `JSON invalido` al sondear con curl desde PowerShell | PS 5.1 corrompe las comillas del payload al pasarlo a curl.exe | No sondees a mano: usa `python scripts\probe_mcp.py <nombre>` |
| `Invoke-WebRequest` falla con error TLS pero curl funciona | El .NET viejo de PS 5.1 no negocia ese TLS | Usa curl o `probe_mcp.py` (urllib); no concluyas que el servidor esta caido |
| El servidor no devuelve `Mcp-Session-Id` | Servidor stateless: no exige sesion | No es un error; `probe_mcp.py` reintenta `tools/list` sin la cabecera si hiciera falta |
| `failed: Connection closed` | Comando local roto o falta dependencia | Revisa el `command` del servidor en la config; prueba el comando a mano |
| MCP no aparece en `opencode mcp list` | No esta en la config activa (la de proyecto puede anadir otros) | Buscalo en `opencode.json` global y de proyecto; crealo con `opencode mcp add <nombre>` |
| El script dice "servidor inexistente" | Nombre mal escrito | Ejecuta `python scripts\toggle_mcp.py status`, que lista los existentes |
| Error de validacion tipo "campo requerido undefined" aunque el schema no liste ese campo | Schema publicado incompleto por el servidor (leccion real: `send-email` de resend sin `from`) | Sondea tools de solo lectura relacionadas (p. ej. `list-domains`) para descubrir el valor valido, anade el campo al payload y reintenta; el intento fallido por validacion suele quedarse en el servidor y NO llega a la API (sin duplicados) |
| `--args '{...}'` inline falla con "JSON no valido" | cmd/PowerShell corrompen las comillas del JSON | Pon los argumentos en un archivo UTF-8 y usa `--args-file payload.json` |
| La primera llamada a un MCP local (`npx -y ...`) expira | `npx` descarga el paquete en el primer arranque | Sube `--timeout 90` (o mas) en `probe_mcp.py`, `call_mcp.py` y `audit_mcp.py`; en `--all` usa 120 por servidor |
| Cada llamada a un MCP local tarda 15-20 s y se repite | `probe_mcp.py`/`call_mcp.py` relanzan el proceso en cada ejecucion | Agrupa en `python scripts\audit_mcp.py <nombre> --batch plan.json` (1 arranque, N llamadas) o lee los schemas con `--from-cache` |
| `--from-cache` sale con codigo 2 | Cache inexistente o mas vieja que `--max-age` horas (defecto 24) | Refresca sin el flag, o con `--refresh` (tambien en `audit_mcp.py`) |
| Un `--call` falla con "tool no encontrada" y se imprime la lista | El nombre no coincide con `tools/list` (el script valida antes de llamar) | Copia el nombre exacto de la lista; si la cache esta vieja, relanza sin `--from-cache` |
| `audit_mcp.py --all` marca FALLO en un servidor y termina igual | Un servidor roto (timeout, proceso muerto) no aborta el lote | Revisa ese servidor con `python scripts\audit_mcp.py <nombre> --timeout 120` a mano; la tabla global ya anoto el resto |
| `off --all --bg` retorno OK pero luego `verify` marca algun servidor ENCENDIDO | El proceso detached aun no termina, o fallo al escribir (linea final FALLO en el log) | Revisa `.tmp\mcp-off-all.log` de la skill (cabecera con timestamp, resumen por servidor, linea final VERIFICADO/FALLO) y ejecuta `python scripts\toggle_mcp.py verify --bg-log`; si persiste el FALLO, lanza `off --all` en primer plano y mira el error |
