#!/usr/bin/env python3
"""Auto-refresco de la cache de schemas MCP: SIN sesion de opencode y SIN gastar tokens.

Via PRIMARIA (recomendada): el plugin global de opencode
~/.config/opencode/plugins/mcp-refresco-cache.js lanza ESTE script en segundo
plano (detached, con el pythonw.exe real) al ARRANCAR opencode, con
throttle/lock en <skill>\\.tmp\\ultimo_refresco.json (defecto: 6 h entre
refrescos exitosos). El plugin no registra hook alguno => no inyecta texto en
prompts ni sesiones => coste de tokens CERO.

Alternativa OPCIONAL (sin opencode): ejecutarlo al iniciar Windows mediante la
tarea programada MCP-AhorroTokens-RefrescoCache, registrada con
scripts\\registrar_tarea.ps1 y lanzada con pythonw.exe.

En ambos casos refresca .cache\\<server>.json sondeando cada servidor MCP
definido en la config de OpenCode, 100% por fuera de la sesion de opencode:

  - NO abre ni reinicia sesion de opencode, NO escribe en su config, NO cambia
    'enabled'/'disabled' de nadie y NO inyecta tool alguna en el prompt: coste
    de tokens CERO. Solo negocia JSON-RPC directo contra cada servidor
    (handshake initialize + tools/list) usando McpSession de mcp_common.py —
    remotos Streamable HTTP y locales stdio/npx, igual que audit_mcp.py.
  - Con la cache fresca, scripts como 'audit_mcp.py <srv> --show-tools' o
    'probe_mcp.py <srv> --from-cache' responden los schemas SIN red.

Comportamiento:
  - Lee la seccion mcp de la config (defecto: ~/.config/opencode/opencode.json;
    acepta los dos formatos: anidado mcp.servers.<n> y plano mcp.<n>).
  - Para CADA servidor (o solo los de --solo): tools/list y escritura de
    .cache\\<server>.json con timestamp nuevo. NO consulta resources/prompts
    (mas rapido; se dejan sin consultar en la cache).
  - --timeout <seg> POR SERVIDOR (defecto 60). Si uno falla (caido, timeout,
    proceso roto) se anota FALLO y se CONTINUA con el siguiente.
  - Log APPEND (nunca overwrite) a <skill>\\.tmp\\refresco_cache.log: cabecera
    con fecha, una linea por servidor y RESUMEN final con duracion. Es el
    unico rastro visible cuando corre sin consola (pythonw).
  - NUNCA imprime secretos: las URLs con credenciales se enmascaran
    (scheme://***@host) — tambien dentro de los mensajes de error de red que
    las contengan — y las cabeceras/variables {file:...} resueltas no se
    imprimen jamas.

Uso:
  python refrescar_cache.py [--config <ruta>] [--timeout 60]
  python refrescar_cache.py --solo wsl-port context7
  python refrescar_cache.py --verificar   (sin red: edad/herramientas por cache local)

Exit codes: 0 = al menos un servidor refrescado; 1 = error de config/uso
(config ilegible, sin servidores, --solo con nombres inexistentes);
2 = todos los servidores fallaron el sondeo. Con --verificar: 0 informativo
(tambien si faltan caches; muestra el estado), 1 si la config es ilegible.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    McpError,
    default_config_path,
    find_server,
    list_servers,
    load_config,
    server_kind,
    write_cache,
    cache_path,
    McpSession,
)

LOG_NAME = "refresco_cache.log"
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def salir_utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def log_path():
    """<skill>\\.tmp\\refresco_cache.log (regla: temporales dentro del proyecto)."""
    d = os.path.join(SKILL_ROOT, ".tmp")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return os.path.join(d, LOG_NAME)


# ------------------------------------------------------------------ secretos -

def mask_url(url):
    """Enmascara credenciales de una URL: scheme://user:pass@host/... ->
    scheme://***@host (sin query ni fragment: podrian llevar tokens)."""
    if not url:
        return url
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return "(url no parseable)"
    if p.username is None and p.password is None:
        return url
    host = p.netloc.split("@", 1)[-1]
    return urllib.parse.urlunsplit((p.scheme, "***@" + host, p.path, "", ""))


def safe_msg(err, url):
    """Mensaje de error con la URL original reemplazada por la enmascarada."""
    msg = str(err)
    if url:
        masked = mask_url(url)
        if url != masked:
            msg = msg.replace(url, masked)
    return msg


def target_label(kind, data):
    """Descripcion del destino SIN secretos: URL enmascarada o comando local."""
    if kind == "remote":
        return mask_url(data.get("url") or "")
    cmd = data.get("command") or []
    return " ".join(str(c) for c in cmd) if cmd else "(sin command)"


# ------------------------------------------------------------------ refresco --

def refresh_one(name, data, config_dir, timeout):
    """Sondea UN servidor (1 arranque: initialize + tools/list) y escribe cache.

    Devuelve (ok, mensaje_corto_sin_secretos, duracion_s). Nunca lanza al
    llamador: todo fallo se anota y el lote continua.
    """
    url = data.get("url") or ""
    t0 = time.time()
    try:
        with McpSession(data, config_dir, timeout) as session:
            session.start()
            tools = session.list_tools()
        path = write_cache(name, session.server_info, session.capabilities,
                           tools, None, None)
        dur = time.time() - t0
        if path is None:
            return False, f"tools/list OK ({len(tools)}) pero no se pudo escribir .cache\\{name}.json", dur
        return True, f"{len(tools)} tools en {dur:.1f}s -> .cache\\{name}.json", dur
    except McpError as err:
        return False, safe_msg(err, url)[:300], time.time() - t0
    except Exception as err:  # red/proceso inesperado: el lote no se cae
        return False, f"{type(err).__name__}: {safe_msg(err, url)}"[:300], time.time() - t0


def escribir_log(lineas, resumen):
    """Append del bloque de refresco al log de la skill. Devuelve aviso o None."""
    try:
        with open(log_path(), "a", encoding="utf-8") as fh:
            fh.write("=== " + time.strftime("%Y-%m-%d %H:%M:%S")
                     + " | refrescar_cache ===\n")
            for l in lineas:
                fh.write(l + "\n")
            fh.write(resumen + "\n\n")
        return None
    except OSError as err:
        return f"AVISO: no se pudo escribir el log ({log_path()}): {err}"


# ----------------------------------------------------------------- verificar --

def verificar(infos):
    """Estado de la cache LOCAL sin red: edad y n° de tools por servidor."""
    print("verificar cache (lectura local, sin red):")
    for name in sorted(infos):
        path = cache_path(name)
        if not os.path.isfile(path):
            print(f"  {name:<20} SIN CACHE — refresca: python scripts\\refrescar_cache.py --solo {name}")
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as err:
            print(f"  {name:<20} CACHE ILEGIBLE: {err}")
            continue
        edad_s = time.time() - float(data.get("timestamp", 0))
        n = len(data.get("tools") or [])
        if edad_s < 3600:
            edad = f"{edad_s / 60:.0f} min"
        else:
            edad = f"{edad_s / 3600:.1f} h"
        fecha = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data.get("timestamp", 0)))
        print(f"  {name:<20} OK — {n} tools | snapshot {fecha} (hace {edad})")
    return 0


# ----------------------------------------------------------------------- main -

def main(argv=None):
    salir_utf8()
    parser = argparse.ArgumentParser(
        description="Refrescar la cache de schemas MCP (.cache\\<server>.json) "
                    "sondeando cada servidor fuera de opencode: cero tokens.")
    parser.add_argument("--config", default=default_config_path(),
                        help="Ruta a opencode.json (por defecto: global de OpenCode)")
    parser.add_argument("--timeout", type=int, default=60, metavar="SEG",
                        help="Segundos POR SERVIDOR para el handshake+tools/list "
                             "(defecto 60; los locales npx pueden tardar mas)")
    parser.add_argument("--solo", nargs="+", metavar="NOMBRE", default=None,
                        help="Refrescar SOLO estos servidores (por defecto: todos)")
    parser.add_argument("--verificar", action="store_true", dest="verificar",
                        help="No tocar la red: mostrar el estado de la cache local "
                             "(edad y n° de tools por servidor)")
    ns = parser.parse_args(argv)

    if not os.path.isfile(ns.config):
        print(f"ERROR: no existe la config: {ns.config}", file=sys.stderr)
        return 1
    try:
        cfg = load_config(ns.config)
    except json.JSONDecodeError as err:
        print(f"ERROR: la config no es JSON valido: {err}", file=sys.stderr)
        return 1

    infos = list_servers(cfg)
    if not infos:
        print("ERROR: no hay servidores MCP definidos en la config.", file=sys.stderr)
        return 1

    if ns.verificar:
        return verificar(infos)

    seleccion = ns.solo or sorted(infos)
    desconocidos = [n for n in seleccion if n not in infos]
    for n in desconocidos:
        print(f"AVISO: '{n}' de --solo no existe en la config, se ignora.",
              file=sys.stderr)
    seleccion = [n for n in seleccion if n in infos]
    if not seleccion:
        print("ERROR: ningun servidor seleccionado para refrescar.", file=sys.stderr)
        return 1

    config_dir = os.path.dirname(os.path.abspath(ns.config))
    print(f"[refresco] {len(seleccion)} servidor(es), timeout {ns.timeout}s por "
          f"servidor, sin opencode y sin tocar la config...")
    lineas = []
    ok_n = 0
    t_total = time.time()
    for name in seleccion:
        data, _ruta, _flag, _off = find_server(cfg, name)
        kind = server_kind(data)
        label = target_label(kind, data)
        ok, msg, dur = refresh_one(name, data, config_dir, ns.timeout)
        estado = "OK" if ok else "FALLO"
        if ok:
            ok_n += 1
        linea = f"  {name}: {estado} — {msg} ({kind}: {label})"
        lineas.append(linea)
        print(linea)

    dur_total = time.time() - t_total
    fail_n = len(seleccion) - ok_n
    resumen = (f"RESUMEN: {ok_n} OK, {fail_n} fallidos, "
               f"duracion {dur_total:.1f}s (timeout {ns.timeout}s/servidor)")
    print(resumen)
    aviso = escribir_log(lineas, resumen)
    if aviso:
        print(aviso, file=sys.stderr)
    else:
        print(f"log: {log_path()}")

    if ok_n == 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
