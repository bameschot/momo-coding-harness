"""HTTPS for the web UI (--web-tls auto, --web-cert) and plain HTTP without a token
(--web-insecure).

The certificate tests shell out to openssl, as tls.py does; they run once per
distinct openssl binary found (macOS ships LibreSSL at /usr/bin/openssl), because
tls.py must only use options both implementations understand.
"""
import http.client
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import harness as hmod
from harness import session as session_mod
from harness.controller import Controller
from harness.llm.base import ChatResponse, LLMClient
from harness.web import tls
from harness.web.server import start_web_server


def _openssls() -> list[str]:
    found = []
    for cand in ("/usr/bin/openssl", shutil.which("openssl")):
        if cand and os.path.exists(cand) and os.path.realpath(cand) not in map(os.path.realpath, found):
            found.append(cand)
    return found


OPENSSLS = _openssls()


class FakeClient(LLMClient):
    provider_name = "fake"

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return ChatResponse(content="ok")

    def context_length(self):
        return 8192

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


@unittest.skipUnless(OPENSSLS, "openssl not installed")
class AutoCA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "tls"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ca_and_server_cert_per_openssl(self):
        for openssl in OPENSSLS:
            with self.subTest(openssl=openssl):
                shutil.rmtree(self.dir, ignore_errors=True)
                a = tls.ensure("127.0.0.1", openssl=openssl, directory=self.dir)
                self.assertTrue(a.issued)
                self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)
                for key in (self.dir / "momo-ca.key", a.key):
                    self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                self.assertIn("localhost", a.names)
                self.assertIn("127.0.0.1", a.names)
                # Python's default context is strict (VERIFY_X509_STRICT since 3.13).
                ctx = ssl.create_default_context(cafile=str(a.ca))
                for host in ("localhost", "127.0.0.1"):
                    self._handshake(a, ctx, host)
                text = subprocess.run([openssl, "x509", "-in", str(a.ca), "-noout", "-text"],
                                      capture_output=True, text=True).stdout
                self.assertIn("Name Constraints", text)

    def test_reuse_and_reissue(self):
        a = tls.ensure("127.0.0.1", openssl=OPENSSLS[0], directory=self.dir)
        ca_mtime, srv_mtime = a.ca.stat().st_mtime_ns, a.cert.stat().st_mtime_ns
        b = tls.ensure("127.0.0.1", openssl=OPENSSLS[0], directory=self.dir)
        self.assertFalse(b.issued)
        self.assertEqual(b.cert.stat().st_mtime_ns, srv_mtime)
        # A new name the CA permits (a private address) re-issues the server cert only.
        c = tls.ensure("127.0.0.1", ["10.9.8.7"], openssl=OPENSSLS[0], directory=self.dir)
        self.assertTrue(c.issued)
        self.assertIn("10.9.8.7", c.names)
        self.assertEqual(c.ca.stat().st_mtime_ns, ca_mtime)

    def test_name_outside_constraints_is_refused(self):
        tls.ensure("127.0.0.1", openssl=OPENSSLS[0], directory=self.dir)
        with self.assertRaises(tls.TLSError) as cm:
            tls.ensure("127.0.0.1", ["momo.example.com"], openssl=OPENSSLS[0], directory=self.dir)
        self.assertIn("name constraints", str(cm.exception))

    def test_name_given_at_ca_creation_is_permitted(self):
        a = tls.ensure("127.0.0.1", ["momo.example.com"], openssl=OPENSSLS[0], directory=self.dir)
        self.assertIn("momo.example.com", a.names)

    def test_group_writable_file_refused(self):
        a = tls.ensure("127.0.0.1", openssl=OPENSSLS[0], directory=self.dir)
        os.chmod(a.key, 0o620)
        with self.assertRaises(tls.TLSError):
            tls.ensure("127.0.0.1", openssl=OPENSSLS[0], directory=self.dir)

    def test_missing_openssl(self):
        with mock.patch.object(tls.shutil, "which", return_value=None):
            with self.assertRaises(tls.TLSError) as cm:
                tls.ensure("127.0.0.1", directory=self.dir)
        self.assertIn("openssl", str(cm.exception))

    def _handshake(self, a, client_ctx, host):
        import socket
        import threading
        srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        srv_ctx.load_cert_chain(a.cert, a.key)
        with socket.socket() as ls:
            ls.bind(("127.0.0.1", 0))
            ls.listen()

            def serve():
                conn, _ = ls.accept()
                try:
                    with srv_ctx.wrap_socket(conn, server_side=True) as s:
                        s.recv(1)
                except OSError:
                    pass
            t = threading.Thread(target=serve, daemon=True)
            t.start()
            with socket.create_connection(("127.0.0.1", ls.getsockname()[1])) as raw:
                with client_ctx.wrap_socket(raw, server_hostname=host) as s:
                    self.assertTrue(s.getpeercert()["subjectAltName"])
            t.join(5)


class _ServerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: FakeClient("http://fake", "fake")
        self._patches = [mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                         mock.patch.dict(os.environ, {"HOME": str(self.home)})]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        self.controller = Controller(self.h)
        self.web = None

    def tearDown(self):
        if self.web is not None:
            self.web.close()
        hmod.make_client = self._orig
        for p in reversed(self._patches):
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()


@unittest.skipUnless(OPENSSLS, "openssl not installed")
class TLSServer(_ServerBase):
    def setUp(self):
        super().setUp()
        self.cert = tls.ensure("127.0.0.1", openssl=OPENSSLS[0], directory=self.home / "tls")
        self.ctx = ssl.create_default_context(cafile=str(self.cert.ca))

    def _start(self, token):
        self.web = start_web_server(self.controller, "127.0.0.1", 0, token,
                                    cert=self.cert.cert, key=self.cert.key, ca_pem=self.cert.ca)

    def _get(self, path, headers=None):
        c = http.client.HTTPSConnection("localhost", self.web.port, timeout=5, context=self.ctx)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        return r, r.read()

    def test_https_url_and_state(self):
        self._start(None)
        self.assertTrue(self.web.url.startswith("https://"))
        r, _ = self._get("/api/state")
        self.assertEqual(r.status, 200)

    def test_token_cookie_is_secure(self):
        self._start("tok123")
        r, _ = self._get("/?token=tok123")
        self.assertEqual(r.status, 303)
        cookie = r.getheader("Set-Cookie")
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertEqual(self._get("/api/state")[0].status, 401)
        self.assertEqual(self._get("/?token=wrong")[0].status, 401)
        self.assertEqual(self._get("/api/state", {"Authorization": "Bearer tok123"})[0].status, 200)

    def test_ca_download_needs_no_token(self):
        self._start("tok123")
        r, body = self._get("/momo-ca.pem")
        self.assertEqual(r.status, 200)
        self.assertEqual(body, self.cert.ca.read_bytes())
        self.assertNotIn(b"PRIVATE KEY", body)

    def test_plain_http_to_tls_port_does_not_wedge_server(self):
        self._start(None)
        c = http.client.HTTPConnection("127.0.0.1", self.web.port, timeout=3)
        with self.assertRaises((OSError, http.client.HTTPException)):
            c.request("GET", "/api/state")
            c.getresponse().read()
        self.assertEqual(self._get("/api/state")[0].status, 200)

    def test_bad_key_fails_before_binding(self):
        bad = self.home / "bad.key"
        bad.write_text("not a key\n")
        with self.assertRaises(OSError):
            start_web_server(self.controller, "127.0.0.1", 0, None, cert=self.cert.cert, key=bad)


class InsecureHosts(_ServerBase):
    """--web-insecure: no token, but the Host check still blocks DNS rebinding."""

    def _get(self, host_header):
        c = http.client.HTTPConnection("127.0.0.1", self.web.port, timeout=5)
        c.request("GET", "/api/state", headers={"Host": host_header})
        r = c.getresponse()
        r.read()
        return r.status

    def test_allow_list(self):
        self.web = start_web_server(self.controller, "127.0.0.1", 0, None,
                                    extra_hosts=["my-mac.local", "192.168.1.20", "fd00::1"])
        port = self.web.port
        self.assertEqual(self._get(f"my-mac.local:{port}"), 200)
        self.assertEqual(self._get(f"MY-MAC.LOCAL:{port}"), 200)
        self.assertEqual(self._get(f"192.168.1.20:{port}"), 200)
        self.assertEqual(self._get(f"[fd00::1]:{port}"), 200)
        self.assertEqual(self._get(f"localhost:{port}"), 200)
        self.assertEqual(self._get(f"evil.example:{port}"), 403)

    def test_local_names_cover_loopback_and_extra(self):
        names = tls.local_names("0.0.0.0", ["vpn.example"])
        for n in ("localhost", "127.0.0.1", "::1", "vpn.example"):
            self.assertIn(n, names)
        self.assertNotIn("0.0.0.0", names)


class CLIValidation(unittest.TestCase):
    def _err(self, *args):
        r = subprocess.run([os.sys.executable, "momo-coding-harness.py", *args],
                           cwd=Path(__file__).resolve().parent.parent,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2, r.stderr)
        return r.stderr

    def test_conflicts(self):
        self.assertIn("--web-insecure", self._err("--web-insecure", "--web-token", "x"))
        self.assertIn("--web-tls auto", self._err("--web-tls", "auto", "--web-cert", "c.pem"))
        self.assertIn("--web-key needs --web-cert", self._err("--web-key", "k.pem"))
        self.assertIn("--web-allow-host", self._err("--web-allow-host", "foo"))
        self.assertIn("--web-tls-name", self._err("--web-tls-name", "foo"))


if __name__ == "__main__":
    unittest.main()
