"""Characterization + namespace tests for orch.py pure functions. Run from the skill root:
python -B -m unittest discover -s tests -p "test_*.py"
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchtest import FakeCmd, TEST_ENV, fixture, orch

R = orch.R
G = 1 << 30
CFGN = orch.cfg_nodes()
A, W, WD = CFGN["admission"], CFGN["score_weights"], CFGN["watchdog"]
LINUX = orch.Node("n1", {"os": "linux", "ssh": "n1", "context": "n1"})
WIN = orch.Node("n2", {"os": "windows", "ssh": "n2", "context": "n2"})
FIX = "probe-linux-metrics.txt"


def probe_dict(**kw):
    d = {"reach": True, "docker": True, "images": {}, "caches": [], "builder": False}
    d.update(orch.parse_probe(fixture(FIX)))
    d.update(kw)
    return d


class ParseProbe(unittest.TestCase):
    def test_linux_fixture(self):
        d = orch.parse_probe(fixture(FIX), "linux")
        self.assertEqual(d["mem_total"], 16135672 * 1024)
        self.assertEqual(d["mem_avail"], 7860760 * 1024)  # last sample wins
        self.assertIsNone(d["psi"])
        self.assertEqual(d["swap_pages_s"], 0)
        self.assertEqual(d["iowait"], 22)
        self.assertEqual((d["load1"], d["cores"]), (2.2, 8))
        self.assertEqual((d["disk_total"], d["disk_free"]), (976279804 * 1024, 829543888 * 1024))
        self.assertEqual((d["inodes_total"], d["inodes_free"]), (488378368, 485459076))
        self.assertEqual((d["images"], d["heavy_jobs"], d["builder"], d["caches"]), ({}, [], False, []))
        self.assertNotIn("docker", d)

    def test_linux_markers(self):
        t = ("REACH\nDOCKER 29.7.2 8 16522928128 /var/lib/docker\nDEV /dev/sda\nHEAVY j1 j2 \nBUILDER 1\n"
             "CACHE agentt-cache-npm \nIMG img@sha256:aa 1\nIMG img2@sha256:bb 0\nP 3.5\nW 7\n")
        d = orch.parse_probe(t)
        self.assertEqual((d["docker"], d["engine"], d["ncpu"], d["vm_mem"], d["root"]),
                         (True, "29.7.2", 8, 16522928128, "/var/lib/docker"))
        self.assertEqual(d["dev"], "/dev/sda")
        self.assertEqual(d["heavy_jobs"], ["j1", "j2"])
        self.assertTrue(d["builder"])
        self.assertEqual(d["caches"], ["agentt-cache-npm"])
        self.assertEqual(d["images"], {"img@sha256:aa": True, "img2@sha256:bb": False})
        self.assertEqual((d["psi"], d["iowait"]), (3.5, 7))

    def test_linux_bad_metric_line_skipped(self):
        d = orch.parse_probe("M x y\nL 1.0\nW 5\n")
        self.assertNotIn("mem_total", d)
        self.assertNotIn("load1", d)
        self.assertEqual(d["iowait"], 5)

    def test_windows(self):
        t = ("M 8000000 4000000\nP 1.5\nS 0\nW 3\nL 0.5 4\nD 100 50\n\n"
             "WMEM 33554432 16777216\nWDISK 1000 500\nWCPU 12.5\nWCPU\n")
        d = orch.parse_probe(t, "windows")
        self.assertEqual((d["mem_total"], d["mem_avail"]), (8000000 * 1024, 4000000 * 1024))
        self.assertEqual((d["psi"], d["load1"], d["cores"]), (1.5, 0.5, 4))
        self.assertEqual((d["host_mem_total"], d["host_mem_free"]), (33554432 * 1024, 16777216 * 1024))
        self.assertEqual((d["host_c_total"], d["host_c_free"], d["host_cpu_pct"]), (1000, 500, 12.5))
        self.assertNotIn("images", d)

    def test_probe_uses_parser_and_namespace(self):
        out = "REACH\nDOCKER 29.7.2 8 16522928128 /var/lib/docker\nDEV /dev/sda\nHEAVY \nBUILDER 0\nCACHE \n" + \
            fixture(FIX)
        with FakeCmd(lambda argv, kw: R(0, out)) as fc:
            d = orch.probe(LINUX)
        self.assertTrue(d["reach"] and d["docker"])
        self.assertEqual(d["iowait"], 22)
        script = fc.calls[0][1]["input"].decode()
        self.assertIn("label=io.orchtest.agent=1", script)
        self.assertIn("name=^agentt-builder$", script)
        self.assertIn("/tmp/.agentt-dinfo", script)
        self.assertNotIn("io.orch.agent=1", script)
        self.assertNotIn("agentx-", script)


class Admit(unittest.TestCase):
    def test_fixture_admits_1g(self):
        r = orch.admit({"memory": "1g"}, LINUX, probe_dict(), A=A, W=W, images={})
        self.assertTrue(r["ok"])
        self.assertEqual([c["check"] for c in r["checks"]], ["memory", "psi", "swap", "disk", "inodes", "load"])
        self.assertEqual(r["breakdown"]["image_present"], 1.0)

    def test_defaults_match_injected(self):
        p = probe_dict()
        self.assertEqual(orch.admit({"memory": "2g"}, LINUX, p), orch.admit({"memory": "2g"}, LINUX, p, A=A, W=W,
                                                                            images=orch.images_cfg()))

    def test_unreachable_and_docker_down(self):
        r = orch.admit({}, LINUX, {"reach": False, "reason": "unreachable: x"}, A=A, W=W, images={})
        self.assertEqual((r["ok"], r["score"], r["checks"][0]["check"]), (False, None, "reachable"))
        r = orch.admit({}, LINUX, {"reach": True, "docker": False}, A=A, W=W, images={})
        self.assertEqual(r["checks"][0]["check"], "docker")

    def test_memory_reserve_rejects(self):
        r = orch.admit({"memory": "6g"}, LINUX, probe_dict(mem_avail=8 * G), A=A, W=W, images={})
        self.assertFalse(r["ok"])
        self.assertFalse(next(c for c in r["checks"] if c["check"] == "memory")["ok"])

    def test_load_and_psi_reject(self):
        r = orch.admit({}, LINUX, probe_dict(load1=7.5, psi=12.0), A=A, W=W, images={})
        bad = {c["check"] for c in r["checks"] if not c["ok"]}
        self.assertEqual(bad, {"load", "psi"})

    def test_affinity(self):
        p = probe_dict(images={"ref@sha256:1": False}, caches=["agentt-cache-npm"])
        r = orch.admit({"image": "x", "cache": {"name": "npm"}}, LINUX, p, A=A, W=W, images={"x": "ref@sha256:1"})
        self.assertEqual((r["breakdown"]["image_present"], r["breakdown"]["warm_cache"]), (0.0, 1.0))
        p["caches"] = ["agentx-cache-npm"]  # other namespace's cache is not ours
        r = orch.admit({"image": "x", "cache": {"name": "npm"}}, LINUX, p, A=A, W=W, images={"x": "ref@sha256:1"})
        self.assertEqual(r["breakdown"]["warm_cache"], 0.0)

    def test_bug_F2_missing_metric_fail_open(self):
        p = {"reach": True, "docker": True, "images": {}, "mem_total": 16 * G, "mem_avail": 8 * G,
             "disk_total": 900 * G, "disk_free": 800 * G}  # load1/cores missing (truncated probe)
        r = orch.admit({"memory": "1g"}, LINUX, p, A=dict(A, enforce_new_checks=True), W=W, images={})
        self.assertFalse(r["ok"])
        self.assertTrue(any("metric missing" in c["detail"] for c in r["checks"]))


class Classify(unittest.TestCase):
    def test_table(self):
        c = orch.classify
        self.assertEqual(c({"ExitCode": 0}, 0, "", {"guard": True}), "r1-guard")
        self.assertEqual(c({"Error": "boom", "ExitCode": 128}, 128), "infra")
        self.assertEqual(c({"OOMKilled": True, "ExitCode": 137}, 137), "oom")
        self.assertEqual(c({"OOMKilled": True, "Error": "x"}, 137), "oom")
        self.assertEqual(c({"ExitCode": 137}, 137, "", {"timed_out": True}), "timeout")
        self.assertEqual(c({"ExitCode": 0}, 0), "pass")
        self.assertEqual(c({}, 0), "pass")
        self.assertEqual(c({}, 1), "test-fail")
        self.assertEqual(c({"ExitCode": 137}, 137, "Killed process 12"), "oom")
        self.assertEqual(c({"ExitCode": 137}, 137, "all fine"), "test-fail")
        self.assertEqual(c({"ExitCode": 2}, 2, "out of memory"), "test-fail")

    def test_ctx_wrapper_notes(self):
        ctx = mock.Mock(guard=None, timed_out=False, tail="", meta={})
        self.assertEqual(orch.classify_ctx(ctx, {"Error": "no such image"}, 1), "infra")
        self.assertEqual(ctx.meta["notes"], ["container start error: no such image"])

    def test_is_build_oom(self):
        for t in ("OOMKilled", "fatal: out of memory", "signal: killed", "exit code: 137"):
            self.assertTrue(orch.is_build_oom(t), t)
        self.assertFalse(orch.is_build_oom("error CS1002: ; expected"))

    def test_bug_F3_room_is_not_oom(self):
        self.assertFalse(orch.is_build_oom("failed to copy: not enough room for layer"))


class FpDiff(unittest.TestCase):
    OLD = "2026-10-01T08:00:00.000000000Z"
    T = orch.docker_time("2026-10-29T10:00:00Z")

    def c(self, name, status="running", health="healthy", restarts=0, started=OLD):
        return {"name": name, "status": status, "health": health, "restarts": restarts, "started": started}

    def test_stable_vs_recent(self):
        fresh = "2026-10-29T09:59:00Z"
        a = {"containers": {"c1": self.c("app1-api-1"), "c2": self.c("app1-web", started=fresh)}, "services": {}}
        b = {"containers": {"c1": self.c("app1-api-1", health="unhealthy", restarts=1, started="2026-10-29T10:05:00Z"),
                            "c2": self.c("app1-web", health="unhealthy", restarts=1, started="2026-10-29T10:05:00Z")},
             "services": {}}
        worse, info = orch.fp_diff(a, b, self.T)
        self.assertEqual(len(worse), 3)
        self.assertTrue(all(w.startswith("app1-api-1") for w in worse))
        self.assertTrue(any("app1-web" in i and "info only" in i for i in info))

    def test_t_from_a_wins(self):
        a = {"t": 0, "containers": {"c1": self.c("x")}, "services": {}}
        worse, _ = orch.fp_diff(a, {"containers": {}, "services": {}}, self.T)
        self.assertEqual(worse, [])  # started after t=0: not stable

    def test_gone_new_services(self):
        a = {"containers": {"c1": self.c("db"), "c3": self.c("old", status="exited")},
             "services": {"DockerDesktop": "1", "W3SVC": "Running"}}
        b = {"containers": {"c3": self.c("old", status="running"), "c9": self.c("new")},
             "services": {"DockerDesktop": "0", "W3SVC": "Stopped"}}
        worse, info = orch.fp_diff(a, b, self.T)
        self.assertEqual(worse, ["db: container gone", "Docker Desktop process gone", "service W3SVC: Running -> Stopped"])
        self.assertIn("old: exited -> running", info)
        self.assertIn("new: new non-agent container (running)", info)


class Watchdog(unittest.TestCase):
    EPS = [{"name": "api"}]

    def test_ok_line(self):
        s = orch.parse_wd_line("WD na 12 0.010\n", WD, {"api": 0.01}, self.EPS)
        self.assertEqual(s, {"psi": None, "iowait": 12, "lat": [0.01], "bad": []})

    def test_bad_line(self):
        s = orch.parse_wd_line("WD 30 40 1.0", WD, {"api": 0.01}, self.EPS)
        self.assertEqual(len(s["bad"]), 3)
        self.assertTrue(s["bad"][1].startswith("iowait 40%"))

    def test_other_lines(self):
        self.assertIsNone(orch.parse_wd_line("", WD, {}, []))
        self.assertIsNone(orch.parse_wd_line("noise here", WD, {}, []))

    def test_malformed_raises(self):  # current behaviour: the caller's thread dies (fixed on a later branch)
        with self.assertRaises((IndexError, ValueError)):
            orch.parse_wd_line("WD na", WD, {}, [])
        with self.assertRaises(ValueError):
            orch.parse_wd_line("WD na 3 x", WD, {}, self.EPS)

    def test_watchdog_argv_local_linux(self):
        n = orch.Node("hl", {"os": "linux", "ssh": "local", "context": "hl"})
        argv, inp = orch.Watchdog(n, {}, "j1", lambda r: None)._argv()
        self.assertEqual(argv, ["sh", "-s", "--", "720"])
        self.assertIn(b"WD ", inp)
        argv, _ = orch.Watchdog(LINUX, {}, "j1", lambda r: None)._argv()
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-2:], ["n1", "sh -s -- 720"])


class Curl(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(orch.parse_curl_latency("0.012\n"), 0.012)
        self.assertEqual(orch.parse_curl_latency("5"), 5.0)
        with self.assertRaises(ValueError):
            orch.parse_curl_latency("")

    @unittest.skipUnless(shutil.which("sh"), "needs sh")
    def test_bug_F2_curl_concat(self):
        # a stub curl that prints time_total and then fails, as real curl does on a refused connection
        script = "curl() { printf 0.000123; return 7; }\necho \"$(" + orch.curl_cmd({"url": "http://127.0.0.1:9/"}) + ")\"\n"
        out = subprocess.run([shutil.which("sh"), "-c", script], capture_output=True, text=True, timeout=30).stdout
        self.assertGreaterEqual(orch.parse_curl_latency(out), 5.0)


class GcTmp(unittest.TestCase):
    NOW = 1_800_000_000.0
    E = [{"name": "src-1-a.tar", "mtime": NOW - 7200}, {"name": "idx-2-b", "mtime": NOW - 7200}]

    def test_no_live_removes_all(self):
        self.assertEqual(orch.select_gc_tmp(self.E, [], self.NOW), ["src-1-a.tar", "idx-2-b"])
        self.assertEqual(orch.select_gc_tmp(self.E, [{"_alive": False}], self.NOW), ["src-1-a.tar", "idx-2-b"])

    def test_live_keeps_all(self):
        self.assertEqual(orch.select_gc_tmp(self.E, [{"_alive": True}], self.NOW), [])

    def test_bug_F4_gc_tmp_in_flight(self):
        # another session's run is building its source tar before it has a live file
        fresh = [{"name": f"src-{os.getpid()}-c.tar", "mtime": self.NOW - 5}]
        self.assertEqual(orch.select_gc_tmp(fresh, [], self.NOW), [])


# ----------------------------------------------------------------------------- namespace
class FakeDockerNode(orch.Node):
    """Tiny docker emulator for containers: honours --filter label=/name= and renders the --format fields."""

    def __init__(self, objs):
        super().__init__("fake", {"os": "linux", "ssh": "fake", "context": "fake"})
        self.objs, self.sent = objs, []

    def _match(self, o, flt):
        k, _, v = flt.partition("=")
        if k == "label":
            lk, eq, lv = v.partition("=")
            return lk in o["labels"] and (not eq or o["labels"][lk] == lv)
        if k == "name":
            return re.search(v, o["name"]) is not None
        return False

    def _render(self, o, fmt):
        s = re.sub(r'\{\{\.Label "([^"]+)"\}\}', lambda m: o["labels"].get(m.group(1), ""), fmt)
        s = s.replace("{{.Labels}}", ",".join(f"{k}={v}" for k, v in o["labels"].items()))
        return (s.replace("{{.ID}}", "id-" + o["name"]).replace("{{.Names}}", o["name"])
                .replace("{{.Name}}", o["name"]).replace("{{.State}}", "exited"))

    def _ls(self, kind, c):  # objects without "kind" are containers
        flts = [c[i + 1] for i, a in enumerate(c) if a == "--filter"]
        fmt = c[c.index("--format") + 1]
        rows = [self._render(o, fmt) for o in self.objs
                if o.get("kind", "container") == kind and all(self._match(o, f) for f in flts)]
        return R(0, "".join(r + "\n" for r in rows))

    def run1(self, c):
        if c[:1] == ["ps"]:
            return self._ls("container", c)
        if c[:2] in (["volume", "ls"], ["network", "ls"]):
            return self._ls(c[0], c)
        if c[:1] == ["images"]:
            return R(0, "")
        if c[:1] == ["inspect"]:
            o = next((o for o in self.objs if o["name"] == c[-1]), None)
            return R(0, f"/{o['name']}\t{json.dumps(o['labels'])}\n") if o else R(1, "", "Error: No such object")
        self.sent.append(c)
        return R(0, "")

    def dbatch(self, cmds, job=None, timeout=90):
        return [self.run1(list(map(str, c))) for c in cmds]

    def docker(self, args, **kw):
        return self.run1(list(map(str, args)))


def ns_objects():
    own = orch.OWNER
    old = {"io.orch.agent": "1", "io.orch.owner": own, "io.orch.job": "j0", "io.orch.lease": "0", "io.orch.role": "builder"}
    return [
        {"name": "agentx-builder", "labels": dict(old)},
        {"name": "agentx-1-run", "labels": dict(old, **{"io.orch.role": "run"})},
        {"name": "agentx-mixed", "labels": dict(old, **{"io.orchtest.agent": "1", "io.orch.role": "run"})},
        {"name": "agentt-1-run", "labels": {"io.orchtest.agent": "1", "io.orchtest.owner": own, "io.orchtest.job": "j1",
                                            "io.orchtest.lease": "0", "io.orchtest.role": "run"}},
        {"name": "agentt-nolabel", "labels": {"io.orch.agent": "1", "io.orch.owner": own}},
        {"name": "app1-web", "labels": {}},
    ]


class Namespace(unittest.TestCase):
    def test_ours(self):
        self.assertFalse(orch.ours("agentx-builder", "1"))
        self.assertTrue(orch.ours("agentt-builder", "1"))
        self.assertFalse(orch.ours("agentt-builder", ""))

    def test_label_family(self):
        self.assertEqual((orch.LNS, orch.L_OWNER, orch.L_HEAVY), ("io.orchtest.", "io.orchtest.owner", "io.orchtest.heavy"))
        self.assertEqual(orch.COLLIDE[4][1][3], "label=io.orchtest.owner")
        self.assertFalse(any("io.orch." in a for _, c in orch.COLLIDE for a in c))
        with self.assertRaises(ValueError):
            orch.ns_check("agentt-", "nodot")

    def test_filters_use_test_namespace(self):
        allargs = [a for c in list(orch.LIST_FMT.values()) + [c for _, c in orch.COLLIDE] for a in c]
        self.assertFalse(any("io.orch.agent=" in a or '"io.orch.agent"' in a or "agentx-" in a for a in allargs))
        for k, c in orch.LIST_FMT.items():
            self.assertIn("label=io.orchtest.agent=1", c, k)
        self.assertEqual([c[3] for _, c in orch.COLLIDE[:3]], ["name=agentt-"] * 3)
        self.assertEqual(orch.COLLIDE[3][1][2], "reference=agentt-*")
        self.assertIn("io.orchtest.agent=1", orch.label_args("j", "run", 60, False))
        self.assertEqual(orch.builder_ref(LINUX), "agentt-builder-n1")

    def test_list_agent(self):
        la = orch.list_agent(FakeDockerNode(ns_objects()))
        names = sorted(o["name"] for o in la["objects"])
        self.assertEqual(names, ["agentt-1-run", "agentx-mixed"])
        self.assertTrue(any("agentx-mixed" in c and "never touched" in c for c in la["collisions"]))

    def _removed(self, fn):
        n = FakeDockerNode(ns_objects())
        with FakeCmd(), mock.patch.object(orch, "say"):
            fn(n)
        return [c for c in n.sent if c[:1] == ["rm"]]

    # council 2 MF5: this suite runs in the test namespace (agentt- / io.orchtest.*), so objects named agentt-* with the
    # default family's labels (io.orch.*) are another orch namespace's leftovers here, as v1's are for a real master
    OTHER = [{"name": "agentt-1-run", "labels": {"io.orchtest.agent": "1", "io.orchtest.owner": orch.OWNER,
                                                 "io.orchtest.job": "j1", "io.orchtest.lease": "0",
                                                 "io.orchtest.role": "run"}},
             {"name": "agentt-9-job", "labels": {"io.orch.agent": "1", "io.orch.job": "9"}},
             {"name": "agentt-cache-pip", "kind": "volume", "labels": {}},
             {"name": "agentt-9-net", "kind": "network", "labels": {"io.other.agent": "1"}},
             {"name": "agentt-half", "labels": {"io.orchtest.owner": "m2"}},
             {"name": "app1-web", "labels": {}}]
    OTHER_TEXT = ("3 objects named agentt-* belong to another orch namespace (label io.orch.*, io.other.*, none): an "
                  "older orch or another master. Fix: run that orch's gc --session-end on this host (removing "
                  "volumes needs the user's OK); or, if no other master uses this host, remove them by hand: docker "
                  "rm -f agentt-9-job; docker volume rm agentt-cache-pip; docker network rm agentt-9-net")

    def test_list_agent_tells_another_namespace_apart(self):
        la = orch.list_agent(FakeDockerNode(self.OTHER))
        self.assertEqual([o["name"] for o in la["objects"]], ["agentt-1-run"])
        self.assertEqual(la["foreign"], [{"kind": "container", "name": "agentt-9-job", "family": "io.orch"},
                                         {"kind": "volume", "name": "agentt-cache-pip", "family": None},
                                         {"kind": "network", "name": "agentt-9-net", "family": "io.other"}])
        self.assertEqual(la["collisions"][0], self.OTHER_TEXT)  # what status, reconcile and gc print first
        self.assertIn("container agentt-half uses the agentt- prefix without io.orchtest.agent=1", la["collisions"])
        self.assertFalse(any("agentt-9" in c or "cache-pip" in c for c in la["collisions"][1:]))
        self.assertEqual(orch.list_agent(FakeDockerNode(self.OTHER[:1]))["foreign"], [])
        with mock.patch.object(orch, "_discover", lambda: None):  # a broken install: plain collisions, never a crash
            la = orch.list_agent(FakeDockerNode(self.OTHER))
        self.assertEqual(la["foreign"], [])
        self.assertIn("container agentt-9-job uses the agentt- prefix without io.orchtest.agent=1", la["collisions"])

    def test_gc_and_reconcile_never_remove_another_namespace(self):
        for fn in (orch.gc_node, lambda n: orch.reconcile_node(n, quiet=True, force_all=True)):
            n = FakeDockerNode(self.OTHER)
            with FakeCmd(), mock.patch.object(orch, "say"):
                fn(n)
            self.assertEqual([c for c in n.sent if c[:1] == ["rm"] or c[1:2] == ["rm"]],
                             [["rm", "-f", "-v", "agentt-1-run"]])
        with FakeCmd(), mock.patch.object(orch, "say"):
            r = orch.gc_node(FakeDockerNode(self.OTHER))
        self.assertEqual(r["collisions"][0], self.OTHER_TEXT)

    def test_preflight_refuses_another_namespace_with_the_fix(self):
        la = orch.list_agent(FakeDockerNode(self.OTHER))
        node = orch.Node("h12", {"os": "linux", "ssh": "app-host", "context": "orch-h12", "enabled": True})
        self.assertEqual((orch.node_label(node), orch.node_label(LINUX)), ("h12 (app-host)", "n1"))
        opts = SimpleNamespace(lease=300, heartbeat=60, _src=None, _sel_s=0.0)
        with FakeCmd() as fc, mock.patch.object(orch, "list_agent", lambda *a, **k: la), \
                mock.patch.object(orch, "reconcile_node", side_effect=AssertionError("reconciled")), \
                mock.patch.object(orch, "cleanup_job", lambda *a, **k: []), mock.patch.object(orch, "say"):
            cls, ctx = orch.execute(orch.Job("t-ok", []), node, {"caches": []}, {"jobid": "1030000000f00d"}, opts)
        self.assertEqual((cls, orch.EXIT[cls]), ("infra", 5))
        self.assertEqual(ctx.meta["error"], "h12 (app-host): " + self.OTHER_TEXT)
        self.assertEqual(ctx.meta["reason"], ("h12 (app-host): " + self.OTHER_TEXT)[:300])
        self.assertFalse([a for a in fc.argvs if "run" in a or "create" in a])  # nothing started on the node

    def test_reconcile_selection(self):
        self.assertEqual(self._removed(lambda n: orch.reconcile_node(n, quiet=True, force_all=True)),
                         [["rm", "-f", "-v", "agentt-1-run"]])

    def test_gc_selection(self):
        self.assertEqual(self._removed(orch.gc_node), [["rm", "-f", "-v", "agentt-1-run"]])

    def test_stop_all_selection(self):
        def go(n):
            with mock.patch.object(orch, "nodes_all", lambda *a: {"fake": n}), mock.patch("builtins.print"):
                orch.cmd_stop_all(None)
        self.assertEqual(self._removed(go), [["rm", "-f", "-v", "agentt-1-run"]])

    def test_remove_refuses_other_namespace(self):
        n = FakeDockerNode(ns_objects())
        objs = [{"kind": "container", "name": o["name"]} for o in n.objs]
        with mock.patch.object(orch, "say"):
            done = orch.remove_objects(n, objs, "test")
        self.assertEqual(done, ["container agentt-1-run"])
        self.assertEqual(n.sent, [["rm", "-f", "-v", "agentt-1-run"]])

    def test_ns_check(self):
        for p, l in (("agentx-", "io.orch.agent"), ("ab-", "ab.c"), ("a1-b-", "x.y-z")):
            orch.ns_check(p, l)
        for p, l in (("Agentx-", "io.orch.agent"), ("agentx", "io.orch.agent"), ("a-", "io.orch.agent"),
                     ("1abc-", "io.orch.agent"), ("abcdefghijklmnop-", "io.orch.agent"), ("ab_c-", "io.orch.agent"),
                     ("agentx-", "IO.orch"), ("agentx-", "ab"), ("agentx-", "a" * 41), ("agentx-", "io orch"), ("agentx-", "abc"), ("agentx-", ".abc"),
                     ("", "io.orch.agent")):
            with self.assertRaises(ValueError, msg=(p, l)):
                orch.ns_check(p, l)

    def test_bad_env_exits_2(self):
        script = str(Path(orch.__file__))
        for k, v in (("ORCH_PREFIX", "BAD"), ("ORCH_LABEL", "x")):
            env = dict(os.environ)
            env.update(TEST_ENV)
            env[k] = v
            p = subprocess.run([sys.executable, "-B", script, "status"], env=env, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn(k, p.stderr)


class ConfigResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orchcfg-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_orch_config_wins(self):
        (self.tmp / "home" / "config").mkdir(parents=True)
        (self.tmp / "home" / "config" / "nodes.json").write_text("{}")
        with mock.patch.dict(os.environ, {"ORCH_CONFIG": str(self.tmp / "cfg")}), \
                mock.patch.object(orch, "HOME", self.tmp / "home"):
            self.assertEqual(orch.cfg_path("nodes.json"), self.tmp / "cfg" / "nodes.json")

    def test_no_config_no_fallback(self):
        # v1 fell back to $ORCH_HOME/config, then the skill's own config/; v2 has no fallback: "run orch init"
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "nodes.json").write_text("{}")
        env = {k: v for k, v in os.environ.items() if k != "ORCH_CONFIG"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(orch, "HOME", self.tmp):
            for fn in (orch.config_sources, orch.config_line, orch.cfg_nodes, orch.cfg_jobs, orch.cfg_prot):
                with self.assertRaises(orch.NoSetup) as e:
                    fn()
                self.assertEqual(str(e.exception), "no orch setup here: run orch init")
        s = orch.config_sources()
        self.assertEqual(orch.config_line(), f"config: nodes={s['nodes.json']} jobs={s['jobs.json']} protected={s['protected.json']}")

    def test_harness_isolated(self):
        self.assertEqual(orch.config_sources()["protected.json"], str(Path(TEST_ENV["ORCH_CONFIG"]) / "protected.json"))
        self.assertIsNone(orch.cfg_prot()["alert"]["command"])
        self.assertEqual(str(orch.HOME), TEST_ENV["ORCH_HOME"])

    def test_nodes_and_plan_print_config_first(self):
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))), \
                mock.patch.object(orch, "probe"):
            off = {"hx": orch.Node("hx", {"os": "linux", "ssh": "hx", "enabled": False, "note": "off"})}
            with mock.patch.object(orch, "nodes_all", lambda *a, **k: off):
                orch.cmd_nodes(mock.Mock(json=True))
            self.assertTrue(out[0].startswith("config: nodes="))
            out.clear()
            job = next(iter(orch.cfg_jobs()["jobs"]))
            with mock.patch.object(orch, "nodes_all", lambda *a, **k: {}):
                orch.cmd_plan(mock.Mock(job=job))
            self.assertTrue(out[0].startswith("config: nodes="))


class SshLocal(unittest.TestCase):
    def test_linux_local(self):
        n = orch.Node("hl", {"os": "linux", "ssh": "local", "context": "hl"})
        with FakeCmd() as fc:
            n.shell("echo hi\n", args=["a b"])
        argv, kw = fc.calls[0]
        self.assertEqual(argv, ["sh", "-s", "--", "a b"])
        self.assertEqual(kw["input"], b"echo hi\n")
        self.assertEqual(n.docker_argv(["ps"]), ["docker", "--context", "hl", "ps"])

    def test_windows_local(self):
        n = orch.Node("wl", {"os": "windows", "ssh": "local", "context": "wl"})
        with FakeCmd() as fc:
            n.shell("'REACH'")
        argv = fc.argvs[0]
        self.assertEqual(argv[:4], ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand"])
        self.assertIn("'REACH'", base64.b64decode(argv[4]).decode("utf-16-le"))

    def test_remote_unchanged(self):
        with FakeCmd() as fc:
            LINUX.shell("x", args=["1"])
            WIN.shell("x")
        a, b = fc.argvs
        self.assertEqual(a, orch.SSH + ["n1", "sh -s -- 1"])
        self.assertEqual(b[:-1], orch.SSH + ["n2"])
        self.assertTrue(b[-1].startswith("powershell -NoProfile -NonInteractive -EncodedCommand "))

    def test_ssh_docker_local_rejected(self):
        n = json.loads(json.dumps(CFGN))
        n["nodes"]["hx"] = {"enabled": False, "os": "linux", "ssh": "local", "transport": "ssh-docker",
                            "docker_host": "unix:///run/docker.sock"}
        with self.assertRaisesRegex(orch.ConfigError, "ssh-docker cannot use ssh local"):
            orch.validate_nodes(n)
        n["nodes"]["hx"]["ssh"] = "hx"
        orch.validate_nodes(n)


if __name__ == "__main__":
    unittest.main()
