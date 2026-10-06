#!/usr/bin/env python3
"""Nucleo compartido de los scripts MCP de la skill mcp-ahorro-tokens.

Concentra la logica que antes duplicaban probe_mcp.py / call_mcp.py /
toggle_mcp.py:

  - Config: resolucion de servidores en los dos formatos verificados
    (anidado mcp.servers.<n> con flag 'disabled' y plano mcp.<n> con flag
    'enabled').
  - Secretos: las referencias {file:./ruta} se resuelven EN RUNTIME y sus
    valores NUNCA se imprimen ni se propagan a mensajes de error.
  - Protocolo: handshake initialize -> notifications/initialized y peticiones
    JSON-RPC sobre dos transportes:
      * remoto Streamable HTTP (Accept: application/json, text/event-stream;
        tolera SSE y servidores stateless sin Mcp-Session-Id)
      * local stdio newline-delimited (el proceso hijo se mata SIEMPRE)
  - Clase McpSession: SESION UNICA = 1 arranque, N peticiones (tools/list,
    resources/list, prompts/list, tools/call...). Es la base de audit_mcp.py,
    que evita relanzar el servidor (15-20 s en locales npx) en cada llamada.
  - Cache de schemas: <skill>/.cache/<nombre>.json con serverInfo,
    capabilities, tools (nombre+descripcion+inputSchema), resources, prompts y
    timestamp, para responder con --from-cache sin arrancar el servidor.

Solo stdlib (json/os/re/shutil/subprocess/sys/threading/time/urllib).
"""

import itertools
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "mcp_ahorro_tokens", "version": "2.0"}
FILE_REF = re.compile(r"\{file:([^}]+)\}")


# ------------------------------------------------- spawns sin ventana (Win) -

def no_window_kwargs(extra_flags=0):
    """kwargs de subprocess.Popen que garantizan CERO ventana visible.

    En Windows OR-ear `extra_flags` con CREATE_NO_WINDOW (la consola del hijo
    nace SIN ventana: ni conhost grafico ni pestana/popup de Windows Terminal
    — el defecto sin este flag es que cada hijo de consola creado desde un
    padre sin consola, p. ej. pythonw, abre UNA VENTANA visible) y anade un
    STARTUPINFO con SW_HIDE+STARTF_USESHOWWINDOW como segundo cinturon (hijos
    que crean su propia ventana). En POSIX devuelve {} (no existen las consolas
    con ventana).

    Hallazgo (fix 05/10/2026, tormenta de terminales al iniciar opencode): el
    auto-refresco del plugin corre con pythonw.exe y cada servidor MCP local
    stdio (npx/node/cmd) se lanzaba sin estos flags => N ventanas visibles por
    arranque de sesion.

    extra_flags: flags adicionales propios del caller (p. ej. en toggle_mcp.py
    --bg: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP).
    """
    if os.name != "nt":
        return {"creationflags": extra_flags} if extra_flags else {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": si,
        "creationflags": extra_flags | subprocess.CREATE_NO_WINDOW,
    }


def popenv(args, extra_flags=0, **kwargs):
    """subprocess.Popen con cero ventana visible en Windows.

    Acepta los mismos kwargs que subprocess.Popen salvo creationflags y
    startupinfo (los gestiona aqui; para anadir flags propios usa extra_flags).
    """
    kwargs.update(no_window_kwargs(extra_flags))
    return subprocess.Popen(args, **kwargs)


class McpError(Exception):
    """Fallo de protocolo/sondeo/llamada con mensaje claro y SIN secretos.

    code: codigo JSON-RPC cuando el servidor lo devolvio (-32601 = metodo
    no soportado; los demas = fallo real).
    """

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# Alias historico: probe_mcp.py / call_mcp.py lo exportaban con este nombre.
ProbeError = McpError


# ------------------------------------------------------------- secretos -----

def resolve_refs(value, config_dir):
    """Sustituye {file:./ruta} por el contenido del archivo referenciado.

    Las rutas son relativas al DIRECTORIO de la config. El valor resuelto se
    usa solo en runtime (cabeceras HTTP / variables de entorno) y NUNCA se
    imprime ni se propaga a mensajes de error.
    """
    if not isinstance(value, str):
        return value

    def repl(match):
        rel = match.group(1).strip()
        if rel.startswith("./"):
            rel = rel[2:]
        path = os.path.normpath(os.path.join(config_dir, rel))
        if not os.path.isfile(path):
            raise McpError(f"Falta el archivo referenciado por {{file:...}}: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()

    return FILE_REF.sub(repl, value)


# -------------------------------------------------------------- mensajes ----

def rpc_message(msg_id, method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return msg


def parse_jsonrpc_body(raw, content_type):
    """Acepta JSON puro o SSE ('data: {...}') y devuelve el objeto con result/error."""
    text = raw.decode("utf-8", "replace")
    is_sse = "text/event-stream" in (content_type or "") or re.search(r"(?m)^data:", text)
    if is_sse:
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                return obj
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------- config -----

def default_config_path():
    base = os.path.expanduser("~")
    return os.path.join(base, ".config", "opencode", "opencode.json")


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def find_server(cfg, name):
    """Devuelve (diccionario_del_servidor, ruta_legible, clave_de_flag, valor_desactivado).

    Prioriza el formato que ya contenga el servidor. El flag desactivador es
    'disabled' (True = apagado) en formato anidado y 'enabled' (False = apagado)
    en formato plano.
    """
    mcp = cfg.get("mcp")
    if not isinstance(mcp, dict):
        return None
    servers = mcp.get("servers")
    if isinstance(servers, dict) and name in servers and isinstance(servers[name], dict):
        return servers[name], f"mcp.servers.{name}", "disabled", True
    if name in mcp and isinstance(mcp[name], dict):
        return mcp[name], f"mcp.{name}", "enabled", False
    return None


def list_servers(cfg):
    """Mapa nombre -> info para 'status' y mensajes de error."""
    out = {}
    mcp = cfg.get("mcp")
    if not isinstance(mcp, dict):
        return out
    servers = mcp.get("servers")
    if isinstance(servers, dict):
        for name, data in servers.items():
            if isinstance(data, dict):
                off = bool(data.get("disabled", False))
                out[name] = ("mcp.servers." + name, "disabled", off)
    for name, data in mcp.items():
        if name == "servers" or not isinstance(data, dict):
            continue
        off = not bool(data.get("enabled", True))
        out[name] = ("mcp." + name, "enabled", off)
    return out


def server_kind(server):
    """'remote' o 'local' segun el contenido del bloque de config."""
    return server.get("type") or ("remote" if server.get("url") else "local")


def server_target(server):
    return server.get("url") or " ".join(server.get("command", []) or [])


# ---------------------------------------------------------------- caché -----

def cache_dir():
    """Carpeta .cache/ al lado de scripts/ (dentro de la skill)."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".cache")


def cache_path(name):
    return os.path.join(cache_dir(), name + ".json")


def write_cache(name, server_info, capabilities, tools, resources, prompts):
    """Guarda el snapshot de tools/resources/prompts tras un tools/list real."""
    data = {
        "server": name,
        "serverInfo": server_info,
        "capabilities": capabilities,
        "tools": tools,
        "resources": resources,
        "prompts": prompts,
        "timestamp": time.time(),
    }
    path = cache_path(name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except OSError:
        return None  # la caché es un extra: si falla, no se rompe la operacion
    return path


def read_cache(name, max_age_hours=24.0):
    """Devuelve (datos, None) si la caché existe y es fresca; (None, motivo) si no."""
    path = cache_path(name)
    if not os.path.isfile(path):
        return None, f"no existe la caché de '{name}' ({path})"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as err:
        return None, f"caché ilegible: {err}"
    age_h = (time.time() - float(data.get("timestamp", 0))) / 3600.0
    if max_age_hours and max_age_hours > 0 and age_h > max_age_hours:
        return None, f"caché vieja ({age_h:.1f} h > max-age {max_age_hours:g} h)"
    return data, None


# ---------------------------------------------------------- McpSession ------

def _reader_thread(stream, out_queue):
    for line in stream:
        out_queue.put(line)
    out_queue.put(None)  # centinela de EOF


def _stderr_thread(stream, sink):
    for line in stream:
        sink.append(line.decode("utf-8", "replace").rstrip())


class McpSession:
    """Sesion UNICA contra un servidor MCP: 1 arranque, N peticiones JSON-RPC.

    Uso:
        with McpSession(server, config_dir, timeout=60) as s:
            s.start()                      # initialize + notifications/initialized
            tools = s.list_tools()         # handshake + tools/list
            res = s.call_tool("list-domains", {})
            rec = s.list_records()         # None si el servidor no los soporta

    - Servidor remoto (url): Streamable HTTP; captura Mcp-Session-Id si viene
      y, si una peticion falla teniendola, reintenta sin ella (stateless).
      Tolerante a respuestas SSE.
    - Servidor local (command): lanza el proceso por stdio newline-delimited y
      LO MATA al cerrar la sesion, pase lo que pase.

    list_resources()/list_prompts() devuelven None (no lanzan error) cuando el
    servidor no declara la capability o responde -32601; list_tools() si falla
    de verdad lanza McpError.
    """

    def __init__(self, server, config_dir, timeout=60):
        self.server = server
        self.config_dir = config_dir
        self.timeout = timeout
        self.kind = server_kind(server)
        self.target = server_target(server)
        self.server_info = {}
        self.capabilities = {}
        self.protocol_version = None
        self._ids = itertools.count(1)
        self._started = False
        # Estado por transporte
        self._url = None
        self._headers = None
        self._session = None          # Mcp-Session-Id remoto
        self._proc = None             # proceso stdio local
        self._lines = None
        self._stderr_sink = None

    # -- ciclo de vida ------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def start(self):
        """Handshake completo: initialize + notifications/initialized."""
        if self._started:
            return
        if self.kind == "remote":
            self._start_remote()
        else:
            self._start_local()
        self._started = True

    def close(self):
        """Cierra la sesion. En local MATA SIEMPRE el proceso hijo."""
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    # -- API de alto nivel ----------------------------------------------------

    def request(self, method, params=None):
        """Peticion JSON-RPC con id autoincremental; devuelve el dict 'result'."""
        if not self._started:
            raise McpError("La sesion no esta iniciada: llama a start() primero.")
        msg_id = next(self._ids)
        if self.kind == "remote":
            return self._request_remote(msg_id, method, params)
        return self._request_stdio(msg_id, method, params)

    def supports(self, capability):
        """True si el servidor declaro esa capability en initialize.

        Si no declaro ninguna (dict vacio), se asume que puede soportar todo
        y el fallo real llegara como -32601 al intentar la peticion.
        """
        caps = self.capabilities or {}
        if not caps:
            return True
        return bool(caps.get(capability))

    def list_tools(self):
        return self.request("tools/list", {}).get("tools", [])

    def list_resources(self):
        """Lista de recursos, o None si el servidor NO los soporta."""
        if not self.supports("resources"):
            return None
        try:
            return self.request("resources/list", {}).get("resources", [])
        except McpError as err:
            if err.code == -32601:
                return None
            raise

    def list_prompts(self):
        """Lista de prompts, o None si el servidor NO los soporta."""
        if not self.supports("prompts"):
            return None
        try:
            return self.request("prompts/list", {}).get("prompts", [])
        except McpError as err:
            if err.code == -32601:
                return None
            raise

    def call_tool(self, name, arguments=None):
        return self.request("tools/call",
                            {"name": name, "arguments": arguments or {}})

    # ------------------------------------------------------------- remoto ---

    def _post(self, body, extra_headers=None):
        headers = dict(self._headers)
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(self._url,
                                     data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return (resp.status, resp.read(),
                        resp.headers.get("Content-Type", ""),
                        resp.headers.get("Mcp-Session-Id"))
        except urllib.error.HTTPError as err:
            return (err.code, err.read(),
                    err.headers.get("Content-Type", "") if err.headers else "", None)
        except urllib.error.URLError as err:
            raise McpError(f"Error de red contra {self._url}: {err.reason}")

    def _start_remote(self):
        self._url = self.server.get("url")
        if not self._url:
            raise McpError("Servidor type=remote sin campo 'url' en la config.")
        self._headers = {"Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        for key, val in (self.server.get("headers") or {}).items():
            self._headers[key] = resolve_refs(val, self.config_dir)

        status, raw, ctype, session = self._post(
            rpc_message(next(self._ids), "initialize",
                        {"protocolVersion": PROTOCOL_VERSION,
                         "capabilities": {}, "clientInfo": CLIENT_INFO}))
        if status >= 400:
            snippet = raw.decode("utf-8", "replace")[:300]
            hint = " (¿secreto/Authorization invalido?)" if status == 401 else ""
            raise McpError(f"initialize fallo con HTTP {status}{hint}: {snippet}")
        init = parse_jsonrpc_body(raw, ctype)
        if init is None or "error" in init:
            raise McpError(f"Respuesta de initialize no util: {raw.decode('utf-8','replace')[:300]}")
        result = init.get("result", {})
        self.server_info = result.get("serverInfo", {})
        self.capabilities = result.get("capabilities", {}) or {}
        self.protocol_version = result.get("protocolVersion")
        self._session = session
        if session:
            print(f"[sesion Mcp-Session-Id capturada: {session[:8]}...]")

        sess_hdr = {"Mcp-Session-Id": session} if session else {}
        # notifications/initialized (puede responder 200/202; no es fatal)
        self._post(rpc_message(None, "notifications/initialized"), sess_hdr)

    def _request_remote(self, msg_id, method, params):
        sess_hdr = {"Mcp-Session-Id": self._session} if self._session else {}
        label = method
        status, raw, ctype, _ = self._post(rpc_message(msg_id, method, params), sess_hdr)
        # Servidor stateless: si la cabecera de sesion molesta, reintenta sin ella
        if status >= 400 and self._session:
            status, raw, ctype, _ = self._post(rpc_message(msg_id, method, params))
        if status >= 400:
            raise McpError(f"{label} fallo con HTTP {status}: {raw.decode('utf-8','replace')[:300]}")
        obj = parse_jsonrpc_body(raw, ctype)
        if obj is None:
            raise McpError(f"Respuesta de {label} no es JSON-RPC valido.")
        if "error" in obj:
            err = obj["error"]
            raise McpError(f"Error JSON-RPC en {label}: {err.get('message', err)}",
                           code=err.get("code"))
        return obj.get("result", {})

    # -------------------------------------------------------------- local ---

    def _start_local(self):
        cmd = self.server.get("command")
        if not isinstance(cmd, list) or not cmd:
            raise McpError("Servidor type=local sin campo 'command' (array) en la config.")
        cmd = list(cmd)
        exe = shutil.which(cmd[0]) or cmd[0]

        env = dict(os.environ)
        for key, val in (self.server.get("environment") or {}).items():
            env[key] = resolve_refs(val, self.config_dir)

        try:
            # popenv = CREATE_NO_WINDOW + SW_HIDE en Windows (fix tormenta de
            # terminales: antes cada servidor stdio abierto desde pythonw
            # mostraba una ventana de consola visible por arranque de sesion).
            self._proc = popenv([exe] + cmd[1:],
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                env=env)
        except OSError as err:
            raise McpError(f"No se pudo lanzar '{' '.join(cmd)}': {err}")

        self._lines = queue.Queue()
        self._stderr_sink = []
        threading.Thread(target=_reader_thread, args=(self._proc.stdout, self._lines),
                         daemon=True).start()
        threading.Thread(target=_stderr_thread, args=(self._proc.stderr, self._stderr_sink),
                         daemon=True).start()

        result = self._request_stdio(next(self._ids), "initialize",
                                     {"protocolVersion": PROTOCOL_VERSION,
                                      "capabilities": {}, "clientInfo": CLIENT_INFO})
        self.server_info = result.get("serverInfo", {})
        self.capabilities = result.get("capabilities", {}) or {}
        self.protocol_version = result.get("protocolVersion")
        self._send(rpc_message(None, "notifications/initialized"))

    def _send(self, msg):
        proc = self._proc
        if proc is None or proc.poll() is not None:
            code = proc.returncode if proc else "?"
            raise McpError(
                f"El proceso murio antes de responder (exit {code}). "
                f"stderr: {' | '.join(self._stderr_sink[-5:]) or '(vacio)'}")
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def _read_id(self, want_id, label):
        deadline = time.time() + self.timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise McpError(
                    f"Timeout ({self.timeout}s) esperando {label}. Nota: si el comando "
                    f"usa npx, la primera vez puede tardar en descargar el paquete; "
                    f"reintenta con --timeout mayor.")
            try:
                line = self._lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if self._proc.poll() is not None:
                    raise McpError(
                        f"El proceso cerro (exit {self._proc.returncode}) sin responder "
                        f"{label}. stderr: {' | '.join(self._stderr_sink[-5:]) or '(vacio)'}")
                continue
            if line is None:
                raise McpError(
                    f"El servidor cerro su stdout sin responder {label}. "
                    f"stderr: {' | '.join(self._stderr_sink[-5:]) or '(vacio)'}")
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("{"):
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == want_id:
                if "error" in obj:
                    err = obj["error"]
                    raise McpError(f"Error JSON-RPC en {label}: {err.get('message', err)}",
                                   code=err.get("code"))
                return obj

    def _request_stdio(self, msg_id, method, params):
        self._send(rpc_message(msg_id, method, params))
        return self._read_id(msg_id, method).get("result", {})


# ------------------------------------------------------------------ salida --

def print_tools(tools, stream=None):
    """Formato clasico de probe_mcp.py: numero. nombre — descripcion (<=160)."""
    out = stream or sys.stdout
    if not tools:
        print("(el servidor no expone ninguna tool)", file=out)
        return
    for idx, tool in enumerate(tools, 1):
        desc = " ".join((tool.get("description") or "").split())
        if len(desc) > 160:
            desc = desc[:157] + "..."
        line = f"{idx:>3}. {tool.get('name', '?')}"
        if desc:
            line += f" — {desc}"
        print(line, file=out)
    print(f"TOTAL: {len(tools)} tools", file=out)


def load_server_or_die(name, config_path):
    """Carga la config y resuelve el servidor; imprime el error y sale con 2.

    Devuelve (server, config_dir) o llama a sys.exit(2) con el mismo mensaje
    de error que usaban probe_mcp.py / call_mcp.py historicamente.
    """
    if not os.path.isfile(config_path):
        print(f"ERROR: no existe la config: {config_path}", file=sys.stderr)
        sys.exit(2)
    try:
        cfg = load_config(config_path)
    except json.JSONDecodeError as err:
        print(f"ERROR: la config no es JSON valido: {err}", file=sys.stderr)
        sys.exit(2)
    found = find_server(cfg, name)
    if found is None:
        print(f"ERROR: servidor '{name}' no existe en {config_path}.", file=sys.stderr)
        infos = list_servers(cfg)
        if infos:
            print("Existentes: " + ", ".join(sorted(infos)), file=sys.stderr)
        sys.exit(2)
    return found[0], os.path.dirname(os.path.abspath(config_path))
