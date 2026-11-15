# Trained neural policy — isolated kernel replay

Result: **PASS**. Fixed case: predictable / index 0.

Model SHA-256: `c7830f8062fb5f700aa73fa2d333bdbdc6ec70413bbe653ed92c366ac12afbeb`.
Observed logical time: 3.6288e+06; training cutoff: 3.00266e+06.

Only causal observed history was replayed. Raw observation times were retained; no case.actual or result report informed selection.

| Task | Selected GPU | Neural active | B2 GPU | Original lease expiry |
| --- | --- | --- | --- | --- |
| task-0 | provider-03 | True | provider-00 | 3.6297e+06 |
| task-1 | provider-02 | True | provider-00 | 3.6297e+06 |
| task-2 | provider-00 | True | provider-00 | 3.6297e+06 |
| task-3 | provider-01 | True | provider-01 | 3.6297e+06 |

All four selected plans were committed by the actual SQLite kernel and bound to simulated jobs. GPU assignments were exclusive.
The contributor then issued a 1-second recall despite the 12-second declared profile; the kernel preempted the selected lease and removed its placement at that actual deadline.
All leases expired at their original 900-second deadline. A separate local SQLite instance rejected a stale neural plan with stale_version.

| Task | GPU | Task-specific rank | Predicted restricted completion (s) |
| --- | --- | --- | --- |
| task-0 | provider-03 | 1 | 529.329514 |
| task-0 | provider-02 | 2 | 545.102875 |
| task-0 | provider-01 | 3 | 568.354935 |
| task-0 | provider-00 | 4 | 583.541077 |
| task-1 | provider-03 | 1 | 185.747498 |
| task-1 | provider-02 | 2 | 196.348267 |
| task-1 | provider-01 | 3 | 199.279343 |
| task-1 | provider-00 | 4 | 201.920456 |
| task-2 | provider-00 | 1 | 24.000000 |
| task-2 | provider-01 | 1 | 24.000000 |
| task-2 | provider-02 | 1 | 24.000000 |
| task-2 | provider-03 | 1 | 24.000000 |
| task-3 | provider-03 | 1 | 163.131027 |
| task-3 | provider-01 | 2 | 177.505005 |
| task-3 | provider-02 | 3 | 178.559692 |
| task-3 | provider-00 | 4 | 195.355240 |

Full snapshots, plans, commit receipts, recall/expiry receipts and source hashes accompany this record.
This separate verification is outside the frozen comparison. It proves contract execution and authority boundaries, not beneficial predictive performance or deployed learning.
