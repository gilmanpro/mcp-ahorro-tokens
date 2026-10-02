#!/usr/bin/env python3
"""Activar o desactivar un servidor MCP en la configuracion de OpenCode.

Soporta las dos sintaxis verificadas contra el binario instalado:
  - mcp.servers.<nombre>.disabled: true|false   (formato anidado, el que usa
    el opencode.json global de este usuario)
  - mcp.<nombre>.enabled: true|false            (formato plano, doc estable)

La resolucion de config vive ahora en mcp_common.py (compartida con
probe_mcp.py, call_mcp.py y audit_mcp.py); esta CLI no cambia.

Uso:
  python toggle_mcp.py on  <nombre> [--config <ruta>]
  python toggle_mcp.py off <nombre> [--config <ruta>]
  python toggle_mcp.py status [--config <ruta>]

Sin dependencias fuera de stdlib. Respeta la estructura del JSON existente
(no reordena claves, no cambia de formato).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    default_config_path,
    find_server,
    list_servers,
    load_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["on", "off", "status"])
    parser.add_argument("server", nargs="?", help="Nombre del servidor MCP")
    parser.add_argument("--config", default=default_config_path(),
                        help="Ruta a opencode.json (por defecto: global de OpenCode)")
    args = parser.parse_args()

    if not os.path.isfile(args.config):
        print(f"ERROR: no existe la config: {args.config}", file=sys.stderr)
        return 2

    try:
        cfg = load_config(args.config)
    except json.JSONDecodeError as err:
        print(f"ERROR: la config no es JSON valido: {err}", file=sys.stderr)
        return 2

    infos = list_servers(cfg)

    if args.action == "status":
        if not infos:
            print("No hay servidores MCP definidos en la config.")
            return 0
        print("Servidores MCP en", args.config)
        for name, (_, _, off) in sorted(infos.items()):
            print(f"  {name:<20} {'APAGADO (disabled)' if off else 'ENCENDIDO'}")
        return 0

    if not args.server:
        print("ERROR: falta el nombre del servidor.", file=sys.stderr)
        return 2

    found = find_server(cfg, args.server)
    if found is None:
        print(f"ERROR: servidor '{args.server}' no existe en {args.config}.", file=sys.stderr)
        if infos:
            print("Existentes: " + ", ".join(sorted(infos)), file=sys.stderr)
        return 2

    data, path, flag, _ = found
    want_off = args.action == "off"
    # Flag 'disabled': True = apagado. Flag 'enabled': False = apagado.
    data[flag] = want_off if flag == "disabled" else (not want_off)

    with open(args.config, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    estado = "desactivado" if want_off else "activado"
    print(f"OK: '{args.server}' queda {estado} ({path}.{flag} = {data[flag]}).")
    print("Verifica con: opencode mcp list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
