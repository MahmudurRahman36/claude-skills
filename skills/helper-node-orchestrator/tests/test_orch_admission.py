"""Admission, probe parsing, fingerprint completeness, audit redaction and config warnings (F2 items)."""
import json
import re
import shutil
import subprocess
import unittest
from unittest import mock

from orchtest import FakeCmd, fixture, orch

R = orch.R
G = 1 << 30
CFGN = orch.cfg_nodes()
A, W, WD = CFGN["admission"], CFGN["score_weights"], CFGN["watchdog"]
LINUX = orch.Node("n1", {"os": "linux", "ssh": "n1", "context": "n1"})
WIN = orch.Node("n2", {"os": "windows", "ssh": "n2", "context": "n2"})
PROBE = fixture("probe-linux-metrics.txt")
DOCKER_LINE = "REACH\nDOCKER 29.7.2 8 16522928128 /var/lib/docker\n"


def full(**kw):
    d = {"reach": True, "docker": True, "images": {}, "caches": [], "ncpu": 8}
    d.update(orch.parse_probe(PROBE))
    d.update(kw)
    return d


def adm(p, node=LINUX, mem="1g", **akw):
    with mock.patch.object(orch, "say") as s:
        r = orch.admit({"memory": mem}, node, p, A=dict(A, **akw), W=W, images={}, WD=WD)
    return r, [c.args[0] for c in s.call_args_list]


def bad(r):
    return {c["check"]: c["detail"] for c in r["checks"] if not c["ok"]}


class Curl(unittest.TestCase):
    @unittest.skipUnless(shutil.which("sh") and shutil.which("awk"), "needs sh+awk")
    def _run(self, body):
        script = body + "\necho \"$(" + orch.curl_cmd({"url": "http://127.0.0.1:9/"}) + ")\"\n"
        return subprocess.run([shutil.which("sh"), "-c", script], capture_output=True, text=True, timeout=30).stdout

    def test_down_endpoint_reads_5s(self):
        out = self._run("curl() { printf '000 0.000123'; return 7; }")
        self.assertEqual(orch.parse_curl_latency(out), 5.0)

    def test_answer_reads_time(self):
        out = self._run("curl() { printf '200 0.042'; }")
        self.assertEqual(orch.parse_curl_latency(out), 0.042)

    def test_no_curl_reads_5s(self):
        out = self._run("curl() { return 127; }")
        self.assertEqual(orch.parse_curl_latency(out), 5.0)

    def test_parse_forms(self):
        self.assertEqual(orch.parse_curl_latency("000 0.0001"), 5.0)
        self.assertEqual(orch.parse_curl_latency("200 0.5\n"), 0.5)
        self.assertIn("%{http_code}", orch.curl_cmd({"url": "http://x/"}))


class RequiredMetrics(unittest.TestCase):
    def test_full_probe_no_warn(self):
        r, said = adm(full())
        self.assertTrue(r["ok"], bad(r))
        self.assertEqual(said, [])

    def test_missing_enforced_rejects(self):
        for k in orch.REQUIRED_METRICS:
            p = full()
            p.pop(k)
            r, _ = adm(p, enforce_new_checks=True)
            self.assertFalse(r["ok"], k)
            self.assertIn(f"metric missing: {k}", bad(r).values())

    def test_unparsable_counts_as_missing(self):
        r, _ = adm(full(load1="x"), enforce_new_checks=True)
        self.assertIn("metric missing: load1", bad(r).values())

    def test_missing_warn_mode_admits(self):
        p = full()
        p.pop("ncpu")
        r, said = adm(p)
        self.assertTrue(r["ok"], bad(r))
        self.assertIn("warn: n1: metric missing: ncpu", said)
        self.assertEqual(r["warnings"], ["metric missing: ncpu"])
        self.assertNotIn("metric", {c["check"] for c in r["checks"]})

    def test_psi_na_not_missing(self):
        p = full()
        self.assertIsNone(p["psi"])
        r, said = adm(p, enforce_new_checks=True)
        self.assertTrue(r["ok"], bad(r))
        self.assertFalse([s for s in said if "psi" in s])

    def test_enforce_flag_validation(self):
        n = json.loads(json.dumps(CFGN))
        n["admission"]["enforce_new_checks"] = True
        orch.validate_nodes(n)
        n["admission"]["enforce_new_checks"] = "yes"
        with self.assertRaises(orch.ConfigError):
            orch.validate_nodes(n)


class Iowait(unittest.TestCase):
    def test_fixture_samples_pass(self):
        for block in re.split(r"(?m)^# sample \d+.*$", PROBE)[1:]:
            p = full(**orch.parse_probe(block))
            self.assertIn(p["iowait"], (22, 23, 24))
            r, _ = adm(p)
            self.assertNotIn("iowait", bad(r))

    def test_36_rejected_even_warn_mode(self):
        r, _ = adm(full(iowait=36))
        self.assertFalse(r["ok"])
        d = bad(r)["iowait"]
        self.assertIn("36", d)
        self.assertIn(str(WD["iowait_max"]), d)


class WindowsHost(unittest.TestCase):
    def win(self, free):
        return full(host_mem_total=32 * G, host_mem_free=free)

    def test_ok(self):
        r, said = adm(self.win(20 * G), node=WIN)
        self.assertTrue(r["ok"], bad(r))
        self.assertEqual(said, [])

    def test_low_enforced(self):
        r, _ = adm(self.win(8 * G), node=WIN, enforce_new_checks=True)
        self.assertIn("host_memory", bad(r))

    def test_low_warns(self):
        r, said = adm(self.win(8 * G), node=WIN)
        self.assertTrue(r["ok"])
        self.assertTrue(any(s.startswith("warn: n2: host free") for s in said))

    def test_linux_unaffected(self):
        r, said = adm(full(), node=LINUX)
        self.assertFalse(any("host" in c["check"] for c in r["checks"]))


class Details(unittest.TestCase):
    def test_config_values_in_detail(self):
        r, _ = adm(full(), reserve_frac=0.3, disk_free_frac_min=0.2, disk_free_gb_min=50)
        d = {c["check"]: c["detail"] for c in r["checks"]}
        self.assertIn("(30% of node", d["memory"])
        self.assertIn("max(20%, 50G)", d["disk"])


class ProbeParse(unittest.TestCase):
    def test_bad_docker_line_linux(self):
        with FakeCmd(lambda argv, kw: R(0, "REACH\nDOCKER 29 notanint\n")):
            d = orch.probe(LINUX)
        self.assertFalse(d["docker"])
        self.assertIn("probe parse error", d["reason"])

    def test_bad_img_line_linux(self):
        with FakeCmd(lambda argv, kw: R(0, DOCKER_LINE + "IMG\n" + PROBE)):
            d = orch.probe(LINUX)
        self.assertFalse(d["docker"])
        self.assertIn("probe parse error", d["reason"])

    def test_no_docker_line_linux(self):
        with FakeCmd(lambda argv, kw: R(0, "REACH\n" + PROBE)):
            d = orch.probe(LINUX)
        self.assertEqual((d["docker"], d["reason"]), (False, "probe parse error: no DOCKER line"))
        self.assertFalse(orch.admit({}, LINUX, d, A=A, W=W, images={}, WD=WD)["ok"])

    def test_good_linux(self):
        with FakeCmd(lambda argv, kw: R(0, DOCKER_LINE + PROBE)) as fc:
            d = orch.probe(LINUX)
        self.assertTrue(d["docker"])
        script = fc.calls[0][1]["input"].decode()
        self.assertIn("mktemp /tmp/.agentt-dinfo.XXXXXX", script)
        self.assertIn("trap", script)

    def test_bad_docker_info_windows(self):
        def resp(argv, kw):
            return R(0, "8 x") if "info" in argv else R(0, "REACH")
        with FakeCmd(resp):
            d = orch.probe(WIN)
        self.assertFalse(d["docker"])
        self.assertIn("bad docker info line", d["reason"])


FMT_LINE = "FP {id}\t/{n}\t0\t{st}\t{{}}"


class Fingerprint(unittest.TestCase):
    def prot(self, nodes):
        return mock.patch.object(orch, "cfg_prot", return_value={"nodes": nodes})

    def test_linux_all_parsed_ok(self):
        out = "FPN 1\n" + FMT_LINE.format(id="a", n="db", st=json.dumps({"Status": "running"})) + "\nFPEND\nLAT 0 0.1 0.2 0.3\n"
        with self.prot({"n1": {"endpoints": [{"name": "web", "url": "http://x/"}]}}), FakeCmd(lambda a, k: R(0, out)):
            fp = orch.fingerprint(LINUX)
        self.assertTrue(fp["ok"], fp.get("error"))

    def test_linux_bad_json_not_ok(self):
        out = "FPN 1\nFP a\t/db\t0\t{bad\t{}\nFPEND\n"
        with self.prot({"n1": {}}), FakeCmd(lambda a, k: R(0, out)):
            fp = orch.fingerprint(LINUX)
        self.assertFalse(fp["ok"])
        self.assertIn("inspected 0 of 1", fp["error"])

    def test_linux_missing_latency_not_ok(self):
        out = "FPN 0\nFPEND\n"
        with self.prot({"n1": {"endpoints": [{"name": "web", "url": "http://x/"}]}}), FakeCmd(lambda a, k: R(0, out)):
            fp = orch.fingerprint(LINUX)
        self.assertFalse(fp["ok"])
        self.assertIn("latency not parsed for web", fp["error"])

    def test_windows_missing_service_not_ok(self):
        def resp(argv, kw):
            if "ps" in argv:
                return R(0, "")
            return R(0, "SVC com.docker.service Running\n")
        with self.prot({"n2": {"host_services": ["com.docker.service"]}}), FakeCmd(resp):
            fp = orch.fingerprint(WIN)
        self.assertFalse(fp["ok"])
        self.assertIn("DockerDesktop", fp["error"])

    def test_windows_ok(self):
        def resp(argv, kw):
            if "ps" in argv:
                return R(0, "")
            return R(0, "SVC com.docker.service Running\nPROC DockerDesktop 1\n")
        with self.prot({"n2": {"host_services": ["com.docker.service"]}}), FakeCmd(resp):
            fp = orch.fingerprint(WIN)
        self.assertTrue(fp["ok"], fp.get("error"))


class Redact(unittest.TestCase):
    def test_argv(self):
        r = orch.redact(["docker", "run", "-e", "FOO=bar", "--env", "PLAIN", "--build-arg=NPM_TOKEN=abc",
                         "--label", "io.orch.job=x", "DB_PASSWORD=hunter2", "apikey=1", "image"])
        self.assertEqual(r, ["docker", "run", "-e", "FOO=***", "--env", "***", "--build-arg=NPM_TOKEN=***",
                             "--label", "io.orch.job=x", "DB_PASSWORD=***", "apikey=***", "image"])

    def test_ssh_remote_string(self):
        r = orch.redact(["ssh", "n3", "docker -H unix:///x run -e 'SECRET=s1' --env A=b img"])
        self.assertNotIn("s1", r[2])
        self.assertNotIn("A=b", r[2])
        self.assertIn("unix:///x", r[2])

    def test_audit_writes_redacted(self):
        orch.audit("n1", "j", ["docker", "run", "-e", "TOKEN=topsecret", "x"])
        last = (orch.HOME / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        self.assertNotIn("topsecret", last)
        self.assertIn("TOKEN=***", last)


class ProtWarn(unittest.TestCase):
    def test_windows_endpoints_warn(self):
        w = orch.prot_warnings({"nodes": {"n2": {"endpoints": [{"name": "x"}]}, "n1": {"endpoints": [{"name": "y"}]}}},
                               {"n2": {"os": "windows"}, "n1": {"os": "linux"}})
        self.assertEqual(w, ["protected.json node n2: latency probes not supported on windows nodes (watchdog has no network)"])
        self.assertEqual(orch.prot_warnings({"nodes": {"n2": {"endpoints": []}}}, {"n2": {"os": "windows"}}), [])

    def test_cfg_prot_does_not_raise(self):
        orch._validated.pop("protected.json", None)
        with mock.patch.object(orch, "prot_warnings", return_value=["w1"]), mock.patch.object(orch, "say") as s:
            self.assertIsInstance(orch.cfg_prot(), dict)
        s.assert_called_once_with("warn: w1")


if __name__ == "__main__":
    unittest.main()
