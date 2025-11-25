"""
Universal Lambda handler for LibreTranslate with EFS bootstrap and S3 fallback.
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
import tarfile
import shutil

# For S3 download
try:
    import boto3
except Exception:
    boto3 = None

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

app = try_find_app()
if app is not None:
    # Determine ASGI vs WSGI
    is_asgi = False
    try:
        if inspect.iscoroutinefunction(getattr(app, "__call__", None)):
            is_asgi = True
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

    def download_and_extract_model_from_s3(bucket, key, dest_dir="/tmp/models"):
        if not bucket or not key:
            print("[bootstrap] S3 bucket/key not provided", file=sys.stderr)
            return False

        if os.path.exists(dest_dir) and os.listdir(dest_dir):
            print(f"[bootstrap] Models already exist in {dest_dir}")
            return True

        os.makedirs(dest_dir, exist_ok=True)
        if boto3 is None:
            print("[bootstrap] boto3 not available in runtime. Cannot download models.", file=sys.stderr)
            return False

        s3 = boto3.client("s3")
        local_archive = os.path.join(dest_dir, os.path.basename(key))

        try:
            print(f"[bootstrap] Downloading s3://{bucket}/{key} to {local_archive}")
            s3.download_file(bucket, key, local_archive)
        except Exception as e:
            print(f"[bootstrap] ERROR downloading model from S3: {e}", file=sys.stderr)
            traceback.print_exc()
            return False

        # if tar.gz, extract
        try:
            if tarfile.is_tarfile(local_archive):
                print(f"[bootstrap] Extracting {local_archive} -> {dest_dir}")
                with tarfile.open(local_archive, "r:gz") as tf:
                    tf.extractall(dest_dir)
                try:
                    os.remove(local_archive)
                except Exception:
                    pass
            else:
                # not an archive -- leave file there
                pass
        except Exception as e:
            print(f"[bootstrap] ERROR extracting model archive: {e}", file=sys.stderr)
            traceback.print_exc()
            return False

        return True

    def start_main_in_thread():
        global server_start_exc
        try:
            sys.path.insert(0, os.getcwd())
            mod = importlib.import_module("main") if importlib.util.find_spec("main") else importlib.import_module(MAIN_MODULE)
            if hasattr(mod, "main") and callable(mod.main):
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
                if hasattr(mod, "__main__"):
                    mod.main()
            if wait_for_port(SERVER_HOST, SERVER_PORT, timeout=60.0):
                server_started.set()
            else:
                print(f"[handler] Server did not start on {SERVER_HOST}:{SERVER_PORT} within timeout", file=sys.stderr)
        except Exception as e:
            server_start_exc = e
            traceback.print_exc()
            server_started.set()

    def ensure_server_running():
        global server_thread

        # EFS bootstrap
        EFS_MOUNT_PATH = os.environ.get("LT_EFS_MOUNT", "/mnt/models")
        if os.path.exists(EFS_MOUNT_PATH) and os.listdir(EFS_MOUNT_PATH):
            try:
                from scripts import ensure_models_on_efs as _bootstrap
                print("[handler] EFS mount detected. Running in-process EFS bootstrap.", file=sys.stderr)
                try:
                    ok = _bootstrap.ensure_models(mount_path=EFS_MOUNT_PATH)
                    if not ok:
                        print("[handler] WARNING: EFS model bootstrap returned False", file=sys.stderr)
                except Exception:
                    print("[handler] In-process bootstrap raised exception; falling back to subprocess", file=sys.stderr)
                    traceback.print_exc()
                    raise
            except Exception:
                print("[handler] Running model bootstrap via subprocess...", file=sys.stderr)
                try:
                    subprocess.run(["python", "/var/task/scripts/ensure_models_on_efs.py"], check=True, timeout=900)
                except Exception as e:
                    print(f"[handler] WARNING: EFS bootstrap subprocess failed: {e}", file=sys.stderr)
                    traceback.print_exc()
        else:
            # No EFS -> attempt S3 download into /tmp/models
            s3_bucket = os.environ.get("LT_S3_BUCKET") or os.environ.get("MODEL_BUCKET")
            s3_key = os.environ.get("LT_S3_MODEL_KEY") or os.environ.get("MODEL_KEY")
            if s3_bucket and s3_key:
                ok = download_and_extract_model_from_s3(s3_bucket, s3_key, dest_dir="/tmp/models")
                if ok:
                    # ensure application can find models at /mnt/models by symlink
                    try:
                        if os.path.exists("/mnt/models"):
                            if not os.path.islink("/mnt/models") and os.path.isdir("/mnt/models") and not os.listdir("/mnt/models"):
                                shutil.rmtree("/mnt/models", ignore_errors=True)
                        if not os.path.exists("/mnt/models"):
                            os.symlink("/tmp/models", "/mnt/models")
                    except Exception:
                        print("[handler] Warning: failed to create symlink /mnt/models -> /tmp/models", file=sys.stderr)
                else:
                    print("[bootstrap] Model download from S3 failed or incomplete", file=sys.stderr)
            else:
                print("[bootstrap] No EFS mount and no LT_S3_BUCKET/LT_S3_MODEL_KEY configured; skipping model bootstrap.", file=sys.stderr)

        # start server if not started
        if server_started.is_set():
            return

        if server_thread is None or not server_thread.is_alive():
            server_thread = threading.Thread(target=start_main_in_thread, daemon=True)
            server_thread.start()

        server_started.wait(timeout=70.0)
        if server_start_exc:
            raise RuntimeError("Starting main() failed") from server_start_exc
        if not server_started.is_set():
            raise RuntimeError(f"Server did not become ready at {SERVER_HOST}:{SERVER_PORT}")

    def build_target_url(event):
        path = event.get("rawPath") or event.get("path") or "/"
        qs = event.get("rawQueryString")
        if not qs:
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
            if k.lower() == "host":
                continue
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp_body = resp.read()
                resp_headers = dict(resp.getheaders())
                status = resp.getcode()
                try:
                    text = resp_body.decode("utf-8")
                    return {"statusCode": status, "headers": resp_headers, "body": text, "isBase64Encoded": False}
                except Exception:
                    b64 = base64.b64encode(resp_body).decode("ascii")
                    return {"statusCode": status, "headers": resp_headers, "body": b64, "isBase64Encoded": True}
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
        ensure_server_running()
        return forward_to_local(event)
