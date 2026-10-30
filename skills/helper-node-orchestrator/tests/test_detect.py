"""Unit tests for scripts/orch_detect.py (design section 6). Temp repos only: git is faked, absent or (one test) a local
`git init`; never the network, ssh or docker. Run from the skill root:
python -B -m unittest discover -s tests -p "test_detect.py"
"""
import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("orch_detect", ROOT / "scripts" / "orch_detect.py")
od = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("orch_detect", od)
_spec.loader.exec_module(od)

GIT_LS = ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]


def nogit(argv, **kw):
    raise FileNotFoundError("git")


def fakegit(listing):
    """run= answering `git -C <dir> ls-files ...` from {dir: [paths]}; any other dir is 'not a git repository'."""
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        here = Path(argv[2]).resolve()
        for k, v in listing.items():
            if Path(k).resolve() == here:
                return SimpleNamespace(returncode=0, stdout="".join(p + "\0" for p in v), stderr="")
        return SimpleNamespace(returncode=128, stdout="", stderr="fatal: not a git repository")
    run.calls = calls
    return run


def pj(**kw):
    return json.dumps(kw)


def csproj(tfm="net8.0", test=False, tfms=None):
    tf = f"<TargetFrameworks>{tfms}</TargetFrameworks>" if tfms else (
        f"<TargetFramework>{tfm}</TargetFramework>" if tfm else "")
    ref = ('<ItemGroup><PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.11.0" />'
           '<PackageReference Include="xunit" Version="2.9.0" /></ItemGroup>') if test else ""
    return f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>{tf}</PropertyGroup>{ref}</Project>\n'


def sln(*projs):
    lines = ["\ufeffMicrosoft Visual Studio Solution File, Format Version 12.00"]
    for i, p in enumerate(projs):
        name = p.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        lines += [f'Project("{{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}}") = "{name}", "{p}", '
                  f'"{{00000000-0000-0000-0000-00000000000{i}}}"', "EndProject"]
    lines += ['Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = "tests", "tests", '
              '"{99999999-9999-9999-9999-999999999999}"', "EndProject", "Global", "EndGlobal"]
    return "\r\n".join(lines) + "\r\n"


class Base(unittest.TestCase):
    def mk(self, files, name="shop"):
        tmp = Path(tempfile.mkdtemp(prefix="orchdet-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / name
        root.mkdir()
        for rel, text in files.items():
            p = root / rel
            if rel.endswith("/"):   # a bare directory, e.g. a nested repository's .git/
                p.mkdir(parents=True, exist_ok=True)
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return root

    def det(self, files, name="shop", **kw):
        kw.setdefault("run", nogit)
        return od.detect(self.mk(files, name), **kw)


class Dotnet(Base):
    def test_solution_claims_members_and_global_json_wins(self):
        r = self.det({"Shop.sln": sln("src\\Api\\Api.csproj", "tests\\Api.Tests\\Api.Tests.csproj"),
                      "src/Api/Api.csproj": csproj("net8.0"),
                      "tests/Api.Tests/Api.Tests.csproj": csproj("net8.0", test=True),
                      "global.json": '{ "sdk": { "version": "9.0.100", "rollForward": "latestFeature" } }'})
        self.assertEqual(list(r["jobs"]), ["shop-test", "shop-build"])   # members are not their own job sets
        files = ["Shop.sln", "global.json", "src/Api/Api.csproj", "tests/Api.Tests/Api.Tests.csproj"]
        test = r["jobs"]["shop-test"]
        self.assertEqual(test, {
            "stage": "unit", "kind": "container", "image": "mcr.microsoft.com/dotnet/sdk:9.0", "dir": "",
            "repo": ".", "prep": "dotnet restore Shop.sln",
            "cmd": "dotnet test Shop.sln --no-restore --nologo --logger trx --results-directory /out",
            "cache": {"name": "nuget", "path": "/root/.nuget/packages"},
            "env": {"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"}, "memory": "3g", "timeout": 1200,
            "fail_patterns": ["error CS[0-9]+", "Failed!", "Failed: +[1-9]"], "result_paths": ["/out"],
            "detected": "dotnet 9 (Shop.sln)", "files": files, "manifest": test["manifest"]})
        self.assertRegex(test["manifest"], r"^sha256:[0-9a-f]{64}$")
        build = r["jobs"]["shop-build"]
        self.assertEqual((build["stage"], build["memory"], build["timeout"], build["cmd"]),
                         ("build", "4g", 1800, "dotnet build Shop.sln --no-restore -c Release --nologo"))
        self.assertEqual(build["manifest"], test["manifest"])
        self.assertEqual((r["notes"], r["unknown"]), ([], []))

    def test_highest_target_framework_incl_directory_build_props(self):
        r = self.det({"api/Api.csproj": csproj("net8.0"),
                      "Directory.Build.props": "<Project><PropertyGroup><TargetFrameworks>net8.0;net9.0"
                                               "</TargetFrameworks></PropertyGroup></Project>"})
        self.assertEqual(list(r["jobs"]), ["api-build"])   # no test SDK reference -> no test job
        j = r["jobs"]["api-build"]
        self.assertEqual((j["image"], j["dir"], j["prep"], j["detected"]),
                         ("mcr.microsoft.com/dotnet/sdk:9.0", "api", "dotnet restore Api.csproj",
                          "dotnet 9 (api/Api.csproj)"))
        self.assertEqual(j["files"], ["Directory.Build.props", "api/Api.csproj"])

    def test_netcoreapp_label_and_platform_targets_ignored(self):
        r = self.det({"Tool.csproj": csproj(tfms="netcoreapp3.1;net5.0-windows;netstandard2.0")})
        j = r["jobs"]["shop-build"]
        self.assertEqual((j["image"], j["detected"]), ("mcr.microsoft.com/dotnet/sdk:3.1", "dotnet 3.1 (Tool.csproj)"))

    def test_windows_only_and_versionless_projects_are_unknown(self):
        r = self.det({"legacy/Legacy.csproj": csproj("net48"), "lib/Lib.csproj": csproj("netstandard2.0"),
                      "gui/Gui.csproj": csproj("net8.0-windows")})
        self.assertEqual(r["jobs"], {})
        self.assertEqual(r["unknown"], ["gui/Gui.csproj", "legacy/Legacy.csproj", "lib/Lib.csproj"])
        self.assertIn("legacy/Legacy.csproj: target frameworks net48 need Windows or extra workloads; tell orch the "
                      "image and the commands to use, or leave it out.", r["notes"])
        self.assertIn("lib/Lib.csproj: no .NET SDK version found (target frameworks: netstandard2.0); add a "
                      "global.json with sdk.version, or tell orch which SDK image to use.", r["notes"])

    def test_projects_without_solution_each_get_a_job_set(self):
        r = self.det({"src/Api/Api.csproj": csproj(), "tests/Api.Tests/Api.Tests.csproj": csproj(test=True)})
        self.assertEqual(list(r["jobs"]), ["api-build", "api-tests-test", "api-tests-build"])
        self.assertEqual(r["jobs"]["api-tests-test"]["dir"], "tests/Api.Tests")

    def test_slnx_and_several_solutions(self):
        r = self.det({"Shop.slnx": '<Solution>\n  <Project Path="src/Api/Api.csproj" />\n</Solution>\n',
                      "Other.sln": sln("x\\X.csproj"), "src/Api/Api.csproj": csproj("net10.0", test=True),
                      "x/X.csproj": csproj()})
        self.assertEqual(r["jobs"]["shop-test"]["detected"], "dotnet 10 (Shop.slnx)")   # named like the project
        self.assertIn("./: several solutions (Other.sln, Shop.slnx); orch proposes Shop.slnx, say which one to use "
                      "if another is right.", r["notes"])
        self.assertIn("x-build", r["jobs"])   # not a member of the chosen solution -> its own job set

    def test_conditional_target_framework_tags(self):
        r = self.det({"Api.csproj": '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                                    "<TargetFramework Condition=\"'$(Ci)' == ''\">net9.0</TargetFramework>"
                                    "</PropertyGroup></Project>"})
        self.assertEqual(r["jobs"]["shop-build"]["image"], "mcr.microsoft.com/dotnet/sdk:9.0")

    def test_target_with_space_is_quoted(self):
        r = self.det({"My App.csproj": csproj()})
        self.assertEqual(r["jobs"]["shop-build"]["prep"], "dotnet restore 'My App.csproj'")


class Node(Base):
    def test_npm_scripts_typecheck_and_stub_skip(self):
        r = self.det({"web/package.json": pj(scripts={"lint": "eslint .", "build": "next build",
                                                      "test": 'echo "Error: no test specified" && exit 1'},
                                             devDependencies={"typescript": "5.6.3"}),
                      "web/package-lock.json": "{}", "web/tsconfig.json": "{}"})
        self.assertEqual(list(r["jobs"]), ["web-lint", "web-typecheck", "web-build"])   # never the npm stub
        lint, tc, build = (r["jobs"][n] for n in ("web-lint", "web-typecheck", "web-build"))
        self.assertEqual(lint, {
            "stage": "lint", "kind": "container", "image": "node:22-bookworm-slim", "dir": "web", "repo": ".",
            "prep": "npm ci --no-audit --no-fund", "cmd": "npm run lint", "cache": {"name": "npm", "path": "/root/.npm"},
            "env": {"CI": "true"}, "memory": "2g", "timeout": 1200,
            "fail_patterns": ["FAIL ", "\u2715", "[0-9]+ failing", "error TS[0-9]+"], "result_paths": ["/out"],
            "detected": "node 22 (web/package.json)", "files": ["web/package.json"], "manifest": lint["manifest"]})
        self.assertEqual((tc["stage"], tc["cmd"]), ("lint", "npx tsc --noEmit"))
        self.assertEqual((build["stage"], build["memory"], build["timeout"], build["env"]),
                         ("build", "4g", 1800, {"CI": "true", "NODE_OPTIONS": "--max-old-space-size=3072"}))
        self.assertIn("web/package.json: the test script is npm's placeholder, so orch proposes no test job for it.",
                      r["notes"])

    def test_typecheck_script_wins_and_tsconfig_needs_typescript(self):
        r = self.det({"a/package.json": pj(scripts={"typecheck": "tsc -p .", "test": "vitest run"}),
                      "a/package-lock.json": "{}", "a/tsconfig.json": "{}",
                      "b/package.json": pj(scripts={"test": "jest"}), "b/package-lock.json": "{}",
                      "b/tsconfig.json": "{}"})
        self.assertEqual(r["jobs"]["a-typecheck"]["cmd"], "npm run typecheck")
        self.assertEqual(r["jobs"]["a-test"]["cmd"], "npm run test")
        self.assertNotIn("b-typecheck", r["jobs"])

    def test_no_lockfile_uses_npm_install_with_note(self):
        r = self.det({"package.json": pj(scripts={"test": "node --test"})})
        self.assertEqual(r["jobs"]["shop-test"]["prep"], "npm install --no-audit --no-fund")
        self.assertIn("package.json: no lockfile, so prep runs `npm install --no-audit --no-fund` and resolves fresh "
                      "versions on every run; commit the lockfile for repeatable runs.", r["notes"])

    def test_no_lockfile_note_only_when_jobs_are_proposed(self):
        r = self.det({"bridge/package.json": pj(dependencies={"ws": "8"})})
        self.assertEqual(r["notes"][0], "bridge/package.json: no lint, typecheck, test or build script, so orch "
                                        "proposes no job for it; add a script or tell orch the command.")
        self.assertFalse(any("lockfile" in n for n in r["notes"]), r["notes"])

    def test_pnpm_via_corepack(self):
        j = self.det({"package.json": pj(scripts={"test": "vitest run"}), "pnpm-lock.yaml": ""})["jobs"]["shop-test"]
        self.assertEqual((j["prep"], j["cmd"], j["cache"], j["env"]),
                         ("corepack enable && pnpm install --frozen-lockfile", "pnpm run test",
                          {"name": "pnpm", "path": "/root/.local/share/pnpm/store"},
                          {"CI": "true", "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}))
        j = self.det({"package.json": pj(scripts={"test": "vitest run"}, engines={"node": ">=25"}),
                      "pnpm-lock.yaml": ""})["jobs"]["shop-test"]
        self.assertEqual((j["image"], j["prep"]), ("node:25-bookworm-slim", "npm install -g corepack --no-audit "
                                                   "--no-fund && corepack enable && pnpm install --frozen-lockfile"))

    def test_yarn_classic_and_berry(self):
        j = self.det({"package.json": pj(scripts={"test": "jest"}), "yarn.lock": ""})["jobs"]["shop-test"]
        self.assertEqual((j["prep"], j["cmd"], j["cache"]["path"]),
                         ("yarn install --frozen-lockfile --non-interactive", "yarn run test",
                          "/usr/local/share/.cache/yarn"))
        j = self.det({"package.json": pj(scripts={"test": "jest"}), "yarn.lock": "", ".yarnrc.yml": ""})["jobs"]
        self.assertEqual((j["shop-test"]["prep"], j["shop-test"]["cache"]["path"]),
                         ("corepack enable && yarn install --immutable", "/root/.yarn/berry/cache"))

    def test_package_manager_field_beats_lockfiles(self):
        j = self.det({"package.json": pj(packageManager="yarn@4.5.0", scripts={"test": "jest"}),
                      "package-lock.json": "{}", "yarn.lock": ""})["jobs"]["shop-test"]
        self.assertEqual(j["prep"], "corepack enable && yarn install --immutable")
        j = self.det({"package.json": pj(packageManager="pnpm@9.12.0+sha512.abc", scripts={"test": "jest"}),
                      "package-lock.json": "{}"})
        self.assertEqual(j["jobs"]["shop-test"]["prep"], "corepack enable && pnpm install")   # no pnpm lockfile
        self.assertTrue(any("no lockfile" in n for n in j["notes"]))

    def test_bun_is_unknown(self):
        r = self.det({"package.json": pj(scripts={"test": "bun test"}), "bun.lockb": b"\0"})
        self.assertEqual((r["jobs"], r["unknown"]), ({}, ["package.json"]))
        self.assertEqual(r["notes"], ["package.json: the bun package manager is not supported by orch; tell orch the "
                                      "image and the test command."])

    def test_version_resolution_order(self):
        cases = [({"engines": {"node": "^20.11.0"}}, {".nvmrc": "24\n"}, 20),
                 ({"engines": {"node": ">=18"}}, {".nvmrc": "24\n"}, 22),     # engines first; 22 when allowed
                 ({"engines": {"node": "not a range"}}, {".nvmrc": "v18.19.0\n", ".node-version": "20"}, 18),
                 ({}, {".nvmrc": "lts/*\n", ".node-version": "20.1.0"}, 20),  # unreadable .nvmrc -> next source
                 ({}, {".nvmrc": "lts/iron"}, 20),
                 ({}, {}, 22)]
        for pkg, extra, want in cases:
            with self.subTest(pkg=pkg, extra=extra):
                files = {"web/package.json": pj(scripts={"test": "jest"}, **pkg), "web/package-lock.json": "{}"}
                files.update({"web/" + k: v for k, v in extra.items()})
                j = self.det(files)["jobs"]["web-test"]
                self.assertEqual(j["image"], f"node:{want}-bookworm-slim")
                self.assertEqual(j["detected"], f"node {want} (web/package.json)")

    def test_version_file_found_up_to_the_repo_root(self):
        r = self.det({".nvmrc": "20", "apps/web/package.json": pj(scripts={"test": "jest"}),
                      "apps/web/package-lock.json": "{}"})
        j = r["jobs"]["web-test"]
        self.assertEqual((j["image"], j["files"]), ("node:20-bookworm-slim", [".nvmrc", "apps/web/package.json"]))

    def test_node_major_ranges(self):
        for spec, want in [("*", 22), (">=18", 22), (">= 18.0.0", 22), (">=24", 24), ("^20.11.0", 20),
                           ("20.x", 20), ("~18.19", 18), ("18 || 20", 20), (">=14 <17", 16), ("16 - 20", 20),
                           ("v18.17.0", 18), (">20", 22), (">22", 23), (">20.1.0 <21", 20), ("<=16", 16), ("<20.1", 20),
                           ("<20", 19), ("garbage", None), ("", None)]:
            with self.subTest(spec=spec):
                self.assertEqual(od._node_major(spec), want)

    def test_node_version_files(self):
        for text, want in [("v20.11.0\n", 20), ("22", 22), ("lts/jod", 22), ("lts/*", None), ("node", None),
                           ("", None), ("0.10", None)]:
            with self.subTest(text=text):
                self.assertEqual(od._node_file_major(text), want)


class Workspaces(Base):
    def test_npm_workspaces_claim_members_only(self):
        r = self.det({"package.json": pj(workspaces=["packages/*"], scripts={"test": "npm test --workspaces"}),
                      "package-lock.json": "{}",
                      "packages/a/package.json": pj(scripts={"test": "jest"}),
                      "packages/b/package.json": pj(scripts={"build": "tsc"}),
                      "tools/gen/package.json": pj(scripts={"test": "node --test"})})
        self.assertEqual(list(r["jobs"]), ["shop-test", "gen-test"])   # one set for the workspace + the outsider

    def test_yarn_object_form_and_pnpm_negation(self):
        r = self.det({"package.json": pj(workspaces={"packages": ["apps/*"]}, scripts={"lint": "eslint ."}),
                      "yarn.lock": "", "apps/web/package.json": pj(scripts={"test": "jest"})})
        self.assertEqual(list(r["jobs"]), ["shop-lint"])
        r = self.det({"package.json": pj(scripts={"lint": "eslint ."}), "pnpm-lock.yaml": "",
                      "pnpm-workspace.yaml": "packages:\n  - 'apps/*'   # apps\n  - \"!apps/legacy\"\ncatalog:\n"
                                             "  react: ^18\n",
                      "apps/web/package.json": pj(scripts={"test": "jest"}),
                      "apps/legacy/package.json": pj(scripts={"test": "mocha"}),
                      "apps/legacy/package-lock.json": "{}"})
        self.assertEqual(list(r["jobs"]), ["shop-lint", "legacy-test"])
        self.assertEqual(r["jobs"]["shop-lint"]["files"], ["package.json", "pnpm-workspace.yaml"])
        self.assertEqual(r["jobs"]["legacy-test"]["prep"], "npm ci --no-audit --no-fund")

    def test_pnpm_globs_parsing(self):
        self.assertEqual(od._pnpm_globs("packages: ['a/*', \"b\"]\n"), ["a/*", "b"])
        self.assertEqual(od._pnpm_globs("packages:\n- apps/*\n- libs/**  # all\nonlyBuilt: []\n- x\n"),
                         ["apps/*", "libs/**"])
        self.assertEqual(od._pnpm_globs("catalog:\n  a: 1\n"), [])

    def test_workspace_root_without_scripts_notes_the_fix(self):
        r = self.det({"package.json": pj(workspaces=["packages/*"]), "package-lock.json": "{}",
                      "packages/a/package.json": pj(scripts={"test": "jest"})})
        self.assertEqual(r["jobs"], {})
        self.assertIn('package.json: no lint, typecheck, test or build script, so orch proposes no job for it; add a '
                      'script or tell orch the command. A workspace root runs its members through its own scripts, '
                      'for example "test": "npm run test --workspaces --if-present".', r["notes"])
        r = self.det({"package.json": pj(), "pnpm-lock.yaml": "", "pnpm-workspace.yaml": "packages: ['p/*']\n"})
        self.assertTrue(r["notes"][0].endswith('for example "test": "pnpm -r run test".'), r["notes"])
        self.assertEqual(r["unknown"], ["."])

    def test_uv_workspace_claims_members(self):
        r = self.det({"pyproject.toml": '[tool.uv.workspace]\nmembers = ["packages/*"]\n\n[dependency-groups]\n'
                                        'dev = ["pytest>=8"]\n', "uv.lock": "",
                      "packages/core/pyproject.toml": '[project]\nname = "core"\nrequires-python = ">=3.12"\n',
                      "packages/core/tests/test_core.py": "def test_x():\n    pass\n"})
        self.assertEqual(list(r["jobs"]), ["shop-test"])
        self.assertEqual(r["jobs"]["shop-test"]["prep"], "uv sync --frozen --all-extras --dev --all-packages")


class Python(Base):
    UV = ('[project]\nname = "shop"\nrequires-python = ">=3.11"\ndependencies = ["httpx"]\n\n'
          '[dependency-groups]\ndev = ["pytest>=8", "ruff>=0.6"]\n\n[tool.ruff]\nline-length = 110\n')

    def test_uv_project_lint_and_test(self):
        r = self.det({"pyproject.toml": self.UV, "uv.lock": "version = 1\n", "tests/test_api.py": "",
                      "src/shop/__init__.py": ""})
        self.assertEqual(list(r["jobs"]), ["shop-lint", "shop-test"])
        t = r["jobs"]["shop-test"]
        self.assertEqual(t, {
            "stage": "unit", "kind": "container", "image": "ghcr.io/astral-sh/uv:python3.11-bookworm-slim", "dir": "",
            "repo": ".", "prep": "uv sync --frozen --all-extras --dev",
            "cmd": "uv run --frozen --offline pytest -q -p no:cacheprovider",
            "cache": {"name": "uv", "path": "/root/.cache/uv"}, "env": {"UV_LINK_MODE": "copy"}, "memory": "2g",
            "timeout": 1200, "fail_patterns": ["FAILED", "ERROR "], "result_paths": ["/out"],
            "detected": "python 3.11 (pyproject.toml)", "files": ["pyproject.toml"], "manifest": t["manifest"]})
        lint = r["jobs"]["shop-lint"]
        self.assertEqual((lint["stage"], lint["cmd"], lint["prep"]),
                         ("lint", "uv run --frozen --offline ruff check .", "uv sync --frozen --all-extras --dev"))
        self.assertEqual(r["notes"], [])

    def test_uv_without_lock_falls_back_to_pip(self):
        j = self.det({"pyproject.toml": self.UV, "tests/test_api.py": ""})["jobs"]["shop-test"]
        self.assertEqual((j["image"], j["prep"]), ("python:3.11-slim", "python -m venv /src/.venv && "
                                                   "/src/.venv/bin/pip install -e . pytest"))

    def test_poetry_project(self):
        r = self.det({"pyproject.toml": '[tool.poetry]\nname = "shop"\n\n[tool.poetry.dependencies]\n'
                                        'python = "^3.10"\n\n[tool.poetry.group.dev.dependencies]\npytest = "^8.0"\n',
                      "poetry.lock": "", "tests/test_a.py": ""})
        j = r["jobs"]["shop-test"]
        self.assertEqual((j["image"], j["prep"], j["cmd"], j["cache"], j["env"]),
                         ("python:3.10-slim", "pip install poetry && poetry install --no-interaction",
                          "poetry run pytest -q -p no:cacheprovider",
                          {"name": "poetry", "path": "/root/.cache/pypoetry"}, {"POETRY_VIRTUALENVS_IN_PROJECT": "true"}))

    def test_pip_requirements_extras_and_python_version_file(self):
        r = self.det({"requirements.txt": "flask==3.0.3\n", "requirements-dev.txt": "pytest==8.3.3\n",
                      "pyproject.toml": '[project]\nname = "shop"\nrequires-python = ">=3.9"\n\n'
                                        '[project.optional-dependencies]\ntest = ["coverage"]\ndocs = ["mkdocs"]\n',
                      ".python-version": "3.11.9\n", "conftest.py": ""})
        j = r["jobs"]["shop-test"]
        self.assertEqual((j["image"], j["prep"], j["cmd"], j["cache"], j["env"]),
                         ("python:3.11-slim", "python -m venv /src/.venv && /src/.venv/bin/pip install -r "
                          "requirements.txt -r requirements-dev.txt -e '.[test]'",
                          "/src/.venv/bin/python -m pytest -q -p no:cacheprovider",
                          {"name": "pip", "path": "/root/.cache/pip"}, {}))
        self.assertEqual(j["files"], [".python-version", "pyproject.toml", "requirements-dev.txt", "requirements.txt"])

    def test_unittest_fallback_starts_inside_a_plain_tests_dir(self):
        r = self.det({"requirements.txt": "requests\n", "test/test_x.py": "import unittest\n"})
        j = r["jobs"]["shop-test"]
        self.assertEqual((j["prep"], j["cmd"]), ("python -m venv /src/.venv && /src/.venv/bin/pip install -r "
                                                 "requirements.txt", "/src/.venv/bin/python -m unittest discover -s test"))
        j = self.det({"requirements.txt": "", "test/__init__.py": "", "test/test_x.py": ""})["jobs"]["shop-test"]
        self.assertEqual(j["cmd"], "/src/.venv/bin/python -m unittest discover")

    def test_pytest_and_ruff_installed_when_not_declared(self):
        r = self.det({"requirements.txt": "requests\n", "tests/test_api.py": "", "ruff.toml": ""})
        self.assertEqual(r["jobs"]["shop-test"]["prep"],
                         "python -m venv /src/.venv && /src/.venv/bin/pip install -r requirements.txt pytest")
        lint = r["jobs"]["shop-lint"]
        self.assertEqual((lint["prep"], lint["cmd"], lint["fail_patterns"][-1]),
                         ("python -m venv /src/.venv && /src/.venv/bin/pip install -r requirements.txt ruff",
                          "/src/.venv/bin/ruff check .", "Found [0-9]+ error"))
        self.assertIn("requirements.txt: prep installs the latest ruff and pytest because the project does not declare "
                      "them; add them to the dev dependencies to pin the version.", r["notes"])
        r = self.det({"pyproject.toml": '[project]\nname = "x"\n', "uv.lock": "", "pkg/thing_test.py": ""})
        self.assertEqual(r["jobs"]["shop-test"]["prep"], "uv sync --frozen --all-extras --dev && uv pip install pytest")

    def test_no_tests_means_no_job(self):
        r = self.det({"docs/requirements.txt": "mkdocs\n", "docs/conf.py": ""})
        self.assertEqual(r["jobs"], {})
        self.assertIn("docs/requirements.txt: no tests found (test_*.py files, pytest.ini, conftest.py or [tool.pytest] "
                      "settings), so orch proposes no test job for it; add tests or tell orch the command.", r["notes"])

    def test_tool_settings_only_and_requirements_only_subdirs(self):
        r = self.det({"pyproject.toml": "[tool.ruff]\nline-length = 100\n", "web/package.json": pj(
            scripts={"test": "jest"}), "web/package-lock.json": "{}"})
        self.assertEqual(list(r["jobs"]), ["web-test"])   # a settings-only pyproject is not a project
        r = self.det({"pyproject.toml": '[project]\nname = "shop"\n', "tests/test_a.py": "",
                      "docs/requirements.txt": "mkdocs\n", "svc/pyproject.toml": '[project]\nname = "svc"\n',
                      "svc/tests/test_s.py": ""})
        self.assertEqual(list(r["jobs"]), ["shop-test", "svc-test"])   # docs/ claimed, svc/ is its own project
        self.assertEqual(r["notes"], ["pyproject.toml: prep installs the latest pytest because the project does not "
                                      "declare it; add it to the dev dependencies to pin the version.",
                                      "svc/pyproject.toml: prep installs the latest pytest because the project does "
                                      "not declare it; add it to the dev dependencies to pin the version."])

    def test_version_resolution_order(self):
        cases = [({".python-version": "pypy3.10\n"}, ">=3.12", None, "3.10"),
                 ({}, ">=3.9,<4", "^3.11", "3.9"),
                 ({}, None, "^3.11", "3.11"),
                 ({".python-version": "system\n"}, None, None, "3.12")]
        for extra, req, poetry_py, want in cases:
            with self.subTest(want=want, req=req):
                text = '[project]\nname = "a"\n' + (f'requires-python = "{req}"\n' if req else "")
                if poetry_py:
                    text += f'\n[tool.poetry.dependencies]\npython = "{poetry_py}"\n'
                files = {"pyproject.toml": text, "requirements.txt": "pytest\n", "tests/test_a.py": ""}
                files.update(extra)
                self.assertEqual(self.det(files)["jobs"]["shop-test"]["detected"], f"python {want} (pyproject.toml)")

    def test_py_lowest(self):
        for spec, want in [(">=3.10,<4", "3.10"), ("<4.0,>=3.9", "3.9"), ("^3.11", "3.11"), ("==3.12.*", "3.12"),
                           ("~=3.8.1", "3.8"), (">=3.8, !=3.9.*", "3.8"), ("<3.13", None), (None, None), ("*", None)]:
            with self.subTest(spec=spec):
                self.assertEqual(od._py_lowest(spec), want)

    # the three spellings of [project] optional-dependencies (inline table, dotted keys, multi-line section arrays)
    INLINE = ('[project]\nname = "x"\ndependencies = ["httpx"]\n'
              'optional-dependencies = { test = ["pytest>=8"], docs = [\n  "mkdocs",  # site "theme"\n] }\n')
    DOTTED = ('[project]\nname = "x"\ndependencies = [\n  "httpx>=0.27",  # client\n]\n'
              'readme = { text = """\ndependencies = ["fake"]\n""", content-type = "text/plain" }\n'
              'optional-dependencies.testing = [\n  "pytest>=8.3.4",\n  "pytest-cov",\n]\n'
              '"optional-dependencies" . \'lint\' = [\'ruff\']\n')
    SECTION = ('[project]\nname = "x"\n\n[project.optional-dependencies]\ntest = [\n  "pytest==8.3.3",\n'
               "  'coverage[toml]',\n]\n")

    def test_toml_fallback_matches_tomllib(self):
        texts = {"uv": self.UV,
                 "poetry": '[tool.poetry]\nname = "x"\n[tool.poetry.dependencies]\npython = { version = "^3.10" }\n',
                 "extras": '[project]\nname = "x"\n[project.optional-dependencies]\ntest = ["a"]\n"dev" = ["b"]\n'
                           '[tool.pytest.ini_options]\naddopts = "-q"\n',
                 "workspace": '[tool.uv.workspace]\nmembers = [\n  "libs/*",\n  "apps/web",\n]\n[build-system]\n'
                              'requires = []\n',
                 "empty workspace": '[tool.uv.workspace]\n', "empty": "", "invalid": "not = [valid",
                 "inline": self.INLINE, "dotted": self.DOTTED, "section": self.SECTION}
        for name, text in texts.items():
            with self.subTest(text=name):
                fast = od._pyproject(text)
                with mock.patch.object(od, "_toml", None):
                    self.assertEqual(od._pyproject(text), fast)
        want = {"inline": (["httpx"], {"test": ["pytest>=8"], "docs": ["mkdocs"]}),
                "dotted": (["httpx>=0.27"], {"testing": ["pytest>=8.3.4", "pytest-cov"], "lint": ["ruff"]}),
                "section": ([], {"test": ["pytest==8.3.3", "coverage[toml]"]})}
        for name, deps_extras in want.items():
            for toml in (od._toml, None):
                with self.subTest(text=name, tomllib=toml is not None), mock.patch.object(od, "_toml", toml):
                    got = od._pyproject(texts[name])
                    self.assertEqual((got["dependencies"], got["extras"]), deps_extras)

    def test_toml_fallback_survives_broken_values(self):
        cases = [('dependencies = ["a",\n', [], {}), ("dependencies = " + "[" * 3000 + "\n", [], {}),
                 ('optional-dependencies = { test = ["a" }\ndependencies = ["b"]\n', ["b"], {}),
                 ('dependencies = "x\noptional-dependencies.test = [\'pytest\']\n', [], {"test": ["pytest"]})]
        with mock.patch.object(od, "_toml", None):
            for body, deps, extras in cases:   # an unreadable value is skipped up to the next line
                with self.subTest(body=body[:45]):
                    got = od._pyproject("[project]\n" + body)
                    self.assertEqual((got["project"], got["dependencies"], got["extras"]), (True, deps, extras))

    def test_detect_without_tomllib(self):
        files = {"pyproject.toml": self.UV, "uv.lock": "", "tests/test_api.py": ""}
        want = self.det(files)
        with mock.patch.object(od, "_toml", None):
            got = self.det(files)
        self.assertEqual([(n, j["cmd"], j["image"]) for n, j in got["jobs"].items()],
                         [(n, j["cmd"], j["image"]) for n, j in want["jobs"].items()])

    def test_inline_and_dotted_extras_without_tomllib(self):
        pip = "python -m venv /src/.venv && /src/.venv/bin/pip install "
        for spelling, extra in [('optional-dependencies = { test = ["pytest"] }\n', "test"),
                                ('optional-dependencies.testing = [\n  "ruff",\n  "pytest>=8.3.4",\n]\n', "testing")]:
            files = {"pyproject.toml": '[project]\nname = "shop"\n' + spelling, "tests/test_a.py": ""}
            for toml in (od._toml, None):
                with self.subTest(extra=extra, tomllib=toml is not None), mock.patch.object(od, "_toml", toml):
                    r = self.det(files)
                    self.assertEqual({j["prep"] for j in r["jobs"].values()}, {pip + f"-e '.[{extra}]'"})
                    self.assertEqual(r["notes"], [])

    def test_prep_adds_a_tool_declared_only_where_prep_does_not_install(self):
        pip = "python -m venv /src/.venv && /src/.venv/bin/pip install "
        cases = [  # [project] table -> prep of each job: only requirement files, dependencies and chosen extras count
            ('name = "shop"\ndependencies = ["httpx"]\n\n[project.optional-dependencies]\nci = ["pytest>=8", "ruff"]\n',
             {"shop-lint": "-e . ruff", "shop-test": "-e . pytest"}),
            ('name = "shop"\noptional-dependencies = { qa = ["pytest"] }\n', {"shop-test": "-e . pytest"}),
            ('name = "shop"\n[project.optional-dependencies]\ntest = ["pytest"]\nlint = ["ruff"]\n',
             {"shop-lint": "-e '.[test]' ruff", "shop-test": "-e '.[test]'"}),
            ('name = "shop"\ndependencies = ["pytest>=8"]\n', {"shop-test": "-e ."}),
            ('name = "pytest-shop"\n', {"shop-test": "-e . pytest"})]
        for table, want in cases:
            files = {"pyproject.toml": "[project]\n" + table, "tests/test_a.py": ""}
            for toml in (od._toml, None):
                with self.subTest(table=table[:40], tomllib=toml is not None), mock.patch.object(od, "_toml", toml):
                    r = self.det(files)
                    self.assertEqual({n: j["prep"] for n, j in r["jobs"].items()},
                                     {n: pip + a for n, a in want.items()})


class Docker(Base):
    def test_every_dockerfile_is_an_image_job(self):
        r = self.det({"Dockerfile": "FROM node:22-bookworm-slim AS build\nRUN npm ci\n",
                      "api/Dockerfile": "FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:8.0 AS b\n",
                      "api/Dockerfile.dev": "FROM python:3.12-slim\n", "deploy/worker.Dockerfile": "FROM alpine:3.20\n",
                      "api/Dockerfile.dockerignore": "bin/\n", ".devcontainer/Dockerfile": "FROM ubuntu\n"})
        self.assertEqual(list(r["jobs"]), ["shop-image", "api-image", "api-dev-image", "deploy-worker-image"])
        self.assertEqual(r["jobs"]["api-image"], {
            "stage": "image", "kind": "build", "dir": "api", "repo": ".", "dockerfile": "api/Dockerfile",
            "context": "api", "memory": "4g", "timeout": 2400,
            "fail_patterns": ["did not complete successfully: exit code", "error CS[0-9]+", "Failed!",
                              "Failed: +[1-9]"], "result_paths": [], "detected": "docker (api/Dockerfile)",
            "files": ["api/Dockerfile"], "manifest": r["jobs"]["api-image"]["manifest"]})
        root = r["jobs"]["shop-image"]
        self.assertEqual((root["dockerfile"], root["context"], root["dir"]), ("Dockerfile", ".", ""))
        self.assertIn("Failed to compile", root["fail_patterns"])
        self.assertEqual(r["jobs"]["api-dev-image"]["fail_patterns"][1:], ["FAILED", "ERROR "])
        self.assertEqual(r["jobs"]["deploy-worker-image"]["fail_patterns"],
                         ["did not complete successfully: exit code"])
        self.assertEqual(r["jobs"]["deploy-worker-image"]["dockerfile"], "deploy/worker.Dockerfile")


    def test_context_is_the_repo_root_when_copy_sources_live_there(self):
        r = self.det({"deploy/web.Dockerfile": "FROM node:22 AS b\nCOPY --chown=node web/package.json ./\n"
                                               "COPY web/ ./\nCOPY --from=b /app/out /srv\n",
                      "deploy/jobs/Dockerfile": 'FROM alpine:3.20\nCOPY ["crontab", "/etc/crontabs/root"]\n',
                      "deploy/jobs/crontab": "* * * * * true\n", "web/src/index.ts": "",
                      "svc/Dockerfile": "FROM alpine:3.20\nCOPY . .\nADD https://example.com/x.tgz /x\n"
                                        "COPY ${APP}/ /app/\n", "svc/main.sh": ""})
        web, jobs, svc = (r["jobs"][n] for n in ("deploy-web-image", "jobs-image", "svc-image"))
        self.assertEqual((web["dockerfile"], web["context"], web["dir"]), ("deploy/web.Dockerfile", ".", ""))
        self.assertEqual((jobs["dockerfile"], jobs["context"], jobs["dir"]), ("deploy/jobs/Dockerfile", "deploy/jobs",
                                                                              "deploy/jobs"))
        self.assertEqual(svc["context"], "svc")   # nothing decides: the Dockerfile's own dir
        self.assertTrue(od.proposal_lines({"w": web})[0].endswith("docker build -f deploy/web.Dockerfile ."))


class Monorepo(Base):
    FILES = {"api/Api.sln": sln("src\\Api\\Api.csproj", "tests\\Api.Tests\\Api.Tests.csproj"),
             "api/src/Api/Api.csproj": csproj("net8.0"),
             "api/tests/Api.Tests/Api.Tests.csproj": csproj("net8.0", test=True),
             "api/Dockerfile": "FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build\n",
             "web/package.json": pj(scripts={"lint": "next lint", "test": "vitest run", "build": "next build"},
                                    devDependencies={"typescript": "5.6.3"}, engines={"node": ">=20"}),
             "web/package-lock.json": "{}", "web/tsconfig.json": "{}",
             "web/Dockerfile": "FROM node:22-bookworm-slim\n", "README.md": "# shop\n"}

    def test_api_dotnet_web_node_and_dockerfiles(self):
        r = self.det(self.FILES)
        self.assertEqual(list(r["jobs"]), ["api-test", "api-build", "api-image", "web-lint", "web-typecheck",
                                           "web-test", "web-build", "web-image"])
        self.assertEqual(r["jobs"]["api-test"]["detected"], "dotnet 8 (api/Api.sln)")
        self.assertEqual(r["jobs"]["api-test"]["dir"], "api")
        self.assertEqual((r["notes"], r["unknown"]), ([], []))
        lines = od.proposal_lines(r["jobs"])
        self.assertEqual(lines[0], "api-test       unit   dotnet 8  api/  3 GB  dotnet test Api.sln --no-restore "
                                   "--nologo --logger trx --...")
        self.assertEqual(lines[2], "api-image      image  docker    api/  4 GB  docker build -f api/Dockerfile api")
        self.assertEqual(lines[3], "web-lint       lint   node 22   web/  2 GB  npm run lint")

    def test_nested_repositories_become_the_job_repo(self):
        root = self.mk({**self.FILES, "api/.git/": "", "web/.git/": ""})
        listing = {str(root): ["README.md", "api/", "web/"],
                   str(root / "api"): [p[4:] for p in self.FILES if p.startswith("api/")],
                   str(root / "web"): [p[4:] for p in self.FILES if p.startswith("web/")]}
        run = fakegit(listing)
        r = od.detect(root, run=run)
        self.assertEqual([c[3:] for c in run.calls], [GIT_LS] * 3)
        test, image = r["jobs"]["api-test"], r["jobs"]["web-image"]
        self.assertEqual((test["repo"], test["dir"], test["prep"], test["detected"]),
                         ("api", "", "dotnet restore Api.sln", "dotnet 8 (api/Api.sln)"))
        self.assertEqual((image["repo"], image["dir"], image["dockerfile"], image["context"]),
                         ("web", "", "Dockerfile", "."))
        self.assertTrue(od.proposal_lines(r["jobs"])[0].startswith("api-test       unit   dotnet 8  api/  3 GB"))
        walked = od.detect(root, run=nogit)   # same answer from the walk fallback
        self.assertEqual(walked["jobs"], r["jobs"])


class NamesAndManifest(Base):
    def test_generic_dirs_are_named_with_their_parent(self):
        r = self.det({"_kernel/src/Kernel.csproj": csproj(), "_kernel/tests/Kernel.Tests.csproj": csproj(test=True),
                      "src/package.json": pj(scripts={"test": "jest"}), "src/package-lock.json": "{}"})
        self.assertEqual(list(r["jobs"]), ["kernel-src-build", "kernel-tests-test", "kernel-tests-build",
                                           "shop-src-test"])

    def test_unique_slugged_names(self):
        r = self.det({"a/Web/package.json": pj(scripts={"test": "jest"}), "b/web/package.json": pj(
            scripts={"test": "jest"}), "My_App.csproj": csproj()}, project_name="My Shop!")
        self.assertEqual(list(r["jobs"]), ["my-shop-build", "web-test", "web-test-2"])

    def test_manifest_follows_the_files_read(self):
        text = json.dumps({"scripts": {"test": "jest"}}, indent=2) + "\n"
        root = self.mk({"package.json": text, "package-lock.json": "{}"})
        m0 = od.detect(root, run=nogit)["jobs"]["shop-test"]["manifest"]
        (root / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
        self.assertEqual(od.detect(root, run=nogit)["jobs"]["shop-test"]["manifest"], m0)   # lockfile not read
        (root / "package.json").write_bytes(text.replace("\n", "\r\n").encode())   # a CRLF checkout: same
        self.assertEqual(od.detect(root, run=nogit)["jobs"]["shop-test"]["manifest"], m0)
        (root / "package.json").write_text(pj(scripts={"test": "jest --ci"}), encoding="utf-8")
        m1 = od.detect(root, run=nogit)["jobs"]["shop-test"]["manifest"]
        self.assertNotEqual(m1, m0)
        self.assertEqual(m1, od.manifest_hash(root, ["package.json"]))

    def test_manifest_hash_rules(self):
        root = self.mk({"a": "x\r\ny\n", "b": "x\ny\n", "empty": ""})
        self.assertEqual(od.manifest_hash(root, ["a", "b"]), od.manifest_hash(root, ["b", "a", "a"]))
        self.assertNotEqual(od.manifest_hash(root, ["a"]), od.manifest_hash(root, ["b"]))   # the path counts too
        self.assertEqual(od.manifest_hash(root, ["a"]), od.manifest_hash(self.mk({"a": "x\ny\n"}), ["a"]))
        self.assertNotEqual(od.manifest_hash(root, ["missing"]), od.manifest_hash(root, ["empty"]))
        self.assertNotEqual(od.manifest_hash(root, ["a"]), od.manifest_hash(self.mk({"a": "x\ny\nz\n"}), ["a"]))


class Listing(Base):
    def test_ignored_hidden_and_deep_dirs(self):
        test = pj(scripts={"test": "jest"})
        r = self.det({"node_modules/x/package.json": test, "examples/demo/package.json": test,
                      "tests/fixtures/p/package.json": test, ".devcontainer/Dockerfile": "FROM x\n",
                      "Build/package.json": test, "a/b/c/d/package.json": test, "a/b/c/package.json": test})
        self.assertEqual(list(r["jobs"]), ["c-test"])

    def test_git_list_is_used_and_honours_ignores(self):
        root = self.mk({"web/package.json": pj(scripts={"test": "jest"}), "junk/package.json": pj(
            scripts={"test": "jest"}), "web/package-lock.json": "{}"})
        run = fakegit({str(root): ["web/package.json", "web/package-lock.json"]})
        r = od.detect(root, run=run)
        self.assertEqual(list(r["jobs"]), ["web-test"])
        self.assertEqual(run.calls, [["git", "-C", str(root.resolve())] + GIT_LS])

    def test_git_failures_fall_back_to_walking(self):
        files = {"web/package.json": pj(scripts={"test": "jest"})}
        for exc in (subprocess.TimeoutExpired("git", 120), PermissionError("denied")):
            def run(argv, **kw):
                raise exc
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(list(self.det(files, run=run)["jobs"]), ["web-test"])
        self.assertEqual(list(self.det(files, run=fakegit({}))["jobs"]), ["web-test"])   # rc 128: not a repository

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_real_git_ls_files(self):
        root = self.mk({".gitignore": "ignored/\n", "ignored/package.json": pj(scripts={"test": "jest"}),
                        "svc/package.json": pj(scripts={"test": "jest"}), "svc/package-lock.json": "{}",
                        "Dockerfile": "FROM alpine:3.20\n"})
        env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(root.parent / "gitconfig")}
        for d in (root, root / "svc"):
            subprocess.run(["git", "init", "-q", str(d)], check=True, env={**os.environ, **env})
        r = od.detect(root)
        self.assertEqual(list(r["jobs"]), ["shop-image", "svc-test"])
        self.assertEqual((r["jobs"]["svc-test"]["repo"], r["jobs"]["svc-test"]["dir"]), ("svc", ""))


class Unknown(Base):
    def test_other_stacks_are_reported_once_per_workspace(self):
        r = self.det({"svc/go.mod": "module x\n", "rust/Cargo.toml": "[workspace]\n", "rust/crates/a/Cargo.toml": "",
                      "Pipfile": "", "web/package.json": pj(scripts={"test": "jest"}), "web/package-lock.json": "{}"})
        self.assertEqual(list(r["jobs"]), ["web-test"])
        self.assertEqual(r["unknown"], ["Pipfile", "rust/Cargo.toml", "svc/go.mod"])
        self.assertIn("svc/go.mod: orch does not detect go projects; tell orch the image and the test command.",
                      r["notes"])

    def test_mobile_platform_folders_of_an_app_are_not_reported(self):
        r = self.det({"app/pubspec.yaml": "name: app\n", "app/android/build.gradle.kts": "",
                      "app/android/app/build.gradle.kts": "", "svc/go.mod": "", "svc/Cargo.toml": ""})
        self.assertEqual(r["unknown"], ["app/pubspec.yaml", "svc/Cargo.toml", "svc/go.mod"])

    def test_pipfile_next_to_requirements_is_python(self):
        r = self.det({"Pipfile": "", "requirements.txt": "pytest\n", "tests/test_a.py": ""})
        self.assertEqual((list(r["jobs"]), r["unknown"]), (["shop-test"], []))

    def test_missing_root_is_a_plain_error(self):
        missing = Path(tempfile.gettempdir()) / "orchdet-does-not-exist" / "shop"
        with self.assertRaisesRegex(NotADirectoryError, "is not a directory; run orch init from inside the project"):
            od.detect(missing, run=nogit)

    def test_nothing_detected(self):
        r = self.det({"README.md": "# x\n", "scripts/run.sh": "echo\n"})
        self.assertEqual((r["jobs"], r["unknown"]), ({}, ["."]))
        self.assertEqual(r["notes"], ["orch found no job to propose within 3 levels of shop (it detects dotnet, node, "
                                      "python and Dockerfiles); tell orch the image and the test command."])

    def test_unreadable_package_json(self):
        r = self.det({"web/package.json": "{nope"})
        self.assertEqual(r["unknown"], ["web/package.json"])
        self.assertIn("web/package.json: not a readable JSON object; fix it, then run orch init --detect again.",
                      r["notes"])


class ProposalLines(Base):
    def test_alignment_truncation_and_fallbacks(self):
        jobs = {"a": {"stage": "unit", "detected": "python 3.12 (pyproject.toml)", "dir": "", "repo": ".",
                      "memory": "512m", "cmd": "x" * 61},
                "longer-name": {"stage": "image", "kind": "build", "detected": "docker (Dockerfile)", "repo": "svc",
                                "dir": "", "dockerfile": "Dockerfile", "context": ".", "memory": "4g"},
                "bare": {}}
        lines = od.proposal_lines(jobs)
        self.assertEqual(lines, ["a            unit   python 3.12  ./    512 MB  " + "x" * 57 + "...",
                                 "longer-name  image  docker       svc/  4 GB    docker build -f Dockerfile .",
                                 "bare         -      -            ./    -       -"])
        self.assertEqual(od.proposal_lines({}), [])


class Compat(unittest.TestCase):
    def test_parses_as_python_3_10(self):
        ast.parse((ROOT / "scripts" / "orch_detect.py").read_text(encoding="utf-8"), feature_version=(3, 10))


if __name__ == "__main__":
    unittest.main()
