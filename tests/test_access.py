"""Cloudflare Access JWT verification and its use by the viewer."""

import base64
import hashlib
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from srtvast.access import AccessError, AccessVerifier, rsa_verify_sha256, token_from_headers
from srtvast.viewer import Hub, ViewerServer

# Throwaway 2048-bit test key (generated for these tests only, e = 65537).
N = 0xc3e6a7ff2d0dae87ecf28a02b1d682d39af36e575356229c5c54c02f34e69a4ccf8a2df61f25529fbe4d1c5d27beb265dd3cbcfab0d932b6c41c3c89d3acda75edd114875d1dda720de58b9b06145dea79fe1336c569fef55e0f05d28d17c9abd1b034f43ea715d9c77830c6351304f40416a58a33647ae690d26bf3f2e30895f99565454fd5216f7a7325827bfd55be5b863d86f8472fc5f17a6695a0232ea5c48f332fd3194dc78f5bff1398d28d3734f4ad32810e80ecee2a86573a2a4c4072c1021c041bff1d94c21f1ff79f0c6f0c2e1d396da85784e6a10d51bbb8ef05f1f2f8df78c64b603fc5681fb0df975d93794ad29ca0f86e48c06bb25bc11309
D = 0x61ce98e4d1c92970eacfe330da3a67e21f5457ac58cac208001f3e3eb37248ea7d9a173d712a70b899ca2515ab80cdd006e7cad07d184bf62d2e8dd9206a863de7c9022f5d41c8ed35dbfbda1fec6e4161bc053accd0917f4f01401e71dc450fb866f9e1522098cbbd07d88e3534743e22866e0af482bfcbbad59c1955949d9566d0f9f2fafc533c69dd6efd1241f392e223ae7dd8132311781ff5012f81f278dacbfa550e56fb9430d980edac9302ee25198679aeb4947b6830d957e621e6ddb5db26aa4828d3e783919c43196dd62cc950b71460da1858d12d0920919c57f5f92df5e5ee509be76f478178fefe0ea8c49af7d81c4dc7eaeba1c54606a46e8b
E = 65537
TEAM = "myteam.cloudflareaccess.com"
AUD = "aud-tag-123"
DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign(message: bytes) -> bytes:
    k = (N.bit_length() + 7) // 8
    t = DIGEST_INFO + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), D, N).to_bytes(k, "big")


def make_token(exp, aud=AUD, iss=f"https://{TEAM}", kid="k1", email="ana@example.com"):
    header = b64url(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    claims = b64url(json.dumps({"aud": [aud], "iss": iss, "exp": exp, "iat": exp - 1800,
                                "email": email, "sub": "u1"}).encode())
    return f"{header}.{claims}.{b64url(sign(f'{header}.{claims}'.encode()))}"


JWKS = json.dumps({"keys": [{"kid": "k1", "kty": "RSA", "alg": "RS256",
                             "n": b64url(N.to_bytes(256, "big")), "e": b64url(E.to_bytes(3, "big"))}]}).encode()


def verifier():
    fetched = []
    v = AccessVerifier(TEAM, AUD, fetch=lambda url: fetched.append(url) or JWKS)
    return v, fetched


class AccessTests(unittest.TestCase):
    def test_valid_token(self):
        v, fetched = verifier()
        who = v.verify(make_token(exp=2_000_000_000), now=1_999_999_000)
        self.assertEqual((who.email, who.expires), ("ana@example.com", 2_000_000_000.0))
        self.assertEqual(fetched, [f"https://{TEAM}/cdn-cgi/access/certs"])

    def test_rejections(self):
        v, _ = verifier()
        now = 1_999_999_000
        cases = {
            "expired": make_token(exp=now - 60),
            "other app": make_token(exp=now + 600, aud="someone-else"),
            "other issuer": make_token(exp=now + 600, iss="https://evil.cloudflareaccess.com"),
            "unknown key": make_token(exp=now + 600, kid="k2"),
        }
        for name, token in cases.items():
            with self.subTest(name), self.assertRaises(AccessError):
                v.verify(token, now=now)
        good = make_token(exp=now + 600)
        h, c, s = good.split(".")
        forged_claims = b64url(json.dumps({"aud": [AUD], "iss": f"https://{TEAM}", "exp": now + 99999,
                                           "email": "mallory@example.com"}).encode())
        for name, token in {"tampered claims": f"{h}.{forged_claims}.{s}", "garbage": "a.b",
                            "alg none": b64url(b'{"alg":"none"}') + f".{c}."}.items():
            with self.subTest(name), self.assertRaises(AccessError):
                v.verify(token, now=now)

    def test_rsa_primitive_rejects_wrong_message(self):
        sig = sign(b"hello")
        self.assertTrue(rsa_verify_sha256(b"hello", sig, N, E))
        self.assertFalse(rsa_verify_sha256(b"hellO", sig, N, E))

    def test_token_from_header_or_cookie(self):
        self.assertEqual(token_from_headers({"Cf-Access-Jwt-Assertion": "abc"}), "abc")
        self.assertEqual(token_from_headers({"Cookie": "x=1; CF_Authorization=tok"}), "tok")
        self.assertIsNone(token_from_headers({}))


class ViewerAuthTests(unittest.TestCase):
    def setUp(self):
        v, _ = verifier()
        self.server = ViewerServer(("127.0.0.1", 0), Hub(10), Hub(10, backlog=5), v)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, token=None):
        req = urllib.request.Request(self.base + path,
                                     headers={"Cf-Access-Jwt-Assertion": token} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_requires_access_token(self):
        self.assertEqual(self.get("/")[0], 403)
        self.assertEqual(self.get("/live.ts")[0], 403)
        self.assertEqual(self.get("/", make_token(exp=1))[0], 403)  # expired

    def test_session_and_status(self):
        import time
        exp = int(time.time()) + 1800
        code, body = self.get("/api/session", make_token(exp=exp))
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {"email": "ana@example.com", "expires": exp, "auth": True})
        code, body = self.get("/api/status")  # watchdog endpoint: no token, counts only
        status = json.loads(body)
        self.assertEqual((code, status["streams"]), (200, 0))
        self.assertGreaterEqual(status["idle_seconds"], 0)

    def test_event_stream_ends_with_the_session(self):
        import time
        token = make_token(exp=int(time.time()) + 2)
        started = time.monotonic()
        code, body = self.get("/events", token)
        self.assertEqual(code, 200)
        self.assertIn(b"event: expired", body)
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()
