# mcp-ahorro-tokens

Skill para [OpenCode](https://opencode.ai) que **activa y desactiva servidores MCP bajo demanda para ahorrar tokens de contexto**.

Cada servidor MCP conectado inyecta sus tools (nombres, descripciones y schemas completos) en el system prompt del agente **en cada mensaje**. Un MCP con 40 tools puede costar varios miles de tokens por turno aunque la conversación no lo use. Esta skill impone la disciplina: **encender solo lo pedido, apagar al terminar, y preferir rutas de coste cero cuando sea posible**.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![OpenCode](https://img.shields.io/badge/OpenCode-compatible-blue)](https://opencode.ai)
[![Python](https://img.shields.io/badge/Python-3.9+-yellow)](https://python.org)

## Qué hace

La skill enseña al agente un árbol de decisión con 4 rutas, de menor a mayor coste de tokens:

| Necesidad | Ruta | Coste |
|---|---|---|
| ¿Qué tools tiene el MCP X? (informativa) | `probe_mcp.py` (sondeo JSON-RPC directo; `--from-cache` = 0 s, 0 red) | **cero** — no toca la config |
| Una acción puntual (enviar un correo, crear un registro) | `call_mcp.py <mcp> <tool> --args-file ...` (tools/call directo) | **cero** de prompt, sin reiniciar sesión |
| Auditoría completa o varias llamadas al mismo MCP | `audit_mcp.py <mcp>` (**1 arranque, N operaciones**, salida compacta, `--batch plan.json`) | **cero** de prompt |
| Uso interactivo repetido en la sesión | `toggle_mcp.py on <mcp>` + reinicio de sesión → `toggle_mcp.py off` al terminar | paga tools solo mientras está encendido |

Regla de oro: **inspeccionar sondeando, auditar en lote, acción puntual por tools/call, encender solo lo pedido, apagar al terminar, nunca apagar los permanentes.**

Los scripts son **agnósticos del inventario**: descubren los MCPs configurados en tiempo de ejecución (`opencode mcp list` / sección `mcp` de `opencode.json`), no asumen servidores concretos. Los secretos referenciados como `{file:./.secrets/...}` se resuelven **solo en runtime y nunca se imprimen**.

## Instalación

Copia la carpeta de la skill a uno de estos destinos (global o por proyecto):

```bash
# Global
cp -r mcp-ahorro-tokens ~/.agents/skills/

# o bien, a nivel de proyecto
cp -r mcp-ahorro-tokens .opencode/skills/
```

Requisitos: OpenCode 2.x y Python 3.9+ (solo stdlib; sin dependencias externas).

## Uso

```bash
# Estado de TODOS los servidores MCP definidos en la config
python scripts/toggle_mcp.py status

# Encender / apagar un servidor (edita opencode.json sin romper estructura)
python scripts/toggle_mcp.py on  <nombre>
python scripts/toggle_mcp.py off <nombre>

# Verificar con el CLI oficial
opencode mcp list
```

```bash
# Inspeccionar las tools de un MCP sin activarlo
python scripts/probe_mcp.py <nombre>
python scripts/probe_mcp.py <nombre> --from-cache      # desde la caché local (0 s)

# Invocar UNA tool sin activar el MCP (argumentos siempre por archivo UTF-8)
python scripts/call_mcp.py <nombre> <tool> --args-file payload.json

# Auditoría completa o LOTE de llamadas en una sola sesión
python scripts/audit_mcp.py <nombre> --timeout 120
python scripts/audit_mcp.py <nombre> --call tool_a --args-file a.json --call tool_b --args-file b.json
python scripts/audit_mcp.py <nombre> --batch plan.json --calls-only
```

Rutas por defecto: los scripts operan sobre `~/.config/opencode/opencode.json` (admite `--config` para otro archivo). Tras activar/desactivar, las tools se inyectan **al arrancar la sesión**: hay que reiniciarla para que estén disponibles.

## Estructura

```
mcp-ahorro-tokens/
├── SKILL.md                          # Definición de la skill (frontmatter + flujo para el agente)
├── README.md                         # Este archivo
├── LICENSE                           # MIT
├── .gitignore
├── scripts/
│   ├── mcp_common.py                 # Núcleo compartido: config, secretos, JSON-RPC (HTTP + stdio), caché
│   ├── toggle_mcp.py                 # Activar/desactivar servidores MCP en opencode.json
│   ├── probe_mcp.py                  # Sondear tools/list sin activar (con caché de schemas)
│   ├── call_mcp.py                   # tools/call de una sola tool, sin activar ni reiniciar
│   └── audit_mcp.py                  # Auditoría y lote de llamadas: 1 arranque, N operaciones
└── references/
    ├── sintaxis-opencode-mcp.md      # Sintaxis de la sección mcp de opencode.json + CLI opencode mcp
    └── probe-jsonrpc.md              # Protocolo MCP por JSON-RPC y por qué no sondear a mano con curl/PowerShell
```

## Cómo funciona el ahorro

- `.cache/<nombre>.json` guarda los schemas sondeados: repetir la inspección con `--from-cache` no arranca el servidor ni gasta red.
- La salida de `audit_mcp.py` es compacta por defecto (número de tools + nombres); `--verbose` solo cuando hace falta el detalle.
- Alternativa intermedia documentada en `SKILL.md`: mantener el servidor conectado con `tools: { "servidor_*": false }` + whitelist de 2-3 tools.

## Licencia

[MIT](LICENSE) © 2026 Gilberto Castillo
