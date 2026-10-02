#!/usr/bin/env python3
"""Sondear las tools de un servidor MCP definido en opencode.json SIN activarlo.

Evita el infierno de quoting de sondear tools/list a mano con curl/PowerShell:
resuelve el servidor desde la config (formato anidado mcp.servers.<n> o plano
mcp.<n>), sustituye las referencias {file:./...} de secretos en runtime (NUNCA
las imprime) y hace el handshake JSON-RPC completo hasta tools/list.

Protocolo, transportes y cache viven en mcp_common.py (McpSession).

  - Servidor remoto (url):    handshake Streamable HTTP
                              initialize -> notifications/initialized -> tools/list.
                              Captura Mcp-Session-Id si el servidor lo devuelve;
                              tolera respuestas SSE y servidores stateless
                              (si tools/list falla con sesion, reintenta sin ella).
  - Servidor local (command): lanza el proceso por stdio newline-delimited y
                              LO MATA al terminar.
                              Timeout generoso por defecto (npx puede descargar).
  - Despues de cada tools/list real escribe la cache de schemas en
    .cache/<nombre>.json; con --from-cache responde desde ella SIN arrancar el
    servidor (0 s, 0 red).

Uso:
  python probe_mcp.py <nombre> [--config <ruta>] [--timeout <seg>]
                      [--from-cache] [--max-age <h>] [--refresh]

Exit codes: 0 = tools listadas; 1 = fallo de sondio (red, proceso, JSON-RPC
error); 2 = error de uso/config (no existe la config o el servidor, o
--from-cache sin cache valida y sin --refresh).

Sin dependencias fuera de stdlib.
Detalle del protocolo y troubleshooting Windows: references/probe-jsonrpc.md
"""

import argparse
import os
import sys
import time

# Reutilizar el nucleo comun de los scripts MCP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402,E402
    CLIENT_INFO,
    PROTOCOL_VERSION,
    McpError,
    ProbeError,
    default_config_path,
    load_server_or_die,
    parse_jsonrpc_body,
    print_tools,
    read_cache,
    resolve_refs,
    rpc_message,
    server_kind,
    server_target,
    write_cache,
    McpSession,
)


def main():
    parser = argparse.ArgumentParser(
        description="Sondear tools/list de un servidor MCP de opencode.json sin activarlo.")
    parser.add_argument("server", help="Nombre del servidor tal como esta definido en la config")
    parser.add_argument("--config", default=default_config_path(),
                        help="Ruta a opencode.json (por defecto: global de OpenCode)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="Segundos por operacion HTTP / respuesta del proceso local (defecto 60)")
    parser.add_argument("--from-cache", action="store_true", dest="from_cache",
                        help="Responder desde .cache/<nombre>.json sin arrancar el servidor")
    parser.add_argument("--max-age", type=float, default=24.0, metavar="HORAS",
                        help="Edad maxima aceptada de la cache con --from-cache (defecto 24)")
    parser.add_argument("--refresh", action="store_true",
                        help="Con --from-cache: si la cache falta o es vieja, sondear en vivo y refrescarla")
    args = parser.parse_args()

    # Via rapida: cache fresca = 0 arranques, 0 red
    if args.from_cache:
        data, motivo = read_cache(args.server, args.max_age)
        if data is not None:
            edad_h = (time.time() - float(data.get("timestamp", 0))) / 3600.0
            print(f"[caché] '{args.server}' leido de .cache/{args.server}.json "
                  f"(snapshot de {edad_h:.1f} h atras; servidor NO consultado)")
            print_tools(data.get("tools", []))
            print("(Datos leidos de la cache local: el servidor NO fue consultado; "
                  "usa --refresh o quita --from-cache para un snapshot fresco.)")
            return 0
        if not args.refresh:
            print(f"ERROR: {motivo}; refresca sondeando en vivo (quita --from-cache "
                  f"o anade --refresh).", file=sys.stderr)
            return 2

    server, config_dir = load_server_or_die(args.server, args.config)
    kind = server_kind(server)
    target = server_target(server)
    print(f"Sondeando '{args.server}' ({kind}: {target}) — sin tocar la config...")

    try:
        with McpSession(server, config_dir, args.timeout) as session:
            session.start()
            tools = session.list_tools()
    except McpError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print_tools(tools)
    write_cache(args.server, session.server_info, session.capabilities,
                tools, None, None)
    print("(El MCP NO se activo: sus tools no estaran en esta sesion hasta "
          "toggle on + reinicio de sesion. Usa toggle_mcp.py on <nombre> solo "
          "si vas a USARLAS.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
