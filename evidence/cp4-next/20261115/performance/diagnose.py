#!/usr/bin/env python3
"""Bounded causal probes of frozen production Kernel; not an acceptance load."""
import collections, contextlib, cProfile, gzip, hashlib, json, os, platform, pstats, statistics, sys, tempfile, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[4]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from check_checkpoint import current_build
from campus_fabric.contracts import models as m
from campus_fabric.domain import kernel as kernel_module
from campus_fabric.domain.kernel import Kernel
from campus_fabric.storage.sqlite import SQLiteStore
from campus_fabric.workflows.devhost import WorkflowWorker
OUT=Path(__file__).resolve().parent
SOURCE=ROOT/'evidence/cp4/load/059f477b-native-4cpu8g-20261114'
TEMP=Path(tempfile.mkdtemp(prefix='cp4-causal-probe-'))
def dump(name,obj):
    (OUT/name).write_text(json.dumps(obj,indent=2,sort_keys=True)+'\n')
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def entity(receipt,kind):
    if receipt.outcome=='rejected': raise RuntimeError(str(receipt))
    return next(x.entity_id for x in receipt.objects if x.kind==kind)
def actor(i): return m.AuthenticatedActor(actor_id=f'owner-{i%64:03}',owner_id=f'owner-{i%64:03}',role='researcher')
def commit(k,params,i,label):
    return k.commit(m.CommandEnvelope(request_id=f'diagnostic-{i}-{label}',actor=actor(i),operation=params.kind,parameters=params))
def flow(k,i,measure=None):
    def call(label,p):
        return measure(label,lambda:commit(k,p,i,label)) if measure else commit(k,p,i,label)
    a=entity(call('allocate',m.AllocationCreateParameters(kind='allocation.create',owner_id=actor(i).owner_id,ttl_seconds=300,request=m.ResourceRequest(cpu_millicores=1000,memory_mib=1024,ephemeral_storage_mib=32,gpu=m.GPURequest(count=1,model='A10',memory_mib=4096)))),'allocation')
    j=entity(call('create_job',m.JobCreateParameters(kind='job.create',allocation_id=a,image='cp4-logical-fixed-model',command=('work',),timeout_seconds=100,max_retries=0)),'job')
    call('renew',m.AllocationRenewParameters(kind='allocation.renew',allocation_id=a,ttl_seconds=600))
    call('cancel',m.JobCancelParameters(kind='job.cancel',job_id=j))
    call('release',m.AllocationTargetParameters(kind='allocation.release',allocation_id=a))
    return a,j
nodes=json.loads((SOURCE/'inventory.json').read_text())['nodes']
full=json.load(gzip.open(SOURCE/'final_state.json.gz','rt'))
protocol={'build':current_build(ROOT),'purpose':'Causal mechanism diagnosis, not acceptance or a throughput claim','source_evidence':{'state_sha256':sha(SOURCE/'final_state.json.gz'),'http_sha256':sha(SOURCE/'http_history.jsonl.gz')},'cases':[{'name':'n4_h0','nodes':4,'prior_completed_jobs':0},{'name':'n128_h0','nodes':128,'prior_completed_jobs':0},{'name':'n128_h32','nodes':128,'prior_completed_jobs':32},{'name':'n128_h256','nodes':128,'prior_completed_jobs':256,'history':'exact observed final state, no history removed'}],'per_case':'One warmup five-write lifecycle to replace initialization baseline journal head; two measured unprofiled lifecycles; one profiled lifecycle; three direct selected-job reads; one inactive real WorkflowWorker.tick','budget':'4 cases, 80 probe writes plus 160 writes to construct the h32 production history. No concurrency/TCP reference-load rerun. No production source changes. No history deleted. SQLite WAL/FULL unchanged.','environment':{'platform':platform.platform(),'python':sys.version,'logical_cpus':os.cpu_count(),'limits':'Host diagnostic, not 4vCPU/8GiB reference environment','temporary_directory':str(TEMP)},'scope':'Main-thread product Kernel/store calls and cProfile inclusive/exclusive time. No event-loop queue time measured here.'}
dump('protocol.json',protocol)

# Recompute the eight observed waves from the frozen TCP history before probing.
with gzip.open(SOURCE/'http_history.jsonl.gz','rt') as h: rows=[json.loads(line) for line in h]
alloc_rows=sorted([r for r in rows if r['operation']=='allocate'],key=lambda r:r['started_offset_seconds'])
aw={r['response']['id']:i//32 for i,r in enumerate(alloc_rows)}
ja={jid:j['allocation_id'] for jid,j in full['jobs'].items()}
wave_rows=collections.defaultdict(list)
for r in rows:
    op=r['operation']
    aid=r['response']['id'] if op=='allocate' else r['body']['allocation_id'] if op=='create_job' else r['path'].split('/')[3] if op in ('renew','release') else ja[r['path'].split('/')[3]]
    wave_rows[aw[aid]].append(r)
def q95(xs): return sorted(xs)[max(0,__import__('math').ceil(.95*len(xs))-1)]
waves=[]
for w,rr in sorted(wave_rows.items()):
    start=min(r['started_offset_seconds'] for r in rr); end=max(r['started_offset_seconds']+r['elapsed_ms']/1000 for r in rr)
    waves.append({'wave':w+1,'prior_completed_jobs':w*32,'requests':len(rr),'wall_span_s':end-start,'mean_ms':statistics.mean(r['elapsed_ms'] for r in rr),'p95_ms':q95([r['elapsed_ms'] for r in rr]),'operations':{op:{'count':len(xx),'p95_ms':q95(xx),'mean_ms':statistics.mean(xx)} for op in sorted({r['operation'] for r in rr}) if (xx:=[r['elapsed_ms'] for r in rr if r['operation']==op])}})
dump('observed_http_waves.json',{'build':protocol['build'],'source_sha256':sha(SOURCE/'http_history.jsonl.gz'),'waves':waves,'limitations':'Client-observed elapsed time includes queueing, server work and response transport. No per-stage server timestamps exist, so these values alone do not identify a causal percentage.'})

seed=Kernel(TEMP/'seed32.sqlite3',inventory=tuple(m.NodeInventory.model_validate_json(json.dumps(n)) for n in nodes))
for i in range(32): flow(seed,i)
state32=seed.store.read(); seed.store.close()
results=[]
for spec in protocol['cases']:
    name=spec['name']; path=TEMP/(name+'.sqlite3')
    if spec['prior_completed_jobs']:
        state=state32 if spec['prior_completed_jobs']==32 else full
        store=SQLiteStore(path); store.initialize(state); store.close()
        k=Kernel(path)
    else:
        k=Kernel(path,inventory=tuple(m.NodeInventory.model_validate_json(json.dumps(n)) for n in nodes[:spec['nodes']]))
    initial=k.store.read()
    if len(initial['jobs'])!=spec['prior_completed_jobs']: raise RuntimeError('Unexpected seed history')
    # Real durable warmup; it preserves all existing history and changes only this new temporary DB.
    flow(k,1000)
    base=k.store.read()
    phase=collections.defaultdict(list)
    def wrap(fn,label):
        def invoke(*args,**kwargs):
            start=time.perf_counter()
            try: return fn(*args,**kwargs)
            finally: phase[label].append((time.perf_counter()-start)*1000)
        return invoke
    for method in ('load','save','_append_journal'):
        setattr(k.store,method,wrap(getattr(k.store,method),'store.'+method))
    for method in ('_apply','_schedule','_inventory'):
        setattr(k,method,wrap(getattr(k,method),'kernel.'+method))
    orig_transaction=k.store.transaction
    @contextlib.contextmanager
    def transaction():
        t=time.perf_counter()
        with orig_transaction() as connection:
            phase['transaction.enter_connect_begin'].append((time.perf_counter()-t)*1000)
            try: yield connection
            finally: t=time.perf_counter()
        phase['transaction.exit_commit_close'].append((time.perf_counter()-t)*1000)
    k.store.transaction=transaction
    orig_choose=kernel_module.choose_placement
    kernel_module.choose_placement=wrap(orig_choose,'choose_placement')
    measurements=[]
    def measurement(op,call,profile=False):
        phase.clear(); p=cProfile.Profile() if profile else None
        wall=time.perf_counter(); cpu=time.process_time()
        if p: p.enable()
        value=call()
        if p: p.disable()
        data={'operation':op,'profiled':profile,'wall_ms':(time.perf_counter()-wall)*1000,'process_cpu_ms':(time.process_time()-cpu)*1000,'phase_inclusive_ms':{key:sum(values) for key,values in phase.items()},'phase_call_counts':{key:len(values) for key,values in phase.items()}}
        if p:
            target=OUT/(name+'-'+op+'.pstats'); p.dump_stats(str(target))
            stats=pstats.Stats(p)
            data['profile_total_seconds']=stats.total_tt
            all_functions=[{'file':key[0],'line':key[1],'function':key[2],'primitive_calls':v[0],'calls':v[1],'exclusive_s':v[2],'inclusive_s':v[3]} for key,v in stats.stats.items()]
            data['top_inclusive']=sorted(all_functions,key=lambda x:x['inclusive_s'],reverse=True)[:25]
            data['top_exclusive']=sorted(all_functions,key=lambda x:x['exclusive_s'],reverse=True)[:25]
            dump(name+'-'+op+'-profile.json',data)
        measurements.append(data)
        return value
    for n in range(2): flow(k,1001+n,lambda op,call:measurement(op,call))
    _,jid=flow(k,1003,lambda op,call:measurement(op,call,True))
    query=m.ReadQuery(actor=actor(1003),entities=(m.EntityRef(kind='job',entity_id=jid),))
    for _ in range(3): measurement('selected_job_read',lambda:k.read_snapshot(query))
    measurement('inactive_worker_tick',lambda:WorkflowWorker(k).tick(),True)
    kernel_module.choose_placement=orig_choose
    end=k.store.read()
    if len(end['jobs'])!=spec['prior_completed_jobs']+4: raise RuntimeError('History count not preserved')
    if end['events'][:len(initial['events'])]!=initial['events']: raise RuntimeError('Existing events changed')
    for key,value in initial['jobs'].items():
        if end['jobs'][key]!=value: raise RuntimeError('Historical job changed')
    with k.store._connect() as c:
        pragmas={'synchronous':c.execute('PRAGMA synchronous').fetchone()[0],'journal_mode':c.execute('PRAGMA journal_mode').fetchone()[0]}
    results.append({'name':name,'registered_nodes':spec['nodes'],'initial_jobs':len(initial['jobs']),'measured_baseline_jobs':len(base['jobs']),'initial_events':len(initial['events']),'baseline_json_bytes':len(json.dumps(base,separators=(',',':')).encode()),'end_jobs':len(end['jobs']),'history_preserved':True,'pragmas':pragmas,'measurements':measurements})
    k.store.close()
    print(json.dumps({'case':name,'baseline_bytes':results[-1]['baseline_json_bytes'],'unprofiled_write_median_ms':statistics.median(x['wall_ms'] for x in measurements if not x['profiled'] and x['operation']!='selected_job_read'),'read_median_ms':statistics.median(x['wall_ms'] for x in measurements if x['operation']=='selected_job_read')}),flush=True)
dump('kernel_probe_results.json',{'build':protocol['build'],'build_at_finish':current_build(ROOT),'protocol_sha256':sha(OUT/'protocol.json'),'diagnostic_script_sha256':sha(Path(__file__)),'cases':results,'limitations':['Small single-thread host probes, not a CP4 acceptance result.','cProfile adds overhead; compare unprofiled times separately.','Inclusive phases overlap and must not be summed as independent percentages.','The 256-job seed is the full observed authoritative state; local initialization records it as a journal baseline. One warmup lifecycle removes the large initialization record from the latest journal head before measurement.','No pending competition policy P or real GPU compute was part of the frozen load.']})
