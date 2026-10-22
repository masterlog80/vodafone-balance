import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, request, Response
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

VODAFONE_LOGIN_URL = "https://www.vodafone.it/area-utente/fai-da-te/Common/PaginaUnicadiLogin.html"
PROFILE_DIR = Path(os.getenv("PROFILE_DIR", "/data/profile"))
APP_USERNAME = os.getenv("APP_USERNAME", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
PORT = int(os.getenv("PORT", "8080"))

app = Flask(__name__)
browser_state = {"page": None, "context": None, "last_check": None, "last_balance": None, "last_error": None}
playwright_runtime = None


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
        context = " ".join(lines[max(0, i - 2):min(len(lines), i + 3)])
        if re.search(r"credito|residuo|saldo", context, re.I):
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
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source": "Vodafone Fai da te (authenticated browser)", "evidence": evidence,
    }
    browser_state["last_check"] = result["checked_at"]
    browser_state["last_balance"] = result
    browser_state["last_error"] = None
    return result


@app.get("/")
@require_auth
def index():
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vodafone Balance</title><style>body{font:16px system-ui;max-width:760px;margin:3rem auto;padding:0 1rem;color:#222}button,a{font:inherit;padding:.65rem 1rem;margin:.3rem 0;cursor:pointer}.card{border:1px solid #ddd;border-radius:12px;padding:1rem;margin:1rem 0}#result{font-size:2rem;font-weight:650}small{color:#555}code{overflow-wrap:anywhere}</style>
<h1>Vodafone Balance</h1><p>Checks the credit shown in your authenticated Vodafone Fai da te account.</p>
<div class="card"><button onclick="checkBalance()">Check balance</button><div id="result">No balance checked yet</div><p id="detail"></p></div>
<div class="card"><h2>Browser session</h2><p>Open the browser display to complete the first login or SMS verification. The browser profile is stored on the persistent volume.</p>
<p><a href="/api/status">View status JSON</a></p><p><strong>Browser display:</strong> use the separate kubectl port-forward instructions in the README, then open noVNC at <code>http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale</code>.</p></div>
<script>async function checkBalance(){const r=document.getElementById('result'),d=document.getElementById('detail');r.textContent='Checking…';d.textContent='';try{const res=await fetch('/api/balance',{method:'POST'});const data=await res.json();if(!res.ok)throw new Error(data.error||'Request failed');r.textContent=data.balance_eur+' €';d.textContent='Checked '+data.checked_at+' · '+data.source;}catch(e){r.textContent='Unable to read balance';d.textContent=e.message}}</script></html>"""


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/api/status")
@require_auth
def status():
    return jsonify({"browser_ready": browser_state.get("page") is not None,
                    "last_check": browser_state["last_check"], "last_balance": browser_state["last_balance"],
                    "last_error": browser_state["last_error"], "login_url": VODAFONE_LOGIN_URL})


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
