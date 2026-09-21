"""run_command steers web requests to fetch_url.

With /net off a model that has no fetch_url falls back to curl through
run_command, which quietly bypasses the user's setting — so curl/wget in command
position are refused while /net is off, and run with a fetch_url hint while on.
"""
import tempfile
import unittest
from pathlib import Path

from harness import tools
from harness.tools import _web_client_in, dispatch


class Matcher(unittest.TestCase):
    def test_web_clients_in_command_position(self):
        for cmd in [
            'curl -s "https://repo1.maven.org/maven2/x/maven-metadata.xml" 2>&1',
            "wget -qO- https://example.com",
            "/usr/bin/curl https://example.com",
            "cd sub && curl https://example.com",
            "true; curl https://example.com",
            "false || wget https://example.com",
            "echo $(curl -s https://example.com)",
            "echo `curl -s https://example.com`",
            "(curl https://example.com)",
            "sudo curl https://example.com",
            "env HTTPS_PROXY=x A= curl https://example.com",
            "time curl https://example.com",
            "ls\ncurl https://example.com",
            'curl -s -X POST "https://api.osv.dev/v1/querybatch" \\\n  -H "Content-Type: application/json" -d \'{}\'',
            "curl",
        ]:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(_web_client_in(cmd))

    def test_mentions_that_are_not_invocations(self):
        for cmd in [
            "grep -rn curl .",
            'echo "use curl"',
            "git log --grep=wget",
            "cat curl.txt",
            "./mycurl https://example.com",
            "curling --help",
            "brew info curl-config",
            "python -m pytest",
            "mvn -q dependency:list",
        ]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(_web_client_in(cmd))


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cmd(self, command, net_access):
        return dispatch("run_command", {"command": command}, self.workdir, net_access)

    def test_refused_when_net_off(self):
        # curl --version never touches the network, so if the guard failed the
        # test would see curl's banner rather than make a request.
        out = self.run_cmd("curl --version", "off")
        self.assertTrue(out.startswith("ERROR: this command was not run"), out)
        self.assertIn("/net on", out)
        self.assertNotIn("libcurl", out)

    def test_default_net_access_is_off(self):
        out = tools.dispatch("run_command", {"command": "curl --version"}, self.workdir)
        self.assertTrue(out.startswith("ERROR:"), out)

    def test_runs_with_hint_when_net_on(self):
        for access in ("on", "local"):
            with self.subTest(access=access):
                out = self.run_cmd("curl --version", access)
                self.assertTrue(out.startswith("(note: to read a page or call an API, use the fetch_url tool"), out)
                self.assertIn("fine for downloading files", out)
                self.assertIn("curl", out.splitlines()[1])

    def test_other_commands_unaffected(self):
        out = self.run_cmd("echo hello", "off")
        self.assertEqual(out, "hello")

    def test_model_cannot_pass_net_access(self):
        out = dispatch("run_command", {"command": "curl --version", "net_access": "on"},
                       self.workdir, "off")
        self.assertIn("does not accept", out)


if __name__ == "__main__":
    unittest.main()
