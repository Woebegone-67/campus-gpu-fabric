import cProfile,json,pstats,sys,time
from pathlib import Path
OUT=Path(__file__).resolve().parent; ROOT=OUT.parents[3]
sys.path[:0]=[str(ROOT/'src')]
from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.api.queries.service import QueryServices
protocol=json.loads((OUT/'protocol.json').read_text())
(OUT/'query_followup_protocol.json').write_text(json.dumps({'purpose':'Correctly measure the owner-wide snapshot used by actual B detail route. Initial selected_job_read is a narrower existing Kernel port, not the actual HTTP detail implementation.','source':'api/queries/routes.py:169 calls services.read(actor), no entity filter','budget':'Three unprofiled read-only actual QueryServices.read calls plus one cProfile, on the existing isolated n128_h256 diagnostic DB only; no writes or HTTP traffic.','original_protocol_build':protocol['build']},indent=2)+'\n')
k=Kernel(Path(protocol['environment']['temporary_directory'])/'n128_h256.sqlite3')
services=QueryServices(read_port=k)
a=m.AuthenticatedActor(actor_id='owner-043',owner_id='owner-043',role='researcher')
rows=[]
for i in range(4):
 p=cProfile.Profile() if i==3 else None
 wall=time.perf_counter();cpu=time.process_time()
 if p:p.enable()
 snap=services.read(a)
 if p:p.disable()
 row={'profiled':bool(p),'wall_ms':(time.perf_counter()-wall)*1000,'cpu_ms':(time.process_time()-cpu)*1000,'visible_jobs':len(snap.jobs),'visible_events':len(snap.events)}
 if p:
  p.dump_stats(str(OUT/'actual_owner_detail_snapshot.pstats'))
  stats=pstats.Stats(p)
  row['top_inclusive']=[{'file':key[0],'line':key[1],'function':key[2],'exclusive_s':v[2],'inclusive_s':v[3]} for key,v in sorted(stats.stats.items(),key=lambda pair:pair[1][3],reverse=True)[:20]]
 rows.append(row)
(OUT/'query_followup_results.json').write_text(json.dumps({'measurements':rows,'scope':'Actual detail route owner snapshot service and validation; HTTP dispatch/serialization and event-loop queueing are not timed.'},indent=2)+'\n')
print(json.dumps(rows[:3],indent=2))
k.store.close()
