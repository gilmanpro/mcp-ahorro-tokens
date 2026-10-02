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
  python toggle_mcp.py off --all [--except n1,n2] [--config <ruta>]
  python toggle_mcp.py status [--config <ruta>]

'off --all' apaga TODOS los servidores definidos en la config (se usa al
cargar la skill para garantizar coste cero de tokens de MCP en la sesion).
Es idempotente: los que ya estaban apagados NO se tocan y se reportan como
"ya estaba apagado". '--except' excluye servidores (para los "permanentes"
que marco el usuario). Exit 0 si todos quedan apagados, 2 en error de config.

Sin dependencias fuera de stdlib. Respeta la estructura del JSON existente
(no reordena claves, no cambia de formato) y NO toca las referencias
{file:...} de secretos (quedan intactas al reescribir el JSON).
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


def write_config(path, cfg) -> None:
    """Reescribe la config conservando el orden de claves y las referencias
    {file:...} de secretos (json.load/dump no las interpreta)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def off_all(cfg, infos, excluded) -> tuple:
    """Apaga todos los servidores de la config (idempotente).

    Devuelve (lineas_de_resumen, changed, restantes_encendidos).
    """
    lines, changed = [], False
    for name in sorted(infos):
        if name in excluded:
            lines.append(f"  {name}: excluido por --except (no se toca)")
            continue
        data, _path, flag, off_value = find_server(cfg, name)
        if infos[name][2]:  # ya estaba apagado: NO se toca
            lines.append(f"  {name}: ya estaba apagado")
            continue
        data[flag] = off_value  # disabled=True / enabled=False segun formato
        lines.append(f"  {name}: apagado")
        changed = True
    # Verificacion POST-cambio sobre la config ya modificada (no sobre infos viejo)
    restantes = [n for n, (_p, _f, off) in sorted(list_servers(cfg).items())
                 if not off and n not in excluded]
    return lines, changed, restantes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["on", "off", "status"])
    parser.add_argument("server", nargs="?", help="Nombre del servidor MCP")
    parser.add_argument("--all", action="store_true",
                        help="con 'off': apagar TODOS los servidores de la config")
    parser.add_argument("--except", dest="except_names", metavar="N1,N2",
                        help="con 'off --all': nombres a excluir (permanentes), "
                             "separados por comas")
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

    if args.all and args.action != "off":
        print("ERROR: --all solo aplica a 'off' (off --all).", file=sys.stderr)
        return 2
    if args.except_names and not (args.action == "off" and args.all):
        print("ERROR: --except solo aplica a 'off --all'.", file=sys.stderr)
        return 2

    if args.action == "off" and args.all:
        if args.server:
            print("ERROR: 'off --all' no acepta nombre de servidor; "
                  "usa --except para excluir.", file=sys.stderr)
            return 2
        excluded = {n.strip() for n in (args.except_names or "").split(",")
                    if n.strip()}
        desconocidos = excluded - set(infos)
        for n in sorted(desconocidos):
            print(f"AVISO: '{n}' de --except no existe en la config, se ignora.",
                  file=sys.stderr)
        if not infos:
            print("No hay servidores MCP definidos en la config.")
            return 0
        lines, changed, restantes = off_all(cfg, infos, excluded)
        if changed:
            write_config(args.config, cfg)
        print(f"off --all ({args.config}):")
        for line in lines:
            print(line)
        if restantes:
            print("ERROR: siguen encendidos: " + ", ".join(restantes),
                  file=sys.stderr)
            return 2
        n_off = sum(1 for l in lines if "apagado" in l and "ya estaba" not in l)
        n_ya = sum(1 for l in lines if "ya estaba apagado" in l)
        print(f"RESUMEN: {n_off} apagados, {n_ya} ya estaban apagados"
              + (f", {len(excluded) - len(desconocidos)} excluidos"
                 if excluded - desconocidos else "") + ".")
        if changed:
            print("Verifica con: opencode mcp list "
                  "(las tools dejan de inyectarse al reiniciar la sesion).")
        return 0

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

    write_config(args.config, cfg)

    estado = "desactivado" if want_off else "activado"
    print(f"OK: '{args.server}' queda {estado} ({path}.{flag} = {data[flag]}).")
    print("Verifica con: opencode mcp list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
