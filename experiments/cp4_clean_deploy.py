"""Automatic clean backend rehearsal, explicitly not another person's CP4 sign-off.

The controller clones only this checkout's committed HEAD, verifies both source
and deployment identities, installs locked Python dependencies in a new venv,
then runs the real B host. UI, TypeScript regeneration and human tasks are NOT_RUN.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_checkpoint import current_build  # noqa: E402

RELEASE_FILES = ("uv.lock", "web/package-lock.json", "pyproject.toml", "web/package.json",
                 ".python-version", "README.md", "Makefile")
OPTIONAL_DEPLOY_FILES = ("Dockerfile", "compose.yaml", "compose.yml", "docker-compose.yml")


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True,
                       allow_nan=False) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = encoded(value)
    path.write_bytes(raw)
    return {"path": path.name, "sha256": digest(raw)}


def fresh_output(path):
    path = Path(path).resolve()
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ValueError("Refuse to overwrite existing clean-deployment evidence") from None
    return path


def require_assertions():
    if not __debug__:
        raise RuntimeError("Clean deployment verification requires assertions; do not use python -O/-OO")


def release_manifest(root):
    """Cover deployment files omitted by the older shared build digest."""
    root = Path(root).resolve()
    paths = [root / name for name in RELEASE_FILES]
    paths += [root / name for name in OPTIONAL_DEPLOY_FILES
              if (root / name).exists() or (root / name).is_symlink()]
    deploy = root / "deploy"
    if deploy.is_symlink():
        raise ValueError("Deployment symlinks cannot enter a clean release manifest")
    if deploy.exists():
        paths += sorted(path for path in deploy.rglob("*") if path.is_file() or path.is_symlink())
    rows = []
    for path in sorted(paths):
        if any(parent.is_symlink() for parent in (path, *path.parents)
               if parent == root or parent.is_relative_to(root)):
            raise ValueError("Release files must not follow symlinks")
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError(f"Missing or oversized release file: {path.relative_to(root)}")
        raw = path.read_bytes()
        rows.append({"path": path.relative_to(root).as_posix(), "sha256": digest(raw),
                     "bytes": len(raw), "executable": bool(path.stat().st_mode & 0o111)})
    return {"schema": "cp4-release-file-manifest-v1", "files": rows,
            "missing_optional": [name for name in OPTIONAL_DEPLOY_FILES if not (root / name).exists()],
            "deploy_directory_present": deploy.is_dir(), "manifest_sha256": digest(encoded(rows))}


def verify_identity(source_build, clone_build, source_release, clone_release):
    if not source_build.get("git_commit"):
        raise ValueError("A committed source HEAD is required")
    if source_build != clone_build:
        raise ValueError("Current source differs from the clean HEAD clone; commit the candidate first")
    if source_release != clone_release:
        raise ValueError("Deployment files or dependency locks differ from the clean HEAD clone")


def clean_environment():
    """Do not forward credentials, proxy URLs, Git overrides or an existing venv."""
    allowed = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT")
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    result.update(PYTHONNOUSERSITE="1", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
                  GIT_CONFIG_GLOBAL=os.devnull, UV_NO_CONFIG="1", NO_COLOR="1")
    return result


def sanitize_log(text):
    # No credentials are supplied to controller commands. Redact any accidental
    # credential-bearing dependency URL or echoed authorization value as well.
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1<redacted>@", text)
    return re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,]+",
                  r"\1<redacted>", text)


class CommandFailed(RuntimeError):
    pass


class Commands:
    def __init__(self, output, timeout_seconds):
        self.output = Path(output)
        self.deadline = time.monotonic() + timeout_seconds
        self.records = []

    def run(self, name, argv, *, cwd, env, timeout=180):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CommandFailed("Overall clean-deployment deadline expired")
        duration = min(timeout, remaining)
        arguments = [str(arg) for arg in argv]
        row = {"id": name, "argv": [sanitize_log(arg) for arg in arguments], "cwd": str(cwd),
               "started_at": datetime.now(timezone.utc).isoformat(), "timeout_seconds": duration,
               "status": "NOT_RUN", "returncode": None, "timed_out": False}
        self.records.append(row)
        started = time.monotonic()
        payload = b""
        try:
            process = subprocess.Popen(arguments, cwd=cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                payload, _ = process.communicate(timeout=duration)
            except subprocess.TimeoutExpired:
                row["timed_out"] = True
                # This group is created by this runner and contains only this
                # command and its children (including the temporary TCP host).
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    payload, _ = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    payload, _ = process.communicate(timeout=3)
            row["returncode"] = process.returncode
            row["status"] = "MET" if process.returncode == 0 and not row["timed_out"] else "NOT_MET"
        except Exception as exc:
            row.update(status="NOT_MET", error=sanitize_log(f"{type(exc).__name__}: {exc}"))
        finally:
            row["elapsed_seconds"] = time.monotonic() - started
            log = sanitize_log(payload.decode("utf-8", errors="replace")).encode()
            path = self.output / f"command-{len(self.records):02d}-{name}.log"
            path.write_bytes(log)
            row["log"] = {"path": path.name, "sha256": digest(log), "bytes": len(log)}
            save(self.output / "commands.json", {"commands": self.records})
        if row["status"] != "MET":
            raise CommandFailed(f"{name} failed; returncode={row['returncode']}, timed_out={row['timed_out']}")
        return row


def run_worker(output):
    """Run only the TCP journey stage; useful as a separately labelled regression."""
    require_assertions()
    from accept_cp1_a import Harness, registration
    from accept_cp2_b import clock, until

    output = fresh_output(output)
    before = current_build(ROOT)
    rows = [{"id": name, "status": "NOT_RUN"} for name in
            ("normal-auto-release", "w2-timeout-auto-release")]
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    failure = None
    with tempfile.TemporaryDirectory(prefix="cp4-clean-worker-", dir=runtime) as temporary:
        h = Harness(temporary, app_target="campus_fabric.workflows.devhost:create_app")
        h.env.update(FABRIC_SCHEDULING_MODE="external", FABRIC_B_POLICY="P",
                     PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT), str(ROOT / "scripts"))))
        try:
            h.start()
            assert h.get("/v1/version")["execution_mode"] == "logical_simulation"
            for row in rows:
                name = row["id"]
                row["status"] = "NOT_MET"
                timeout = 2 if name.startswith("w2") else 100
                body = registration(name, timeout=timeout,
                                    settlement="release_on_terminal" if timeout == 2 else "release_on_success")
                body["specification"]["allocation"].update(preemption_mode="best_effort", max_restarts=0)
                h.request("POST", "/x/v1/work-intents", body=body, expected=202)
                path = f"/x/v1/work-intents/{name}"
                initial = until(h, path, lambda value: value["item"]["state"] == "running", timeout=8)
                clock(h, 10)
                final = until(h, path, lambda value: value["allocations"][0]["state"] == "Released", timeout=8)
                expected_state = "failed" if timeout == 2 else "completed"
                expected_kind = "failure" if timeout == 2 else "success"
                assert final["item"]["state"] == expected_state
                result = final["item"]["result"]
                assert result["kind"] == expected_kind and result["artifact_uris"] == []
                assert len(final["item"]["attempts"]) == 1
                assert final["item"]["allocation_ids"] == initial["item"]["allocation_ids"]
                assert final["item"]["job_ids"] == initial["item"]["job_ids"]
                events = [event["event_type"] for event in final["events"]]
                assert events.count("allocation.released") == events.count("intent.lease_settled") == 1
                if timeout == 2:
                    assert result["failure"]["detail"] == "Job timeout"
                    assert result["recorded_at"] == initial["now"] + 2
                    assert final["jobs"][0]["retry_count"] == 0
                    assert events.count("job.failed") == events.count("intent.failed") == 1
                    assert "job.succeeded" not in events
                totals = h.get("/v1/capacity")["totals"]
                assert totals["available"] == totals["capacity"]
                h.request("GET", path, actor="b", expected=404)
                row.update(status="MET", start_time=initial["now"], result=result,
                           final_state=final["item"]["state"], lease_state="Released",
                           actual="One attempt, terminal result and automatic release observed over TCP.")
        except Exception:
            failure = h.redact(traceback.format_exc())
        finally:
            try:
                h.stop()
            except Exception:
                failure = h.redact(traceback.format_exc())
            log = Path(temporary) / "server.log"
            history = {"execution_mode": "logical_simulation", "app_target": h.app_target,
                       "server_command": getattr(h, "command", None), "source_checkout": str(ROOT),
                       "credentials_recorded": False, "records": h.history,
                       "server_log": log.read_text(errors="replace") if log.exists() else ""}
            raw = h.redact(encoded(history).decode()).encode()
            assert all(token.encode() not in raw for token in (*h.tokens.values(), h.test_key))
            (output / "http_history.json").write_bytes(raw)
    after = current_build(ROOT)
    report = {"schema": "cp4-clean-worker-v1", "status": "MET" if failure is None and before == after
              and all(row["status"] == "MET" for row in rows) else "NOT_MET",
              "build": before, "build_at_finish": after, "checks": rows, "failure": failure,
              "scope": "Automatic TCP journeys only; this worker alone does not prove clean installation",
              "formal_cp4_clean_deploy": "NOT_RUN", "human_acceptance": "NOT_RUN",
              "evidence": [{"path": "http_history.json", "sha256": digest(raw)}]}
    save(output / "worker_receipt.json", report)
    return report


def run_rehearsal(output, uv, *, timeout_seconds=300):
    require_assertions()
    output = fresh_output(output)
    uv = Path(uv).expanduser().absolute()
    if not uv.is_file() or not os.access(uv, os.X_OK):
        raise ValueError("--uv must name an existing executable; no tool is downloaded implicitly")
    if not 1 <= timeout_seconds <= 1800:
        raise ValueError("Overall timeout must be between 1 and 1800 seconds")
    before = current_build(ROOT)
    release_before = release_manifest(ROOT)
    manifest_ref = save(output / "source_release_manifest.json", release_before)
    commands = Commands(output, timeout_seconds)
    rows = [{"id": name, "status": "NOT_RUN"} for name in
            ("clean-head-identity", "fresh-locked-venv", "python-contract-schema",
             "backend-tcp-journeys", "unchanged-final-identities")]
    report = {"schema": "cp4-clean-backend-rehearsal-v1", "check": "CP4-B-CLEAN-BACKEND-REHEARSAL",
              "status": "NOT_RUN", "scope": "Automatic clean backend clone/venv/TCP rehearsal only",
              "build": before, "observed_at": datetime.now(timezone.utc).isoformat(),
              "checks": rows, "release_manifest": manifest_ref, "evidence": [],
              "formal_cp4_clean_deploy": "NOT_RUN", "human_different_operator": "NOT_RUN",
              "ui_build": "NOT_RUN", "typescript_regeneration": "NOT_RUN",
              "execution_mode": "logical_simulation", "unresolved_issues": [],
              "cleanup": "NOT_RUN", "timeout_seconds": timeout_seconds}
    report["uv"] = {"path": str(uv), "sha256": digest(uv.read_bytes())}
    report["controller_python"] = {"executable": sys.executable, "version": sys.version}
    env = clean_environment()
    stage = rows[0]
    try:
        if not before.get("git_commit"):
            raise ValueError("A committed HEAD is required before clean deployment")
        with tempfile.TemporaryDirectory(prefix="campus-cp4-clean-clone-") as temporary:
            base = Path(temporary)
            clone = base / "checkout"
            report["temporary_clone"] = str(clone)
            stage["status"] = "NOT_MET"
            commands.run("clone", ["git", "clone", "--no-hardlinks", "--no-checkout", str(ROOT), str(clone)],
                         cwd=base, env=env, timeout=30)
            commands.run("detach-head", ["git", "-C", str(clone), "checkout", "--detach", before["git_commit"]],
                         cwd=base, env=env, timeout=30)
            clone_build = current_build(clone)
            clone_release = release_manifest(clone)
            report["clone_build"] = clone_build
            report["clone_release_manifest"] = save(output / "clone_release_manifest.json", clone_release)
            verify_identity(before, current_build(ROOT), release_before, release_manifest(ROOT))
            verify_identity(before, clone_build, release_before, clone_release)
            assert not (clone / ".venv").exists(), "A clean clone must not carry a virtual environment"
            stage.update(status="MET", actual="Committed clone matches current source, locks and deploy manifest.")
            stage = rows[1]
            stage["status"] = "NOT_MET"
            install_env = {**env, "UV_PROJECT_ENVIRONMENT": str(clone / ".venv"),
                           "UV_CACHE_DIR": str(base / "uv-cache")}
            commands.run("uv-version", [str(uv), "--version"], cwd=clone, env=install_env, timeout=10)
            commands.run("locked-install", [str(uv), "sync", "--locked", "--python", sys.executable],
                         cwd=clone, env=install_env)
            python = clone / ".venv/bin/python"
            assert python.is_file(), "Locked installation did not create the new Python environment"
            stage.update(status="MET", actual="uv sync --locked created a new clone-local venv; isolated cache.")
            worker_env = {**env, "PYTHONPATH": os.pathsep.join((str(clone / "src"), str(clone)))}
            stage = rows[2]
            stage["status"] = "NOT_MET"
            commands.run("python-contract-schema", [str(python), "scripts/export_contracts.py", "--check"],
                         cwd=clone, env=worker_env, timeout=30)
            stage.update(status="MET", actual="Checked-in generated JSON schema equals fresh Pydantic generation.")
            stage = rows[3]
            stage["status"] = "NOT_MET"
            worker_dir = output / "worker"
            commands.run("backend-worker", [str(python), "-m", "experiments.cp4_clean_deploy", "--worker",
                         "--output", str(worker_dir)], cwd=clone, env=worker_env, timeout=60)
            worker = json.loads((worker_dir / "worker_receipt.json").read_bytes())
            if worker["status"] != "MET" or worker["build"] != before or worker["build_at_finish"] != before:
                raise ValueError("Worker journeys did not pass on the sealed clone identity")
            for entry in worker["evidence"]:
                if entry["path"] != "http_history.json" or digest((worker_dir / entry["path"]).read_bytes()) != entry["sha256"]:
                    raise ValueError("Worker HTTP evidence hash differs from its receipt")
            report["evidence"] += [{"path": "worker/" + name,
                "sha256": digest((worker_dir / name).read_bytes())}
                for name in ("worker_receipt.json", "http_history.json")]
            stage.update(status="MET", actual="Normal completion and timeout failure both released capacity automatically.")
            stage = rows[4]
            stage["status"] = "NOT_MET"
            after, clone_after = current_build(ROOT), current_build(clone)
            release_after, clone_release_after = release_manifest(ROOT), release_manifest(clone)
            verify_identity(before, after, release_before, release_after)
            verify_identity(before, clone_after, release_before, clone_release_after)
            report.update(build_at_finish=after, clone_build_at_finish=clone_after)
            stage.update(status="MET", actual="Source, clean clone, lockfiles and deployment files remained identical.")
        report["cleanup"] = "MET"
        report["status"] = "MET"
    except Exception as exc:
        stage["status"] = "NOT_MET"
        report["status"] = "NOT_MET"
        report["unresolved_issues"].append(sanitize_log(f"{type(exc).__name__}: {exc}"))
    finally:
        clone_path = report.get("temporary_clone")
        if clone_path:
            report["cleanup"] = "MET" if not Path(clone_path).exists() else "NOT_MET"
            if report["cleanup"] != "MET":
                report["status"] = "NOT_MET"
                report["unresolved_issues"].append("Owned temporary clone was not fully removed")
        report["build_at_finish"] = current_build(ROOT)
        if report["status"] == "MET" and (report["build_at_finish"] != before
                or release_manifest(ROOT) != release_before):
            report["status"] = rows[-1]["status"] = "NOT_MET"
            report["unresolved_issues"].append("Source or deployment manifest changed before final receipt")
        report["commands"] = commands.records
        save(output / "receipt.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", type=Path, help="Absolute path to an existing uv executable")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--worker", action="store_true", help="Only run TCP journeys in this checkout")
    args = parser.parse_args()
    if not args.worker and args.uv is None:
        parser.error("--uv is required for the clean-clone controller")
    output = args.output or ROOT / "evidence/cp4/b/clean-deploy" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    result = run_worker(output) if args.worker else run_rehearsal(
        output, args.uv, timeout_seconds=args.timeout_seconds)
    print(json.dumps({"status": result["status"], "output": str(output.resolve()),
                      "formal_cp4_clean_deploy": "NOT_RUN"}))
    return 0 if result["status"] == "MET" else 1


if __name__ == "__main__":
    raise SystemExit(main())
