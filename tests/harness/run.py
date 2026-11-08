"""VeroMesh regression test harness. Run it through scripts/test.sh.

Runs the suites registered in suites.py, tier by tier, and prints one summary.
Standard library only, Python 3.9+, so it runs with the python3 that ships
with macOS; the tests themselves run in the test-runner Docker image (default)
or in a local virtualenv (--local).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from suites import SUITES, TIER_HELP, TIERS, Suite  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / ".build"
RUNNER_IMAGE = "veromesh/test-runner"
TEST_NETWORK = "veromesh-test"
FUSEKI_CONTAINER = "veromesh-test-fuseki"
FUSEKI_PORT = int(os.environ.get("VEROMESH_TEST_FUSEKI_PORT", "3039"))
STACK_NETWORK = "datahub_network"
IGNORED_DIRS = {".build", ".git", "third_party", "node_modules", ".venv"}

TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


GREEN, RED, YELLOW, DIM, BOLD = "32", "31", "33", "2", "1"
STATUS_COLOR = {"PASS": GREEN, "FAIL": RED, "SKIP": YELLOW}


def say(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------- results


@dataclass
class Result:
    suite: Suite
    status: str = "SKIP"
    seconds: float = 0.0
    detail: str = ""
    log: Path | None = None
    failed_step: str | None = None


@dataclass
class Run:
    args: argparse.Namespace
    results_dir: Path
    mode: str  # docker | local
    docker: bool
    results: list[Result] = field(default_factory=list)
    fuseki_started: bool = False
    fuseki_error: str | None = None
    stack: dict | None = None
    stack_error: str | None = None
    runner_ready: bool = False
    venv: Path | None = None


# ---------------------------------------------------------------------------- helpers


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return sh(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def java_major() -> int:
    if not shutil.which("java"):
        return 0
    m = re.search(r'version "(\d+)', sh(["java", "-version"]).stderr)
    return int(m.group(1)) if m else 0


def http_ok(url: str, timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def fmt_seconds(s: float) -> str:
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


def pytest_counts(text: str) -> str:
    """Last pytest summary line in a log, e.g. '48 passed, 2 skipped'."""
    found = re.findall(
        r"^=*\s*((?:\d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|deselected)(?:, )?)+)", text, re.M
    )
    totals: dict[str, int] = {}
    for line in found:
        for n, word in re.findall(r"(\d+) (\w+)", line):
            totals[word] = totals.get(word, 0) + int(n)
    order = ["passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed", "deselected"]
    return ", ".join(f"{totals[w]} {w}" for w in order if w in totals)


# ---------------------------------------------------------------------------- registry


def check_registry() -> list[str]:
    problems = []
    names = [s.name for s in SUITES]
    for n in {n for n in names if names.count(n) > 1}:
        problems.append(f"suite name '{n}' is used more than once")
    for s in SUITES:
        if s.tier not in TIERS:
            problems.append(f"suite '{s.name}': unknown tier '{s.tier}'")
        if s.where not in ("runner", "host"):
            problems.append(f"suite '{s.name}': where must be 'runner' or 'host'")
        for c in s.covers:
            if not (ROOT / c).is_dir():
                problems.append(f"suite '{s.name}': covers '{c}', which does not exist")
    covered = {c for s in SUITES for c in s.covers}
    for d in sorted(ROOT.glob("*/tests")) + sorted(ROOT.glob("tests/*")):
        if (
            not d.is_dir()
            or d.parts[len(ROOT.parts)] in IGNORED_DIRS
            or d.name in ("harness", "runner", "__pycache__")
        ):
            continue
        if not any(p.name.startswith("test_") for p in d.rglob("*.py")):
            continue
        rel = d.relative_to(ROOT).as_posix()
        if rel not in covered:
            problems.append(
                f"'{rel}' has tests but no suite covers it: register it in tests/harness/suites.py"
            )
    return problems


def select(targets: list[str]) -> list[Suite]:
    if not targets:
        targets = ["quick"]
    chosen: list[Suite] = []
    unknown = []
    for t in targets:
        if t == "all":
            picked = list(SUITES)
        elif t in TIERS:
            # A tier includes the tiers before it; e2e stands alone (it needs the running stack).
            levels = ("e2e",) if t == "e2e" else tuple(x for x in TIERS[: TIERS.index(t) + 1] if x != "e2e")
            picked = [s for s in SUITES if s.tier in levels]
        else:
            picked = [s for s in SUITES if s.name == t] or [s for s in SUITES if s.component == t]
            if not picked:
                unknown.append(t)
        chosen += [s for s in picked if s not in chosen]
    if unknown:
        raise SystemExit(
            f"Unknown suite, component or tier: {', '.join(unknown)}. See: scripts/test.sh --list"
        )
    order = {name: i for i, name in enumerate(s.name for s in SUITES)}
    return sorted(chosen, key=lambda s: (TIERS.index(s.tier), order[s.name]))


def print_list() -> None:
    say(color("Tiers", BOLD))
    for t in (*TIERS, "all"):
        say(f"  {t:12} {TIER_HELP[t]}")
    say()
    say(color("Suites", BOLD))
    say(f"  {'name':14} {'tier':12} {'component':14} {'runs on':8} description")
    for s in SUITES:
        needs = f" [needs {', '.join(sorted(s.needs))}]" if s.needs else ""
        say(f"  {s.name:14} {s.tier:12} {s.component:14} {s.where:8} {s.description}{needs}")


# ---------------------------------------------------------------------------- environments


def runner_image_tag() -> str:
    h = hashlib.sha256()
    for p in (
        "tests/runner/Dockerfile",
        "tests/runner/requirements.py",
        "service/pyproject.toml",
        "connectors/pyproject.toml",
        "deploy/versions.env",
    ):
        h.update((ROOT / p).read_bytes())
    return f"{RUNNER_IMAGE}:{h.hexdigest()[:12]}"


def prepare_runner(run: Run) -> str | None:
    """Make the test-runner available. Returns an error message, or None."""
    if run.runner_ready:
        return None
    if run.mode == "docker":
        tag = runner_image_tag()
        if sh(["docker", "image", "inspect", tag]).returncode != 0:
            say(color(f">> Building the test runner image {tag} (only when dependencies change)", DIM))
            ctx = BUILD / "test-runner-context"
            shutil.rmtree(ctx, ignore_errors=True)
            for p in (
                "service/pyproject.toml",
                "connectors/pyproject.toml",
                "tests/runner/requirements.py",
                "deploy/versions.env",
            ):
                (ctx / p).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(ROOT / p, ctx / p)
            r = subprocess.run(
                ["docker", "build", "-t", tag, "-f", str(ROOT / "tests/runner/Dockerfile"), str(ctx)],
                text=True,
                capture_output=not run.args.verbose,
            )
            if r.returncode != 0:
                return (
                    "could not build the test runner image:\n" + ((r.stdout or "") + (r.stderr or ""))[-3000:]
                )
        run.runner_ready = True
        return None
    # --local: a virtualenv under .build/ with the same requirements
    venv = BUILD / "test-venv"
    py = find_python311()
    if py is None:
        return "--local needs Python 3.11 or newer (python3.12, python3.11 or python3 on PATH)"
    stamp = venv / ".requirements-hash"
    want = hashlib.sha256(
        b"".join(
            (ROOT / p).read_bytes()
            for p in ("tests/runner/requirements.py", "service/pyproject.toml", "connectors/pyproject.toml")
        )
    ).hexdigest()
    vpy = venv / "bin" / "python"
    if not vpy.exists() or not stamp.exists() or stamp.read_text() != want:
        say(
            color(
                f">> Preparing the local test environment in {venv.relative_to(ROOT)} (only when dependencies change)",
                DIM,
            )
        )
        if not vpy.exists():
            r = sh([py, "-m", "venv", str(venv)])
            if r.returncode != 0:
                return "could not create the virtualenv: " + r.stderr
        reqs = sh([str(vpy), str(ROOT / "tests/runner/requirements.py"), str(ROOT)])
        if reqs.returncode != 0:
            return reqs.stderr
        req_file = venv / "requirements.txt"
        req_file.write_text(reqs.stdout)
        r = subprocess.run(
            [str(vpy), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(req_file)],
            text=True,
            capture_output=not run.args.verbose,
        )
        if r.returncode != 0:
            return "pip install failed:\n" + ((r.stdout or "") + (r.stderr or ""))[-3000:]
        stamp.write_text(want)
    run.venv = venv
    run.runner_ready = True
    return None


def find_python311() -> str | None:
    for name in ("python3.13", "python3.12", "python3.11", "python3"):
        p = shutil.which(name)
        if not p:
            continue
        r = sh([p, "-c", "import sys; print(sys.version_info >= (3, 11))"])
        if r.stdout.strip() == "True":
            return p
    return None


# ---------------------------------------------------------------------------- resources


def start_fuseki(run: Run) -> str | None:
    if run.fuseki_started:
        return None
    if run.fuseki_error:
        return run.fuseki_error
    env = dict(
        line.split("=", 1)
        for line in (ROOT / "deploy/versions.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    tag = f"veromesh/fuseki:{env.get('JENA_VERSION', 'local')}"
    say(color(f">> Starting a throwaway Fuseki ({tag}) for integration tests", DIM))
    r = sh(
        [
            "docker",
            "build",
            "-q",
            "--build-arg",
            f"JENA_VERSION={env.get('JENA_VERSION', '')}",
            "-t",
            tag,
            str(ROOT / "deploy/fuseki"),
        ]
    )
    if r.returncode != 0:
        run.fuseki_error = "could not build the Fuseki image:\n" + (r.stdout + r.stderr)[-3000:]
        return run.fuseki_error
    sh(["docker", "rm", "-f", FUSEKI_CONTAINER])
    if sh(["docker", "network", "inspect", TEST_NETWORK]).returncode != 0:
        sh(["docker", "network", "create", TEST_NETWORK])
    r = sh(
        [
            "docker",
            "run",
            "-d",
            "--name",
            FUSEKI_CONTAINER,
            "--network",
            TEST_NETWORK,
            "-p",
            f"127.0.0.1:{FUSEKI_PORT}:3030",
            tag,
        ]
    )
    if r.returncode != 0:
        run.fuseki_error = "could not start Fuseki: " + r.stderr
        return run.fuseki_error
    for _ in range(90):
        status = sh(["docker", "inspect", "-f", "{{.State.Health.Status}}", FUSEKI_CONTAINER]).stdout.strip()
        if status == "healthy":
            run.fuseki_started = True
            return None
        time.sleep(2)
    run.fuseki_error = (
        "Fuseki did not become healthy:\n" + sh(["docker", "logs", "--tail", "60", FUSEKI_CONTAINER]).stdout
    )
    return run.fuseki_error


def stop_fuseki(run: Run) -> None:
    if run.fuseki_started and not run.args.keep:
        sh(["docker", "rm", "-f", FUSEKI_CONTAINER])
        sh(["docker", "network", "rm", TEST_NETWORK])


def check_stack(run: Run) -> str | None:
    if run.stack is not None:
        return None
    if run.stack_error:
        return run.stack_error
    api = os.environ.get("VEROMESH_API", "http://localhost:8700")
    gms = os.environ.get("DATAHUB_GMS", "http://localhost:8080")
    fuseki = os.environ.get("VEROMESH_FUSEKI", "http://localhost:3030")
    probes = {"Veromesh API": f"{api}/health", "DataHub GMS": f"{gms}/health", "Fuseki": f"{fuseki}/$/ping"}
    down = [n for n, u in probes.items() if not http_ok(u)]
    if down and run.args.start_stack:
        say(
            color(
                ">> Starting the local stack with scripts/dev-up.sh (the first start takes 15-20 minutes)",
                DIM,
            )
        )
        r = subprocess.run([str(ROOT / "scripts/dev-up.sh")], cwd=ROOT)
        if r.returncode != 0:
            run.stack_error = "scripts/dev-up.sh failed"
            return run.stack_error
        down = [n for n, u in probes.items() if not http_ok(u)]
    if down:
        run.stack_error = (
            f"the local stack is not running ({', '.join(down)} not reachable). "
            "Start it with scripts/dev-up.sh, or pass --start-stack."
        )
        return run.stack_error
    token = BUILD / "datahub-token"
    if run.mode == "docker":
        # Tests run in a container on DataHub's network and use the service names.
        run.stack = {
            "api": "http://veromesh-service:8700",
            "gms": "http://datahub-gms:8080",
            "fuseki_stack": "http://fuseki:3030/veromesh",
            "token_file": "/repo/.build/datahub-token",
            "frontend": "http://datahub-frontend-react:9002",
        }
    else:
        run.stack = {
            "api": api,
            "gms": gms,
            "fuseki_stack": f"{fuseki}/veromesh",
            "token_file": str(token),
            "frontend": os.environ.get("DATAHUB_FRONTEND", "http://localhost:9002"),
        }
    return None


# ---------------------------------------------------------------------------- running a suite


def substitutions(run: Run, suite: Suite) -> dict[str, str]:
    in_container = suite.where == "runner" and run.mode == "docker"
    results = (
        f"/repo/{run.results_dir.relative_to(ROOT).as_posix()}" if in_container else str(run.results_dir)
    )
    subs = {"results": results}
    if "fuseki" in suite.needs:
        subs["fuseki"] = (
            f"http://{FUSEKI_CONTAINER}:3030/veromesh"
            if in_container
            else f"http://127.0.0.1:{FUSEKI_PORT}/veromesh"
        )
    if "stack" in suite.needs and run.stack:
        subs.update(run.stack)
        if suite.where == "host":
            subs["token_file"] = str(BUILD / "datahub-token")
    return subs


def fill(text: str, subs: dict[str, str]) -> str:
    for k, v in subs.items():
        text = text.replace("{" + k + "}", v)
    return text


def command_for(run: Run, suite: Suite, step: str, env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    base_env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "RUFF_NO_CACHE": "true",
        "VEROMESH_UPDATE_GOLDEN": "1" if run.args.update_golden else "",
    }
    if suite.where == "host":
        return ["bash", "-c", f"set -eo pipefail; {step}"], {**os.environ, **base_env, **env}
    pythonpath = "{root}/service/src:{root}/connectors/src"
    if run.mode == "docker":
        network = []
        if "fuseki" in suite.needs:
            network = ["--network", TEST_NETWORK]
        elif "stack" in suite.needs:
            network = ["--network", STACK_NETWORK]
        all_env = {**base_env, **env, "PYTHONPATH": pythonpath.format(root="/repo"), "HOME": "/tmp"}
        cmd = ["docker", "run", "--rm", "-v", f"{ROOT}:/repo", "-w", "/repo", *network]
        if hasattr(os, "getuid"):
            cmd += ["-u", f"{os.getuid()}:{os.getgid()}"]
        for k, v in all_env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [runner_image_tag(), "bash", "-c", f"set -eo pipefail; {step}"]
        return cmd, dict(os.environ)
    assert run.venv is not None
    path = f"{run.venv / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    all_env = {
        **os.environ,
        **base_env,
        **env,
        "PATH": path,
        "VIRTUAL_ENV": str(run.venv),
        "PYTHONPATH": pythonpath.format(root=ROOT),
    }
    return ["bash", "-c", f"set -eo pipefail; {step}"], all_env


def blocked_reason(run: Run, suite: Suite) -> str | None:
    if (
        "docker" in suite.needs
        or "fuseki" in suite.needs
        or (suite.where == "runner" and run.mode == "docker")
    ) and not run.docker:
        return "Docker is not running"
    if "jdk25-or-docker" in suite.needs and not (run.docker or java_major() >= 25):
        return "needs JDK 25 or Docker"
    # The test-runner image ships Helm and kubeconform; --local needs them on PATH.
    if "helm" in suite.needs and run.mode != "docker" and not shutil.which("helm"):
        return "needs Helm on PATH (or Docker, whose test runner has it)"
    if "kind" in suite.needs:
        missing = [t for t in ("kind", "kubectl", "helm") if not shutil.which(t)]
        if missing:
            return "needs " + ", ".join(missing) + " on PATH"
    if suite.where == "runner":
        err = prepare_runner(run)
        if err:
            return err
    if "fuseki" in suite.needs:
        err = start_fuseki(run)
        if err:
            return err
    if "stack" in suite.needs:
        err = check_stack(run)
        if err:
            return err
    return None


def run_suite(run: Run, suite: Suite) -> Result:
    res = Result(suite)
    log_path = run.results_dir / f"{suite.name}.log"
    res.log = log_path
    reason = blocked_reason(run, suite)
    if reason:
        res.status = "SKIP"
        res.detail = reason.splitlines()[0]
        log_path.write_text(reason + "\n")
        return res
    subs = substitutions(run, suite)
    env = {k: fill(v, subs) for k, v in suite.env.items()}
    start = time.time()
    with open(log_path, "w") as log:
        for step in suite.steps:
            step = fill(step, subs)
            log.write(f"$ {step}\n")
            log.flush()
            cmd, proc_env = command_for(run, suite, step, env)
            proc = subprocess.Popen(
                cmd, cwd=ROOT, env=proc_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                if run.args.verbose:
                    sys.stdout.write("    " + line)
            try:
                rc = proc.wait(timeout=run.args.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = -1
                log.write(f"\n!! timed out after {run.args.timeout}s\n")
            log.write(f"[exit {rc}]\n\n")
            log.flush()
            if rc != 0:
                res.status = "FAIL"
                res.failed_step = step
                break
        else:
            res.status = "PASS"
    res.seconds = time.time() - start
    text = log_path.read_text(errors="replace")
    res.detail = pytest_counts(text)
    if res.status == "FAIL" and not res.detail:
        res.detail = "failed: " + (res.failed_step or "")[:70]
    elif not res.detail:
        # Last line a check printed, e.g. "84 third-party packages checked; ..."
        lines = [x.strip() for x in text.splitlines() if x.strip() and not x.startswith(("$ ", "[exit"))]
        res.detail = lines[-1][:70] if lines else ""
    return res


# ---------------------------------------------------------------------------- reporting


def write_reports(run: Run, total: float) -> None:
    rows = [
        {
            "suite": r.suite.name,
            "component": r.suite.component,
            "tier": r.suite.tier,
            "status": r.status,
            "seconds": round(r.seconds, 1),
            "detail": r.detail,
            "log": r.log.name if r.log else None,
        }
        for r in run.results
    ]
    summary = {
        "startedAt": run.results_dir.name,
        "mode": run.mode,
        "git": sh(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT).stdout.strip(),
        "seconds": round(total, 1),
        "passed": sum(r.status == "PASS" for r in run.results),
        "failed": sum(r.status == "FAIL" for r in run.results),
        "skipped": sum(r.status == "SKIP" for r in run.results),
        "suites": rows,
    }
    (run.results_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    md = [
        f"# Test run {run.results_dir.name}",
        "",
        f"Commit `{summary['git']}`, {run.mode} runner, {fmt_seconds(total)}: "
        f"{summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped.",
        "",
        "| Suite | Tier | Status | Time | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    md += [f"| {r['suite']} | {r['tier']} | {r['status']} | {r['seconds']}s | {r['detail']} |" for r in rows]
    (run.results_dir / "summary.md").write_text("\n".join(md) + "\n")
    cases = []
    for r in run.results:
        body = ""
        if r.status == "FAIL":
            tail = r.log.read_text(errors="replace")[-4000:] if r.log else ""
            body = f"<failure message={quoteattr(r.detail)}>{escape(tail)}</failure>"
        elif r.status == "SKIP":
            body = f"<skipped message={quoteattr(r.detail)}/>"
        cases.append(
            f'  <testcase classname="veromesh.{r.suite.tier}" name="{r.suite.name}" time="{r.seconds:.1f}">{body}</testcase>'
        )
    (run.results_dir / "harness.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite name="veromesh-harness" tests="{len(rows)}" failures="{summary["failed"]}" '
        f'skipped="{summary["skipped"]}" time="{total:.1f}">\n' + "\n".join(cases) + "\n</testsuite>\n"
    )
    latest = run.results_dir.parent / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(run.results_dir.name)
    except OSError:
        pass


def print_line(r: Result) -> None:
    dots = "." * max(2, 18 - len(r.suite.name))
    status = color(f"{r.status:4}", STATUS_COLOR[r.status])
    say(f"  {r.suite.name} {color(dots, DIM)} {status} {fmt_seconds(r.seconds):>7}  {r.detail}")


def print_failure(r: Result) -> None:
    if not r.log:
        return
    lines = r.log.read_text(errors="replace").splitlines()
    say(color(f"  ---- {r.suite.name}: last lines of {r.log.relative_to(ROOT)}", DIM))
    for line in lines[-40:]:
        say("  | " + line)


# ---------------------------------------------------------------------------- main


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="scripts/test.sh",
        description="Run the VeroMesh regression suites. Targets are tiers, suite names or components "
        "(default: quick). See --list.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
        "  scripts/test.sh                      quick tier (lint, unit, contracts)\n"
        "  scripts/test.sh integration          quick + real Fuseki + container images\n"
        "  scripts/test.sh all --start-stack    everything, starting the local stack if needed\n"
        "  scripts/test.sh e2e                  end-to-end against the running stack\n"
        "  scripts/test.sh connectors ontology  just these suites / components\n"
        "  scripts/test.sh contracts --update-golden   accept intended contract changes\n",
    )
    p.add_argument("targets", nargs="*", help="tiers, suite names or components")
    p.add_argument("--list", action="store_true", help="list tiers and suites, then exit")
    p.add_argument(
        "--local", action="store_true", help="run tests in a local virtualenv instead of the Docker runner"
    )
    p.add_argument("-v", "--verbose", action="store_true", help="stream every suite's output")
    p.add_argument("-x", "--fail-fast", action="store_true", help="stop at the first failing suite")
    p.add_argument("--strict", action="store_true", help="a skipped suite fails the run (CI uses this)")
    p.add_argument(
        "--start-stack", action="store_true", help="start the local stack for e2e if it is not running"
    )
    p.add_argument("--keep", action="store_true", help="leave the throwaway Fuseki running afterwards")
    p.add_argument(
        "--update-golden", action="store_true", help="rewrite contract snapshots instead of comparing"
    )
    p.add_argument("--timeout", type=int, default=3600, help="per-step timeout in seconds (default 3600)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.list:
        print_list()
        return 0
    problems = check_registry()
    if problems:
        say(color("Suite registry problems (tests/harness/suites.py):", RED))
        for p in problems:
            say(f"  - {p}")
        return 2
    suites = select(args.targets)
    if args.update_golden and not any(s.name == "contracts" for s in suites):
        suites = select(["contracts"])
    docker = docker_available()
    mode = "local" if args.local else "docker"
    if mode == "docker" and not docker:
        say(
            color(
                "Docker is not running. Start Docker Desktop, or run with --local (needs Python 3.11+).", RED
            )
        )
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results_dir = BUILD / "test-results" / stamp
    results_dir.mkdir(parents=True, exist_ok=True)
    run = Run(args=args, results_dir=results_dir, mode=mode, docker=docker)

    commit = sh(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT).stdout.strip() or "unknown"
    say(color(f"VeroMesh regression suite: {len(suites)} suites, {mode} runner, commit {commit}", BOLD))
    start = time.time()
    current_tier = None
    try:
        for suite in suites:
            if suite.tier != current_tier:
                current_tier = suite.tier
                say(color(f"\n{current_tier}", BOLD))
            res = run_suite(run, suite)
            run.results.append(res)
            print_line(res)
            if res.status == "FAIL" and args.fail_fast:
                break
    except KeyboardInterrupt:
        say(color("\nInterrupted.", RED))
    finally:
        stop_fuseki(run)
    total = time.time() - start
    write_reports(run, total)

    failed = [r for r in run.results if r.status == "FAIL"]
    skipped = [r for r in run.results if r.status == "SKIP"]
    for r in failed:
        say()
        print_failure(r)
    passed = sum(r.status == "PASS" for r in run.results)
    say()
    verdict = "FAILED" if failed or (args.strict and skipped) else "PASSED"
    say(
        color(verdict, RED if verdict == "FAILED" else GREEN)
        + f"  {passed} passed, {len(failed)} failed, {len(skipped)} skipped in {fmt_seconds(total)}"
    )
    for r in skipped:
        say(color(f"  skipped {r.suite.name}: {r.detail}", YELLOW))
    say(
        f"Results: {results_dir.relative_to(ROOT)}/  (summary.md, harness.xml, one log and JUnit file per suite)"
    )
    if run.results and len(run.results) < len(suites):
        say(color(f"Stopped early: {len(suites) - len(run.results)} suites not run.", YELLOW))
    return 1 if verdict == "FAILED" else 0


if __name__ == "__main__":
    sys.exit(main())
