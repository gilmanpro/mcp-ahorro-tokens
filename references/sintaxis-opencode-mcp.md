# Sintaxis MCP en OpenCode — referencia verificada

Verificado contra el binario opencode 2.x instalado en esta maquina (comandos reales, no doc de memoria). Fuentes: `opencode mcp --help`, `opencode mcp list`, pruebas con configs temporales en proyecto, y el `~/.config/opencode/opencode.json` global del usuario.

**Nota sobre el inventario:** el conjunto real de MCPs de cada usuario NO esta fijado en esta referencia ni en la skill; se descubre en tiempo de ejecucion (`opencode mcp list`, `toggle_mcp.py status`, o leyendo la seccion `mcp` de la config global y de proyecto). Los nombres de servidor de los ejemplos siguientes son solo ilustrativos de la sintaxis.

## Los dos formatos que acepta el binario (probados)

Ambos se pueden usar; `opencode mcp list` los muestra. Usa SIEMPRE el formato que ya tenga el archivo que editas — no mezcles ni migres el formato por el camino.

### 1. Formato anidado con `disabled` (el del opencode.json global de esta maquina)

```json
{
  "mcp": {
    "servers": {
      "mi-servidor-local": {
        "type": "local",
        "command": ["npx", "-y", "paquete-mcp"],
        "disabled": false,
        "environment": { "MI_API_KEY": "{file:./.secrets/mi-api-key}" }
      },
      "mi-servidor-remoto": {
        "type": "remote",
        "url": "https://mcp.ejemplo.com/",
        "disabled": false,
        "headers": { "Authorization": "Bearer {file:./.secrets/mi-token}" }
      }
    }
  }
}
```

- `disabled: true` → el servidor aparece en `opencode mcp list` con estado `disabled` (no se conecta, no inyecta tools).
- `disabled: false` o ausente → se conecta.

### 2. Formato plano con `enabled` (doc oficial estable)

```json
{
  "mcp": {
    "mi-servidor": {
      "type": "local",
      "command": ["npx", "-y", "paquete-mcp"],
      "enabled": false
    },
    "otro-remoto": {
      "type": "remote",
      "url": "https://ejemplo.com/mcp",
      "headers": { "Authorization": "Bearer TOKEN" }
    }
  }
}
```

- `enabled: false` → estado `disabled` en list. `enabled: true` o ausente → se conecta.
- En este formato las claves de servidor conviven al mismo nivel que `servers` si existiera; en la practica un archivo usa uno u otro.

### Campos comunes a ambos

| Campo | Aplica a | Descripcion |
|---|---|---|
| `type` | ambos | `"local"` (levanta un proceso) o `"remote"` (URL) |
| `command` | local | array de exec + args (ej. `["npx","-y","@playwright/mcp@latest"]`) |
| `url` | remote | endpoint del servidor |
| `environment` | local | variables para el proceso (acepta `{file:./ruta}`) |
| `headers` | remote | cabeceras HTTP (acepta `{file:./ruta}`) |
| `disabled` / `enabled` | segun formato | el flag de encendido/apagado |

## Ubicaciones de config

- Global: `~/.config/opencode/opencode.json` (o `opencode.jsonc`).
- Proyecto: `opencode.json` en la raiz del proyecto (se mergea sobre la global; una prueba local confirmo que un proyecto puede anadir servidores propios).
- Secretos: nunca valores literales en la config si hay `{file:./.secrets/...}` disponibles; es el patron del usuario.

## CLI `opencode mcp` (verificado con --help)

```bat
opencode mcp list            :: estado de todos: connected / disabled / failed
opencode mcp add <nombre>    :: crear entrada en la config
    --url <url>              :: servidor remote
    --env KEY=VALUE          :: (repetible) variables para local
    --header KEY=VALUE       :: (repetible) cabeceras para remote
opencode mcp auth <nombre>   :: OAuth de servidores que lo soportan
opencode mcp logout <nombre> :: borrar credenciales OAuth
opencode mcp debug <nombre>  :: depurar conexion OAuth
```

No existe `opencode mcp disable/enable`: el toggle se hace editando la config (script `../scripts/toggle_mcp.py` o edicion manual).

## Efecto de los cambios

Cambiar el flag en el JSON no reconecta/desconecta magicamente una sesion en curso en todos los casos: si tras `opencode mcp list` el estado no cuadra, reinicia la sesion (o la app). El ahorro de tokens se nota en los MENSAJES siguientes a la desactivacion.

**Leccion verificada en sesion real (opencode 1.18.x / 2.x):** las tools de un MCP se inyectan en el system prompt **al arrancar la sesion**. Si activas un MCP a mitad de sesion, `opencode mcp list` lo mostrara `connected` pero el agente NO podra llamar sus tools hasta reiniciar la sesion (o abrir una nueva). Corolario: para solo INSPECCIONAR que tools tiene un MCP no hace falta activarlo — sondéalo directamente por JSON-RPC con `../scripts/probe_mcp.py` (ver `probe-jsonrpc.md`).

## Ocultar tools sin apagar el servidor

En `opencode.json` (ejemplo ilustrativo con un servidor cualquiera del inventario descubierto):

```json
{
  "tools": {
    "mi-servidor_*": false
  }
}
```

El patron `<servidor>_*` deshabilita las tools de ese MCP en el prompt mientras el servidor sigue definido. Apagar (`disabled`/`enabled:false`) es mas radical: ni se conecta. Usa ocultar solo si otra integracion necesita el servidor vivo.
