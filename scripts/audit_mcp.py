#!/usr/bin/env python3
"""Auditoria y llamadas EN LOTE contra un servidor MCP: SESION UNICA.

Problema que resuelve (leccion de una auditoria real de 5 servidores):
probe_mcp.py y call_mcp.py relanzan el servidor en CADA ejecucion; en locales
`npx` eso paga ~15-20 s de arranque por llamada y cada --list reimprime todas
las tools (resend = 106 tools -> miles de tokens en el contexto del modelo).

audit_mcp.py hace 1 arranque y N operaciones dentro de la misma sesion:

  - Auditoria completa: handshake + serverInfo + capabilities + tools/list +
    resources/list + prompts/list (lo no soportado se marca como
    "no soportado (-32601)" SIN fallar) + resumen COMPACTO por defecto
    (numero de tools y nombres; descripciones solo con --verbose).
  - --call <tool> --args-file <json>: repetible; ejecuta varias llamadas en
    el MISMO comando, en orden, con una sola sesion.
  - --batch plan.json: plan = lista de {"tool": ..., "args": {...}} o
    {"tool": ..., "args_file": "ruta.json"} -> tabla de resultados
    (OK / ERROR con motivo corto), outputs truncados a --max-output lineas.
  - --out <dir>: con --call/--batch vuelca la salida COMPLETA (JSON o texto,
    sin truncar) de cada llamada en <dir>/<NN>_<tool>.json (NN = indice del
    lote) y deja en stdout solo el estado, la preview corta y la RUTA del
    archivo. Asi un lote de estatus NO obliga a re-llamar tool por tool con
    call_mcp.py para leer el detalle. Sin --out el comportamiento es
    exactamente el de siempre (retrocompatible). El dir se crea si falta;
    usa rutas DENTRO de la skill (p. ej. .tmp/resultados).
  - --max-output <lineas>: controla el detalle impreso en stdout por
    resultado. Con N>0 (y --verbose) sigue siendo N lineas cortadas cada una
    a ANCHO_LINEA (200) caracteres. Con 0 = SIN TRUNCADO en stdout: vuelca el
    resultado completo (todas las lineas, ancho libre) sin necesidad de
    --verbose. La preview de la tabla OK/ERROR se corta siempre a
    ANCHO_PREVIEW (140) para mantener compacta la cabecera; para el detalle
    usa --out (archivo) o --max-output 0 (stdout).
  - --show-tools [--tool <nombre>]: catalogo de tools (o SOLO el inputSchema
    de esa tool en JSON compacto) leido de .cache/<nombre>.json sin red, sin
    re-sondear y sin exigir cache fresca; funciona con el servidor apagado y
    no toca la config. Si no hay cache, error claro sugiriendo el sondeo.
  - Tras cada tools/list escribe la cache de schemas en
    .cache/<nombre>.json; con --from-cache responde desde ella sin arrancar el
    servidor (0 s, 0 red). --refresh refresca en vivo si falta o es vieja.
  - --all: audita en SERIE TODOS los servidores definidos en la config
    (1 arranque por servidor, misma logica del audit individual en bucle).
    Por cada uno imprime el bloque compacto y escribe su cache; al final,
    tabla resumen global (servidor | transporte | serverInfo | n° tools |
    estado). Si un servidor falla (timeout, proceso roto) se anota FALLO y
    CONTINUA con el siguiente (no aborta el lote). Incompatible con
    --call/--batch. El --timeout aplica POR SERVIDADOR y por operacion: los
    locales npx pueden tardar, se recomienda --timeout 120. Con --from-cache
    responde al instante desde la cache los que la tengan fresca y AVISA de
    los que no (con --refresh los sin cache fresca se auditan en vivo).

Uso:
  python audit_mcp.py <nombre> [--config <ruta>] [--timeout <seg>]
      [--verbose] [--max-output <lineas>] [--calls-only] [--out <dir>]
      [--no-tools] [--no-resources] [--no-prompts]
      [--call <tool> [--args-file <json> | --args <json>]]  (repetible)
      [--batch plan.json]
      [--from-cache [--max-age <h>] [--refresh]]

  python audit_mcp.py <nombre> --show-tools [--tool <nombre-tool>] [--verbose]

  python audit_mcp.py --all [--config <ruta>] [--timeout 120]
      [--verbose] [--no-resources] [--no-prompts]
      [--from-cache [--max-age <h>] [--refresh]]

Los --args-file/--args se asignan por ORDEN a los --call que no traen
argumentos; el --batch se anexa al final.

Remotos (Streamable HTTP, SSE, stateless) y locales (stdio, npx lento) se
soportan igual que en probe_mcp.py / call_mcp.py. Los secretos {file:...} se
resuelven en runtime y NUNCA se imprimen.

Exit codes: 0 = todo OK; 1 = algun fallo de llamada/sondeo (handshake roto,
una tool respondio isError:true, error JSON-RPC/red/proceso) o, con --all,
algun servidor fallo (el lote continua y la tabla lo muestra); 2 = error de
config/uso (config o servidor inexistente, plan ilegible, --from-cache con
llamadas o sin cache valida y sin --refresh, --all con nombre o con
--call/--batch, --show-tools sin cache o con --call/--batch/--all, --tool
sin --show-tools, --out sin llamadas).

Solo stdlib. Protocolo: references/probe-jsonrpc.md
"""

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    McpError,
    default_config_path,
    find_server,
    list_servers,
    load_config,
    load_server_or_die,
    print_tools,
    cache_path,
    read_cache,
    server_kind,
    server_target,
    write_cache,
    McpSession,
)

ANCHO_LINEA = 200    # truncado POR LINEA de resultado en --verbose (0 = sin corte)
ANCHO_PREVIEW = 140  # preview de una linea en la tabla OK/ERROR (siempre corta)


def salir_utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


# ------------------------------------------------------------ argumentos ----

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Auditoria MCP y llamadas en lote con una sola sesion "
                    "(1 arranque, N operaciones).")
    parser.add_argument("server", nargs="?",
                        help="Nombre del servidor tal como esta definido en la config "
                             "(obligatorio salvo con --all)")
    parser.add_argument("--all", action="store_true", dest="all_servers",
                        help="Auditar en serie TODOS los servidores de la config "
                             "(1 arranque por servidor + tabla resumen global; "
                             "incompatible con <nombre>, --call y --batch; "
                             "recomendado --timeout 120 en locales npx)")
    parser.add_argument("--config", default=default_config_path(),
                        help="Ruta a opencode.json (por defecto: global de OpenCode)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="Segundos por operacion HTTP / respuesta del proceso local (defecto 60)")
    parser.add_argument("--verbose", action="store_true",
                        help="Detalle: descripciones de tools y hasta --max-output lineas por resultado")
    parser.add_argument("--max-output", type=int, default=6, metavar="LINEAS", dest="max_output",
                        help=f"Detalle impreso en stdout por resultado: maximo de lineas "
                             f"(cada linea cortada a {ANCHO_LINEA} caracteres) y visible con "
                             f"--verbose; con 0 se vuelca el resultado COMPLETO en stdout "
                             f"(sin corte de lineas ni de ancho) sin necesidad de --verbose. "
                             f"Defecto 6. La preview de la tabla OK/ERROR se corta siempre a "
                             f"{ANCHO_PREVIEW} caracteres: para todo el detalle usa --out.")
    parser.add_argument("--out", default=None, metavar="DIR", dest="out_dir",
                        help="Con --call/--batch: vuelca la salida COMPLETA (sin truncar) de "
                             "cada llamada en DIR\\<NN>_<tool>.json e imprime en stdout solo "
                             "estado, preview y la ruta. El DIR se crea si falta; usa rutas "
                             "dentro de la skill (p. ej. .tmp\\resultados). Sin --out, "
                             "comportamiento identico al anterior.")
    parser.add_argument("--show-tools", action="store_true", dest="show_tools",
                        help="Imprimir el catalogo de tools desde .cache\\<server>.json sin red "
                             "y sin re-sondear (funciona con el servidor apagado; no exige "
                             "cache fresca). Con --tool <nombre> muestra solo su inputSchema "
                             "en JSON compacto. Incompatible con --call/--batch/--all/"
                             "--from-cache. Si no hay cache, sale con 2 sugiriendo el sondeo.")
    parser.add_argument("--tool", default=None, metavar="NOMBRE", dest="tool_name",
                        help="Con --show-tools: imprimir solo el inputSchema de esa tool "
                             "(JSON compacto, desde la cache)")
    parser.add_argument("--call", action="append", default=[], metavar="TOOL", dest="calls",
                        help="Tool a invocar; repetible. Va acompanada de su --args-file/--args por orden")
    parser.add_argument("--args-file", action="append", default=[], metavar="RUTA", dest="args_files",
                        help="Archivo JSON (UTF-8) con los argumentos del proximo --call sin argumentos")
    parser.add_argument("--args", action="append", default=[], metavar="JSON", dest="args_inline",
                        help="Argumentos JSON inline para el proximo --call (frágil en Windows)")
    parser.add_argument("--batch", default=None, metavar="PLAN.JSON",
                        help="Plan JSON: lista de {\"tool\":..., \"args\":{...}} o {\"tool\":..., \"args_file\":\"...\"}")
    parser.add_argument("--calls-only", action="store_true", dest="calls_only",
                        help="Omitir el resumen de la auditoria: solo ejecutar las llamadas")
    parser.add_argument("--no-tools", action="store_true", dest="no_tools",
                        help="No imprimir la seccion TOOLS (tools/list se consulta igual: cache y validacion)")
    parser.add_argument("--no-resources", action="store_true", dest="no_resources",
                        help="No consultar resources/list")
    parser.add_argument("--no-prompts", action="store_true", dest="no_prompts",
                        help="No consultar prompts/list")
    parser.add_argument("--from-cache", action="store_true", dest="from_cache",
                        help="Responder desde .cache/<nombre>.json sin arrancar el servidor (sin --call/--batch)")
    parser.add_argument("--max-age", type=float, default=24.0, metavar="HORAS", dest="max_age",
                        help="Edad maxima aceptada de la cache con --from-cache (defecto 24)")
    parser.add_argument("--refresh", action="store_true",
                        help="Con --from-cache: si la cache falta o es vieja, ir en vivo y refrescarla")
    return parser.parse_args(argv)


def build_calls(ns):
    """Ensambla la lista de llamadas [(tool, args)] a partir de los flags.

    Devuelve (calls, error). Los --args-file y --args inline se asignan por
    orden a los --call que aun no tienen argumentos. El --batch se anexa al
    final. Cualquier problema de uso/lectura devuelve error (salida 2).
    """
    if len(ns.args_files) + len(ns.args_inline) > len(ns.calls):
        return None, ("hay mas --args-file/--args que --call: cada lote de "
                      "argumentos debe pertenecer a un --call")
    calls = [[name, None] for name in ns.calls]
    idx = 0
    for src in ns.args_files:
        while idx < len(calls) and calls[idx][1] is not None:
            idx += 1
        try:
            with open(src, "r", encoding="utf-8-sig") as fh:
                args = json.load(fh)
        except OSError as err:
            return None, f"no se pudo leer --args-file: {err}"
        except json.JSONDecodeError as err:
            return None, f"el JSON de --args-file no es valido: {err}"
        if not isinstance(args, dict):
            return None, "los argumentos de --args-file deben ser un objeto JSON"
        calls[idx][1] = args
    for inline in ns.args_inline:
        while idx < len(calls) and calls[idx][1] is not None:
            idx += 1
        try:
            args = json.loads(inline)
        except json.JSONDecodeError as err:
            return None, (f"el JSON de --args no es valido: {err}. Pista: en Windows "
                          f"el quoting inline corrompe el JSON; usa --args-file.")
        if not isinstance(args, dict):
            return None, "los argumentos de --args deben ser un objeto JSON"
        calls[idx][1] = args

    if ns.batch is not None:
        try:
            with open(ns.batch, "r", encoding="utf-8-sig") as fh:
                plan = json.load(fh)
        except OSError as err:
            return None, f"no se pudo leer --batch: {err}"
        except json.JSONDecodeError as err:
            return None, f"el plan {ns.batch} no es JSON valido: {err}"
        if not isinstance(plan, list):
            return None, "el plan del --batch debe ser una lista JSON de {\"tool\":..., \"args\":...}"
        plan_dir = os.path.dirname(os.path.abspath(ns.batch))
        for pos, entry in enumerate(plan, 1):
            if not isinstance(entry, dict) or not entry.get("tool"):
                return None, f"entrada {pos} del plan sin campo 'tool'"
            args = entry.get("args", {})
            if entry.get("args_file"):
                ruta = entry["args_file"]
                if not os.path.isabs(ruta):
                    ruta = os.path.join(plan_dir, ruta)
                try:
                    with open(ruta, "r", encoding="utf-8-sig") as fh:
                        args = json.load(fh)
                except (OSError, json.JSONDecodeError) as err:
                    return None, f"entrada {pos} del plan: args_file ilegible ({err})"
            if not isinstance(args, dict):
                return None, f"entrada {pos} del plan: 'args' debe ser un objeto JSON"
            calls.append([entry["tool"], args])
    return [(name, args if args is not None else {}) for name, args in calls], None


# ------------------------------------------------------------- resultados ---

def extract_text(result):
    """Texto plano del resultado de tools/call (sin volcar base64)."""
    partes = []
    for item in result.get("content", []) or []:
        if item.get("type") == "text":
            partes.append(item.get("text", ""))
        elif item.get("type") in ("image", "audio"):
            partes.append(f"[{item['type']}: {item.get('mimeType', '?')} omitido]")
    if result.get("structuredContent") is not None and not partes:
        partes.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(partes)


def render_output(text, max_lines):
    """Trunca el output a max_lines lineas y ANCHO_LINEA caracteres por linea.

    max_lines == 0 => SIN TRUNCADO: se devuelve el texto completo (todas las
    lineas, ancho libre). Es la semantica de --max-output 0.
    """
    if max_lines == 0:
        return text
    lineas = text.splitlines() or [""]
    cortadas = [(l[:ANCHO_LINEA] + ("..." if len(l) > ANCHO_LINEA else ""))
                for l in lineas[:max_lines]]
    extra = len(lineas) - len(cortadas)
    if extra > 0:
        cortadas.append(f"...(+{extra} lineas mas; --max-output para ver mas)")
    return "\n".join(cortadas)


def sanitize_nombre(name):
    """Nombre de tool convertido a componente seguro de archivo."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "tool"


def save_call_output(out_dir, idx, tool_name, result):
    """Vuelca la salida COMPLETA (sin truncar) de una llamada en el lote.

    Escribe <out_dir>/<NN>_<tool>.json con el texto de la tool (formateado
    como JSON si el texto es JSON valido; raw si es texto plano) o, si no hay
    texto, el structuredContent / result completo. Sirve tambien para
    resultados isError (el texto del error se guarda integro). Devuelve la
    ruta escrita o None si fallo (se avisa en stdout pero no rompe el lote).
    """
    try:
        os.makedirs(out_dir, exist_ok=True)
        texto = extract_text(result)
        if texto.strip():
            try:
                contenido = json.dumps(json.loads(texto), indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                contenido = texto
        elif result.get("structuredContent") is not None:
            contenido = json.dumps(result["structuredContent"], indent=2, ensure_ascii=False)
        else:
            contenido = json.dumps(result, indent=2, ensure_ascii=False)
        ruta = os.path.join(out_dir, f"{idx:02d}_{sanitize_nombre(tool_name)}.json")
        with open(ruta, "w", encoding="utf-8") as fh:
            fh.write(contenido)
            if not contenido.endswith("\n"):
                fh.write("\n")
        return ruta
    except OSError:
        return None


def run_calls(server_name, session, calls, tools, ns, any_fail):
    """Ejecuta las llamadas en orden dentro de la MISMA sesion y pinta la tabla.

    Con --out: cada resultado (OK o isError) se ademas vuelca COMPLETO a
    <out>/<NN>_<tool>.json y stdout solo muestra estado + preview + ruta.
    Con --max-output 0 (o --verbose) se imprime el detalle por consola.
    """
    nombres = [t.get("name") for t in tools]
    out_dir = ns.out_dir
    print("CALLS:")
    for idx, (name, args) in enumerate(calls, 1):
        if name not in nombres:
            print(f"ERROR  {name} — tool inexistente en '{server_name}' "
                  f"({len(nombres)} tools disponibles)")
            any_fail[0] = True
            continue
        try:
            result = session.call_tool(name, args)
        except McpError as err:
            print(f"ERROR  {name} — {str(err)[:ANCHO_PREVIEW]}")
            any_fail[0] = True
            continue
        if result.get("isError"):
            motivo = extract_text(result).strip().replace("\n", " ")[:ANCHO_PREVIEW] or "isError sin detalle"
            print(f"ERROR  {name} — isError: {motivo}")
            if out_dir:
                ruta = save_call_output(out_dir, idx, name, result)
                print(f"       {'salida completa: ' + ruta if ruta else 'AVISO: no se pudo escribir la salida en ' + out_dir}")
            any_fail[0] = True
            continue
        texto = extract_text(result)
        preview = texto.strip().replace("\n", " ")[:ANCHO_PREVIEW]
        print(f"OK     {name} — {preview or '(sin contenido)'}")
        if out_dir:
            ruta = save_call_output(out_dir, idx, name, result)
            if ruta:
                print(f"       salida completa: {ruta}")
            else:
                print(f"       AVISO: no se pudo escribir la salida en {out_dir}")
        if ns.verbose or ns.max_output == 0:
            for linea in render_output(texto, ns.max_output).splitlines():
                print(f"       {linea}")


# --------------------------------------------------------------- auditoria --

def audit_sections(session, ns):
    """Consulta y pinta las secciones pedidas. Devuelve (tools, resources, prompts, fallo).

    'no soportado' NO cuenta como fallo; un error real de transporte/red si.
    tools/list se consulta SIEMPRE (cache + validacion de llamadas); lo que
    controlan --no-tools/--calls-only es la IMPRESION.
    """
    tools, resources, prompts = None, None, None
    fallo = False

    info = session.server_info or {}
    caps = ", ".join(sorted((session.capabilities or {}).keys())) or "(ninguna declarada)"
    print(f"SERVER   {info.get('name', '?')} v{info.get('version', '?')} "
          f"(protocolo {session.protocol_version or '?'})")
    print(f"CAPS     {caps}")

    try:
        tools = session.list_tools()
    except McpError as err:
        print(f"ERROR  tools/list — {err}", file=sys.stderr)
        return None, None, None, True

    if not ns.no_tools and not ns.calls_only:
        if ns.verbose:
            print(f"TOOLS ({len(tools)}):")
            print_tools(tools)
        else:
            nombres = ", ".join(t.get("name", "?") for t in tools)
            print(f"TOOLS    {len(tools)} — {nombres or '(ninguna)'}")

    if not ns.no_resources:
        resources = session.list_resources()
        if not ns.calls_only:
            if resources is None:
                print("RESOURCES no soportado (-32601)")
            else:
                nombres = ", ".join(r.get("name") or r.get("uri", "?") for r in resources)
                print(f"RESOURCES {len(resources)} — {nombres or '(ninguno)'}")

    if not ns.no_prompts:
        prompts = session.list_prompts()
        if not ns.calls_only:
            if prompts is None:
                print("PROMPTS  no soportado (-32601)")
            else:
                nombres = ", ".join(p.get("name", "?") for p in prompts)
                print(f"PROMPTS  {len(prompts)} — {nombres or '(ninguno)'}")

    return tools, resources, prompts, fallo


def show_from_cache(name, verbose, data):
    """Resumen desde la cache: mismo aspecto que la auditoria, 0 arranques."""
    info = data.get("serverInfo") or {}
    edad_h = (time.time() - float(data.get("timestamp", 0))) / 3600.0
    print(f"[caché] '{name}' leido de .cache/{name}.json "
          f"(snapshot de {edad_h:.1f} h atras; servidor NO consultado)")
    caps = ", ".join(sorted((data.get("capabilities") or {}).keys())) or "(ninguna declarada)"
    print(f"SERVER   {info.get('name', '?')} v{info.get('version', '?')}")
    print(f"CAPS     {caps}")
    tools = data.get("tools") or []
    if verbose:
        print(f"TOOLS ({len(tools)}):")
        print_tools(tools)
    else:
        nombres = ", ".join(t.get("name", "?") for t in tools)
        print(f"TOOLS    {len(tools)} — {nombres or '(ninguna)'}")
    for clave, etiqueta in (("resources", "RESOURCES"), ("prompts", "PROMPTS")):
        valor = data.get(clave)
        if valor is None:
            print(f"{etiqueta} no consultado / no soportado (-32601)")
        else:
            nombres = ", ".join((v.get("name") or v.get("uri") or v.get("title", "?")) for v in valor)
            print(f"{etiqueta} {len(valor)} — {nombres or '(ninguno)'}")


def edad_texto(seg):
    """Edad legible en segundos -> 'X min' o 'X.X h'."""
    if seg < 3600:
        return f"{seg / 60:.0f} min"
    return f"{seg / 3600:.1f} h"


def show_tools_from_cache(name, tool_name, verbose):
    """Catalogo de tools (o inputSchema de una tool) desde .cache/<name>.json.

    SIN red, SIN re-sondear y SIN exigir cache fresca (a diferencia de
    --from-cache): lee el ultimo snapshot del disco y funciona con el
    servidor apagado. --tool <nombre> imprime solo su inputSchema en JSON
    compacto (una linea). Sin cache: error claro sugiriendo el sondeo.
    """
    path = cache_path(name)
    if not os.path.isfile(path):
        print(f"ERROR: no hay cache para '{name}' ({path}).", file=sys.stderr)
        print(f"Sondéala una vez en vivo para escribirla: python scripts\\probe_mcp.py {name} "
              f"(o python scripts\\audit_mcp.py {name} --timeout 120; para auto-refrescarla "
              f"sin opencode: python scripts\\refrescar_cache.py --solo {name})",
              file=sys.stderr)
        return 2
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as err:
        print(f"ERROR: cache ilegible ({path}): {err}", file=sys.stderr)
        return 2
    tools = data.get("tools") or []
    edad = edad_texto(time.time() - float(data.get("timestamp", 0)))
    print(f"[cache] '{name}' — {len(tools)} tools en {path} "
          f"(snapshot hace {edad}; servidor NO consultado, sin re-sondeo)")
    if tool_name:
        for t in tools:
            if t.get("name") == tool_name:
                schema = t.get("inputSchema")
                if schema is None:
                    print(f"(la tool '{tool_name}' no declara inputSchema en la cache)")
                else:
                    print(json.dumps(schema, ensure_ascii=False))
                return 0
        restantes = ", ".join(t.get("name", "?") for t in tools)
        print(f"ERROR: la tool '{tool_name}' no esta en la cache de '{name}' "
              f"({len(tools)} tools). Existentes: {restantes[:ANCHO_PREVIEW]}...",
              file=sys.stderr)
        return 2
    if verbose:
        print_tools(tools)
    else:
        nombres = ", ".join(t.get("name", "?") for t in tools)
        print(f"TOOLS    {len(tools)} — {nombres or '(ninguna)'}")
    return 0


# ----------------------------------------------------------------- --all ----

ANCHO_SERVIDOR = 16
ANCHO_INFO = 24
ANCHO_MOTIVO = 90


def audit_server_live(name, server, config_dir, ns):
    """Auditoria en vivo de UN servidor (1 arranque) para el modo --all.

    Reutiliza la logica del audit individual: handshake + audit_sections +
    write_cache. Devuelve (ok, serverInfo_str, n_tools, motivo_corto).
    Nunca lanza McpError al llamador: el fallo se anota y el lote continua.
    """
    t0 = time.time()
    try:
        with McpSession(server, config_dir, ns.timeout) as session:
            session.start()
            tools, resources, prompts, fallo = audit_sections(session, ns)
            if tools is None:
                return False, "?", 0, "tools/list fallo (handshake ok)"
            info = session.server_info or {}
            info_str = f"{info.get('name', '?')} v{info.get('version', '?')}"
            path_cache = write_cache(name, session.server_info,
                                     session.capabilities, tools,
                                     resources, prompts)
    except McpError as err:
        return False, "-", 0, str(err)[:ANCHO_MOTIVO]
    motivo = "recursos/prompts parciales" if fallo else ""
    ok = not fallo
    print(f"[total {time.time() - t0:.1f}s | 1 arranque]"
          + (f" [caché: {path_cache}]" if path_cache else ""))
    return ok, info_str, len(tools), motivo


def show_row(name, kind, info_str, n_tools, estado):
    """Linea de la tabla resumen global del modo --all."""
    print(f"  {name[:ANCHO_SERVIDOR]:<{ANCHO_SERVIDOR}} {kind:<8} "
          f"{info_str[:ANCHO_INFO]:<{ANCHO_INFO}} {n_tools:>5}  {estado}")


def main_all(ns):
    """Audita en serie TODOS los servidores de la config (1 arranque por
    servidor, fallos anotados sin abortar el lote) y pinta la tabla global.

    Exit 0 si todos OK, 1 si algun servidor fallo, 2 error de config/uso.
    """
    if not os.path.isfile(ns.config):
        print(f"ERROR: no existe la config: {ns.config}", file=sys.stderr)
        return 2
    try:
        cfg = load_config(ns.config)
    except json.JSONDecodeError as err:
        print(f"ERROR: la config no es JSON valido: {err}", file=sys.stderr)
        return 2

    infos = list_servers(cfg)
    if not infos:
        print("No hay servidores MCP definidos en la config.")
        return 0

    config_dir = os.path.dirname(os.path.abspath(ns.config))
    names = sorted(infos)
    any_fail = False
    rows = []
    for idx, name in enumerate(names, 1):
        data, _ruta, _flag, _off = find_server(cfg, name)
        kind, target = server_kind(data), server_target(data)

        if ns.from_cache:
            cache, motivo = read_cache(name, ns.max_age)
            if cache is not None:
                print(f"\n=== [{idx}/{len(names)}] {name} (desde caché, sin arrancar) ===")
                show_from_cache(name, ns.verbose, cache)
                n = len(cache.get("tools") or [])
                info = cache.get("serverInfo") or {}
                rows.append((name, kind,
                             f"{info.get('name', '?')} v{info.get('version', '?')}",
                             n, "OK (caché)"))
                continue
            if not ns.refresh:
                print(f"\n=== [{idx}/{len(names)}] {name} ({kind}) ===")
                print(f"AVISO: sin cache fresca usable ({motivo}); "
                      f"se omite (quita --from-cache o anade --refresh).")
                rows.append((name, kind, "-", 0, "SIN CACHE (omitido)"))
                continue
            # --refresh: cae a la auditoria en vivo para refrescar

        print(f"\n=== [{idx}/{len(names)}] {name} ({kind}: {target}) "
              f"— 1 arranque, timeout {ns.timeout}s...")
        ok, info_str, n_tools, motivo = audit_server_live(name, data, config_dir, ns)
        if not ok:
            any_fail = True
            estado = f"FALLO: {motivo or 'error de sondeo'}"[:ANCHO_MOTIVO]
        else:
            estado = "OK"
        rows.append((name, kind, info_str, n_tools, estado))

    print("\nRESUMEN GLOBAL (--all):")
    print(f"  {'servidor':<{ANCHO_SERVIDOR}} {'transporte':<8} "
          f"{'serverInfo':<{ANCHO_INFO}} tools  estado")
    for row in rows:
        show_row(*row)
    n_ok = sum(1 for r in rows if r[4].startswith("OK"))
    n_fail = sum(1 for r in rows if r[4].startswith("FALLO"))
    n_cache = sum(1 for r in rows if r[4] == "SIN CACHE (omitido)")
    print(f"TOTAL: {len(rows)} servidores — {n_ok} OK, {n_fail} FALLO"
          + (f", {n_cache} sin cache" if n_cache else "") + ".")
    return 1 if any_fail else 0


# --------------------------------------------------------------------- main -

def main(argv=None):
    salir_utf8()
    ns = parse_args(argv)

    if ns.show_tools:
        if ns.all_servers:
            print("ERROR: --show-tools no aplica a --all (la cache es por servidor).",
                  file=sys.stderr)
            return 2
        if ns.from_cache:
            print("ERROR: --show-tools y --from-cache son alternativas; elige una. "
                  "--show-tools lee la cache sin exigir frescura y con --tool "
                  "imprime el inputSchema.", file=sys.stderr)
            return 2
        if ns.calls or ns.args_files or ns.args_inline or ns.batch is not None:
            print("ERROR: --show-tools es de inspeccion: incompatible con "
                  "--call/--args/--args-file/--batch.", file=sys.stderr)
            return 2
        if not ns.server:
            print("ERROR: --show-tools necesita el servidor: "
                  "audit_mcp.py <servidor> --show-tools [--tool <nombre>]",
                  file=sys.stderr)
            return 2
        return show_tools_from_cache(ns.server, ns.tool_name, ns.verbose)

    if ns.tool_name and not ns.show_tools:
        print("ERROR: --tool solo aplica junto a --show-tools (inputSchema desde "
              "la cache). Para INVOCAR una tool usa --call, --batch o call_mcp.py.",
              file=sys.stderr)
        return 2

    if ns.out_dir and ns.from_cache:
        print("ERROR: --out vuelca resultados de LLAMADAS; con --from-cache (solo "
              "inspeccion) no hay nada que volcar.", file=sys.stderr)
        return 2

    if ns.all_servers:
        if ns.server:
            print("ERROR: --all no acepta nombre de servidor; audita TODOS "
                  "los de la config.", file=sys.stderr)
            return 2
        if ns.calls or ns.args_files or ns.args_inline or ns.batch is not None:
            print("ERROR: --all es incompatible con --call/--args/--args-file/"
                  "--batch (audita, no invoca).", file=sys.stderr)
            return 2
        return main_all(ns)

    if not ns.server:
        print("ERROR: falta el nombre del servidor (o usa --all para auditar "
              "toda la config).", file=sys.stderr)
        return 2

    if ns.from_cache and (ns.calls or ns.batch is not None):
        print("ERROR: --from-cache solo sirve para inspeccion; para INVOCAR tools "
              "hay que hablar con el servidor (quita --from-cache).", file=sys.stderr)
        return 2

    if ns.from_cache:
        data, motivo = read_cache(ns.server, ns.max_age)
        if data is not None:
            show_from_cache(ns.server, ns.verbose, data)
            return 0
        if not ns.refresh:
            print(f"ERROR: {motivo}; refresca ejecutando el audit en vivo "
                  f"(quita --from-cache o anade --refresh).", file=sys.stderr)
            return 2

    calls, err = build_calls(ns)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    if ns.out_dir and not calls:
        print("ERROR: --out solo tiene sentido con --call o --batch (no hay "
              "llamadas que volcar).", file=sys.stderr)
        return 2

    server, config_dir = load_server_or_die(ns.server, ns.config)
    kind = server_kind(server)
    target = server_target(server)
    n_ops = len(calls) if calls else 1
    print(f"AUDITORIA '{ns.server}' ({kind}: {target}) — 1 sesion, "
          f"{n_ops} llamada(s), sin activar el MCP...")

    t0 = time.time()
    any_fail = [False]
    try:
        with McpSession(server, config_dir, ns.timeout) as session:
            session.start()
            t_start = time.time()
            tools, resources, prompts, fallo = audit_sections(session, ns)
            if fallo:
                any_fail[0] = True
            if tools is not None and calls:
                run_calls(ns.server, session, calls, tools, ns, any_fail)
            path_cache = None
            if tools is not None:
                path_cache = write_cache(ns.server, session.server_info,
                                         session.capabilities, tools,
                                         resources, prompts)
    except McpError as err_sesion:
        # Fallo del handshake/transporte: la auditoria no pudo completarse
        print(f"ERROR: {err_sesion}", file=sys.stderr)
        return 1

    print(f"[arranque+handshake {t_start - t0:.1f}s | total {time.time() - t0:.1f}s | 1 arranque]")
    if path_cache:
        print(f"[caché escrita: {path_cache}]")
    return 1 if any_fail[0] else 0


if __name__ == "__main__":
    sys.exit(main())
