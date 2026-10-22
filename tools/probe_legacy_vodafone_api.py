#!/usr/bin/env python3
"""One-shot probe for the legacy Vodafone Italy MyVodafone REST API.

This is a diagnostic only; it is not used by the dashboard and does not
change the deployed application. It uses Python's standard library only.

Set VODAFONE_USERNAME and VODAFONE_PASSWORD in the shell where you run it.
The script never prints credentials, phone numbers, session IDs, tokens, or raw
API responses. It makes one login attempt and does not try to bypass MFA.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import uuid
from urllib import error, parse, request

BASE_URL = "https://my190.vodafone.it"
TIMEOUT_SECONDS = 15
USER_AGENT = "VodafoneBalance-LegacyApiProbe/0.1"


class ProbeError(RuntimeError):
    pass


def post_json(path: str, payload: dict, headers: dict | None = None):
    return request_json("POST", path, payload, headers)


def get_json(path: str, headers: dict | None = None):
    return request_json("GET", path, None, headers)


def request_json(method: str, path: str, payload: dict | None, headers: dict | None):
    all_headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if payload is not None:
        all_headers["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        all_headers.update(headers)

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(BASE_URL + path, data=body, headers=all_headers, method=method)
    try:
        with request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
            raw = response.read(1024 * 1024)
    except error.HTTPError as exc:
        status = exc.code
        raw = exc.read(1024 * 1024)
    except (error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None)
        # Avoid printing request details, credentials, or any response content.
        detail = type(reason).__name__ if reason is not None else type(exc).__name__
        raise ProbeError(f"network request failed ({detail})") from None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    return status, data


def response_obj(data):
    if not isinstance(data, dict):
        return {}
    nested = data.get("result")
    return nested if isinstance(nested, dict) else data


def safe_status(data):
    """Extract only non-sensitive response markers for a useful diagnostic."""
    if not isinstance(data, dict):
        return "non-JSON response"
    description = data.get("description")
    code = data.get("code") or data.get("errorCode")
    markers = []
    if isinstance(description, str):
        markers.append("description=" + description[:80])
    if isinstance(code, (str, int)):
        markers.append("code=" + str(code)[:40])
    return ", ".join(markers) if markers else "JSON response without standard status fields"


def contains_mfa_marker(data) -> bool:
    if not isinstance(data, dict):
        return False
    keys_to_check = ("description", "code", "error", "status", "message", "detail")
    text = " ".join(str(data.get(k, "")) for k in keys_to_check).lower()
    terms = ("otp", "mfa", "two factor", "two-factor", "2fa", "verification code",
             "sms", "one time password", "one-time password", "challenge")
    return any(term in text for term in terms)


def require_ok(stage: str, status: int, data):
    if status < 200 or status >= 300 or not isinstance(data, dict):
        raise ProbeError(f"{stage} failed: HTTP {status}; {safe_status(data)}")


def main() -> int:
    username = os.environ.get("VODAFONE_USERNAME", "")
    password = os.environ.get("VODAFONE_PASSWORD", "")
    if not username or not password:
        print("Set VODAFONE_USERNAME and VODAFONE_PASSWORD before running this probe.")
        return 2

    # Match the installation/session handshake documented by the historical
    # MyVodafone Android client implementation.
    device_id = str(uuid.uuid4())
    print("1/4 Registering a legacy MyVodafone API installation...")
    try:
        status, registration = post_json(
            "/api/v1/client/register",
            {"app_id": "MyVodafone", "device_udid": device_id,
             "app_platform": "Android", "app_version": "4.03"},
        )
        require_ok("installation registration", status, registration)
        install_obj = response_obj(registration)
        installation_id = install_obj.get("installation_id")
        secret_key = install_obj.get("secret_key")
        if not isinstance(installation_id, str) or not isinstance(secret_key, str):
            raise ProbeError("registration response did not contain expected installation fields")
        print("   Registration endpoint responded with the expected fields.")

        signature_data = ("iid=" + installation_id).encode("utf-8")
        signature = base64.b64encode(
            hmac.new(secret_key.encode("utf-8"), signature_data, hashlib.sha1).digest()
        ).decode("ascii")
        common_headers = {
            "X-Bwb-InstallationId": installation_id,
            "Authorization": "MYVF_V1 " + signature,
        }

        print("2/4 Starting a legacy API session...")
        status, session_response = post_json(
            "/api/v1/session/start",
            {"app_id": "MyVodafone", "app_platform": "Android",
             "app_version": "4.03", "os_name": "Android", "os_version": "2.3.4",
             "device_vendor": "IPhone", "device_model": "3gs",
             "screen_width": "320", "screen_height": "480", "screen_ratio": "1",
             "push_provider": "Android", "push_types": "broadcast-simple",
             "device_is_tablet": False, "device_udid": device_id},
            common_headers,
        )
        require_ok("session start", status, session_response)
        session_obj = response_obj(session_response)
        session_id = session_obj.get("session_id")
        if not isinstance(session_id, str):
            raise ProbeError("session response did not contain a session ID")
        print("   Session endpoint responded with the expected fields.")

        print("3/4 Attempting one username/password login (no OTP automation)...")
        login_headers = {**common_headers, "X-Bwb-SessionId": session_id}
        status, login_response = post_json(
            "/api/v1/session/login",
            {"username": username, "password": password},
            login_headers,
        )
        if status < 200 or status >= 300 or not isinstance(login_response, dict):
            if contains_mfa_marker(login_response):
                print("   The login endpoint returned an HTTP response with an MFA/verification marker.")
                print("RESULT: this legacy API flow does not complete login unattended.")
                return 4
            require_ok("login", status, login_response)
        description = login_response.get("description")
        login_obj = response_obj(login_response)
        sims = login_obj.get("sims")
        if description != "RESPONSE_OK" or not isinstance(sims, list) or not sims:
            if contains_mfa_marker(login_response):
                print("   The legacy login returned an MFA/verification marker.")
                print("RESULT: this API flow does not complete login unattended.")
                return 4
            print("   Login was not accepted in the expected legacy format: "
                  + safe_status(login_response))
            print("RESULT: legacy API login is not currently usable by this probe.")
            return 5

        first_sim = sims[0] if isinstance(sims[0], dict) else {}
        msisdn = first_sim.get("msisdn")
        if not isinstance(msisdn, str) or not msisdn:
            raise ProbeError("login succeeded, but no SIM identifier was returned")

        print("4/4 Requesting the balance for the authenticated SIM...")
        status, balance_response = get_json(
            "/api/v1/sim/" + parse.quote(msisdn, safe="") + "/balance",
            {"X-Bwb-SessionId": session_id},
        )
        require_ok("balance request", status, balance_response)
        balance_obj = response_obj(balance_response)
        balance = balance_obj.get("balance")
        if not isinstance(balance, (int, float)) or isinstance(balance, bool):
            print("   Authentication succeeded, but the balance response format was unexpected.")
            print("RESULT: API authentication works; balance parsing needs adjustment.")
            return 6

        print(f"   Balance returned: €{balance:.2f}")
        print("RESULT: legacy HTTP API succeeded for this login.")
        print("NOTE: this one-time result does not prove the login will remain MFA-free in future.")
        return 0

    except ProbeError as exc:
        print(f"RESULT: probe inconclusive or failed: {exc}")
        print("No raw API response or secret was printed.")
        return 3


if __name__ == "__main__":
    sys.exit(main())
