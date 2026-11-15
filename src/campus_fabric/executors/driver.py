"""One monotonic CPU driver; durable kernel outbox precedes all process effects."""
from __future__ import annotations

import fcntl
import logging
import platform
import subprocess
import threading
import time
from pathlib import Path

from campus_fabric.contracts import models as m
from campus_fabric.executors.cpu_reference import CpuReferenceExecutor, ExecutorRejected

LOG = logging.getLogger(__name__)
SYSTEM = m.AuthenticatedActor(actor_id="cpu-driver", role="system")


def host_boot_id():
    """Stable across service restarts, different after a host reboot."""
    linux = Path("/proc/sys/kernel/random/boot_id")
    if linux.is_file():
        return "linux-" + linux.read_text().strip()
    if platform.system() == "Darwin":
        boot = subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.boottime"], text=True)
        return platform.node() + "-" + boot.strip()
    raise RuntimeError("CPU reference clock requires a Linux or macOS boot identity")


class CpuDriver:
    def __init__(self, kernel, executor: CpuReferenceExecutor, *, lock_path: Path):
        self.kernel, self.executor = kernel, executor
        self.kernel.reclaim_stop_cost_upper = executor.config.stop_timeout_seconds + 1.0
        self.lock_path = lock_path
        self.guard = threading.RLock()
        self._file = None
        self._boot = None
        self.dispatch_errors = {}

    def start(self):
        with self.guard:
            if self._file is not None:
                return
            stream = self.lock_path.open("a+b")
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.close()
                raise RuntimeError("Another CPU clock driver owns this instance") from None
            self._file = stream
            try:
                self._boot = host_boot_id()
                # Deadlines elapsed during downtime precede late process observations.
                self.kernel.sync_cpu_clock(self._boot, time.monotonic_ns())
            except BaseException:
                self.close()
                raise

    def close(self):
        # No unconfirmed stop is turned into a resource release on server shutdown.
        with self.guard:
            if self._file:
                fcntl.flock(self._file, fcntl.LOCK_UN)
                self._file.close()
                self._file = None

    def tick(self, *, advance_wall=True):
        with self.guard:
            if self._file is None:
                raise RuntimeError("CPU driver has not acquired its instance lock")
            if advance_wall:
                self.kernel.sync_cpu_clock(self._boot, time.monotonic_ns())
            logical_time, pending = self.kernel.pending_dispatches()
            for command, owner in pending:
                if not owner:
                    continue
                try:
                    self.executor.dispatch(command, owner_id=owner, logical_time=logical_time)
                except (ExecutorRejected, OSError) as exc:
                    # Keep the claim and the outbox. Dispatch cannot be assumed side-effect free.
                    if self.dispatch_errors.get(command.dispatch_id) != str(exc):
                        LOG.error("CPU dispatch %s requires reconciliation: %s", command.dispatch_id, exc)
                    self.dispatch_errors[command.dispatch_id] = str(exc)
                    continue
                self.dispatch_errors.pop(command.dispatch_id, None)
                self.kernel.acknowledge_dispatch(command.dispatch_id, command.version.epoch)
            state = self.kernel.runtime_metadata()
            version = m.VersionStamp(epoch=state["epoch"], revision=state["revision"])
            for event in self.executor.poll(logical_time=state["now"], version=version):
                current = self.kernel.runtime_metadata()
                if event.version.epoch != current["epoch"]:
                    self.kernel.reconcile_retired(event)
                    self.executor.acknowledge(event.callback_id)
                    continue
                receipt = self.kernel.commit(m.CommandEnvelope(
                    request_id="observe-" + event.callback_id, actor=SYSTEM,
                    operation="executor.report", idempotency_key=event.callback_id,
                    parameters=m.ExecutorReportParameters(kind="executor.report", event=event)))
                if receipt.outcome != "rejected" or receipt.error.code in {
                    "attempt_stale", "lease_unavailable",
                }:
                    self.executor.acknowledge(event.callback_id)
                else:
                    LOG.error("CPU observation %s rejected: %s", event.callback_id, receipt.error.detail)
