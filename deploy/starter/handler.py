"""Starter Lambda: sign-in flow and wake-up for the stopped-when-idle instance.

Behind CloudFront (function URL, reachable only through CloudFront):

* ``/_auth/login``       redirect to the Cognito hosted login (state cookie against CSRF)
* ``/_auth/callback``    exchange the code, verify the ID token, set the session cookie
* ``/_auth/logout``      clear the cookie and end the Cognito login too
* ``/_auth/signed-out``  landing page after logout / session end
* anything else          CloudFront failed over here because the instance is down:
                         signed-in users start it and get a page that reloads until
                         it is up; everyone else is sent to sign in first, so only
                         allowed people can wake (and pay for) the instance.
"""

from __future__ import annotations

import base64
import html
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

import boto3

from auth import SESSION_COOKIE, AuthError, JwtVerifier, cookie_value

HOST = os.environ["PUBLIC_HOST"]
BASE = f"https://{HOST}"
COGNITO = f"https://{os.environ['COGNITO_DOMAIN']}"
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
INSTANCE_ID = os.environ["INSTANCE_ID"]
CALLBACK = f"{BASE}/_auth/callback"
SIGNED_OUT = f"{BASE}/_auth/signed-out"
STATE_COOKIE = "srtvast_state"

verifier = JwtVerifier(os.environ["ISSUER"], CLIENT_ID)
ec2 = boto3.client("ec2")


def handler(event, context):
    path = event.get("rawPath") or "/"
    query = {k: v[-1] for k, v in urllib.parse.parse_qs(event.get("rawQueryString") or "").items()}
    cookies = "; ".join(event.get("cookies") or [])
    accept = (event.get("headers") or {}).get("accept", "")
    if path == "/_auth/login":
        return login()
    if path == "/_auth/callback":
        return callback(query, cookies)
    if path == "/_auth/logout":
        return logout()
    if path == "/_auth/signed-out":
        return page("Signed out", "You are signed out. Sessions last 30 minutes.",
                    '<a class="btn" href="/_auth/login">Sign in</a>')
    return wake(cookies, accept)


# ---------------------------------------------------------------- sign-in

def login():
    state = secrets.token_urlsafe(24)
    params = urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": CALLBACK,
        "scope": "openid email", "state": state,
    })
    return redirect(f"{COGNITO}/oauth2/authorize?{params}",
                    [cookie(STATE_COOKIE, state, 600, path="/_auth")])


def callback(query, cookies):
    if "error" in query:
        return page("Sign-in failed", html.escape(query.get("error_description") or query["error"]),
                    '<a class="btn" href="/_auth/login">Try again</a>', status=400)
    state = cookie_value(cookies, STATE_COOKIE)
    if not state or not secrets.compare_digest(state, query.get("state", "")):
        return page("Sign-in expired", "That sign-in link is stale. Please sign in again.",
                    '<a class="btn" href="/_auth/login">Sign in</a>', status=400)
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": CLIENT_ID,
        "code": query.get("code", ""), "redirect_uri": CALLBACK,
    }).encode()
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(f"{COGNITO}/oauth2/token", data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            tokens = json.load(resp)
        who = verifier.verify(tokens["id_token"])
    except Exception as exc:  # noqa: BLE001
        log("callback_failed", error=str(exc))
        return page("Sign-in failed", "Could not complete sign-in.",
                    '<a class="btn" href="/_auth/login">Try again</a>', status=400)
    log("signed_in", email=who.email, expires=who.expires)
    max_age = max(0, int(who.expires - time.time()))
    return redirect("/", [cookie(SESSION_COOKIE, tokens["id_token"], max_age),
                          cookie(STATE_COOKIE, "", 0, path="/_auth")])


def logout():
    params = urllib.parse.urlencode({"client_id": CLIENT_ID, "logout_uri": SIGNED_OUT})
    return redirect(f"{COGNITO}/logout?{params}", [cookie(SESSION_COOKIE, "", 0)])


# ---------------------------------------------------------------- wake-up

def wake(cookies, accept):
    token = cookie_value(cookies, SESSION_COOKIE)
    try:
        if not token:
            raise AuthError("not signed in")
        who = verifier.verify(token)
    except AuthError:
        if "text/html" in accept:
            return redirect("/_auth/login")
        return {"statusCode": 401, "headers": {"Cache-Control": "no-store"}, "body": "sign in required\n"}

    try:
        state = ec2.describe_instances(InstanceIds=[INSTANCE_ID])["Reservations"][0]["Instances"][0]["State"]["Name"]
        if state == "stopped":
            ec2.start_instances(InstanceIds=[INSTANCE_ID])
            log("started", email=who.email, instance=INSTANCE_ID)
            state = "starting"
    except Exception as exc:  # noqa: BLE001
        log("wake_failed", error=str(exc))
        state = "unavailable"

    if "text/html" not in accept:  # streams / API: the page retries on its own
        return {"statusCode": 503, "headers": {"Cache-Control": "no-store", "Retry-After": "10"},
                "body": f"stream server {state}\n"}
    note = {
        "running": "The server is up; the stream services are starting.",
        "stopping": "The server is just shutting down after being idle; it will start again in a moment.",
        "unavailable": "The server could not be reached. This page keeps trying.",
    }.get(state, "The stream server was asleep and is starting.")
    return page("Starting the stream server…",
                f"{note} This usually takes 1–2 minutes; this page reloads by itself.",
                f'<p class="muted">Instance state: <code>{html.escape(state)}</code> · '
                f'signed in as {html.escape(who.email)} · <a href="/_auth/logout">Log out</a></p>',
                refresh=8, spinner=True)


# ---------------------------------------------------------------- helpers

def cookie(name, value, max_age, path="/"):
    return f"{name}={value}; Max-Age={max_age}; Path={path}; Secure; HttpOnly; SameSite=Lax"


def redirect(location, cookies=None):
    return {"statusCode": 302, "headers": {"Location": location, "Cache-Control": "no-store"},
            "cookies": cookies or [], "body": ""}


def log(event, **fields):
    print(json.dumps({"event": event, **fields}))


def page(title, message, extra="", status=200, refresh=None, spinner=False):
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    spin = '<div class="spin" aria-hidden="true"></div>' if spinner else ""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{meta}<title>SRT VAST</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0d1014; color: #d9e1ea;
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; padding: 16px; box-sizing: border-box; }}
  main {{ max-width: 460px; text-align: center; }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }}
  p {{ margin: 6px 0; }}
  .muted {{ color: #7d8a99; font-size: 13px; }}
  .spin {{ width: 28px; height: 28px; margin: 0 auto 18px; border: 3px solid #262f3a; border-top-color: #2fbf71;
          border-radius: 50%; animation: s 1s linear infinite; }}
  @keyframes s {{ to {{ transform: rotate(360deg); }} }}
  a {{ color: #d9e1ea; }}
  .btn {{ display: inline-block; margin-top: 14px; padding: 8px 18px; border: 1px solid #3a4552; border-radius: 6px;
          background: #1b222b; text-decoration: none; }}
  code {{ color: #d9e1ea; }}
</style></head>
<body><main>{spin}<h1>{html.escape(title)}</h1><p>{message}</p>{extra}</main></body></html>"""
    return {"statusCode": status, "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
            "body": body}
