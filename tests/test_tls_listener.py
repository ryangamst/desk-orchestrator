"""Local socket integration test; never contacts AWS or an ACME service."""
import socket
import ssl
import subprocess
import sys
import time
import unittest

from tests import test_tls as fixtures
from desk_orchestrator.web import set_password


class TLSListenerTests(unittest.TestCase):
    def test_https_cookie_host_validation_and_live_certificate_reload(self):
        fixture = fixtures.TLSTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        set_password(fixture.directory, "private-test-password")
        cert_path, _ = fixture.certificate(serial=100)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        script = """
import sys
from desk_orchestrator import tls
tls.RELOAD_SECONDS = 0.1
tls.start = lambda *args: None  # Disable all remote certificate operations.
tls.serve(sys.argv[1], sys.argv[2], '127.0.0.1', int(sys.argv[3]))
"""
        log = (fixture.directory / "server.log").open("w")
        self.addCleanup(log.close)
        process = subprocess.Popen([sys.executable, "-c", script, str(fixture.directory), str(fixtures.SEED), str(port)], stdout=log, stderr=log)
        def stop():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        self.addCleanup(stop)

        def request(host):
            context = ssl.create_default_context(cafile=str(cert_path))
            with socket.create_connection(("127.0.0.1", port), timeout=1) as raw:
                with context.wrap_socket(raw, server_hostname=fixtures.CONFIG["domain"]) as client:
                    serial = client.getpeercert()["serialNumber"]
                    client.sendall(f"GET /login HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                    data = b""
                    while part := client.recv(65536):
                        data += part
                    return serial, data
        def eventually(host, serial):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    actual, response = request(host)
                    if int(actual, 16) == serial:
                        return response
                except (OSError, ssl.SSLError):
                    pass
                if process.poll() is not None:
                    self.fail((fixture.directory / "server.log").read_text())
                time.sleep(.05)
            self.fail("HTTPS listener did not serve the expected certificate.")

        response = eventually(fixtures.CONFIG["domain"], 100)
        self.assertIn(b"200 OK", response)
        cookies = [line for line in response.split(b"\r\n") if line.lower().startswith(b"set-cookie:")]
        self.assertEqual(len(cookies), 1)
        for value in (b"__Host-desk_session=", b"Secure", b"HttpOnly", b"Path=/", b"SameSite=Lax"):
            self.assertIn(value, cookies[0])
        self.assertIn(b"400 Bad Request", request("untrusted.example.net")[1])
        fixture.certificate(serial=101)
        self.assertIn(b"200 OK", eventually(fixtures.CONFIG["domain"], 101))


if __name__ == "__main__":
    unittest.main()
