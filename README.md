# mcp-ahorro-tokens

Skill para [OpenCode](https://opencode.ai) que **activa y desactiva servidores MCP bajo demanda para ahorrar tokens de contexto**.

Cada servidor MCP **conectado** inyecta los nombres, descripciones y schemas completos de sus tools en el prompt del agente **en cada mensaje**, aunque la conversación nunca las use: un servidor con 100+ tools cuesta decenas de miles de tokens por turno. Esta skill impone la disciplina opuesta: apagar todo al empezar, encender solo lo pedido, apagar al terminar y —siempre que se pueda— **usar un MCP sin encenderlo** (sondear sus tools, auditarlo o llamar una tool exacta por JSON-RPC directo, coste cero de prompt).

[![skills.sh](https://skills.sh/b/gilmanpro/mcp-ahorro-tokens)](https://skills.sh/gilmanpro/mcp-ahorro-tokens)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![OpenCode](https://img.shields.io/badge/OpenCode-compatible-blue)](https://opencode.ai)
[![Python](https://img.shields.io/badge/Python-3.9+-yellow)](https://python.org)

## Que hace

El agente aprende un arbol de decision con 4 rutas, de menor a mayor coste:

| Necesidad | Ruta | Coste de tokens |
|---|---|---|
| "¿Que tools tiene el MCP X?" (informativa) | `probe_mcp.py` (sondeo JSON-RPC; `--from-cache` = 0 s, sin red) | **cero** — no toca la config |
| Una accion puntual (enviar un correo, crear un registro) | `call_mcp.py <mcp> <tool> --args-file ...` | **cero** de prompt, sin reiniciar sesion |
| Auditoria completa o varias llamadas al mismo MCP | `audit_mcp.py <mcp>` (1 arranque, N operaciones, `--batch`, `--out`) | **cero** de prompt |
| Uso interactivo repetido en la sesion | `toggle_mcp.py on <mcp>` + reinicio → `off` al terminar | paga tools solo mientras esta encendido |

Regla de oro: **inspeccionar sondeando, auditar en lote, accion puntual por tools/call, encender solo lo pedido, apagar al terminar, nunca apagar los permanentes.** Los scripts son agnosticos del inventario: descubren los MCPs configurados en tiempo de ejecucion y los secretos (`{file:./.secrets/...}`) se resuelven solo en runtime y **nunca se imprimen**.

## Instalacion

### a) Con el CLI de skills.sh (recomendado)

```bash
npx skills add gilmanpro/mcp-ahorro-tokens
```

El CLI descarga la skill de este repo y la **instala en la carpeta de skills de los agentes que detecta en tu maquina** (las deja disponibles para que cada agente las cargue segun su `description`). Utilidades:

- `-g` la instala a nivel de usuario (global, todas las sesiones) en lugar del proyecto actual.
- `-a <agente>` elige el agente destino; `--copy` copia los archivos en vez de enlazarlos.
- `-l` lista las skills disponibles del repo sin instalar nada; `--all -y` instala sin preguntas.
- La **primera instalacion es la que indexa la ficha** en https://skills.sh/gilmanpro/mcp-ahorro-tokens (el indexado es asincrono: la pagina puede tardar en aparecer).

### b) Copia manual (2 segundos)

**Global** — visible en todas tus sesiones:

```bat
:: Windows (cmd / PowerShell)
git clone https://github.com/gilmanpro/mcp-ahorro-tokens.git %USERPROFILE%\.agents\skills\mcp-ahorro-tokens
```

```bash
# Linux / macOS
git clone https://github.com/gilmanpro/mcp-ahorro-tokens.git ~/.agents/skills/mcp-ahorro-tokens
```

**Por proyecto**: clona (o copia la carpeta) en `<proyecto>/.opencode/skills/mcp-ahorro-tokens`.

**Sin pasos extra**: OpenCode descubre y carga automaticamente las skills de `~/.agents/skills` y `.opencode/skills` al iniciar — no hay que registrar nada, ni reiniciar nada especial, ni instalar dependencias.

### c) Como agente de OpenCode (la parte importante)

- Una vez instalada (via a o b), **no hay que invocarla a mano**: cuando una tarea matchea su `description` ("activa/apaga un MCP", "que tools tiene X", "cuanto cuesta tenerlo conectado", "ahorrar tokens"), el **orquestador y los sub-agentes cargan la skill solos** y siguen su arbol de decision.
- La skill trae la politica ya escrita: `off --all --bg` al iniciar la sesion (los MCPs quedan a coste cero), encender solo lo pedido, y apagar avisando al terminar.
- Los agentes con terminal tambien pueden **instalarla o apagar MCPs por si mismos** al detectar la necesidad — el flujo completo esta documentado en `SKILL.md`.

## Requisitos

- **Python 3.9+** con solo la **biblioteca estandar** (los scripts no tienen dependencias externas: nada de `pip install`).
- **OpenCode** solo si quieres la integracion como skill de agente; las CLIs (`toggle_mcp.py`, `audit_mcp.py`...) funcionan aparte desde cualquier terminal.
- Servidores MCP definidos en `~/.config/opencode/opencode.json` (seccion `mcp`) o en el `opencode.json` del proyecto.

## Primeros 60 segundos

```bat
cd %USERPROFILE%\.agents\skills\mcp-ahorro-tokens

python scripts\toggle_mcp.py status
python scripts\toggle_mcp.py off --all --bg
python scripts\audit_mcp.py <servidor> --timeout 90
python scripts\refrescar_cache.py --verificar
```

1. `status`: estado (encendido/apagado) de todos los MCPs definidos, sin arrancar ninguno.
2. `off --all --bg`: los apaga **todos** en segundo plano — retorna al instante, se auto-verifica releyendo la config y deja evidencia en `.tmp\mcp-off-all.log` (`--except n1,n2` protege los permanentes).
3. `audit_mcp.py <servidor>`: auditoria completa (serverInfo, capabilities, tools) en 1 arranque **sin activar el servidor**, y escribe la cache de schemas.
4. `refrescar_cache.py --verificar`: edad y numero de tools por servidor desde la cache local (sin red).

## Comandos principales

| Comando | Que hace |
|---|---|
| `python scripts\toggle_mcp.py status` | Estado de cada MCP definido en la config (lectura, no arranca nada) |
| `python scripts\toggle_mcp.py off --all [--bg] [--except n1,n2]` | Apaga todos; `--bg` = detached con auto-verificacion inmediata |
| `python scripts\toggle_mcp.py verify` | Confirmacion instantanea: exit 0 = todos apagados (1 = alguno encendido) |
| `python scripts\toggle_mcp.py on\|off <nombre>` | Enciende/apaga uno (los cambios de tools aplican al reiniciar la sesion) |
| `python scripts\probe_mcp.py <nombre> [--from-cache]` | Lista las tools sin activar; `--from-cache` responde desde disco (0 s, sin red) |
| `python scripts\call_mcp.py <nombre> <tool> --args-file payload.json` | Una llamada `tools/call` puntual sin activar ni reiniciar |
| `python scripts\audit_mcp.py <nombre> [--call ... --batch plan.json --out DIR] [--show-tools [--tool X]]` | Auditoria o lote de N llamadas en 1 arranque; `--out` vuelca cada resultado completo a archivo; `--show-tools` lee catalogo/inputSchema desde la cache |
| `python scripts\refrescar_cache.py [--verificar] [--solo n1 n2]` | Refresca la cache de schemas de todo el inventario (~10 s); `--verificar` = lectura local |

`audit_mcp.py --all --timeout 120` recorre **todo** el inventario en serie con tabla resumen. Por defecto todos los scripts operan sobre `~/.config/opencode/opencode.json` (`--config` admite otro archivo) y aceptan ambos formatos de config (anidado `mcp.servers.*` + `disabled`, y plano oficial `mcp.*` + `enabled`).

## Auto-refresco de la cache al iniciar OpenCode

La cache de schemas (`.cache\<servidor>.json`) es lo que hace posibles `--from-cache` y `--show-tools` **sin red**. El plugin `mcp-refresco-cache.js` (colocalo en `~/.config/opencode/plugins/`) la mantiene fresca: al arrancar OpenCode lanza `refrescar_cache.py` **en segundo plano** (pythonw real, detached — el arranque nunca se bloquea), con throttle de 6 h y lock anti-doble-lanzamiento. **No registra hooks ni toca la seccion `mcp` de la config: cero tokens y cero cambios de estado.** Log y marcador quedan en `.tmp\` de la skill. Desactivarlo: variable de entorno `MCP_REFRESH_DISABLED=1`, quitar el archivo del plugin, o su constante `DESACTIVADO=true`.

Alternativa **solo si no usas OpenCode**: `registrar_tarea.ps1` crea una tarea de Windows (Task Scheduler, trigger al iniciar sesion, sin admin) con el mismo refresco; `-DryRun` muestra el comando sin registrarlo, `-IntervaloMin N` anade repeticion cada N minutos. `quitar_tarea.ps1` la elimina (idempotente).

## Estructura del repo

```
mcp-ahorro-tokens/
├── SKILL.md                    # definicion de la skill (frontmatter + flujos que lee el agente)
├── README.md                   # este archivo
├── LICENSE                     # MIT
├── evals/trigger.json          # casos de prueba de activacion de la skill
├── scripts/
│   ├── mcp_common.py           # nucleo: config, secretos, JSON-RPC (HTTP + stdio), cache
│   ├── toggle_mcp.py           # on/off/status/verify de servidores MCP en opencode.json
│   ├── probe_mcp.py            # tools/list sin activar (con cache de schemas)
│   ├── call_mcp.py             # tools/call puntual sin activar ni reiniciar
│   ├── audit_mcp.py            # auditoria y lote: 1 arranque, N operaciones, --out/--show-tools
│   ├── refrescar_cache.py      # refresco de cache de todos los servidores (cero tokens)
│   ├── registrar_tarea.ps1     # alternativa: tarea programada de Windows
│   └── quitar_tarea.ps1        # ...y su eliminacion idempotente
└── references/
    ├── sintaxis-opencode-mcp.md  # seccion mcp de opencode.json (anidado vs plano) + CLI
    ├── probe-jsonrpc.md          # protocolo MCP por JSON-RPC: porque no sondear a mano con curl
    └── troubleshooting.md        # sintomas -> causa -> solucion
```

## Licencia e incidencias

MIT © 2026 Gilberto Castillo — ver [LICENSE](LICENSE). Bugs, dudas de uso y peticiones: [Issues de este repo](https://github.com/gilmanpro/mcp-ahorro-tokens/issues). El detalle operativo completo (arbol de decision, anti-patrones, codigos de salida) vive en [SKILL.md](SKILL.md).
