#!/usr/bin/env python3
"""Invocar UNA tool de un servidor MCP definido en opencode.json SIN activarlo.

Tercera via junto a probe_mcp.py (inspeccionar) y toggle_mcp.py (activar):
para una ACCION PUNTUAL (enviar un correo, crear un registro...) NO hace
falta encender el MCP y reiniciar la sesion pagando sus tools en cada
mensaje: se hace el handshake completo y un tools/call directo, con coste
cero de prompt.

Protocolo, transportes y cache viven en mcp_common.py (McpSession).

  - Servidor remoto (url):    handshake Streamable HTTP
                                initialize -> notifications/initialized ->
                                tools/list (validar) -> tools/call.
                                Captura Mcp-Session-Id si el servidor lo
                                devuelve; tolera respuestas SSE y servidores
                                stateless (si una peticion falla con sesion,
                                reintenta sin ella).
  - Servidor local (command): lanza el proceso por stdio newline-delimited,
                                envia los mensajes y LO MATA al terminar.
                                Timeout generoso (npx puede descargar).

SI Vas a hacer VARIAS llamadas al mismo servidor, no encadenes este script
(cada ejecucion relanza el proceso: 15-20 s en locales npx): usa
audit_mcp.py --batch o --call repetido, que hace 1 arranque y N llamadas.

Uso:
  python call_mcp.py <nombre> --list [--config <ruta>] [--timeout <seg>]
                                        [--from-cache] [--max-age <h>] [--refresh]
  python call_mcp.py <nombre> <tool> [--args '{"clave":"valor"}']
                                     [--args-file payload.json]
                                     [--config <ruta>] [--timeout <seg>]

En Windows el quoting JSON inline es frágil (cmd/PowerShell corrompen las
comillas): prefiere --args-file (lectura UTF-8) para payloads reales.

El tool name se valida contra tools/list: si no existe, se imprime la lista
de tools disponibles y se sale con 2.

Exit codes: 0 = tool invocada con exito (isError false); 1 = fallo de
sondio/invocacion (red, proceso, JSON-RPC error, la tool respondio
isError:true); 2 = error de uso/config (config o servidor inexistente,
JSON de argumentos invalido, tool no encontrada, --from-cache sin cache
valida y sin --refresh).

Sin dependencias fuera de stdlib. Detalle del protocolo:
references/probe-jsonrpc.md
"""

import argparse
import json
import os
import sys

# Reutilizar el nucleo comun de los scripts MCP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    McpError,
    ProbeError,
    default_config_path,
    load_server_or_die,
    print_tools,
    read_cache,
    server_kind,
    server_target,
    write_cache,
    McpSession,
)


class ToolNotFound(Exception):
    """La tool pedida no aparece en tools/list (se comprueba ANTES del call)."""

    def __init__(self, name, tools):
        super().__init__(name)
        self.name = name
        self.tools = tools


def print_call_result(result):
    """Imprime el resultado de tools/call: content[].text tal cual.

    Nunca se imprimen los argumentos enviados ni secretos de config; solo lo
    que devuelve la tool.
    """
    if "structuredContent" in result:
        print(json.dumps(result["structuredContent"], indent=2, ensure_ascii=False))
    is_error = bool(result.get("isError"))
    for item in result.get("content", []):
        kind = item.get("type")
        if kind == "text":
            print(item.get("text", ""))
        elif kind in ("image", "audio"):
            # No volcar base64 en consola
            print(f"[{kind}: {item.get('mimeType', '?')} omitido]")
        elif kind == "resource":
            print("[resource] " + json.dumps(item.get("resource", {}), ensure_ascii=False)[:500])
        else:
            print(json.dumps(item, ensure_ascii=False)[:500])
    if not result.get("content") and "structuredContent" not in result:
        print("(la tool no devolvio contenido)")
    return is_error


def main():
    # Salida UTF-8 (resultados con tildes/emoji desde consola cp850/cp1252)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Invocar una tool de un servidor MCP de opencode.json sin activarlo "
                    "(tools/call directo por JSON-RPC).")
    parser.add_argument("server", help="Nombre del servidor tal como esta definido en la config")
    parser.add_argument("tool", nargs="?",
                        help="Nombre de la tool a invocar (obligatorio salvo con --list)")
    parser.add_argument("--list", action="store_true", dest="do_list",
                        help="Solo listar tools (equivalente a probe_mcp.py) y salir")
    parser.add_argument("--args", default=None, metavar="JSON",
                        help="Argumentos de la tool como objeto JSON inline "
                             "(frágil en Windows: prefiere --args-file)")
    parser.add_argument("--args-file", default=None, metavar="RUTA",
                        help="Ruta a un archivo JSON (UTF-8) con los argumentos de la tool")
    parser.add_argument("--config", default=default_config_path(),
                        help="Ruta a opencode.json (por defecto: global de OpenCode)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="Segundos por operacion HTTP / respuesta del proceso local (defecto 60)")
    parser.add_argument("--from-cache", action="store_true", dest="from_cache",
                        help="Con --list: responder desde .cache/<nombre>.json sin arrancar el servidor")
    parser.add_argument("--max-age", type=float, default=24.0, metavar="HORAS",
                        help="Edad maxima aceptada de la cache con --from-cache (defecto 24)")
    parser.add_argument("--refresh", action="store_true",
                        help="Con --from-cache: si la cache falta o es vieja, sondear en vivo y refrescarla")
    ns = parser.parse_args()

    want_call = not ns.do_list
    if want_call and not ns.tool:
        print("ERROR: falta el nombre de la tool (o usa --list).", file=sys.stderr)
        return 2
    if ns.args is not None and ns.args_file is not None:
        print("ERROR: usa --args o --args-file, no ambos.", file=sys.stderr)
        return 2
    if ns.from_cache and want_call:
        print("ERROR: --from-cache solo sirve con --list; para INVOCAR una tool "
              "hay que hablar con el servidor (o usa audit_mcp.py).", file=sys.stderr)
        return 2

    tool_args = {}
    if want_call:
        if ns.args_file is not None:
            try:
                with open(ns.args_file, "r", encoding="utf-8-sig") as fh:
                    tool_args = json.load(fh)
            except OSError as err:
                print(f"ERROR: no se pudo leer --args-file: {err}", file=sys.stderr)
                return 2
            except json.JSONDecodeError as err:
                print(f"ERROR: el JSON de --args-file no es valido: {err}", file=sys.stderr)
                return 2
        elif ns.args is not None:
            try:
                tool_args = json.loads(ns.args)
            except json.JSONDecodeError as err:
                print(f"ERROR: el JSON de --args no es valido: {err}", file=sys.stderr)
                print("Pista: en Windows el quoting inline corrompe el JSON; "
                      "guarda los argumentos en un archivo UTF-8 y usa --args-file.",
                      file=sys.stderr)
                return 2
        if not isinstance(tool_args, dict):
            print("ERROR: los argumentos deben ser un objeto JSON {clave:valor}.",
                  file=sys.stderr)
            return 2

    # Via rapida de inventario: --list --from-cache = 0 arranques, 0 red
    if ns.from_cache:
        data, motivo = read_cache(ns.server, ns.max_age)
        if data is not None:
            import time as _time
            edad_h = (_time.time() - float(data.get("timestamp", 0))) / 3600.0
            print(f"[caché] '{ns.server}' leido de .cache/{ns.server}.json "
                  f"(snapshot de {edad_h:.1f} h atras; servidor NO consultado)")
            print_tools(data.get("tools", []))
            print("(El MCP NO se activo: para una accion puntual usa "
                  "call_mcp.py <nombre> <tool> --args-file ...; para varias "
                  "llamadas, audit_mcp.py --batch.)")
            return 0
        if not ns.refresh:
            print(f"ERROR: {motivo}; refresca sondeando en vivo (quita --from-cache "
                  f"o anade --refresh).", file=sys.stderr)
            return 2

    server, config_dir = load_server_or_die(ns.server, ns.config)
    kind = server_kind(server)
    target = server_target(server)

    if want_call:
        print(f"Invocando '{ns.tool}' en '{ns.server}' ({kind}: {target}) — "
              f"sin activar el MCP ni tocar la config...")
    else:
        print(f"Sondeando '{ns.server}' ({kind}: {target}) — sin tocar la config...")

    try:
        with McpSession(server, config_dir, ns.timeout) as session:
            session.start()
            tools = session.list_tools()
            if want_call:
                if ns.tool not in [t.get("name") for t in tools]:
                    raise ToolNotFound(ns.tool, tools)
                result = session.call_tool(ns.tool, tool_args)
    except ToolNotFound as err:
        print(f"ERROR: la tool '{err.name}' no existe en '{ns.server}' "
              f"({len(err.tools)} tools disponibles):", file=sys.stderr)
        print_tools(err.tools)
        return 2
    except McpError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    if not want_call:
        print_tools(tools)
        write_cache(ns.server, session.server_info, session.capabilities,
                    tools, None, None)
        print("(El MCP NO se activo: para una accion puntual usa "
              "call_mcp.py <nombre> <tool> --args-file ...; para uso "
              "interactivo repetido, toggle_mcp.py on + reinicio de sesion.)")
        return 0

    failed = print_call_result(result)
    if failed:
        print("ERROR: la tool respondio isError:true (ver contenido arriba).",
              file=sys.stderr)
        return 1
    print(f"(Tool '{ns.tool}' ejecutada correctamente. El MCP sigue sin activar: "
          f"sus tools no se inyectaron en el prompt.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
