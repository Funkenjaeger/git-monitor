"""The control plane is only reachable through lanauth.

git-monitor's dashboard is deliberately authless — reading which repos have
uncommitted work is low-risk, and there is no attribution to preserve. But the
same app serves a config editor that rewrites the collector's SSH targets and
`remote_python`, and a test endpoint that opens an SSH connection to a target
supplied in the request body. Those are a control plane.

It is reachable two ways: through lanauth (Pocket ID login, then Caddy stamps a
shared-secret header) and directly on the published port, which any LAN host can
reach. Source IP cannot separate them — docker SNATs published-port traffic — so
the header is the only signal, and these tests exist because "the proxy handles
it" is false for the direct port.

    python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the app at throwaway state BEFORE importing it: module-level constants
# capture these at import time.
_TMP = tempfile.mkdtemp()
os.environ["GITMON_DB"] = os.path.join(_TMP, "test.db")
os.environ["GITMON_CONFIG"] = os.path.join(_TMP, "config.yaml")
with open(os.environ["GITMON_CONFIG"], "w", encoding="utf-8") as _fh:
    _fh.write("targets: []\n")

try:
    import app as app_module  # noqa: E402
except ImportError as exc:  # pragma: no cover
    # The rest of the suite is stdlib-only and runs anywhere, including on
    # dserver's system python. This module needs Flask. Skip rather than error:
    # a red result on a machine that was never going to have the dependency
    # teaches people the suite is unreliable, and then a real failure gets the
    # same shrug.
    raise unittest.SkipTest("git-monitor's runtime deps are absent here (%s)" % exc)

SECRET = "gate-secret-for-tests"

#: Everything that can change something, or reach out to something.
CONTROL = [
    ("GET", "/config"),
    ("GET", "/api/config"),
    ("POST", "/api/config"),
    ("POST", "/api/config/test"),
    ("POST", "/api/refresh"),
    ("POST", "/refresh"),
]

#: Everything that only reads. /api/summary is what the Homepage widget calls.
READ = ["/", "/api/summary", "/api/data"]


class GateBase(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self._real_secret = app_module.GATE_SECRET
        app_module.GATE_SECRET = SECRET
        # A scan reaches out over SSH to every configured target. No test here
        # may run one -- the guards are proven by watching them refuse, never by
        # letting the action through.
        self._real_scan = app_module.run_scan
        self.scanned = []
        app_module.run_scan = lambda: self.scanned.append(1) or {}

    def tearDown(self):
        app_module.GATE_SECRET = self._real_secret
        app_module.run_scan = self._real_scan

    def call(self, method, path, gate=None):
        headers = {app_module.GATE_HEADER: gate} if gate is not None else {}
        return self.client.open(path, method=method, headers=headers, json={})


class ControlPlaneIsGated(GateBase):
    def test_every_control_route_refuses_without_the_header(self):
        for method, path in CONTROL:
            with self.subTest(route="%s %s" % (method, path)):
                self.assertEqual(self.call(method, path).status_code, 403)

    def test_a_wrong_secret_is_refused(self):
        for method, path in CONTROL:
            with self.subTest(route="%s %s" % (method, path)):
                r = self.call(method, path, gate=SECRET + "x")
                self.assertEqual(r.status_code, 403)

    def test_an_empty_header_is_refused(self):
        self.assertEqual(self.call("GET", "/config", gate="").status_code, 403)

    def test_the_right_secret_gets_through(self):
        # /config only reads, so it is safe to exercise the success path.
        self.assertEqual(self.call("GET", "/config", gate=SECRET).status_code, 200)
        self.assertEqual(self.call("GET", "/api/config", gate=SECRET).status_code, 200)

    def test_a_refused_refresh_never_starts_a_scan(self):
        """The guard must run BEFORE the side effect, not alongside it."""
        for gate in (None, "wrong"):
            with self.subTest(gate=gate):
                self.call("POST", "/api/refresh", gate=gate)
                self.call("POST", "/refresh", gate=gate)
        self.assertEqual(self.scanned, [],
                         "a refused request still triggered a scan")

    def test_refresh_no_longer_answers_GET(self):
        """It used to, which made a scan fire on any link or prefetch."""
        r = self.client.get("/api/refresh")
        self.assertEqual(r.status_code, 405)
        self.assertEqual(self.scanned, [])


class ReadPlaneIsOpen(GateBase):
    def test_read_routes_need_no_header(self):
        for path in READ:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_api_summary_stays_open_because_homepage_reads_it(self):
        # Called out separately: gating this breaks the Homepage widget, and
        # that failure shows up days later on a different dashboard.
        self.assertEqual(self.client.get("/api/summary").status_code, 200)


class FailsClosed(GateBase):
    """An unset secret means the gate was never wired up. The wrong answer to
    that is letting everyone in."""

    def setUp(self):
        super().setUp()
        app_module.GATE_SECRET = ""

    def test_control_plane_is_503_not_open(self):
        for method, path in CONTROL:
            with self.subTest(route="%s %s" % (method, path)):
                self.assertEqual(self.call(method, path).status_code, 503)

    def test_a_supplied_header_cannot_satisfy_an_unset_secret(self):
        self.assertEqual(self.call("GET", "/config", gate="").status_code, 503)
        self.assertEqual(self.call("GET", "/config", gate="anything").status_code, 503)

    def test_reading_still_works(self):
        for path in READ:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


class DashboardShowsOnlyWhatWorks(GateBase):
    def test_ungated_dashboard_offers_no_control_affordances(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("read-only view", body)
        self.assertNotIn('href="/config"', body)
        self.assertNotIn('id="refresh"', body)

    def test_ungated_dashboard_offers_a_way_in_when_a_public_url_is_set(self):
        """Hiding the controls without a signpost leaves someone stranded: the
        published port cannot authenticate, so the page has to point at the one
        that can."""
        real = app_module.PUBLIC_URL
        app_module.PUBLIC_URL = "https://gitmonitor.example.net"
        try:
            body = self.client.get("/").get_data(as_text=True)
        finally:
            app_module.PUBLIC_URL = real
        self.assertIn("Sign in to configure", body)
        self.assertIn("https://gitmonitor.example.net/config", body)
        self.assertNotIn("read-only view", body)

    def test_gated_dashboard_offers_them(self):
        body = self.client.get(
            "/", headers={app_module.GATE_HEADER: SECRET}).get_data(as_text=True)
        self.assertIn('href="/config"', body)
        self.assertIn('id="refresh"', body)
        self.assertNotIn("read-only view", body)


if __name__ == "__main__":
    unittest.main()
