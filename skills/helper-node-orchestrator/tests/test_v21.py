"""v2.1: per-helper max_heavy, the policy answer, health URL resolve, deploy-only production, selftest capacity skip.
Run from the skill root:  python -B -m unittest discover -s tests -p "test_v21.py"
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import orch_config as oc  # noqa: E402
import orch_deploy as dp  # noqa: E402
from test_config import full_cfg, state  # noqa: E402
from test_init import Base  # noqa: E402
from orchtest import orch  # noqa: E402


def errs(cfg):
    return oc._check_config({"schema": 1, **cfg})


class TestConfig(unittest.TestCase):
    def test_max_heavy_and_reach_validation(self):
        h = {"ssh": "a1", "roles": ["helper"], "jobs": "all"}
        self.assertEqual(errs({"hosts": {"h11": {**h, "max_heavy": 2}}}), [])
        for bad in (0, 3, True, "2"):
            self.assertIn("max_heavy must be 1 or 2", " ".join(errs({"hosts": {"h11": {**h, "max_heavy": bad}}})))
        prod = {"ssh": "a2", "roles": ["helper", "prod"], "jobs": "tests", "max_heavy": 2}
        self.assertIn("at most 1 heavy job", " ".join(errs({"hosts": {"h12": prod}})))
        self.assertEqual(errs({"hosts": {"p20": {"ssh": "p20", "roles": ["prod"], "reach": "deploy-only"}}}), [])
        self.assertIn("only for a production-only server",
                      " ".join(errs({"hosts": {"h11": {**h, "reach": "deploy-only"}}})))
        self.assertIn("reach must be one of", " ".join(errs({"hosts": {"p20": {"ssh": "p", "roles": ["prod"],
                                                                                "reach": "bridge"}}})))

    def test_policy_floors_and_nested_keys(self):
        self.assertEqual(errs({"policy": {"admission": {"max_heavy": 2}}}), [])
        self.assertIn("not in 1..2", " ".join(errs({"policy": {"admission": {"max_heavy": 3}}})))
        self.assertIn("past the safety floor", " ".join(errs({"policy": {"admission": {"reserve_frac": 0.1}}})))
        self.assertIn("past the safety floor", " ".join(errs({"policy": {"watchdog": {"psi_max": 40}}})))
        self.assertIn("policy.admission: unknown key 'nope'", " ".join(errs({"policy": {"admission": {"nope": 1}}})))
        self.assertIn("past the safety floor", " ".join(errs({"policy": {"watchdog": {"psi_max": float("nan")}}})))
        for bad in ({"admission": 5}, {"watchdog": "x"}):  # a section that is not an object: an error, not a crash
            self.assertIn("must be an object", " ".join(errs({"policy": bad})))

    def test_loosening_counts_effective_capacity(self):
        # policy max_heavy 2 approved: dropping a host's own max_heavy 1 raises it to 2, so approval is needed
        old, new = full_cfg(), full_cfg()
        for c in (old, new):
            c["policy"] = {"admission": {"max_heavy": 2}}
        old["hosts"]["h11"]["max_heavy"] = 1
        self.assertEqual(oc.loosening(old, new), ["h11 (deploy@192.0.2.11): runs up to 2 heavy jobs at once (was 1)"])
        self.assertEqual(oc.loosening(new, old), [])

    def test_source_allow_accepts_one_character_classes(self):
        # real project pattern: [-] is a literal hyphen, still one exact file
        for pat in ("^src/Scripts/Migrate[-]Database[.]ps1$", r"^src/a\-b[+]c[ ]d[.]txt$", r"^src/a\.py$"):
            self.assertEqual(errs({"jobs": {"x": {"cmd": "true", "source_allow": [pat]}}}), [], pat)
        for pat in ("^src/[ab][.]py$", r"^src/a\w$", "^src/[^a]$", "^src/a[]]$", "^src/[[]$", "^src/a?$",
                    "^src/a{2}$", "^src/(a)$", "^src/a.py$", "^src/[\n]$", "^$", "^src/a$|^b$"):
            self.assertIsNone(oc.source_allow_path(pat), pat)

    def test_source_allow_names_exactly_one_path(self):
        # every accepted entry, in the engine's form, exempts its own literal path and nothing else
        import random
        import re
        rnd = random.Random(7)
        toks = [*"abcXY09_/-", "[.]", "[-]", "[ ]", "[+]", "[(]", r"\.", r"\-", r"\[", r"\\", r"\$"]
        for _ in range(500):
            pat = "^src/" + "".join(rnd.choice(toks) for _ in range(rnd.randint(1, 8))) + "$"
            lit = oc.source_allow_path(pat)
            self.assertIsNotNone(lit, pat)
            eng = oc.source_allow_engine(pat)
            self.assertTrue(re.search(eng, lit, re.I), pat)
            self.assertTrue(orch.secret_allowed(lit, [eng]), pat)  # the engine's own matcher
            self.assertFalse(orch.secret_allowed(lit + "\n", [eng]), pat)
            for other in (lit + "\n", lit + "x", "x" + lit, lit[:-1], lit.swapcase() if lit.swapcase() != lit else None):
                if other is not None:
                    self.assertIsNone(re.search(eng, other, re.I), (pat, other))

    def test_new_job_with_source_allow_is_loosening(self):
        old, new = full_cfg(), full_cfg()
        new["jobs"]["fresh"] = {**new["jobs"]["api-test"], "source_allow": ["^src/a[.]py$"]}
        self.assertIn("job fresh: secret scan skips ^src/a[.]py$", oc.loosening(old, new))
        new["jobs"]["fresh"].pop("source_allow")
        self.assertNotIn("job fresh: secret scan skips ^src/a[.]py$", oc.loosening(old, new))

    def test_health_resolve(self):
        ok = {"url": "https://uat.example/", "resolve": "203.0.113.10", "insecure": True}
        self.assertEqual(errs({"deploy": {"command": "./d.sh", "health": [ok]}}), [])
        self.assertEqual(errs({"deploy": {"command": "./d.sh", "health": [{**ok, "resolve": "2001:db8::1"}]}}), [])
        self.assertIn("deploy.health", " ".join(errs({"deploy": {"command": "./d.sh",
                                                                 "health": [{**ok, "resolve": "uat.example"}]}})))
        self.assertEqual(oc.health_label(ok), "https://uat.example/ (via 203.0.113.10) (insecure)")
        self.assertEqual(dp.health_targets({"deploy": {"health": [ok]}}),
                         [{"url": "https://uat.example/", "insecure": True, "resolve": "203.0.113.10"}])

    def test_loosening(self):
        old, new = full_cfg(), full_cfg()
        new["hosts"]["h11"]["max_heavy"] = 2
        self.assertEqual(oc.loosening(old, new), ["h11 (deploy@192.0.2.11): runs up to 2 heavy jobs at once (was 1)"])
        self.assertEqual(oc.loosening(new, old), [])
        new = full_cfg()
        new["hosts"]["p20"]["reach"] = "deploy-only"
        self.assertEqual(len(oc.loosening(old, new)), 1)
        self.assertIn("no longer probed over ssh", oc.loosening(old, new)[0])

    def test_compile_carries_max_heavy(self):
        cfg = full_cfg()
        cfg["hosts"]["h11"]["max_heavy"] = 2
        d = Path(tempfile.mkdtemp(prefix="orchv21-"))
        self.addCleanup(shutil.rmtree, d, True)
        n = oc.compile_engine(oc.Project(d / "shop", d / "shop" / ".git" / "orch", True), cfg, state())["nodes"]["nodes"]
        self.assertEqual(n["h11"].get("max_heavy"), 2)
        self.assertNotIn("max_heavy", n["h12"])

    def test_production_elsewhere_drops_max_heavy(self):
        # another approved project saves h11 as production: its node stays valid for the engine, capacity 1
        cfg = full_cfg()
        cfg["hosts"]["h11"]["max_heavy"] = 2
        st = state()
        d = Path(tempfile.mkdtemp(prefix="orchv21-"))
        self.addCleanup(shutil.rmtree, d, True)
        ident = oc._ident(cfg["hosts"]["h11"]["ssh"], st.get("h11"))
        eng = oc.compile_engine(oc.Project(d / "shop", d / "shop" / ".git" / "orch", True), cfg, st,
                                master_prod={ident: [str(d / "other")]})
        n = eng["nodes"]["nodes"]["h11"]
        self.assertEqual(n["role"], "helper+prod")
        self.assertNotIn("max_heavy", n)
        orch.validate_nodes(eng["nodes"])


class TestEngine(unittest.TestCase):
    def test_node_max_heavy(self):
        A = {"max_heavy": 1}
        with mock.patch.object(orch, "cfg_nodes", return_value={"nodes": {"n1": {"max_heavy": 2}, "n2": {}}}):
            self.assertEqual(orch.node_max_heavy("n1", A), 2)
            self.assertEqual(orch.node_max_heavy("n2", A), 1)
            self.assertEqual(orch.node_max_heavy("gone", A), 1)
        with mock.patch.object(orch, "cfg_nodes", return_value={"nodes": {"p1": {"role": "helper+prod"}}}):
            self.assertEqual(orch.node_max_heavy("p1", {"max_heavy": 2}), 1)  # policy 2 never reaches production

    def test_validate_nodes_max_heavy(self):
        base = {"ssh": "x", "os": "linux", "transport": "context", "role": "helper"}
        orch.validate_nodes({**self._pol(), "nodes": {"n1": {**base, "max_heavy": 2}}})
        for bad, needle in (({**base, "max_heavy": 3}, "not in 1..2"),
                            ({**base, "role": "helper+prod", "max_heavy": 2}, "at most 1 heavy job")):
            with self.assertRaises(orch.ConfigError) as e:
                orch.validate_nodes({**self._pol(), "nodes": {"n1": bad}})
            self.assertIn(needle, str(e.exception))

    @staticmethod
    def _pol():
        return {k: json.loads(json.dumps(v)) for k, v in oc.DEFAULT_POLICY.items()
                if k in ("admission", "score_weights", "watchdog", "lease", "subnet_pool", "subnet_prefix", "builder")}


class TestReach(unittest.TestCase):
    def cfg(self, reach=None):
        c = {"hosts": {"h11": {"ssh": "a1", "roles": ["helper"], "jobs": "all"},
                       "p20": {"ssh": "deploy@192.0.2.20", "roles": ["prod"], **({"reach": reach} if reach else {})}},
             "production": {"answer": "set"}}
        return c

    def test_deploy_only_is_a_warning_and_never_probed(self):
        calls = []
        g = dp._g_reach(self.cfg("deploy-only"), True, lambda specs: calls.append(specs) or {})
        self.assertEqual((g["status"], g["ok"]), ("warn", True))
        self.assertIn("reached only through the deploy command; not probed", g["detail"])
        self.assertEqual(calls, [])

    def test_default_still_probes(self):
        g = dp._g_reach(self.cfg(), True, lambda specs: {"p20": {"ok": False, "problem": {"what": "timed out"}}})
        self.assertEqual(g["status"], "fail")


class _Host(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):  # noqa: N802
        _Host.seen.append(self.headers.get("Host"))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class TestLiveResolve(unittest.TestCase):
    def test_resolve_connects_to_the_ip_and_sends_the_name(self):
        patch_env = mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"})
        patch_env.start()
        self.addCleanup(patch_env.stop)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Host)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        url = f"http://uat.invalid:{port}/health"
        c = dp.check_url(url, tries=1, resolve="127.0.0.1")
        self.assertEqual((c["status"], c["error"], c["resolve"]), (200, None, "127.0.0.1"))
        self.assertEqual(_Host.seen[-1], f"uat.invalid:{port}")
        self.assertIn("(via 127.0.0.1)", dp.live_line(c))
        got = dp.live([{"url": url, "insecure": False, "resolve": "127.0.0.1"}], tries=1)
        self.assertEqual(got[0]["error"], None)
        c = dp.check_url(url, tries=1)  # without resolve the name does not resolve
        self.assertIsNotNone(c["error"])
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                                          "no_proxy": "", "NO_PROXY": ""}):  # resolve never goes through a proxy
            self.assertEqual(dp.check_url(url, tries=1, resolve="127.0.0.1")["error"], None)


class TestInit(Base):
    def test_policy_answer_needs_approve_and_respects_floors(self):
        self.reach_ready()
        _, js = self.ok(ans={"policy": {"admission": {"max_heavy": 2}}})
        self.assertEqual(js["state"], "unapproved", js)
        self.assertIn("policy overrides changed", js["ask"]["question"])
        self.assertEqual(self.cfg()["policy"], {"admission": {"max_heavy": 2}})
        rc, _, js = self.init(ans={"policy": {"admission": {"reserve_frac": 0.1}}})
        self.assertEqual(rc, 2)
        self.assertIn("safety floor", js["error"])
        self.ok(ans={"policy": {"admission": None}})
        self.assertNotIn("policy", self.cfg())

    def test_host_max_heavy_and_deploy_only_reach(self):
        self.reach_ready()
        _, js = self.ok(ans={"hosts": {"h11": {"max_heavy": 2}}})
        self.assertEqual(js["state"], "unapproved", js)
        self.assertIn("runs up to 2 heavy jobs at once", js["ask"]["question"])
        self.assertEqual(self.cfg()["hosts"]["h11"]["max_heavy"], 2)
        rc, _, js = self.init(ans={"hosts": {"h12": {"max_heavy": 2}}})  # a shared helper is fine, production is not
        self.assertEqual(rc, 0, js)
        self.ok(ans={"hosts": {"h12": {"roles": ["helper", "prod"], "jobs": "readonly", "max_heavy": None}}})
        rc, _, js = self.init(ans={"hosts": {"h12": {"max_heavy": 2}}})
        self.assertEqual(rc, 2)
        self.assertIn("at most 1 heavy job", js["error"])
        prod = next(n for n, h in self.cfg()["hosts"].items() if h["roles"] == ["prod"])
        self.disc.calls = getattr(self.disc, "calls", [])
        lines, js = self.ok(ans={"hosts": {prod: {"reach": "deploy-only"}}})
        self.assertIn("not probed (reached only through the deploy command)", "\n".join(lines))
        self.assertTrue(self.state()[prod].get("deploy_only"))
        _, js = self.ok("approve")
        self.assertEqual(js["state"], "ready", js)
        self.assertEqual(js["problems"], [])


class ZombieAndReconcile(unittest.TestCase):  # v2.1.2: found by the selftest on a real Linux helper
    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs Linux /proc")
    def test_zombie_is_dead(self):
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        st = Path(f"/proc/{p.pid}/stat")
        for _ in range(200):  # exited but not reaped: state Z
            if st.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            threading.Event().wait(0.05)
        try:
            self.assertFalse(orch.pid_alive(p.pid), "an exited, unreaped orch must not keep its objects")
        finally:
            p.wait()

    def test_live_process_is_alive(self):
        self.assertTrue(orch.pid_alive(os.getpid()))

    def test_reconcile_removed_reads_the_removed_line_only(self):
        name = "agentt-1102163319e154-job"
        kept = f"local: removed 0: -\norch[16:33:40] local: keep container {name} (live job)\n"
        self.assertFalse(orch.reconcile_removed(kept, "local", name))
        self.assertTrue(orch.reconcile_removed(f"local: removed 1: container {name}\n", "local", name))
        self.assertFalse(orch.reconcile_removed(f"h11: removed 1: container {name}\n", "local", name))
        self.assertFalse(orch.reconcile_removed(f"local: removed 1: container {name}2\n", "local", name))
        self.assertTrue(orch.reconcile_removed(f"local: removed 2: container x, container {name}\n", "local", name))

    def test_other_users_process_is_alive(self):  # EPERM: it exists
        with mock.patch.object(orch.os, "kill", side_effect=PermissionError):
            self.assertTrue(orch.IS_WIN or orch.pid_alive(4242))


if __name__ == "__main__":
    unittest.main()
