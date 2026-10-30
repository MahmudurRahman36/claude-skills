"""Round-2 review fixes (O2-1..O2-9). Nothing real runs: subprocess is stubbed via FakeCmd / mocks."""
import io
import json
import os
import sys
import tarfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from orchtest import FakeCmd, orch  # noqa: E402
from test_orch_run import J, WIN, fake_ctx, run_container  # noqa: E402

LIN = orch.Node("hz", {"os": "linux", "ssh": "hz", "context": "hz", "enabled": True, "opt_in": ["app-image"]})
A = {"reserve_frac": 0.1, "psi_some_avg10_max": 10, "swap_pages_per_s_max": 100, "disk_free_frac_min": 0.1,
     "disk_free_gb_min": 1, "inode_free_frac_min": 0.05, "load_frac_max": 2.0}
W = {"mem": 1, "load": 1, "affinity": 1}
WD = {"iowait_max": 50}
GOOD = {"reach": True, "docker": True, "mem_total": 8 << 30, "mem_avail": 6 << 30, "disk_total": 100 << 30,
        "disk_free": 50 << 30, "load1": 0.5, "ncpu": 4, "cores": 4, "iowait": 1, "psi": None, "images": {}}


def run(fn, a):
    o, e = io.StringIO(), io.StringIO()
    with redirect_stdout(o), redirect_stderr(e):
        rc = fn(a)
    return rc, o.getvalue(), e.getvalue()


def adm(p, enforce=False):
    with mock.patch.object(orch, "say"):
        return orch.admit({"memory": "1g"}, LIN, p, A=dict(A, enforce_new_checks=enforce), W=W, WD=WD, images={})


class NodesMissingMetricO21(unittest.TestCase):
    def test_no_keyerror(self):
        p = {"reach": True, "docker": True, "engine": "27", "psi": None, "iowait": 2, "load1": 0.5, "cores": 4, "probe_s": 0.1}
        ns = {"h1": orch.Node("h1", {"os": "linux", "ssh": "h1", "enabled": True})}
        for full in (False, True):
            with mock.patch.object(orch, "nodes_all", lambda *a, **k: ns), mock.patch.object(orch, "probe", lambda n: dict(p)), \
                    mock.patch.object(orch, "admit", lambda *a, **k: {"ok": False}):
                rc, out, _ = run(orch.cmd_nodes, SimpleNamespace(json=False, all=full))
            self.assertIn("?/?", out)


class AdmissionO22(unittest.TestCase):
    def test_good_admits(self):
        self.assertTrue(adm(dict(GOOD))["ok"])

    def test_mem_disk_missing_always_reject(self):
        for k in ("mem_total", "mem_avail", "disk_total", "disk_free"):
            p = dict(GOOD)
            del p[k]
            r = adm(p)
            self.assertFalse(r["ok"], k)
            details = [c["detail"] for c in r["checks"] if not c["ok"]]
            self.assertIn(f"metric missing: {k}", details)
            self.assertFalse(any(" 0M" in d for d in details), details)

    def test_load_ncpu_missing_warn_only(self):
        for k in ("load1", "ncpu"):
            p = dict(GOOD)
            del p[k]
            r = adm(p)
            self.assertTrue(r["ok"], k)
            self.assertIn(f"metric missing: {k}", r["warnings"])
            self.assertFalse(adm(p, enforce=True)["ok"], k)

    def test_psi_na_allowed(self):
        r = adm(dict(GOOD, psi=None))
        self.assertTrue(r["ok"])
        self.assertEqual(r["warnings"], [])


class RetryNotAdmittedO23(unittest.TestCase):
    def test_previous_exit_kept(self):
        ctx = fake_ctx()
        ctx.meta.update(result="oom", exit=orch.EXIT["oom"], wall_s=5, wait_s=1, overhead_s=2, failures=[])
        ctx.job.c["memory"] = "1g"
        a = SimpleNamespace(node=None, no_retry=False, _sel_s=0)
        sel = iter([(WIN, {}, {"jobid": J}, {}), (None, None, None, "h1: memory too low")])
        buf = io.StringIO()
        with mock.patch.object(orch, "select_node", lambda *x, **k: next(sel)), mock.patch.object(orch, "say"), \
                mock.patch.object(orch, "execute", lambda *x, **k: ("oom", ctx)), mock.patch("sys.stdout", buf):
            rc = orch._run_attempts(ctx.job, a)
        d = json.loads(buf.getvalue().strip().splitlines()[-1])
        self.assertEqual(rc, orch.EXIT["oom"])
        self.assertNotEqual(rc, orch.NOADMIT)
        self.assertEqual(d["result"], "oom")
        self.assertEqual(d["exit"], orch.EXIT["oom"])
        self.assertEqual(d["reason"], "retry not admitted: h1: memory too low")


class NonceO24(unittest.TestCase):
    def test_script_reads_file_not_env(self):
        s = orch.compose_script("prep", "run", nonce=True)
        first = s.split("\n")[0]
        self.assertIn(orch.NONCE_FILE, first)
        self.assertIn("rm -f", first)
        self.assertNotIn("AGENTX_NONCE", s)
        self.assertNotIn("export", s)
        self.assertLess(s.index("rm -f"), s.index("prep"))

    def test_nonce_not_in_argv_but_copied(self):
        ok = lambda c: [f"AGENTX_PREP_DONE {c.nonce}\n", f"AGENTX_NET_NONE {c.nonce} interfaces: lo\n", "t\n"]
        cls, ctx, fc = run_container(ok)
        self.assertEqual(cls, "pass")
        self.assertFalse(any(ctx.nonce in " ".join(a) for a in fc.argvs))
        cps = [(a, kw) for a, kw in fc.calls if "cp" in a and kw.get("input")]
        self.assertEqual(len(cps), 1)
        with tarfile.open(fileobj=io.BytesIO(cps[0][1]["input"])) as t:
            m = t.getmember(orch.NONCE_FILE.lstrip("/"))
            self.assertEqual(t.extractfile(m).read().decode(), ctx.nonce)

    def test_cp_failure_is_infra(self):
        ctx = fake_ctx(prep="echo p", cmd="echo t")

        def resp(argv, kw):
            return orch.R(1, "", "read-only") if "cp" in argv and kw.get("input") else None
        with FakeCmd(resp), mock.patch.object(orch, "ensure_image", lambda *a, **k: orch.R(0)), \
                mock.patch.object(orch, "alloc_subnet", lambda *a, **k: "10.252.20.16/28"):
            with self.assertRaises(orch.OrchError) as e:
                orch.run_container_job(ctx)
        self.assertEqual(e.exception.cls, "infra")


class BuilderOwnerO25(unittest.TestCase):
    def test_foreign_owner_refused(self):
        jobs = {"jobs": {"app-image": {"kind": "build"}}}

        def resp(argv, kw):
            return orch.R(0, "running\t1\tother-master\n") if "ps" in argv else None
        with FakeCmd(resp) as fc, mock.patch.object(orch, "cfg_jobs", lambda: jobs):
            with self.assertRaises(orch.OrchError) as e:
                orch.ensure_builder(LIN, {}, J)
        self.assertEqual(e.exception.cls, "infra")
        self.assertIn(f"builder {orch.PREFIX}builder owned by other-master", e.exception.msg)
        self.assertEqual(len(fc.calls), 1)

    def test_own_builder_ok(self):
        jobs = {"jobs": {"app-image": {"kind": "build"}}}

        def resp(argv, kw):
            return orch.R(0, f"running\t1\t{orch.OWNER}\n") if "ps" in argv else None
        with FakeCmd(resp), mock.patch.object(orch, "cfg_jobs", lambda: jobs), \
                mock.patch.object(orch, "cfg_nodes", lambda: {"builder": {}}):
            try:
                orch.ensure_builder(LIN, {}, J)
            except orch.OrchError as e:
                self.assertNotIn("owned by", e.msg)


class StopAllO26(unittest.TestCase):
    def test_failed_job_objects_kept(self):
        n = orch.Node("hz", {"os": "linux", "ssh": "hz", "enabled": True})
        objs = [{"kind": "container", "name": "agentt-a-job", "job": "jobA", "role": "job"},
                {"kind": "container", "name": "agentt-b-job", "job": "jobB", "role": "job"}]
        got = []
        with mock.patch.object(orch, "kill_local_jobs", lambda: ([], ["jobA (pid 1234): still alive after 10s"])), \
                mock.patch.object(orch, "nodes_all", lambda *a: {"hz": n}), \
                mock.patch.object(orch, "list_agent", lambda *a, **k: {"ok": True, "objects": objs}), \
                mock.patch.object(orch, "remove_objects", lambda n_, o, *a, **k: got.extend(o) or [x["name"] for x in o]):
            rc, out, _ = run(orch.cmd_stop_all, None)
        self.assertEqual(rc, 5)
        self.assertEqual([o["name"] for o in got], ["agentt-b-job"])


class NsCheckO27(unittest.TestCase):
    def test_default_family_reserved(self):
        with self.assertRaises(ValueError):
            orch.ns_check("agentx-", "io.orch.agent2")
        orch.ns_check("agentx-", "io.orch.agent")
        orch.ns_check("agentt-", "io.orchtest.agent")


class InventoryJsonO28(unittest.TestCase):
    def test_pure_json(self):
        n = orch.Node("hz", {"os": "linux", "ssh": "hz", "context": "hz", "enabled": True})
        fp = {"ok": True, "containers": {}, "latency": {}, "services": {}}
        with mock.patch.object(orch, "nodes_all", lambda *a, **k: {"hz": n}), \
                mock.patch.object(orch, "list_agent", lambda *a, **k: {"ok": True, "err": "", "objects": [], "collisions": []}), \
                mock.patch.object(orch, "fingerprint", side_effect=[dict(fp), dict(fp)]), \
                mock.patch.object(n, "docker", lambda *a, **k: orch.R(0, "TYPE TOTAL\n")):
            rc, out, _ = run(orch.cmd_inventory, SimpleNamespace(node="hz", all=False, json=True, top=None))
        d = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(d["fingerprint"]["worse"], [])


class FingerprintBaselineO29(unittest.TestCase):
    def test_unreachable_at_baseline(self):
        prot = {"nodes": {"hz": {"endpoints": [{"name": "app", "url": "http://x"}, {"name": "ok", "url": "http://y"}]}}}
        out = "FPN 0\nFPEND\nLAT 0 5 5 5\nLAT 1 0.1 0.2 0.1\n"
        said = []
        with mock.patch.object(orch, "cfg_prot", lambda: prot), mock.patch.object(orch.Node, "shell", lambda *a, **k: orch.R(0, out)), \
                mock.patch.object(orch, "say", lambda m, **k: said.append(m)):
            fp = orch.fingerprint(orch.Node("hz", LIN.c))
        self.assertEqual(fp["unreachable_at_baseline"], ["app"])
        self.assertTrue(any("unreachable" in m and "app" in m for m in said))


if __name__ == "__main__":
    unittest.main()
