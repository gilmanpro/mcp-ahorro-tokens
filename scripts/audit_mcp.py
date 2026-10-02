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
  - Tras cada tools/list escribe la cache de schemas en
    .cache/<nombre>.json; con --from-cache responde desde ella sin arrancar el
    servidor (0 s, 0 red). --refresh refresca en vivo si falta o es vieja.

Uso:
  python audit_mcp.py <nombre> [--config <ruta>] [--timeout <seg>]
      [--verbose] [--max-output <lineas>] [--calls-only]
      [--no-tools] [--no-resources] [--no-prompts]
      [--call <tool> [--args-file <json> | --args <json>]]  (repetible)
      [--batch plan.json]
      [--from-cache [--max-age <h>] [--refresh]]

Los --args-file/--args se asignan por ORDEN a los --call que no traen
argumentos; el --batch se anexa al final.

Remotos (Streamable HTTP, SSE, stateless) y locales (stdio, npx lento) se
soportan igual que en probe_mcp.py / call_mcp.py. Los secretos {file:...} se
resuelven en runtime y NUNCA se imprimen.

Exit codes: 0 = todo OK; 1 = algun fallo de llamada/sondeo (handshake roto,
una tool respondio isError:true, error JSON-RPC/red/proceso); 2 = error de
config/uso (config o servidor inexistente, plan ilegible, --from-cache con
llamadas o sin cache valida y sin --refresh).

Solo stdlib. Protocolo: references/probe-jsonrpc.md
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    McpError,
    default_config_path,
    load_server_or_die,
    print_tools,
    read_cache,
    server_kind,
    server_target,
    write_cache,
    McpSession,
)

ANCHO_LINEA = 200    # truncado por linea de resultado (ahorro de contexto)
ANCHO_PREVIEW = 140  # preview de una linea en la tabla


def salir_utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


# ------------------------------------------------------------ argumentos ----

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Auditoria MCP y llamadas en lote con una sola sesion "
                    "(1 arranque, N operaciones).")
    parser.add_argument("server", help="Nombre del servidor tal como esta definido en la config")
    parser.add_argument("--config", default=default_config_path(),
                        help="Ruta a opencode.json (por defecto: global de OpenCode)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="Segundos por operacion HTTP / respuesta del proceso local (defecto 60)")
    parser.add_argument("--verbose", action="store_true",
                        help="Detalle: descripciones de tools y hasta --max-output lineas por resultado")
    parser.add_argument("--max-output", type=int, default=6, metavar="LINEAS", dest="max_output",
                        help="Maximo de lineas impresas por resultado de tool en --verbose (defecto 6)")
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
    """Trunca el output a max_lines lineas y ANCHO_LINEA caracteres por linea."""
    lineas = text.splitlines() or [""]
    cortadas = [(l[:ANCHO_LINEA] + ("..." if len(l) > ANCHO_LINEA else ""))
                for l in lineas[:max_lines]]
    extra = len(lineas) - len(cortadas)
    if extra > 0:
        cortadas.append(f"...(+{extra} lineas mas; --max-output para ver mas)")
    return "\n".join(cortadas)


def run_calls(server_name, session, calls, tools, ns, any_fail):
    """Ejecuta las llamadas en orden dentro de la MISMA sesion y pinta la tabla."""
    nombres = [t.get("name") for t in tools]
    print("CALLS:")
    for name, args in calls:
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
            any_fail[0] = True
            continue
        texto = extract_text(result)
        preview = texto.strip().replace("\n", " ")[:ANCHO_PREVIEW]
        print(f"OK     {name} — {preview or '(sin contenido)'}")
        if ns.verbose:
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


def show_from_cache(ns, data):
    """Resumen desde la cache: mismo aspecto que la auditoria, 0 arranques."""
    info = data.get("serverInfo") or {}
    edad_h = (time.time() - float(data.get("timestamp", 0))) / 3600.0
    print(f"[caché] '{ns.server}' leido de .cache/{ns.server}.json "
          f"(snapshot de {edad_h:.1f} h atras; servidor NO consultado)")
    caps = ", ".join(sorted((data.get("capabilities") or {}).keys())) or "(ninguna declarada)"
    print(f"SERVER   {info.get('name', '?')} v{info.get('version', '?')}")
    print(f"CAPS     {caps}")
    tools = data.get("tools") or []
    if ns.verbose:
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


# --------------------------------------------------------------------- main -

def main(argv=None):
    salir_utf8()
    ns = parse_args(argv)

    if ns.from_cache and (ns.calls or ns.batch is not None):
        print("ERROR: --from-cache solo sirve para inspeccion; para INVOCAR tools "
              "hay que hablar con el servidor (quita --from-cache).", file=sys.stderr)
        return 2

    if ns.from_cache:
        data, motivo = read_cache(ns.server, ns.max_age)
        if data is not None:
            show_from_cache(ns, data)
            return 0
        if not ns.refresh:
            print(f"ERROR: {motivo}; refresca ejecutando el audit en vivo "
                  f"(quita --from-cache o anade --refresh).", file=sys.stderr)
            return 2

    calls, err = build_calls(ns)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
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
