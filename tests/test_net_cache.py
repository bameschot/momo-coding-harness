"""/net-cache and the page cache in the status event (the web UI's NET popover).

The cache itself is exercised against a live server in test_net.py; here pages
are put in directly, so the command and the status payload can be checked
without a network.

Run with:  python -m unittest discover tests
"""
import unittest
from unittest import mock

from harness import net
from harness.commands import handle as handle_command

from test_review_fixes import HarnessCase


def put(url: str, body: bytes = b"<p>x</p>", ctype: str = "text/html", via: str = "fetch_url",
        local: bool = False) -> None:
    net._cache_put((url, local, net.DEFAULT_MAX_BYTES),
                   {"raw": body, "headers": {"Content-Type": ctype}, "status": 200,
                    "reason": "OK", "final_url": url, "hops": [], "notes": [], "at": 0}, via)


class NetCacheCommand(HarnessCase):
    def setUp(self):
        super().setUp()
        net.clear_cache()
        self.addCleanup(net.clear_cache)

    def cmd(self, line: str) -> str:
        return handle_command(line, self.h).output

    def test_empty_cache_explains_itself(self):
        out = self.cmd("/net-cache")
        self.assertIn("Page cache: empty", out)
        self.assertIn("links=", out)

    def test_listing(self):
        put("https://docs.test/guide", b"x" * 2048)
        put("https://api.test/v1", b"{}", "application/json", via="web_search", local=True)
        out = self.cmd("/net-cache").splitlines()
        self.assertTrue(out[0].startswith(f"Page cache: 2 of {net._CACHE_MAX} pages, 2.0 KB of "
                                          f"{net.format_size(net._CACHE_MAX_BYTES)}"), out[0])
        self.assertRegex(out[1], r"^ 1\. api\.test/v1 +json .* 0 hits +web_search  local$")
        self.assertRegex(out[2], r"^ 2\. docs\.test/guide +html +2 KB .*fetch_url$")
        self.assertIn("/net-cache clear", out[-1])

    def test_drop_and_clear(self):
        put("https://a.test/1")
        put("https://a.test/2")
        self.assertEqual(self.cmd("/net-cache drop 1"), "Page cache: dropped https://a.test/2.")
        self.assertIn("from 1 to 1", self.cmd("/net-cache drop 5"))
        self.assertIn("ERROR", self.cmd("/net-cache drop x"))
        self.assertEqual(self.cmd("/net-cache clear"), "Page cache: cleared 1 page.")
        self.assertIn("the cache is empty", self.cmd("/net-cache drop 1"))
        self.assertIn("ERROR", self.cmd("/net-cache purge"))

    def test_net_state_mentions_the_cache(self):
        self.assertNotIn("Page cache", self.cmd("/net"))
        put("https://a.test/1")
        self.assertIn("Page cache: 1 page (/net-cache)", self.cmd("/net"))

    def test_status_event_carries_the_cache(self):
        put("https://a.test/1")
        s = self.h.status_event()
        self.assertEqual([p["url"] for p in s.net_cache], ["https://a.test/1"])
        self.assertEqual((s.net_cache_max, s.net_cache_max_bytes),
                         (net._CACHE_MAX, net._CACHE_MAX_BYTES))

    def test_byte_ceiling_evicts_least_recently_used(self):
        with mock.patch.object(net, "_CACHE_MAX_BYTES", 2500):
            put("https://a.test/1", b"x" * 1000)
            put("https://a.test/2", b"x" * 1000)
            put("https://a.test/3", b"x" * 1000)          # 3000 > 2500: page 1 goes
            self.assertEqual([p["url"] for p in net.cache_snapshot()],
                             ["https://a.test/3", "https://a.test/2"])
            put("https://a.test/big", b"x" * 5000)        # over the cap alone: kept, alone
            self.assertEqual([p["url"] for p in net.cache_snapshot()], ["https://a.test/big"])

    def test_commands_refresh_the_status(self):
        put("https://a.test/1")
        self.h.emit_status()
        self.assertEqual(len(self.h.event_queue._latest["StatusEvent"].net_cache), 1)
        self.cmd("/net-cache clear")
        self.assertEqual(self.h.event_queue._latest["StatusEvent"].net_cache, [])

    def test_in_help(self):
        from harness import commands
        usages = [c["usage"] for c in commands.help_commands() if c["cmd"] == "/net-cache"]
        self.assertEqual(usages, ["/net-cache", "/net-cache clear", "/net-cache drop <n>"])


if __name__ == "__main__":
    unittest.main()
