#!/usr/bin/env python3
r"""Activar o desactivar un servidor MCP en la configuracion de OpenCode.

Soporta las dos sintaxis verificadas contra el binario instalado:
  - mcp.servers.<nombre>.disabled: true|false   (formato anidado, el que usa
    el opencode.json global de este usuario)
  - mcp.<nombre>.enabled: true|false            (formato plano, doc estable)

La resolucion de config vive ahora en mcp_common.py (compartida con
probe_mcp.py, call_mcp.py y audit_mcp.py); esta CLI no cambia.

Uso:
  python toggle_mcp.py on  <nombre> [--config <ruta>]
  python toggle_mcp.py off <nombre> [--config <ruta>]
  python toggle_mcp.py off --all [--except n1,n2] [--no-verify] [--bg]
                             [--config <ruta>]
  python toggle_mcp.py verify [--except n1,n2] [--bg-log [ruta]]
                             [--config <ruta>]
  python toggle_mcp.py status [--config <ruta>]

'off --all' apaga TODOS los servidores definidos en la config (se usa al
cargar la skill para garantizar coste cero de tokens de MCP en la sesion).
Es idempotente: los que ya estaban apagados NO se tocan y se reportan como
"ya estaba apagado". '--except' excluye servidores (para los "permanentes"
que marco el usuario).

Verificacion integrada (--verify, ACTIVA por defecto; --no-verify la desactiva):
tras escribir la config se RELEE el JSON desde disco y se comprueba que todos
los servidores (menos los de --except) esten apagados. Si alguno no, se
reintenta la escritura UNA vez; si sigue mal, exit 1 con "X no quedo apagado
tras reintentar". Al exito imprime "VERIFICADO: N servidores apagados".

Modo segundo plano (--bg): 'off --all --bg' relanza el propio script DETACHED
(Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) con --verify forzado y
retorna INMEDIATAMENTE (exit 0). El hijo escribe su salida en
%TEMP%\mcp-off-all.log (sobrescrito en cada ejecucion; cabecera con timestamp,
resumen por servidor y linea final VERIFICADO/FALLO). El hijo recibe la ruta
del log via la variable de entorno MCP_OFF_ALL_LOG.

'verify': lectura pura del JSON (instantaneo, sin red) que imprime una linea
por servidor ("X: apagado" / "X: ENCENDIDO") + resumen. Exit 0 si todos
apagados, 1 si alguno encendido, 2 error de config/uso. Con --bg-log muestra
la ultima linea del log del ultimo off --all en segundo plano como evidencia.

Exit codes globales: 0 OK, 1 verificacion fallida, 2 error de uso/config.

Sin dependencias fuera de stdlib. Respeta la estructura del JSON existente
(no reordena claves, no cambia de formato) y NO toca las referencias
{file:...} de secretos (quedan intactas al reescribir el JSON).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    default_config_path,
    find_server,
    list_servers,
    load_config,
)

BG_LOG_NAME = "mcp-off-all.log"


def bg_log_path():
    """Ruta del log del off --all en segundo plano (%TEMP%\\mcp-off-all.log)."""
    return os.path.join(tempfile.gettempdir(), BG_LOG_NAME)


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


def verify_disk_off(config_path, excluded) -> tuple:
    """Relee la config desde disco y comprueba el estado real.

    Devuelve (encendidos_no_excluidos, total_no_excluidos). Lanza
    OSError/json.JSONDecodeError si la config es ilegible.
    """
    infos = list_servers(load_config(config_path))
    encendidos = [n for n, (_p, _f, off) in sorted(infos.items())
                  if not off and n not in excluded]
    total = sum(1 for n in infos if n not in excluded)
    return encendidos, total


def parse_excluded(raw):
    """Convierte 'n1,n2' en un set de nombres (sin espacios)."""
    return {n.strip() for n in (raw or "").split(",") if n.strip()}


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["on", "off", "status", "verify"])
    parser.add_argument("server", nargs="?", help="Nombre del servidor MCP")
    parser.add_argument("--all", action="store_true",
                        help="con 'off': apagar TODOS los servidores de la config")
    parser.add_argument("--except", dest="except_names", metavar="N1,N2",
                        help="con 'off --all' y 'verify': nombres a excluir "
                             "(permanentes), separados por comas")
    parser.add_argument("--verify", dest="verify", action="store_true", default=True,
                        help="con 'off --all' (ACTIVO por defecto): releer la "
                             "config tras escribir y reintentar una vez si algo "
                             "no quedo apagado")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="con 'off --all': omitir la verificacion post-escritura")
    parser.add_argument("--bg", action="store_true",
                        help="con 'off --all': relanzarse en segundo plano "
                             "(detached) y retornar de inmediato; el hijo "
                             "escribe en %%TEMP%%\\mcp-off-all.log")
    parser.add_argument("--bg-log", dest="bg_log", nargs="?", const=bg_log_path(),
                        metavar="RUTA",
                        help="con 'verify': mostrar la ultima linea del log del "
                             "off --all en segundo plano (defecto: "
                             "%%TEMP%%\\mcp-off-all.log)")
    parser.add_argument("--config", default=default_config_path(),
                        help="Ruta a opencode.json (por defecto: global de OpenCode)")
    args = parser.parse_args()

    if args.all and args.action != "off":
        print("ERROR: --all solo aplica a 'off' (off --all).", file=sys.stderr)
        return 2
    if args.except_names and not ((args.action == "off" and args.all)
                                  or args.action == "verify"):
        print("ERROR: --except solo aplica a 'off --all' y 'verify'.",
              file=sys.stderr)
        return 2
    if args.bg and not (args.action == "off" and args.all):
        print("ERROR: --bg solo aplica a 'off --all'.", file=sys.stderr)
        return 2
    if args.bg_log is not None and args.action != "verify":
        print("ERROR: --bg-log solo aplica a 'verify'.", file=sys.stderr)
        return 2

    # -- Modo segundo plano: relanzarse detached y salir YA (exit 0) ----------
    if args.bg:
        log = bg_log_path()
        cmd = [sys.executable, os.path.abspath(__file__), "off", "--all",
               "--verify", "--config", os.path.abspath(args.config)]
        if args.except_names:
            cmd += ["--except", args.except_names]
        creationflags = 0
        if os.name == "nt":
            creationflags = (subprocess.DETACHED_PROCESS
                             | subprocess.CREATE_NEW_PROCESS_GROUP)
        env = dict(os.environ, MCP_OFF_ALL_LOG=log)
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=creationflags, env=env)
        except OSError as err:
            print(f"ERROR: no se pudo lanzar el proceso en segundo plano: {err}",
                  file=sys.stderr)
            return 2
        print("off --all lanzado en segundo plano; verificar con: "
              "python scripts\\toggle_mcp.py verify")
        print(f"log: {log}")
        return 0

    if not os.path.isfile(args.config):
        print(f"ERROR: no existe la config: {args.config}", file=sys.stderr)
        return 2

    try:
        cfg = load_config(args.config)
    except json.JSONDecodeError as err:
        print(f"ERROR: la config no es JSON valido: {err}", file=sys.stderr)
        return 2

    infos = list_servers(cfg)

    # -- off --all ------------------------------------------------------------
    if args.action == "off" and args.all:
        if args.server:
            print("ERROR: 'off --all' no acepta nombre de servidor; "
                  "usa --except para excluir.", file=sys.stderr)
            return 2
        excluded = parse_excluded(args.except_names)
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
        n_off = sum(1 for l in lines if "apagado" in l and "ya estaba" not in l)
        n_ya = sum(1 for l in lines if "ya estaba apagado" in l)
        print(f"RESUMEN: {n_off} apagados, {n_ya} ya estaban apagados"
              + (f", {len(excluded) - len(desconocidos)} excluidos"
                 if excluded - desconocidos else "") + ".")
        if changed:
            print("Verifica con: opencode mcp list "
                  "(las tools dejan de inyectarse al reiniciar la sesion).")

        if not args.verify:
            # Comportamiento historico: chequeo en memoria, exit 2 si algo sigue encendido
            if restantes:
                print("ERROR: siguen encendidos: " + ", ".join(restantes),
                      file=sys.stderr)
                return 2
            return 0

        # Verificacion integrada: releer el JSON desde disco, UN reintento
        try:
            encendidos, total = verify_disk_off(args.config, excluded)
        except (OSError, json.JSONDecodeError) as err:
            print(f"ERROR: la config releida desde disco no es util: {err}",
                  file=sys.stderr)
            print("FALLO: verificacion imposibile (config ilegible)",
                  file=sys.stderr)
            return 2
        if encendidos:
            print(f"AVISO: siguen encendidos tras escribir "
                  f"({', '.join(encendidos)}); reintentando escritura una vez...",
                  file=sys.stderr)
            try:
                cfg2 = load_config(args.config)
                off_all(cfg2, list_servers(cfg2), excluded)
                write_config(args.config, cfg2)
                encendidos, total = verify_disk_off(args.config, excluded)
            except (OSError, json.JSONDecodeError) as err:
                print(f"ERROR: la config releida desde disco no es util: {err}",
                      file=sys.stderr)
                print("FALLO: verificacion imposibile (config ilegible)",
                      file=sys.stderr)
                return 2
        if encendidos:
            for n in encendidos:
                print(f"ERROR: {n} no quedó apagado tras reintentar",
                      file=sys.stderr)
            print("FALLO: " + ", ".join(encendidos) + " no quedaron apagados",
                  file=sys.stderr)
            return 1
        print(f"VERIFICADO: {total} servidores apagados")
        return 0

    # -- verify (lectura pura, instantanea, sin red) ---------------------------
    if args.action == "verify":
        if args.server:
            print("ERROR: 'verify' no acepta nombre de servidor; "
                  "usa --except para excluir.", file=sys.stderr)
            return 2
        excluded = parse_excluded(args.except_names)
        desconocidos = excluded - set(infos)
        for n in sorted(desconocidos):
            print(f"AVISO: '{n}' de --except no existe en la config, se ignora.",
                  file=sys.stderr)
        if not infos:
            print("No hay servidores MCP definidos en la config.")
            return 0
        print(f"verify ({args.config}):")
        encendidos = []
        for name, (_p, _f, off) in sorted(infos.items()):
            if name in excluded:
                print(f"  {name}: excluido por --except")
            elif off:
                print(f"  {name}: apagado")
            else:
                print(f"  {name}: ENCENDIDO")
                encendidos.append(name)
        print(f"RESUMEN: {len(infos) - len(excluded & set(infos)) - len(encendidos)} "
              f"apagados, {len(encendidos)} encendidos"
              + (f", {len(excluded - desconocidos)} excluidos"
                 if excluded - desconocidos else "") + ".")
        if args.bg_log is not None:
            if os.path.isfile(args.bg_log):
                ultima = ""
                try:
                    with open(args.bg_log, "r", encoding="utf-8",
                              errors="replace") as fh:
                        for fila in fh:
                            if fila.strip():
                                ultima = fila.strip()
                except OSError as err:
                    ultima = f"(log ilegible: {err})"
                print(f"evidencia bg ({args.bg_log}): {ultima or '(vacio)'}")
            else:
                print(f"evidencia bg: no existe {args.bg_log}")
        if encendidos:
            return 1
        return 0

    # -- status -----------------------------------------------------------------
    if args.action == "status":
        if not infos:
            print("No hay servidores MCP definidos en la config.")
            return 0
        print("Servidores MCP en", args.config)
        for name, (_, _, off) in sorted(infos.items()):
            print(f"  {name:<20} {'APAGADO (disabled)' if off else 'ENCENDIDO'}")
        return 0

    # -- on/off de un servidor --------------------------------------------------
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


def main() -> int:
    """Punto de entrada: si MCP_OFF_ALL_LOG esta definido (hijo de --bg),
    redirige stdout/stderr al log (sobrescrito, con cabecera y timestamp) y
    deja como linea final VERIFICADO o FALLO segun el resultado."""
    log_path = os.environ.get("MCP_OFF_ALL_LOG")
    log_fh = None
    if log_path:
        try:
            log_fh = open(log_path, "w", encoding="utf-8", buffering=1)
            log_fh.write(f"=== off --all en segundo plano | inicio: "
                         f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            log_fh.flush()
            sys.stdout = sys.stderr = log_fh
        except OSError:
            log_fh = None
    rc = run()
    if log_fh is not None:
        log_fh.flush()
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        log_fh.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
