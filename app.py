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
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

APP_NAME = "Vodafone Balance"
APP_VERSION = "1.2.0"
VODAFONE_LOGIN_URL = "https://www.vodafone.it/area-utente/fai-da-te/Common/PaginaUnicadiLogin.html"
PROFILE_DIR = Path(os.getenv("PROFILE_DIR", "/data/profile"))
APP_USERNAME = os.getenv("APP_USERNAME", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
PORT = int(os.getenv("PORT", "8080"))
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
POD_NAME = os.getenv("POD_NAME", "")
POD_NAMESPACE = os.getenv("POD_NAMESPACE", "default")
FALLBACK_IMAGE = os.getenv("IMAGE_NAME", "zot.salvetti.info/vodafone-balance:latest")

app = Flask(__name__)
browser_state = {"page": None, "context": None, "last_check": None, "last_balance": None, "last_error": None}
# Opt-in, in-memory metadata capture for troubleshooting the current Vodafone web flow.
# Request/response bodies, headers, cookies, query strings, and field values are never stored.
network_diagnostics = {"enabled": False, "events": []}
NETWORK_EVENT_LIMIT = 80
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



def _safe_url_path(raw_url):
    """Keep endpoint shape while removing query strings and likely personal identifiers."""
    path = urlsplit(raw_url).path or "/"
    path = re.sub(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", "{id}", path)
    path = re.sub(r"\d{5,}", "{id}", path)
    parts = []
    for part in path.split("/"):
        if "@" in part or len(part) > 48:
            part = "{redacted}"
        elif len(part) >= 10 and re.search(r"\d", part) and re.fullmatch(r"[A-Za-z0-9._-]+", part):
            part = "{id}"
        parts.append(part)
    return "/".join(parts)[:240] or "/"


def _json_shape(value, depth=0):
    """Describe JSON structure using key names and type labels only, never values."""
    if isinstance(value, dict):
        keys = sorted(str(key) for key in value.keys())[:40]
        result = {"type": "object", "keys": keys}
        if depth < 2:
            children = {}
            for key, child in list(value.items())[:10]:
                if isinstance(child, (dict, list)):
                    children[str(key)[:80]] = _json_shape(child, depth + 1)
            if children:
                result["children"] = children
        return result
    if isinstance(value, list):
        result = {"type": "array", "length_bucket": "0" if not value else "1+"}
        if value:
            result["item_shape"] = _json_shape(value[0], depth + 1) if depth < 2 else (
                {"type": "object", "keys": sorted(str(k) for k in value[0].keys())[:40]}
                if isinstance(value[0], dict) else {"type": type(value[0]).__name__}
            )
        return result
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    return {"type": "other"}


def capture_network_request(request_obj):
    """Capture sanitized metadata from completed Vodafone-origin XHR/fetch requests."""
    if not network_diagnostics["enabled"]:
        return
    try:
        if request_obj.resource_type not in ("xhr", "fetch"):
            return
        parsed = urlsplit(request_obj.url)
        host = (parsed.hostname or "").lower()
        if host != "vodafone.it" and not host.endswith(".vodafone.it"):
            return
        response = request_obj.response()
        if response is None:
            return

        item = {
            "method": request_obj.method[:12],
            "host": host[:120],
            "path": _safe_url_path(request_obj.url),
            "resource_type": request_obj.resource_type,
            "status": int(response.status),
            "content_type": str(response.headers.get("content-type", "")).split(";", 1)[0][:80],
        }
        content_type = item["content_type"].lower()
        if "json" in content_type:
            try:
                item["json_shape"] = _json_shape(response.json())
            except Exception:
                item["json_shape"] = {"type": "unavailable"}

        events = network_diagnostics["events"]
        events.append(item)
        if len(events) > NETWORK_EVENT_LIMIT:
            del events[:-NETWORK_EVENT_LIMIT]
    except Exception:
        # Diagnostics must never interrupt the actual balance check.
        return


def attach_network_diagnostics(page):
    page.on("requestfinished", capture_network_request)


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
    attach_network_diagnostics(page)
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
<html lang="en" data-theme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light"><title>Vodafone Balance</title>
<style>
:root{color-scheme:dark;--bg:#0d1117;--bg2:#161b22;--card:#1c2431;--border:#30363d;--green:#3fb950;--blue:#58a6ff;--red:#f85149;--yellow:#e3b341;--txt:#e6edf3;--muted:#8b949e;--shadow:0 8px 24px #00000012}
:root[data-theme="light"]{color-scheme:light;--bg:#f6f8fa;--bg2:#eef2f6;--card:#fff;--border:#d0d7de;--green:#1a7f37;--blue:#0969da;--red:#cf222e;--yellow:#9a6700;--txt:#1f2328;--muted:#656d76;--shadow:0 4px 14px #1f23280d}
*{box-sizing:border-box}html,body{margin:0;min-height:100%;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;background:var(--bg);color:var(--txt)}body{min-height:100vh;display:flex;flex-direction:column}button,select{font:inherit}
#app-header{background:var(--card);border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;position:relative;z-index:2}
.header-brand{display:flex;align-items:center;gap:12px;min-width:230px}.header-logo{width:38px;height:38px;display:grid;place-items:center;border:1px solid #f8514933;border-radius:10px;background:#f8514914;color:var(--red);font-size:24px;font-weight:800}.header-title{font-size:18px;line-height:1.25;font-weight:700;color:var(--blue);margin:0}.header-sub{font-size:11px;color:var(--muted);margin-top:2px}.header-right{display:flex;align-items:center;justify-content:flex-end;gap:9px;flex-wrap:wrap}.header-badge{font-size:11px;font-weight:600;padding:4px 10px;border-radius:20px;border:1px solid var(--border);display:inline-flex;align-items:center;gap:7px;white-space:nowrap}.badge-ok{color:var(--green);border-color:color-mix(in srgb,var(--green) 35%,transparent);background:color-mix(in srgb,var(--green) 8%,transparent)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);flex-shrink:0}.refresh-control{display:flex;align-items:center;gap:7px;padding:4px 8px;border:1px solid var(--border);border-radius:8px;color:var(--muted)}.refresh-control select{color:var(--txt);background:transparent;border:0;outline:none;cursor:pointer;font-size:12px}.refresh-control option{background:var(--card);color:var(--txt)}.refresh-ring{position:relative;width:34px;height:34px;display:grid;place-items:center}.ring-svg{position:absolute;inset:0;width:34px;height:34px;transform:rotate(-90deg)}.ring-bg,.ring-fg{fill:none;stroke-width:3}.ring-bg{stroke:var(--border)}.ring-fg{stroke:var(--blue);stroke-linecap:round;transition:stroke-dashoffset .3s}.ring-label{font-size:10px;font-weight:700;color:var(--txt);font-variant-numeric:tabular-nums}.icon-btn{width:34px;height:34px;display:grid;place-items:center;background:var(--card);color:var(--txt);border:1px solid var(--border);border-radius:8px;cursor:pointer;font-size:17px}.icon-btn:hover{border-color:var(--blue);color:var(--blue)}.icon-btn:focus-visible,button:focus-visible,select:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
main{width:min(100% - 32px,1180px);margin:26px auto 30px;flex:1}.page-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:18px}.eyebrow{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:700}.page-heading h2{font-size:24px;line-height:1.2;margin:4px 0 0;letter-spacing:-.03em}.subheading{font-size:12px;color:var(--muted)}
.top-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(270px,.8fr);gap:18px}.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:22px;box-shadow:var(--shadow);min-width:0}.card-title{display:flex;align-items:center;gap:9px;font-size:15px;font-weight:650;margin:0 0 20px}.title-icon{display:grid;place-items:center;width:30px;height:30px;border-radius:8px;background:var(--bg2);color:var(--blue);font-size:17px}.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.09em;font-weight:700}.balance-row{display:flex;align-items:baseline;flex-wrap:wrap;gap:9px;margin:12px 0 8px}.balance{font-size:clamp(48px,7vw,74px);letter-spacing:-.065em;font-weight:750;line-height:1.08;font-variant-numeric:tabular-nums}.currency{font-size:18px;color:var(--muted);font-weight:600}.detail{font-size:12px;color:var(--muted);min-height:19px}.actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:22px}.btn{border:1px solid var(--border);border-radius:8px;padding:9px 13px;background:var(--bg2);color:var(--txt);font-weight:650;font-size:12px;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:7px;cursor:pointer}.btn:hover{border-color:var(--blue)}.btn-primary{background:#1f6feb;border-color:#1f6feb;color:#fff}.btn-primary:hover{background:#388bfd;color:#fff}.btn:disabled{opacity:.55;cursor:wait}.message{min-height:20px;margin-top:12px;color:var(--red);font-size:12px;overflow-wrap:anywhere}
.account-card{display:flex;flex-direction:column;gap:16px}.account-copy{font-size:12px;color:var(--muted);margin:0}.session-row{display:flex;align-items:center;gap:11px;border-top:1px solid var(--border);padding-top:15px;margin-top:auto}.session-icon{width:34px;height:34px;border-radius:8px;background:var(--bg2);border:1px solid var(--border);display:grid;place-items:center;font-size:16px;color:var(--blue)}.session-copy strong{display:block;font-size:12px}.session-copy span{display:block;font-size:11px;color:var(--muted)}.account-card .btn{width:100%}
.section-heading{font-size:14px;font-weight:650;margin:24px 0 11px}.info-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.info{background:var(--card);border:1px solid var(--border);border-radius:9px;padding:15px;min-width:0}.info .value{font-size:13px;font-weight:650;margin-top:7px;overflow-wrap:anywhere}.info .value code{font:inherit;color:var(--txt)}.info .hint{font-size:11px;color:var(--muted);margin-top:3px}
#app-footer{border-top:1px solid var(--border);padding:15px 24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px 16px;font-size:12px;color:var(--muted);background:var(--card)}.footer-left,.footer-right{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.footer-name{font-weight:650;color:var(--txt)}.footer-sep{color:var(--border);user-select:none}.footer-image{font-family:"SFMono-Regular",Consolas,monospace;font-size:11px;overflow-wrap:anywhere}.footer-version{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border:1px solid var(--border);border-radius:999px;background:var(--bg2);color:var(--txt);font-size:12px;font-weight:700;white-space:nowrap}.footer-version .dot{width:7px;height:7px}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid #ffffff66;border-top-color:white;border-radius:50%;animation:spin .7s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
@media(max-width:760px){#app-header{padding:12px 15px}.header-right{justify-content:flex-start;width:100%}.header-brand{min-width:0}main{width:calc(100% - 24px);margin-top:19px}.top-grid{grid-template-columns:1fr}.info-grid{grid-template-columns:1fr}.card{padding:18px}.page-heading h2{font-size:21px}#app-footer{padding:14px 15px}.footer-right{width:100%;justify-content:space-between}.footer-image{max-width:calc(100vw - 150px)}}@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
</style>
<script>
(function(){try{var t=localStorage.getItem('vodafoneBalanceTheme');document.documentElement.setAttribute('data-theme',t==='light'?'light':'dark')}catch(e){document.documentElement.setAttribute('data-theme','dark')}})();
</script></head><body>
<header id="app-header">
  <div class="header-brand"><div class="header-logo" aria-hidden="true">V</div><div><h1 class="header-title">Vodafone Balance</h1><div class="header-sub">PREPAID CREDIT · ACCOUNT STATUS</div></div></div>
  <div class="header-right">
    <span class="header-badge badge-ok"><span class="dot"></span><span>Dashboard ready</span></span>
    <label class="refresh-control" title="Automatically check balance at this interval"><span aria-hidden="true">↻</span><span class="sr-only">Auto-refresh interval</span><select id="refresh-interval" aria-label="Auto-refresh interval"><option value="0">Auto-refresh off</option><option value="300">5 min</option><option value="600">10 min</option><option value="900" selected>15 min</option><option value="1800">30 min</option><option value="3600">60 min</option></select></label>
    <div class="refresh-ring" title="Time until next automatic balance check" aria-label="Time until next refresh"><svg class="ring-svg" viewBox="0 0 36 36" aria-hidden="true"><circle class="ring-bg" cx="18" cy="18" r="15"></circle><circle id="ring-progress" class="ring-fg" cx="18" cy="18" r="15" stroke-dasharray="94.25 94.25" stroke-dashoffset="0"></circle></svg><span class="ring-label" id="countdown">15m</span></div>
    <button class="icon-btn" type="button" id="refresh-now" title="Check balance now" aria-label="Check balance now">↻</button>
    <button class="icon-btn" type="button" id="theme-toggle" title="Switch to light mode" aria-label="Switch to light mode"><span id="theme-icon" aria-hidden="true">☼</span></button>
  </div>
</header>
<main>
  <div class="page-heading"><div><div class="eyebrow">Account overview</div><h2>Remaining credit</h2></div><div class="subheading" id="refresh-status">Auto-refresh every 15 minutes</div></div>
  <section class="top-grid">
    <article class="card">
      <h3 class="card-title"><span class="title-icon" aria-hidden="true">€</span> Vodafone prepaid balance</h3>
      <div class="label">Available credit</div><div class="balance-row"><div class="balance" id="result">—</div><span class="currency">EUR</span></div>
      <div class="detail" id="detail">Run a check to read the latest credit from Vodafone Fai da te.</div>
      <div class="actions"><button class="btn btn-primary" type="button" id="check">↻ &nbsp; Check balance</button><a class="btn" href="/api/status" target="_blank" rel="noreferrer">View API status ↗</a></div>
      <div class="message" id="message" role="status" aria-live="polite"></div>
    </article>
    <aside class="card account-card">
      <h3 class="card-title"><span class="title-icon" aria-hidden="true">◉</span> Vodafone account</h3>
      <p class="account-copy">Checks the credit through your authenticated Vodafone Fai da te browser session. The session is saved on persistent storage and can require SMS verification again if Vodafone expires it.</p>
      <div class="session-row"><div class="session-icon" aria-hidden="true">↗</div><div class="session-copy"><strong>Browser session</strong><span>Manual login and SMS verification</span></div></div>
      <a class="btn" id="browser-session" href="http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale" target="_blank" rel="noreferrer">Open browser session ↗</a>
      <p class="account-copy">Open only through a local port-forward. Do not expose noVNC publicly.</p>
    </aside>
  </section>
  <h3 class="section-heading">Runtime information</h3>
  <section class="info-grid">
    <div class="info"><div class="label">Last successful check</div><div class="value" id="last-check">Not checked yet</div><div class="hint">Time of the last balance result</div></div>
    <div class="info"><div class="label">Application version</div><div class="value" id="version">Loading…</div><div class="hint">Version running in this process</div></div>
    <div class="info"><div class="label">Kubernetes namespace</div><div class="value" id="namespace">Loading…</div><div class="hint">Runtime environment</div></div>
  </section>
</main>
<footer id="app-footer">
  <div class="footer-left"><span class="footer-name" id="app-name">Vodafone Balance</span><span class="footer-sep">·</span><span>Started <time id="startup">Loading…</time></span></div>
  <div class="footer-right"><span class="footer-image" title="Container image name and tag">Image <strong id="image">Detecting…</strong></span><span class="footer-version"><span class="dot"></span><span id="footer-version">v1.2.0</span></span></div>
</footer>
<script>
const $=id=>document.getElementById(id);
const circumference=94.25;
let infoData=null, busy=false, refreshSeconds=900, remaining=900, timer=null;
function fmt(value){if(!value)return '—';try{return new Date(value).toLocaleString()}catch(e){return value}}
function setBalance(balance, checkedAt){$('result').textContent=Number(balance).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});$('last-check').textContent=fmt(checkedAt);$('detail').textContent='Last checked '+fmt(checkedAt)}
async function loadInfo(){try{const r=await fetch('/api/info',{cache:'no-store'});if(!r.ok)throw new Error('Runtime information unavailable');infoData=await r.json();$('app-name').textContent=infoData.name;$('startup').textContent=fmt(infoData.started_at);$('version').textContent='v'+infoData.version;$('footer-version').textContent='v'+infoData.version;$('image').textContent=infoData.image;$('namespace').textContent=infoData.namespace}catch(e){$('image').textContent='Unavailable'}}
async function loadStatus(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)return;const d=await r.json();if(d.last_check){$('last-check').textContent=fmt(d.last_check);if(d.last_balance)setBalance(d.last_balance.balance_eur,d.last_balance.checked_at)}if(d.last_error&&!d.last_balance)$('detail').textContent='Previous check failed; check the browser session and retry.'}catch(e){}}
async function checkBalance(automatic=false){if(busy)return;busy=true;const b=$('check'),m=$('message');b.disabled=true;b.innerHTML='<span class="spinner"></span> Checking…';if(!automatic)m.textContent='';$('detail').textContent=automatic?'Automatic balance check in progress…':'Connecting to Vodafone…';try{const res=await fetch('/api/balance',{method:'POST',cache:'no-store'});const data=await res.json();if(!res.ok)throw new Error(data.error||'Balance check failed');setBalance(data.balance_eur,data.checked_at);m.textContent='';await loadInfo()}catch(e){m.textContent=e.message;$('detail').textContent='Check the browser session and try again.'}finally{busy=false;b.disabled=false;b.textContent='↻  Check balance';remaining=refreshSeconds;updateRing()}}
function updateRing(){const ring=$('ring-progress'),label=$('countdown');if(refreshSeconds===0){label.textContent='—';ring.style.strokeDashoffset='94.25';return}const ratio=Math.max(0,Math.min(1,remaining/refreshSeconds));ring.style.strokeDashoffset=String(circumference*(1-ratio));label.textContent=remaining>=3600?Math.ceil(remaining/3600)+'h':remaining>=60?Math.ceil(remaining/60)+'m':String(remaining)}
function setRefresh(value){refreshSeconds=Number(value);remaining=refreshSeconds;try{localStorage.setItem('vodafoneBalanceRefresh',String(refreshSeconds))}catch(e){}$('refresh-status').textContent=refreshSeconds?'Auto-refresh every '+(refreshSeconds>=3600?refreshSeconds/3600+' hour'+(refreshSeconds>3600?'s':''):refreshSeconds/60+' minutes'):'Auto-refresh is off';updateRing()}
$('check').addEventListener('click',()=>checkBalance(false));$('refresh-now').addEventListener('click',()=>checkBalance(false));
$('refresh-interval').addEventListener('change',e=>setRefresh(e.target.value));
(function(){let saved=900;try{const n=Number(localStorage.getItem('vodafoneBalanceRefresh'));if([0,300,600,900,1800,3600].includes(n))saved=n;const theme=localStorage.getItem('vodafoneBalanceTheme')==='light'?'light':'dark';applyTheme(theme)}catch(e){applyTheme('dark')} $('refresh-interval').value=String(saved);setRefresh(saved)})();
function applyTheme(theme){document.documentElement.setAttribute('data-theme',theme);const light=theme==='light';$('theme-icon').textContent=light?'☾':'☼';$('theme-toggle').title=light?'Switch to dark mode':'Switch to light mode';$('theme-toggle').setAttribute('aria-label',$('theme-toggle').title);try{localStorage.setItem('vodafoneBalanceTheme',theme)}catch(e){}}
$('theme-toggle').addEventListener('click',()=>applyTheme(document.documentElement.getAttribute('data-theme')==='light'?'dark':'light'));
loadInfo();loadStatus();
setInterval(()=>{if(refreshSeconds===0)return;if(remaining>1){remaining--}else{remaining=refreshSeconds;checkBalance(true)}updateRing()},1000);
</script></body></html>"""



@app.post("/api/diagnostics/network/start")
@require_auth
def start_network_diagnostics():
    """Start collecting sanitized request metadata for the next manual balance check."""
    if browser_state.get("page") is None:
        return jsonify({"error": "Browser is not ready"}), 503
    network_diagnostics["events"].clear()
    network_diagnostics["enabled"] = True
    return jsonify({
        "enabled": True,
        "message": "Capture enabled. Now run one balance check, then GET /api/diagnostics/network.",
        "stored_fields": ["method", "host", "sanitized_path", "resource_type", "status", "content_type", "JSON key/type shape only"],
        "excluded": ["request/response values", "headers", "cookies", "query strings", "credentials", "tokens", "raw bodies"],
    })


@app.get("/api/diagnostics/network")
@require_auth
def read_network_diagnostics():
    return jsonify({
        "enabled": network_diagnostics["enabled"],
        "count": len(network_diagnostics["events"]),
        "events": network_diagnostics["events"],
    })


@app.post("/api/diagnostics/network/stop")
@require_auth
def stop_network_diagnostics():
    network_diagnostics["enabled"] = False
    return jsonify({"enabled": False, "retained_events": len(network_diagnostics["events"])})


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
