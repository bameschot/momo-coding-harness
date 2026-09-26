"""Tests for web_search / add_search_source.

The network is faked at net._get (which net.fetch_page also goes through), so
nothing here touches real DNS or the internet — except the guard tests, which
run the real net._get against a stubbed resolver to prove sources and proposed
URLs are held to the same address policy as fetch_url.

Run with:  python -m unittest tests.test_search
"""
import json
import socket
import tempfile
import threading
import unittest
from email import message_from_string
from http.client import HTTPMessage
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import net, search, search_sources, tools

WD = Path(__file__).parent


def _headers(ctype: str) -> HTTPMessage:
    return message_from_string(f"Content-Type: {ctype}\n", _class=HTTPMessage)


def response(body, status=200, ctype="application/json", reason="OK"):
    raw = body if isinstance(body, bytes) else (
        json.dumps(body) if not isinstance(body, str) else body).encode()
    return {"raw": raw, "headers": _headers(ctype), "status": status, "reason": reason,
            "final_url": "", "hops": [], "notes": [], "at": 0.0}


class FakeWeb:
    """net._get replacement: the first registered URL prefix that matches wins."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw.get("method", "GET"), kw.get("data")))
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                out = resp(url) if callable(resp) else resp
                return out if isinstance(out, str) else {**out, "final_url": url}
        return response({"message": "not found"}, 404, reason="Not Found")


# One canned response per shipped source, in the shape the real API returns.
CANNED = {
    "stackoverflow": ("https://api.stackexchange.com/2.3/search", {"items": [
        {"question_id": 1, "title": "What is &quot;debounce&quot;?", "tags": ["javascript"],
         "link": "https://stackoverflow.com/questions/1/debounce", "score": 5, "answer_count": 2}]}),
    "wikipedia": ("https://en.wikipedia.org/", {"pages": [
        {"key": "Debounce_(x)", "title": "Debounce", "excerpt": "a <span class=\"searchmatch\">debounce</span>",
         "description": "concept"}]}),
    "github": ("https://api.github.com/", {"items": [
        {"full_name": "a/debounce", "html_url": "https://github.com/a/debounce",
         "description": "debounce lib", "stargazers_count": 9}]}),
    "mdn": ("https://developer.mozilla.org/", {"documents": [
        {"title": "Debounce", "mdn_url": "/en-US/docs/Glossary/Debounce", "summary": "Debouncing is…"}]}),
    "npm": ("https://registry.npmjs.org/", {"objects": [
        {"package": {"name": "debounce", "version": "2.0.0", "description": "Delay calls",
                     "links": {"npm": "https://www.npmjs.com/package/debounce"}}}]}),
    "crates": ("https://crates.io/", {"crates": [
        {"name": "debounce", "description": "Debounce events", "max_stable_version": "0.2.2"}]}),
    "maven": ("https://central.sonatype.com/", {"response": {"docs": [
        {"id": "org.x:debounce", "g": "org.x", "a": "debounce", "latestVersion": "1.0"}]}}),
    "pypi": ("https://pypi.org/pypi/", {"info": {
        "name": "debounce", "package_url": "https://pypi.org/project/debounce/",
        "summary": "Debounce things", "version": "0.1"}}),
    "osv": ("https://api.osv.dev/", {"vulns": [
        {"id": "GHSA-1", "summary": "bad thing", "aliases": ["CVE-1"]}]}),
}
EXPECT_LINK = {
    "stackoverflow": "https://stackoverflow.com/questions/1/debounce",
    "wikipedia": "https://en.wikipedia.org/wiki/Debounce_(x)",
    "github": "https://github.com/a/debounce",
    "mdn": "https://developer.mozilla.org/en-US/docs/Glossary/Debounce",
    "npm": "https://www.npmjs.com/package/debounce",
    "crates": "https://crates.io/crates/debounce",
    "maven": "https://central.sonatype.com/artifact/org.x/debounce",
    "pypi": "https://pypi.org/project/debounce/",
    "osv": "https://osv.dev/vulnerability/GHSA-1",
}

PAGE = ("<html><head><title>Asyncio guide</title></head><body><main>"
        "<h2>Installing</h2><p>" + "pip install things and more words here. " * 20 + "</p>"
        "<h2>Timeouts</h2><p>Use asyncio.wait_for to put a timeout on an awaitable; "
        "on expiry it raises TimeoutError.</p>"
        "<h2>History</h2><p>" + "The project started long ago. " * 20 + "</p>"
        "</main></body></html>")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.user = self.tmp / "user"
        self.reg = search.SourceRegistry(user=self.user)
        net.clear_cache()

    def run_search(self, fake=None, **args):
        kw = dict(workdir=WD, net_access="on", search_sources=self.reg, net_max_chars=6000)
        if fake is None:
            return search.web_search(**args, **kw)
        with mock.patch.object(net, "_get", fake):
            return search.web_search(**args, **kw)

    def add(self, spec, query="x", fake=None, **kw):
        base = dict(workdir=WD, net_access="on", search_sources=self.reg)
        with mock.patch.object(net, "_get", fake or FakeWeb({})):
            return search.add_search_source(spec, query, **{**base, **kw})


class ShippedSources(Base):
    def test_all_shipped_sources_load(self):
        self.assertEqual(self.reg.warnings, [])
        self.assertEqual(set(self.reg.names()), set(CANNED))
        self.assertEqual({s.name for s in self.reg.defaults()},
                         {"stackoverflow", "wikipedia", "github", "mdn"})

    def test_each_source_parses_its_api_response(self):
        for name, (prefix, body) in CANNED.items():
            with self.subTest(source=name):
                fake = FakeWeb({prefix: response(body)})
                query = "PyPI:debounce@0.1" if name == "osv" else "debounce"
                out = self.run_search(fake, query=query, source=name)
                self.assertIn(f"<{EXPECT_LINK[name]}>", out)
                self.assertNotIn("<span", out)          # API markup stripped
                self.assertIn(net._BEGIN, out)          # fenced as untrusted

    def test_html_entities_decoded_in_titles(self):
        prefix, body = CANNED["stackoverflow"]
        out = self.run_search(FakeWeb({prefix: response(body)}), query="x", source="stackoverflow")
        self.assertIn('What is "debounce"?', out)

    def test_exact_lookup_404_is_no_results(self):
        out = self.run_search(FakeWeb({}), query="no-such-pkg", source="pypi")
        self.assertTrue(out.startswith("No results"), out)


class OsvHook(unittest.TestCase):
    def test_query_parsing(self):
        build = search_sources.HOOKS["osv"].build
        url, method, body = build(None, "Maven:org.apache.logging.log4j:log4j-core@2.14.1", 5)
        self.assertEqual(method, "POST")
        self.assertEqual(json.loads(body), {"package": {"name": "org.apache.logging.log4j:log4j-core",
                                                        "ecosystem": "Maven"}, "version": "2.14.1"})
        _, _, body = build(None, "npm:@types/node", 5)          # scope, no version
        self.assertEqual(json.loads(body), {"package": {"name": "@types/node", "ecosystem": "npm"}})
        self.assertIsInstance(build(None, "requests", 5), str)  # no ecosystem: explained


class Registry(Base):
    def test_invalid_file_warns_and_is_skipped(self):
        self.user.mkdir(parents=True)
        (self.user / "broken.json").write_text("{nope")
        (self.user / "bad.json").write_text(json.dumps({"name": "bad", "description": "d",
                                                        "url": "http://x/{query}",
                                                        "title": "t", "link": "l"}))
        reg = search.SourceRegistry(user=self.user)
        self.assertIsNone(reg.get("bad"))
        self.assertEqual(len(reg.warnings), 2)
        self.assertIn("Not loaded", reg.listing())

    def test_user_file_overrides_shipped(self):
        self.user.mkdir(parents=True)
        spec = json.loads((search.SHIPPED_DIR / "npm.json").read_text())
        spec["description"] = "my npm mirror"
        (self.user / "npm.json").write_text(json.dumps(spec))
        reg = search.SourceRegistry(user=self.user)
        self.assertEqual(reg.get("npm").origin, "user")
        self.assertEqual(reg.get("npm").description, "my npm mirror")

    def test_workdir_sources_are_never_read(self):
        wd = self.tmp / "project"
        (wd / "search_sources").mkdir(parents=True)
        (wd / "search_sources" / "evil.json").write_text(json.dumps(
            {"name": "evil", "description": "d", "url": "https://evil.example/?q={query}",
             "results": "", "title": "t", "link": "l", "default": True}))
        out = search.web_search(query="x", source="evil", workdir=wd, net_access="on",
                                search_sources=search.SourceRegistry(user=self.user))
        self.assertIn("unknown source", out)

    def test_remove(self):
        self.assertIn("cannot be removed", self.reg.remove("npm"))
        self.user.mkdir(parents=True)
        (self.user / "mine.json").write_text(json.dumps(
            {"name": "mine", "description": "d", "url": "https://x.example/?q={query}",
             "results": "", "title": "t", "link": "l"}))
        self.reg.load()
        self.assertIn("Removed", self.reg.remove("mine"))
        self.assertFalse((self.user / "mine.json").exists())


class Searching(Base):
    def test_defaults_fan_out_and_dedupe(self):
        dup = "https://github.com/a/debounce"
        routes = {p: response(b) for n, (p, b) in CANNED.items()}
        routes["https://developer.mozilla.org/"] = response({"documents": [
            {"title": "Same repo", "mdn_url": "/x", "summary": "s"}]})
        routes["https://api.github.com/"] = response({"items": [
            {"full_name": "a/debounce", "html_url": dup}, {"full_name": "a/debounce", "html_url": dup + "/"}]})
        fake = FakeWeb(routes)
        out = self.run_search(fake, query="debounce")
        hosts = {c[0].split("/")[2] for c in fake.calls}
        self.assertEqual(hosts, {"api.stackexchange.com", "en.wikipedia.org",
                                 "api.github.com", "developer.mozilla.org"})
        self.assertEqual(out.count(dup), 1)

    def test_one_failing_source_is_a_note(self):
        routes = {p: response(b) for n, (p, b) in CANNED.items()}
        routes["https://api.github.com/"] = response({"message": "rate limited"}, 403, reason="Forbidden")
        out = self.run_search(FakeWeb(routes), query="debounce")
        self.assertIn("note: github failed: HTTP 403", out)
        self.assertIn("stackoverflow.com", out)

    def test_unknown_source_lists_available(self):
        out = self.run_search(FakeWeb({}), query="x", source="nope")
        self.assertIn("unknown source", out)
        self.assertIn("npm", out)

    def test_needs_query_or_urls(self):
        self.assertIn("needs a query", self.run_search(FakeWeb({})))

    def test_off_is_refused(self):
        out = search.web_search(query="x", workdir=WD, net_access="off", search_sources=self.reg)
        self.assertIn("/net on", out)


class ProposedUrls(Base):
    def test_dead_url_costs_one_line_and_live_url_leads(self):
        prefix, body = CANNED["npm"]
        fake = FakeWeb({"https://docs.example/good": response(PAGE, ctype="text/html"),
                        prefix: response(body)})
        out = self.run_search(fake, query="timeout", source="npm",
                              urls=["https://docs.example/bad", "https://docs.example/good"])
        lines = out.split("Results:\n", 1)[1].splitlines()
        self.assertTrue(lines[0].startswith("1. ✗ https://docs.example/bad — HTTP 404"), lines[0])
        self.assertTrue(lines[1].startswith("2. ✓ Asyncio guide <https://docs.example/good>"), lines[1])
        self.assertIn("wait_for", lines[2])                     # the matching passage as snippet
        self.assertIn("npmjs.com", out)

    def test_urls_as_a_string(self):
        fake = FakeWeb({"https://docs.example/": response(PAGE, ctype="text/html")})
        out = self.run_search(fake, urls="https://docs.example/a, https://docs.example/b")
        self.assertIn("docs.example/a", out)
        self.assertIn("docs.example/b", out)

    def test_more_than_five_urls_truncated(self):
        fake = FakeWeb({"https://docs.example/": response(PAGE, ctype="text/html")})
        out = self.run_search(fake, urls=[f"https://docs.example/{i}" for i in range(8)])
        self.assertIn("only the first 5 of 8 urls", out)
        self.assertEqual(len(fake.calls), 5)


class Guard(Base):
    """The real net._get with a stubbed resolver: same policy as fetch_url."""

    def setUp(self):
        super().setUp()
        dns = {"evil.example": "127.0.0.1", "api.github.com": "10.0.0.8"}

        def resolve(host, port, *a, **kw):
            if host not in dns:
                raise socket.gaierror(socket.EAI_NONAME, "unknown")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (dns[host], port))]
        p = mock.patch.object(net, "_getaddrinfo", resolve)
        p.start()
        self.addCleanup(p.stop)

    def test_private_and_file_urls_blocked(self):
        out = self.run_search(urls=["http://evil.example/", "file:///etc/passwd"])
        self.assertIn("✗ http://evil.example/ — blocked URL", out)
        self.assertIn("✗ file:///etc/passwd — blocked URL", out)
        self.assertNotIn("root:", out)

    def test_source_resolving_private_is_blocked(self):
        out = self.run_search(query="x", source="github")
        self.assertIn("github failed", out)
        self.assertIn("10.0.0.8", out)


class Passages(Base):
    def test_bm25_picks_the_matching_block(self):
        text = net.html_to_text(PAGE)
        ranked = search.rank_passages([("u", text)], "wait_for timeout")
        self.assertEqual(ranked[0][3], "Timeouts")
        self.assertIn("wait_for", ranked[0][4])

    def test_code_fence_is_one_block(self):
        blocks = search._blocks("# H\n\npara\n\n```\na\n\nb\n```\n\nafter")
        self.assertEqual([b for _, b in blocks], ["para", "```\na\n\nb\n```", "after"])

    def test_read_mode_budget_and_page_cache(self):
        big = ("<html><head><title>Big</title></head><body><main>"
               + "".join(f"<h2>S{i}</h2><p>timeout handling paragraph {i} "
                         + "filler words " * 60 + "</p>" for i in range(60))
               + "</main></body></html>")
        prefix, body = CANNED["npm"]
        fake = FakeWeb({"https://www.npmjs.com/": response(big, ctype="text/html"),
                        prefix: response(body)})
        out = self.run_search(fake, query="timeout handling", source="npm", read=True)
        passages = out.split("## Passages", 1)[1]
        self.assertLess(len(passages), 6000)
        self.assertIn("timeout handling paragraph", passages)
        key = ("https://www.npmjs.com/package/debounce", False, net.DEFAULT_MAX_BYTES)
        self.assertIsNotNone(net._cache_get(key))    # a follow-up fetch_url(find=) is free

    def test_read_uses_stackexchange_answers(self):
        prefix, body = CANNED["stackoverflow"]
        answers = {"items": [{"question_id": 1, "score": 9, "is_accepted": True,
                              "body": "<p>Use a debounce timer with clearTimeout.</p>"}]}
        fake = FakeWeb({"https://api.stackexchange.com/2.3/questions/": response(answers),
                        prefix: response(body)})
        out = self.run_search(fake, query="debounce clearTimeout", source="stackoverflow", read=True)
        self.assertIn("Answer (score 9, accepted)", out)
        self.assertIn("clearTimeout", out.split("## Passages", 1)[1])
        self.assertFalse(any("stackoverflow.com/questions" in c[0] for c in fake.calls))


DOCSRS = {"name": "docsrs", "description": "Rust crate docs", "url":
          "https://crates.io/api/v1/crates?q={query}&per_page={n}", "results": "crates",
          "title": "name", "link": "https://docs.rs/{name}", "snippet": "description"}


class AddSource(Base):
    def crates(self):
        return FakeWeb({"https://crates.io/": response(
            {"crates": [{"name": "serde", "description": "ser/de"}], "meta": {}})})

    def test_wrong_path_lists_the_keys(self):
        out = self.add({**DOCSRS, "results": "items"}, "serde", self.crates())
        self.assertTrue(out.startswith("ERROR: the dry run failed"), out)
        self.assertIn("Keys there: crates, meta", out)
        self.assertIsNone(self.reg.get("docsrs"))

    def test_wrong_title_path_lists_item_keys(self):
        out = self.add({**DOCSRS, "title": "crate_name"}, "serde", self.crates())
        self.assertIn("title path 'crate_name'", out)
        self.assertIn("Its keys: name, description", out)

    def test_zero_results_not_added(self):
        empty = FakeWeb({"https://crates.io/": response({"crates": [], "meta": {}})})
        out = self.add(DOCSRS, "zzzz", empty)
        self.assertIn("found 0 results", out)
        self.assertIsNone(self.reg.get("docsrs"))

    def test_valid_spec_usable_at_once(self):
        out = self.add(DOCSRS, "serde", self.crates())
        self.assertIn("Added search source 'docsrs'", out)
        self.assertIn("<https://docs.rs/serde>", out)
        self.assertEqual(self.reg.get("docsrs").origin, "session")
        found = self.run_search(self.crates(), query="serde", source="docsrs")
        self.assertIn("<https://docs.rs/serde>", found)

    def test_same_name_can_be_retried(self):
        self.add(DOCSRS, "serde", self.crates())
        out = self.add({**DOCSRS, "description": "v2"}, "serde", self.crates())
        self.assertIn("Added", out)
        self.assertEqual(self.reg.get("docsrs").description, "v2")

    def test_refusals(self):
        cases = [({**DOCSRS, "headers": {"Private-Token": "x"}}, "credential"),
                 ({**DOCSRS, "url": "http://crates.io/?q={query}"}, "https://"),
                 ({**DOCSRS, "url": "https://crates.io/?q=fixed"}, "{query}"),
                 ({**DOCSRS, "url": "https://x.example/{query}/{secret}"}, "placeholders"),
                 ({**DOCSRS, "hook": "osv"}, "hook"),
                 ({**DOCSRS, "default": True}, "default"),
                 ({**DOCSRS, "name": "npm"}, "taken by a shipped source"),
                 ({**DOCSRS, "surprise": 1}, "unknown key")]
        for spec, why in cases:
            with self.subTest(why=why):
                out = self.add(spec, "serde", self.crates())
                self.assertTrue(out.startswith("ERROR"), out)
                self.assertIn(why, out)

    def test_spec_as_json_string(self):
        self.assertIn("Added", self.add(json.dumps(DOCSRS), "serde", self.crates()))

    def test_session_limit(self):
        for i in range(search._MAX_SESSION_SOURCES):
            self.assertIn("Added", self.add({**DOCSRS, "name": f"s{i}"}, "serde", self.crates()))
        self.assertIn("at most", self.add({**DOCSRS, "name": "one-more"}, "serde", self.crates()))

    def test_save_through_dispatch_writes_nothing(self):
        # The executor never writes: saving is the harness's, after a y/N.
        with mock.patch.object(net, "_get", self.crates()):
            out = tools.dispatch("add_search_source", {"spec": DOCSRS, "test_query": "serde",
                                                       "save": True}, WD, "on",
                                 search_sources=self.reg)
        self.assertIn("Added", out)
        self.assertFalse(self.user.exists())

    def harness_stub(self, answer: bool):
        reg = self.reg
        fake = self.crates()

        def _dispatch(name, args):
            with mock.patch.object(net, "_get", fake):
                return tools.dispatch(name, args, WD, "on", search_sources=reg)
        asked = []
        stub = SimpleNamespace(search_sources=reg, _dispatch=_dispatch,
                               _confirm=lambda q: asked.append(q) or answer)
        return stub, asked

    def test_harness_saves_only_after_consent(self):
        from harness.harness import Harness
        stub, asked = self.harness_stub(answer=False)
        out = Harness._add_search_source(stub, {"spec": DOCSRS, "test_query": "serde", "save": True})
        self.assertIn("Not saved", out)
        self.assertEqual(len(asked), 1)
        self.assertIn("https://crates.io/api/v1/crates?q={query}", asked[0])
        self.assertFalse(self.user.exists())

        stub, asked = self.harness_stub(answer=True)
        out = Harness._add_search_source(stub, {"spec": DOCSRS, "test_query": "serde", "save": True})
        saved = self.user / "docsrs.json"
        self.assertIn("Saved", out)
        self.assertEqual(json.loads(saved.read_text())["link"], "https://docs.rs/{name}")
        self.assertEqual(search.SourceRegistry(user=self.user).get("docsrs").origin, "user")

    def test_harness_does_not_ask_without_save_or_on_failure(self):
        from harness.harness import Harness
        stub, asked = self.harness_stub(answer=True)
        Harness._add_search_source(stub, {"spec": DOCSRS, "test_query": "serde"})
        Harness._add_search_source(stub, {"spec": {**DOCSRS, "results": "nope"},
                                          "test_query": "serde", "save": True})
        self.assertEqual(asked, [])


class Schema(Base):
    def test_registered_and_listed_with_net(self):
        names = [t["function"]["name"] for t in tools.NET_TOOLS]
        self.assertIn("web_search", names)
        self.assertIn("add_search_source", names)
        self.assertIn("web_search", tools._EXECUTORS)

    def test_source_list_in_schema(self):
        base = next(t for t in tools.NET_TOOLS if t["function"]["name"] == "web_search")
        tool = search.web_search_tool(base, self.reg)
        desc = tool["function"]["parameters"]["properties"]["source"]["description"]
        self.assertIn("npm:", desc)
        self.assertIn("stackoverflow (default)", desc)
        self.assertNotIn("npm:", base["function"]["parameters"]["properties"]["source"]["description"])

    def test_model_cannot_inject_registry_or_access(self):
        out = tools.dispatch("web_search", {"query": "x", "net_access": "local"}, WD, "on")
        self.assertIn("does not accept argument", out)
        out = tools.dispatch("web_search", {"query": "x", "search_sources": "x"}, WD, "on")
        self.assertIn("does not accept argument", out)


class Suggest(Base):
    def write(self, root: Path, files: dict):
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_signals_for_a_code_repo(self):
        repo = self.tmp / "code"
        self.write(repo, {
            "Cargo.toml": "[package]\nname = \"x\"\n",
            "backend/pom.xml": "<project><repositories><repository><id>corp</id>"
                               "<url>https://deploy:s3cret@nexus.corp.example/repository/maven-public/?token=abc</url>"
                               "</repository></repositories></project>",
            "web/.npmrc": "registry=https://npm.corp.example/\n//npm.corp.example/:_authToken=NPMTOKEN\n",
            "node_modules/dep/package.json": "{}",
            "src/main.rs": "fn main() {}\n",
        })
        out = search.project_signals(repo)
        self.assertIn("Cargo.toml", out)
        self.assertIn("backend/pom.xml", out)
        self.assertIn("https://nexus.corp.example/repository/maven-public", out)
        self.assertIn("https://npm.corp.example", out)
        for secret in ("s3cret", "deploy", "token=abc", "NPMTOKEN"):
            self.assertNotIn(secret, out)
        self.assertNotIn("node_modules", out)
        self.assertIn(".rs 1", out)

    def test_public_registries_not_listed(self):
        repo = self.tmp / "pub"
        self.write(repo, {"pom.xml": "<repository><url>https://repo.maven.apache.org/maven2</url></repository>"})
        self.assertNotIn("Custom package repositories", search.project_signals(repo))

    def test_signals_for_a_writing_project(self):
        repo = self.tmp / "novel"
        self.write(repo, {
            "README.md": "# The Ember Crown\n\nA fantasy novel set in a volcanic archipelago.\n",
            "chapters/01-arrival.md": "# Chapter 1: Arrival\n\n## The harbour\nText.\n",
            "chapters/02-storm.md": "# Chapter 2: Storm\nText.\n",
            "world/places.txt": "Places of the archipelago\n",
        })
        out = search.project_signals(repo)
        self.assertIn(".md 3", out)
        self.assertIn("volcanic archipelago", out)
        self.assertIn("Chapter 1: Arrival", out)
        self.assertIn("No package manifests", out)
        prompt = search.suggest_prompt(self.reg, out, "setting research")
        self.assertIn("write your opinion", prompt)
        self.assertIn("subject-matter", prompt)
        self.assertIn("Subject matter:", prompt)
        self.assertIn('"name": "words"', prompt)
        self.assertIn("setting research", prompt)
        self.assertIn("- stackoverflow:", prompt)          # existing sources listed
        self.assertIn("Do NOT pass save=true", prompt)

    def test_every_hint_is_a_valid_session_spec(self):
        for hint in search.SUGGEST_HINTS:
            with self.subTest(hint=hint["spec"]["name"]):
                src, err = search.validate_spec(hint["spec"], "session")
                self.assertIsNone(err)
                self.assertIn(hint["group"], ("code", "subject"))
                self.assertIsNone(self.reg.get(src.name))  # none shadows a shipped source

    def test_placeholder_link_template(self):
        src, err = search.validate_spec({**DOCSRS, "link": "{domain}{path}"}, "session")
        self.assertIsNone(err)
        self.assertEqual(search._link({"domain": "https://a.io", "path": "/x/"}, "{domain}{path}"),
                         "https://a.io/x/")
        self.assertIsNone(search._link({"domain": "javascript:", "path": "x"}, "{domain}{path}"))
        # A full-URL field inside a template is a broken link, and the dry run says so.
        self.assertIsNone(search._link({"u": "http://www.wikidata.org/entity/Q1"},
                                       "https://www.wikidata.org/wiki/{u}"))
        fake = FakeWeb({"https://www.wikidata.org/": response(
            {"search": [{"label": "caldera", "concepturi": "http://www.wikidata.org/entity/Q1"}]})})
        spec = {**search.SUGGEST_HINTS[7]["spec"], "link": "https://www.wikidata.org/wiki/{concepturi}"}
        out = self.add(spec, "caldera", fake)
        self.assertIn("link path", out)
        self.assertIsNone(self.reg.get("wikidata"))


class HarnessIntegration(unittest.TestCase):
    def setUp(self):
        import os
        from harness import harness as hmod
        from harness import session as session_mod
        from harness.llm.base import ChatResponse, LLMClient

        class FakeClient(LLMClient):
            provider_name = "fake"

            def chat(self, *a, **k):
                return ChatResponse(content="ok", done_reason="stop")

            def context_length(self):
                return 32768

            def list_models(self):
                return ["fake"]

            def abort(self):
                pass

        self.tmp = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(hmod, "make_client", lambda *a, **k: FakeClient("http://fake", "fake")),
                        mock.patch.object(session_mod, "_PREFS_PATH", Path(self.home.name) / "prefs.json"),
                        mock.patch.dict(os.environ, {"HOME": self.home.name})]
        for p in self.patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=Path(self.tmp.name))
        self.h.set_mode("coding")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()
        self.home.cleanup()

    def names(self):
        return {t["function"]["name"] for t in self.h._current_tools()}

    def test_listed_only_with_net_and_with_sources(self):
        from harness.commands import handle as handle_command
        self.assertNotIn("web_search", self.names())
        handle_command("/net on", self.h)
        self.assertIn("web_search", self.names())
        self.assertIn("add_search_source", self.names())
        self.assertIn("stackoverflow (default)", self.h.messages[0]["content"])
        out = handle_command("/search-sources", self.h).output
        self.assertIn("npm", out)
        self.assertIn(str(Path(self.home.name)), out)

    def test_suggest_needs_net(self):
        from harness.commands import handle as handle_command
        res = handle_command("/search-sources suggest", self.h)
        self.assertIn("/net on", res.output)
        self.assertIsNone(res.send_prompt)

    def test_suggest_starts_a_model_turn(self):
        import time
        from harness.commands import handle as handle_command
        from harness.controller import Controller
        (Path(self.tmp.name) / "README.md").write_text("# Tide tables\nA tide prediction tool.\n")
        handle_command("/net on", self.h)
        res = handle_command("/search-sources suggest papers on tides", self.h)
        self.assertIn("Tide tables", res.send_prompt)
        self.assertIn("papers on tides", res.send_prompt)
        controller = Controller(self.h)
        controller.submit("/search-sources suggest papers on tides")
        deadline = time.monotonic() + 10
        while controller.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        users = [m["content"] for m in self.h.messages if m.get("role") == "user"]
        self.assertTrue(any("Analyse this project" in u and "Tide tables" in u for u in users))

    def test_save_session_source(self):
        from harness.commands import handle as handle_command
        src, _ = search.validate_spec(DOCSRS, "session")
        self.h.search_sources.add_session(src)
        self.assertIn("already a shipped source", handle_command("/search-sources save npm", self.h).output)
        self.assertIn("No source named", handle_command("/search-sources save nope", self.h).output)
        out = handle_command("/search-sources save all", self.h).output
        self.assertIn("Saved", out)
        self.assertTrue((Path(self.home.name) / ".momo-harness" / "search_sources" / "docsrs.json").exists())
        self.assertIn("No session sources", handle_command("/search-sources save all", self.h).output)

    def test_web_search_not_cut_by_tool_result(self):
        # web_search sizes itself; the generic cut would slice through the fence.
        import inspect
        from harness import harness as hmod
        self.assertIn('name not in ("fetch_url", "web_search")', inspect.getsource(hmod.Harness))


if __name__ == "__main__":
    unittest.main()
