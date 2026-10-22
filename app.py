import json
import os
import re
import secrets
import ssl
import subprocess
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, Response, jsonify, request
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

APP_NAME = "Vodafone Balance"
APP_VERSION = "1.1.0"
VODAFONE_LOGIN_URL = "https://www.vodafone.it/area-utente/fai-da-te/Common/PaginaUnicadiLogin.html"
PROFILE_DIR = Path(os.getenv("PROFILE_DIR", "/data/profile"))
APP_USERNAME = os.getenv("APP_USERNAME", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
PORT = int(os.getenv("PORT", "8080"))
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
POD_NAME = os.getenv("POD_NAME", "")
POD_NAMESPACE = os.getenv("POD_NAMESPACE", "default")
FALLBACK_IMAGE = os.getenv("IMAGE_NAME", "ghcr.io/masterlog80/vodafone-balance:latest")

app = Flask(__name__)
browser_state = {"page": None, "context": None, "last_check": None, "last_balance": None, "last_error": None}
playwright_runtime = None
pod_info_cache = {"image": None, "checked_at": 0}


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not APP_USERNAME or not APP_PASSWORD:
            return jsonify({"error": "Application authentication is not configured"}), 503
        auth = request.authorization
        if not auth or not (
            secrets.compare_digest(auth.username or "", APP_USERNAME)
            and secrets.compare_digest(auth.password or "", APP_PASSWORD)
        ):
            return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Vodafone Balance"'})
        return fn(*args, **kwargs)
    return wrapper


def get_running_image():
    """Read this pod's image from the Kubernetes API using the pod-reader ServiceAccount."""
    if pod_info_cache["image"] and time.time() - pod_info_cache["checked_at"] < 30:
        return pod_info_cache["image"]
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    if not POD_NAME or not token_path.is_file():
        return FALLBACK_IMAGE
    try:
        token = token_path.read_text().strip()
        api = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        url = f"https://{api}:{port}/api/v1/namespaces/{POD_NAMESPACE}/pods/{POD_NAME}"
        req = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        context = ssl.create_default_context(cafile=str(ca_path)) if ca_path.exists() else ssl.create_default_context()
        with urlopen(req, context=context, timeout=3) as response:
            pod = json.loads(response.read().decode("utf-8"))
        for container in pod.get("spec", {}).get("containers", []):
            if container.get("name") == "app":
                pod_info_cache.update(image=container.get("image") or FALLBACK_IMAGE, checked_at=time.time())
                return pod_info_cache["image"]
    except (HTTPError, URLError, OSError, ValueError, TimeoutError) as exc:
        app.logger.warning("Could not read running Pod image from Kubernetes API: %s", type(exc).__name__)
    return FALLBACK_IMAGE


def app_info():
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "started_at": STARTED_AT,
        "image": get_running_image(),
        "pod": POD_NAME or None,
        "namespace": POD_NAMESPACE,
    }


def start_display():
    subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1440x900x24", "-ac"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = ":99"
    time.sleep(1)
    subprocess.Popen(["x11vnc", "-display", ":99", "-forever", "-shared", "-nopw",
                      "-listen", "127.0.0.1", "-rfbport", "5900"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.Popen(["websockify", "--web=/usr/share/novnc", "0.0.0.0:6080", "127.0.0.1:5900"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_browser():
    global playwright_runtime
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    playwright_runtime = sync_playwright().start()
    context = playwright_runtime.chromium.launch_persistent_context(
        str(PROFILE_DIR), executable_path="/usr/bin/chromium", headless=False,
        viewport={"width": 1360, "height": 820},
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
    browser_state["context"] = context
    page = context.pages[0] if context.pages else context.new_page()
    browser_state["page"] = page
    try:
        page.goto(VODAFONE_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        browser_state["last_error"] = None
    except Exception as exc:
        browser_state["last_error"] = f"Initial Vodafone page load failed: {type(exc).__name__}"


def read_balance():
    page = browser_state.get("page")
    if page is None:
        raise RuntimeError("Browser is not ready")
    try:
        page.goto(VODAFONE_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
    except PlaywrightTimeoutError:
        pass
    body = page.locator("body").inner_text(timeout=15000)
    normalized = re.sub(r"[ \t]+", " ", body)
    if re.search(r"(inserisci|username|password|accedi|codice.*sms|codice di verifica)", normalized, re.I) and not re.search(r"credito residuo", normalized, re.I):
        message = "Login may be required. Complete Vodafone login/SMS verification in the browser window, then retry."
        browser_state["last_error"] = message
        raise RuntimeError(message)

    lines = [re.sub(r"\s+", " ", line).strip() for line in body.splitlines() if line.strip()]
    candidates = []
    patterns = [
        re.compile(r"(?<!\d)(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:€|EUR)", re.I),
        re.compile(r"€\s*(\d{1,4}(?:[.,]\d{1,2})?)", re.I),
    ]
    for i, line in enumerate(lines):
        if not re.search(r"credito|residuo|saldo", line, re.I):
            continue
        context = " ".join(lines[max(0, i - 1):min(len(lines), i + 2)])
        for pattern in patterns:
            for match in pattern.finditer(context):
                try:
                    amount = float(match.group(1).replace(".", "").replace(",", "."))
                    if 0 <= amount <= 10000:
                        candidates.append((amount, context[:240]))
                except ValueError:
                    pass
    if not candidates:
        message = "Could not confidently identify the residual credit. Inspect the logged-in page; the Vodafone layout may have changed."
        browser_state["last_error"] = message
        raise RuntimeError(message)
    amount, evidence = candidates[0]
    result = {
        "balance_eur": f"{amount:.2f}", "currency": "EUR",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Vodafone Fai da te (authenticated browser)", "evidence": evidence,
    }
    browser_state["last_check"] = result["checked_at"]
    browser_state["last_balance"] = result
    browser_state["last_error"] = None
    return result


@app.get("/")
@require_auth
def index():
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vodafone Balance</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#131c30;--line:#28364f;--muted:#8ea1bd;--text:#edf4ff;--accent:#e60000;--green:#39d98a}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 15% 0%,#202d4c 0,transparent 44%),var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;min-height:100vh;display:flex;flex-direction:column}
main{width:min(100% - 36px,1000px);margin:38px auto 30px;flex:1}.top{display:flex;align-items:center;gap:14px;margin-bottom:28px}.logo{display:grid;place-items:center;width:48px;height:48px;border-radius:14px;background:linear-gradient(145deg,#ff3434,#c80000);font-size:25px;font-weight:900;box-shadow:0 8px 30px #e6000038}.eyebrow{color:#ff8585;text-transform:uppercase;letter-spacing:.15em;font-size:11px;font-weight:800}.top h1{font-size:clamp(24px,4vw,32px);line-height:1.15;margin:3px 0 0;letter-spacing:-.04em}.status{margin-left:auto;display:flex;align-items:center;gap:8px;border:1px solid #22543f;border-radius:999px;padding:7px 12px;color:#a5f3c9;font-size:12px;white-space:nowrap}.dot{width:7px;height:7px;background:var(--green);border-radius:50%;box-shadow:0 0 10px #39d98a80}.hero{display:grid;grid-template-columns:1.3fr .7fr;gap:18px}.card{background:linear-gradient(155deg,#172239,#111a2d);border:1px solid var(--line);border-radius:20px;padding:25px;box-shadow:0 16px 45px #00000020}.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.12em;font-weight:750}.balance{font-size:clamp(46px,8vw,76px);letter-spacing:-.06em;font-weight:780;line-height:1.12;margin:20px 0 8px}.balance .currency{font-size:.45em;color:#aebdd4;letter-spacing:0;margin-left:6px}.sub{color:var(--muted);font-size:13px}.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:26px}button,a.btn{border:0;border-radius:11px;padding:12px 17px;font-weight:750;font-size:14px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:8px}button.primary{background:var(--accent);color:white;box-shadow:0 6px 22px #e6000030}button.primary:hover{background:#ff2424}a.btn{border:1px solid #354761;color:#dbe7f8;background:#17243a}.side{display:flex;flex-direction:column;justify-content:space-between;gap:18px}.side .big-icon{font-size:30px}.side h2{font-size:19px;margin:10px 0 6px}.side p{color:var(--muted);font-size:13px;margin:0}.session{display:flex;align-items:center;gap:12px;padding-top:18px;margin-top:20px;border-top:1px solid var(--line)}.session-icon{width:38px;height:38px;border:1px solid #34435c;border-radius:12px;display:grid;place-items:center;background:#1c2940}.session strong{display:block;font-size:13px}.session span{font-size:12px;color:var(--muted)}.section-title{font-size:16px;margin:26px 0 12px}.info-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.info{background:#10192a;border:1px solid #202d43;border-radius:13px;padding:15px;min-width:0}.info .value{font-size:13px;font-weight:650;margin-top:7px;overflow-wrap:anywhere}.info .value code{font:inherit;color:#d8e5f8}.footer{border-top:1px solid #202d43;background:#0a0f1c;padding:15px max(18px,calc((100vw - 1000px)/2));display:flex;flex-wrap:wrap;gap:8px 22px;color:#8394ae;font-size:11px}.footer span{white-space:nowrap}.footer strong{color:#c5d3e8;font-weight:650}.footer .image{overflow-wrap:anywhere;white-space:normal}.message{min-height:24px;margin-top:12px;color:#ffb5b5;font-size:13px;overflow-wrap:anywhere}#detail{color:var(--muted);font-size:12px}.spinner{display:inline-block;width:13px;height:13px;border:2px solid #ffffff55;border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}@media(max-width:680px){main{margin-top:22px}.hero{grid-template-columns:1fr}.card{padding:21px}.info-grid{grid-template-columns:1fr}.status{font-size:0;padding:9px}.status .dot{width:9px;height:9px}.footer{gap:7px 14px}}
</style></head><body><main>
<header class="top"><div class="logo">V</div><div><div class="eyebrow">Personal dashboard</div><h1>Vodafone Balance</h1></div><div class="status"><i class="dot"></i> Service online</div></header>
<section class="hero"><article class="card"><div class="label">Remaining credit</div><div class="balance" id="result">— <span class="currency">EUR</span></div><div class="sub" id="detail">Check your Vodafone prepaid balance when you’re ready.</div><div class="actions"><button class="primary" id="check" onclick="checkBalance()">↻ &nbsp; Check balance</button><a class="btn" href="/api/status" target="_blank" rel="noreferrer">View API status ↗</a></div><div class="message" id="message" role="status"></div></article>
<aside class="card side"><div><div class="big-icon">◉</div><h2>Vodafone account</h2><p>Securely checks the credit from your authenticated Vodafone Fai da te session. Your session is stored on the persistent Kubernetes volume.</p></div><div class="session"><div class="session-icon">↗</div><div><strong>Browser session</strong><span>Manual SMS verification supported</span></div></div><a class="btn" href="http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale" target="_blank" rel="noreferrer" onclick="return confirm('Open noVNC? It must be exposed only through your local kubectl port-forward.')">Open browser session ↗</a></aside></section>
<h2 class="section-title">Runtime information</h2><section class="info-grid"><div class="info"><div class="label">Last successful check</div><div class="value" id="last-check">Not checked yet</div></div><div class="info"><div class="label">Application version</div><div class="value" id="version">Loading…</div></div><div class="info"><div class="label">Kubernetes namespace</div><div class="value" id="namespace">Loading…</div></div></section>
</main><footer class="footer"><span>App <strong id="app-name">Vodafone Balance</strong></span><span>Startup <strong id="startup">Loading…</strong></span><span>Version <strong id="footer-version">Loading…</strong></span><span class="image">Image <strong id="image">Detecting running image…</strong></span></footer>
<script>
function fmt(value){if(!value)return '—';try{return new Date(value).toLocaleString()}catch{return value}}
async function loadInfo(){try{const r=await fetch('/api/info');if(!r.ok)throw new Error('Unable to load runtime information');const d=await r.json();document.getElementById('app-name').textContent=d.name;document.getElementById('startup').textContent=fmt(d.started_at);document.getElementById('version').textContent=d.version;document.getElementById('footer-version').textContent=d.version;document.getElementById('image').textContent=d.image;document.getElementById('namespace').textContent=d.namespace}catch(e){document.getElementById('image').textContent='Unavailable';}}
async function loadStatus(){try{const r=await fetch('/api/status');if(!r.ok)return;const d=await r.json();if(d.last_check){document.getElementById('last-check').textContent=fmt(d.last_check);if(d.last_balance){document.getElementById('result').innerHTML=Number(d.last_balance.balance_eur).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})+' <span class="currency">EUR</span>';document.getElementById('detail').textContent='Last checked '+fmt(d.last_check)}}}catch(e){}}
async function checkBalance(){const b=document.getElementById('check'),r=document.getElementById('result'),d=document.getElementById('detail'),m=document.getElementById('message');b.disabled=true;b.innerHTML='<i class="spinner"></i> Checking…';m.textContent='';d.textContent='Connecting to Vodafone…';try{const res=await fetch('/api/balance',{method:'POST'});const data=await res.json();if(!res.ok)throw new Error(data.error||'Request failed');r.innerHTML=Number(data.balance_eur).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})+' <span class="currency">EUR</span>';d.textContent='Checked '+fmt(data.checked_at);document.getElementById('last-check').textContent=fmt(data.checked_at)}catch(e){m.textContent=e.message;d.textContent='Check the browser session and try again.'}finally{b.disabled=false;b.innerHTML='↻ &nbsp; Check balance'}}
loadInfo();loadStatus();
</script></body></html>"""


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/api/info")
@require_auth
def info():
    return jsonify(app_info())


@app.get("/api/status")
@require_auth
def status():
    return jsonify({"browser_ready": browser_state.get("page") is not None,
                    "last_check": browser_state["last_check"], "last_balance": browser_state["last_balance"],
                    "last_error": browser_state["last_error"], "login_url": VODAFONE_LOGIN_URL,
                    **app_info()})


@app.post("/api/balance")
@require_auth
def balance():
    try:
        return jsonify(read_balance())
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        browser_state["last_error"] = message
        return jsonify({"error": message, "last_check": browser_state["last_check"]}), 502


if __name__ == "__main__":
    start_display()
    start_browser()
    app.run(host="0.0.0.0", port=PORT, threaded=False)
