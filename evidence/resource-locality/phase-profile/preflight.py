"""No service, database or workload is started by this compatibility preflight."""
import asyncio
import inspect
from collections import defaultdict
from pathlib import Path
import phase_service as service
from campus_fabric.storage.sqlite import _WriterAdmission

for name in ("phase_service.py", "run_diagnostic.py"):
    path = Path(__file__).parent / name
    compile(path.read_text(), str(path), "exec")
assert inspect.isgeneratorfunction(service.Kernel._inventory_nodes)
for cls, names in ((service.Kernel, ("_schedule", "_validate_resources")),
                   (service.SQLiteStore, ("save", "_save_changes")),
                   (service.StateDict, ("changed_units", "rebase")),
                   (service.MerkleTree, ("set", "delete"))):
    for name in names:
        assert callable(getattr(cls, name))
service.install()
assert list(inspect.signature(service.mutation_http.run_in_threadpool).parameters.values())[0].kind is inspect.Parameter.POSITIONAL_ONLY
async def check():
    def forwarded(*, function):
        return function
    assert await service.mutation_http.run_in_threadpool(forwarded, function="keyword-preserved") == "keyword-preserved"
asyncio.run(check())
record = {"phases": defaultdict(list)}
token = service.TRANSACTION.set(record)
with _WriterAdmission().enter():
    assert record["admission_acquired_ns"] >= record["admission_started_ns"]
assert record["admission_released_ns"] >= record["admission_acquired_ns"]
service.TRANSACTION.reset(token)
print("PREFLIGHT_OK: parsed scripts; lazy generator recognized; methods present; thread keyword forwarding preserved; original admission context returns/releases. No service/database/load started.")
