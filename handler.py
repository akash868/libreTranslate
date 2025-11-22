"""
Universal Lambda handler for LibreTranslate.

Behavior:
1) Try to import an `app` object from common module paths (FastAPI or Flask).
   - If ASGI app found => use mangum
   - If WSGI app found => use awsgi

2) If no `app` found, call the existing `main()` (as your main.py does) in a background thread,
   wait until the local HTTP server is listening on 127.0.0.1:5000, then forward Lambda HTTP
   requests to that local HTTP server and return the response in API Gateway format.

This avoids forcing you to change `main()` immediately. For production it's cleaner to expose
an `app` object and use mangum/awsgi directly.

This version integrates an EFS bootstrap step: if an EFS mount exists at LT_EFS_MOUNT (default
/mnt/models) it will attempt to ensure models are present by calling scripts/ensure_models_on_efs.py.
"""
import importlib
import importlib.util
import inspect
import threading
import time
import socket
import os
import sys
import traceback
import base64
import urllib.parse
import urllib.request
import subprocess

# --- Helper: try common import paths for app object ---
COMMON_PATHS = [
    "main:app",
    "app:app",
    "server:app",
    "libretranslate:app",
    "translate:app",
    "api:app",
    "app.main:app",
    "src.app:app",
]


def try_find_app():
    for path in COMMON_PATHS:
        try:
            if ":" in path:
                modname, attr = path.split(":", 1)
            else:
                modname, attr = path, "app"
            mod = importlib.import_module(modname)
            if hasattr(mod, attr):
                return getattr(mod, attr)
        except Exception:
            continue
    return None


# --- Attempt to find an app ---
app = try_find_app()
if app is not None:
    # Determine ASGI vs WSGI
    is_asgi = False
    try:
        if inspect.iscoroutinefunction(getattr(app, "__call__", None)):
            is_asgi = True
        # FastAPI/Starlette usually has .router attribute too
        if getattr(app, "router", None) is not None:
            is_asgi = True
    except Exception:
        is_asgi = False

    if is_asgi:
        try:
            from mangum import Mangum
        except Exception as e:
            raise RuntimeError("mangum is required in the image to wrap ASGI apps. pip install mangum") from e
        handler = Mangum(app)

        def lambda_handler(event, context):
            return handler(event, context)

    else:
        try:
            import awsgi
        except Exception as e:
            raise RuntimeError("awsgi is required in the image to wrap WSGI apps. pip install awsgi") from e

        def lambda_handler(event, context):
            return awsgi.response(app, event, context)

else:
    # Fallback: run main() in background thread and proxy to local HTTP server
    # Assumptions: main() will start a local HTTP server binding to 127.0.0.1:5000
    # (this mirrors the default libretranslate run configuration).
    SERVER_HOST = os.environ.get("LT_LOCAL_BIND", "127.0.0.1")
    SERVER_PORT = int(os.environ.get("LT_LOCAL_PORT", "5000"))
    MAIN_MODULE = "libretranslate.main"

    server_started = threading.Event()
    server_thread = None
    server_start_exc = None

    def wait_for_port(host, port, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=1):
                    return True
            except Exception:
                time.sleep(0.25)
        return False

    def start_main_in_thread():
        global server_start_exc
        try:
            sys.path.insert(0, os.getcwd())
            mod = importlib.import_module("main") if importlib.util.find_spec("main") else importlib.import_module(MAIN_MODULE)
            # If module exposes main(), call it
            if hasattr(mod, "main") and callable(mod.main):
                # run in separate thread to avoid blocking handler
                def run_main():
                    try:
                        mod.main()
                    except Exception as e:
                        global server_start_exc
                        server_start_exc = e
                        traceback.print_exc()

                t = threading.Thread(target=run_main, daemon=True)
                t.start()
            else:
                # fallback: try to call module as script entry
                if hasattr(mod, "__main__"):
                    mod.main()
            # Wait for server to come up
            if wait_for_port(SERVER_HOST, SERVER_PORT, timeout=60.0):
                server_started.set()
            else:
                print(f"[handler] Server did not start on {SERVER_HOST}:{SERVER_PORT} within timeout", file=sys.stderr)
        except Exception as e:
            server_start_exc = e
            traceback.print_exc()
            server_started.set()

    # Start server thread lazily on first invocation
    def ensure_server_running():
        global server_thread

        # -----------------------------------------
        # NEW: Ensure models exist in EFS (bootstrap)
        # -----------------------------------------
        EFS_MOUNT_PATH = os.environ.get("LT_EFS_MOUNT", "/mnt/models")

        if os.path.exists(EFS_MOUNT_PATH):
            try:
                # First try an in-process import & call (fastest)
                from scripts import ensure_models_on_efs as _bootstrap
                print("[handler] EFS mount detected. Checking models in EFS...", file=sys.stderr)
                try:
                    ok = _bootstrap.ensure_models(mount_path=EFS_MOUNT_PATH)
                    if not ok:
                        print("[handler] WARNING: EFS model bootstrap returned False", file=sys.stderr)
                except Exception:
                    print("[handler] In-process bootstrap raised exception; falling back to subprocess", file=sys.stderr)
                    traceback.print_exc()
                    raise
            except Exception:
                # fallback: run as a subprocess to isolate issues and avoid messing with runtime state
                print("[handler] Running model bootstrap via subprocess...", file=sys.stderr)
                try:
                    # Use absolute path in container
                    subprocess.run(
                        ["python", "/var/task/scripts/ensure_models_on_efs.py"],
                        check=True,
                        timeout=900,
                    )
                except subprocess.CalledProcessError as cpe:
                    print(f"[handler] WARNING: EFS model bootstrap subprocess failed: {cpe}", file=sys.stderr)
                    traceback.print_exc()
                except Exception as e:
                    print(f"[handler] WARNING: EFS model bootstrap subprocess exception: {e}", file=sys.stderr)
                    traceback.print_exc()
        else:
            # No EFS mount present; skip bootstrap
            pass

        # -----------------------------------------
        # ORIGINAL LOGIC BELOW (UNCHANGED)
        # -----------------------------------------
        if server_started.is_set():
            return

        if server_thread is None or not server_thread.is_alive():
            server_thread = threading.Thread(target=start_main_in_thread, daemon=True)
            server_thread.start()

        # wait a bit for start
        server_started.wait(timeout=70.0)
        if server_start_exc:
            raise RuntimeError("Starting main() failed") from server_start_exc
        if not server_started.is_set():
            raise RuntimeError(f"Server did not become ready at {SERVER_HOST}:{SERVER_PORT}")

    # --- Helper to build URL for proxied request ---
    def build_target_url(event):
        # API Gateway v2 (Function URL) uses rawPath + rawQueryString
        path = event.get("rawPath") or event.get("path") or "/"
        qs = event.get("rawQueryString")
        if not qs:
            # older proxy v1
            qparams = event.get("queryStringParameters") or {}
            qs = urllib.parse.urlencode(qparams) if qparams else ""
        url = f"http://{SERVER_HOST}:{SERVER_PORT}{path}"
        if qs:
            url = url + ("?" + qs if not qs.startswith("?") else qs)
        return url

    def get_method(event):
        return event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "GET"

    def get_headers(event):
        return event.get("headers") or {}

    def get_body_bytes(event):
        body = event.get("body", "")
        if not body:
            return b""
        if event.get("isBase64Encoded", False):
            return base64.b64decode(body)
        return body.encode("utf-8") if isinstance(body, str) else body

    def forward_to_local(event):
        url = build_target_url(event)
        method = get_method(event).upper()
        headers = get_headers(event) or {}
        data = get_body_bytes(event) or None

        req = urllib.request.Request(url, data=data, method=method)
        for k, v in headers.items():
            # skip Host header, urllib will set it
            if k.lower() == "host":
                continue
            # urllib expects str values
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp_body = resp.read()
                resp_headers = dict(resp.getheaders())
                status = resp.getcode()
                # try to decode as utf-8, otherwise base64-encode
                try:
                    text = resp_body.decode("utf-8")
                    return {
                        "statusCode": status,
                        "headers": resp_headers,
                        "body": text,
                        "isBase64Encoded": False,
                    }
                except Exception:
                    b64 = base64.b64encode(resp_body).decode("ascii")
                    return {
                        "statusCode": status,
                        "headers": resp_headers,
                        "body": b64,
                        "isBase64Encoded": True,
                    }
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                text = body.decode("utf-8")
                return {"statusCode": e.code, "headers": dict(e.headers), "body": text, "isBase64Encoded": False}
            except Exception:
                b64 = base64.b64encode(body).decode("ascii")
                return {"statusCode": e.code, "headers": dict(e.headers), "body": b64, "isBase64Encoded": True}
        except Exception as e:
            return {"statusCode": 502, "body": f"Upstream proxy error: {e}"}

    def lambda_handler(event, context):
        # ensure server is running (calls main() in a background thread if necessary)
        ensure_server_running()
        # forward the request to the local HTTP server and return the response
        return forward_to_local(event)
