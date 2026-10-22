"""The starter Lambda (deploy/starter/handler.py): sign-in flow and wake-up."""

import importlib
import io
import json
import os
import sys
import time
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from srtvast import auth
from tests.test_auth import CLIENT, ISSUER, JWKS, make_token


class FakeEc2:
    def __init__(self):
        self.state, self.started = "stopped", []

    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [{"State": {"Name": self.state}}]}]}

    def start_instances(self, InstanceIds):
        self.started += InstanceIds


def load_handler(ec2):
    os.environ.update(PUBLIC_HOST="srt.example.com", COGNITO_DOMAIN="login.example.com", CLIENT_ID=CLIENT,
                      CLIENT_SECRET="s3cret", ISSUER=ISSUER, INSTANCE_ID="i-0abc")
    sys.modules["boto3"] = types.SimpleNamespace(client=lambda name: ec2)
    sys.modules["auth"] = auth  # the Lambda zip ships srtvast/auth.py as auth.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "starter"))
    try:
        handler = importlib.reload(sys.modules["handler"]) if "handler" in sys.modules else importlib.import_module("handler")
    finally:
        sys.path.pop(0)
    handler.verifier._fetch = lambda url: JWKS
    return handler


def event(path, query="", cookies=None, accept="text/html"):
    return {"rawPath": path, "rawQueryString": query, "cookies": cookies or [], "headers": {"accept": accept}}


class StarterTests(unittest.TestCase):
    def setUp(self):
        self.ec2 = FakeEc2()
        self.h = load_handler(self.ec2)

    def test_login_redirects_to_hosted_ui_with_state(self):
        r = self.h.handler(event("/_auth/login"), None)
        self.assertEqual(r["statusCode"], 302)
        url = urllib.parse.urlsplit(r["headers"]["Location"])
        q = dict(urllib.parse.parse_qsl(url.query))
        self.assertEqual((url.netloc, url.path), ("login.example.com", "/oauth2/authorize"))
        self.assertEqual((q["client_id"], q["redirect_uri"]), (CLIENT, "https://srt.example.com/_auth/callback"))
        self.assertIn(f"srtvast_state={q['state']};", r["cookies"][0])

    def test_callback_exchanges_code_and_sets_session_cookie(self):
        exp = int(time.time()) + 1800
        token = make_token(exp=exp)
        seen = {}

        def fake_urlopen(req, timeout):
            seen["url"], seen["auth"] = req.full_url, req.headers["Authorization"]
            return io.BytesIO(json.dumps({"id_token": token, "access_token": "x"}).encode())

        with mock.patch.object(self.h.urllib.request, "urlopen", fake_urlopen):
            r = self.h.handler(event("/_auth/callback", "code=abc&state=S1", ["srtvast_state=S1"]), None)
        self.assertEqual((r["statusCode"], r["headers"]["Location"]), (302, "/"))
        self.assertEqual(seen["url"], "https://login.example.com/oauth2/token")
        self.assertTrue(seen["auth"].startswith("Basic "))
        session = r["cookies"][0]
        self.assertTrue(session.startswith(f"srtvast_session={token};"))
        self.assertIn("Secure; HttpOnly; SameSite=Lax", session)
        max_age = int(session.split("Max-Age=")[1].split(";")[0])
        self.assertTrue(1700 < max_age <= 1800)

    def test_callback_rejects_forged_state(self):
        r = self.h.handler(event("/_auth/callback", "code=abc&state=EVIL", ["srtvast_state=S1"]), None)
        self.assertEqual(r["statusCode"], 400)

    def test_logout_clears_cookie_and_ends_cognito_login(self):
        r = self.h.handler(event("/_auth/logout"), None)
        self.assertIn("srtvast_session=; Max-Age=0", r["cookies"][0])
        self.assertTrue(r["headers"]["Location"].startswith("https://login.example.com/logout?"))
        self.assertIn(urllib.parse.quote("https://srt.example.com/_auth/signed-out", safe=""), r["headers"]["Location"])

    def test_signed_out_visitor_cannot_wake_the_instance(self):
        r = self.h.handler(event("/"), None)
        self.assertEqual((r["statusCode"], r["headers"]["Location"]), (302, "/_auth/login"))
        r = self.h.handler(event("/live.ts", accept="*/*"), None)
        self.assertEqual(r["statusCode"], 401)
        self.assertEqual(self.ec2.started, [])

    def test_signed_in_visitor_starts_a_stopped_instance(self):
        cookie = [f"srtvast_session={make_token(exp=int(time.time()) + 600)}"]
        r = self.h.handler(event("/", cookies=cookie), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertIn("Starting the stream server", r["body"])
        self.assertIn('http-equiv="refresh"', r["body"])
        self.assertEqual(self.ec2.started, ["i-0abc"])

    def test_does_not_start_when_already_running(self):
        self.ec2.state = "running"
        cookie = [f"srtvast_session={make_token(exp=int(time.time()) + 600)}"]
        r = self.h.handler(event("/events", cookies=cookie, accept="text/event-stream"), None)
        self.assertEqual((r["statusCode"], r["body"]), (503, "stream server running\n"))
        self.assertEqual(self.ec2.started, [])


if __name__ == "__main__":
    unittest.main()
