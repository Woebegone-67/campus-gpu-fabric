/** Isolated transport tests. Fixtures below are not product runtime records. */
import assert from 'node:assert/strict';
import {afterEach, beforeEach, test} from 'node:test';
import {ApiClient, ApiError, idOf, items, versionOf} from '../src/api.ts';

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;
beforeEach(() => { globalThis.window = {setTimeout, clearTimeout}; });
afterEach(() => { globalThis.fetch = originalFetch; globalThis.window = originalWindow; });
const json = (data, status = 200) => new Response(JSON.stringify(data), {status, headers: {'Content-Type':'application/json','X-Request-Id':'transport-test'}});

test('uses only ordinary owner/token identity, never client role or test credentials', async () => {
  let sent;
  globalThis.fetch = async (path, options) => {sent = {path, options}; return json({items:[]});};
  await new ApiClient({owner:'alice',token:'test-only-token',profile:'admin'}).request('/v1/jobs');
  assert.equal(sent.options.headers['X-Owner'], 'alice');
  assert.equal(sent.options.headers.Authorization, 'Bearer test-only-token');
  assert.equal(sent.options.headers['X-Role'], undefined);
  assert.equal(sent.options.headers['X-Test-Key'], undefined);
  assert.equal(sent.options.cache, 'no-store');
});

test('administrator token without owner omits X-Owner rather than sending an empty value', async () => {
  let headers;
  globalThis.fetch = async (_path, options) => {headers = options.headers; return json({items:[]});};
  await new ApiClient({owner:'',token:'test-admin-token'}).request('/x/v1/acceptance');
  assert.equal(Object.hasOwn(headers,'X-Owner'), false);
});

test('product client refuses test-control paths before issuing a request', async () => {
  let called = false;
  globalThis.fetch = async () => {called = true; return json({});};
  await assert.rejects(new ApiClient({owner:'alice',token:''}).request('/_test/reset'), /不支持/);
  assert.equal(called,false);
});

test('HTTP 503 preserves unavailable reason and request ID', async () => {
  globalThis.fetch = async () => json({error:'dependency_unavailable',detail:'ReadPort absent'},503);
  await assert.rejects(new ApiClient({owner:'alice',token:''}).request('/v1/jobs'), error => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status,503);assert.equal(error.requestId,'transport-test');
    assert.equal(error.message,'ReadPort absent');return true;
  });
});

test('an HTTP 200 rejected receipt is still a failed business operation', async () => {
  globalThis.fetch = async () => json({receipt:{outcome:'rejected',error:{detail:'stale version'}}});
  await assert.rejects(new ApiClient({owner:'alice',token:''}).request('/x/v1/work-intents/work/actions',{method:'POST',body:{action:'create_job'}}), error=>error instanceof ApiError && error.status===409 && error.message==='stale version');
});

test('202 result stays accepted, does not manufacture running or completion status', async () => {
  const receipt={outcome:'committed',request_id:'intent-test',objects:[{kind:'intent',entity_id:'work-test'}]};
  globalThis.fetch=async()=>json(receipt,202);
  const result=await new ApiClient({owner:'alice',token:''}).request('/x/v1/work-intents',{method:'POST',body:{intent_id:'work-test'}});
  assert.equal(result.status,202);assert.deepEqual(result.data,receipt);assert.equal(result.data.state,undefined);
});

test('successful HTML proxy fallback cannot masquerade as API success', async () => {
  globalThis.fetch=async()=>new Response('<html>Vite fallback</html>',{status:200});
  await assert.rejects(new ApiClient({owner:'alice',token:''}).request('/v1/jobs'),error=>error instanceof ApiError && error.status===502);
});

test('lost mutation response is uncertain and releases the in-flight guard', async () => {
  const guard=[];
  globalThis.fetch=async()=>{throw new TypeError('network disconnected');};
  await assert.rejects(new ApiClient({owner:'alice',token:''},delta=>guard.push(delta)).request('/x/v1/work-intents',{method:'POST',body:{intent_id:'work-test'}}),error=>error instanceof ApiError && error.uncertain);
  assert.deepEqual(guard,[1,-1]);
});

test('public-job identity uses job ID even when sharing an allocation', () => {
  assert.equal(idOf({job_id:'job-2',allocation_id:'allocation-1'}),'job-2');
  assert.deepEqual(items({items:[null,5,{id:'real'}]}),[{id:'real'}]);
  assert.equal(versionOf({version:{epoch:'1',revision:2}}),null);
  assert.deepEqual(versionOf({version:{epoch:1,revision:2}}),{epoch:1,revision:2});
});

test('known stale rejection refreshes same work and retries with new version and stable stage',async()=>{
  const calls=[],guard=[];
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});
    if(calls.length===1)return json({error:'stale_version'},409);
    if(options.method==='GET')return json({version:{epoch:4,revision:11},item:{intent_id:'same-work'}});
    return json({receipt:{outcome:'committed'}},202);
  };
  const response=await new ApiClient({owner:'bob',token:'test-token'},delta=>guard.push(delta)).workAction('same-work','create_job',{epoch:4,revision:9},'job-v1');
  assert.equal(response.status,202);assert.equal(calls.length,3);
  const sent=calls.filter(c=>c.options.method==='POST').map(c=>JSON.parse(c.options.body));
  assert.deepEqual(sent.map(b=>b.stage_id),['job-v1','job-v1']);
  assert.deepEqual(sent.map(b=>b.expected_version),[{epoch:4,revision:9},{epoch:4,revision:11}]);
  assert.equal(guard.reduce((sum,value)=>sum+value,0),0);
  assert.ok(guard.slice(0,-1).reduce((state,value)=>{state.sum+=value;state.min=Math.min(state.min,state.sum);return state;},{sum:0,min:1}).min>0,'identity guard stays active across refresh');
});

test('stale retry is bounded and epoch changes stop replay',async()=>{
  let posts=0,reads=0;
  globalThis.fetch=async(_path,options)=>{if(options.method==='POST'){posts++;return json({receipt:{outcome:'rejected',error:{code:'stale_version'}}});}reads++;return json({version:{epoch:0,revision:reads+1}});};
  const client=new ApiClient({owner:'bob',token:''});
  await assert.rejects(client.workAction('work','allocate',{epoch:0,revision:0},'allocate-v1'),e=>e.status===409);
  assert.equal(posts,3);assert.equal(reads,2);
  posts=0;
  globalThis.fetch=async(_path,options)=>{if(options.method==='POST'){posts++;return json({error:'stale_version'},409);}return json({version:{epoch:1,revision:0}});};
  await assert.rejects(client.workAction('work','create_job',{epoch:0,revision:0},'job-v1'),e=>e.data.error==='epoch_changed');
  assert.equal(posts,1);
});

test('lifecycle actions carry the original epoch and never refresh a global revision',async()=>{
  for(const action of ['resume','cancel','release']) {
    const calls=[],guard=[];
    globalThis.fetch=async(path,options)=>{calls.push({path,options});return json({receipt:{outcome:'committed'}},202);};
    await new ApiClient({owner:'alice',token:''},delta=>guard.push(delta)).workAction('same/work',action,{epoch:7,revision:2});
    assert.equal(calls.length,1);
    assert.equal(calls[0].path,'/x/v1/work-intents/same%2Fwork/actions');
    assert.deepEqual(JSON.parse(calls[0].options.body),{action,expected_epoch:7});
    assert.deepEqual(guard,[1,1,-1,-1]);
  }
});

test('lost response and any known rejection never replay lifecycle commands',async()=>{
  for(const response of ['network','unavailable','budget','stale']) {
    let calls=0;
    globalThis.fetch=async()=>{calls++;if(response==='network')throw new TypeError('lost');return response==='unavailable'?json({error:'unavailable'},503):json({error:response==='stale'?'stale_version':'restart_budget_exhausted'},409);};
    await assert.rejects(new ApiClient({owner:'bob',token:''}).workAction('work','resume',{epoch:0,revision:0}));
    assert.equal(calls,1,response);
  }
});

test('registration retries only known stale rejection with identical stable ID/body and continuous identity guard',async()=>{
  const calls=[],guard=[];
  const registration={intent_id:'stable-work',owner_id:'bob',execution_mode:'cpu_reference',specification:{job:{image:'declared-only',command:['not-executed']}}};
  globalThis.fetch=async(path,options)=>{calls.push({path,options});return calls.length<3?json({error:'stale_version',detail:'Refresh snapshot'},409):json({outcome:'committed'},202);};
  const response=await new ApiClient({owner:'bob',token:'test-token'},delta=>guard.push(delta)).registerWork(registration);
  assert.equal(response.status,202);assert.equal(calls.length,3);
  assert.deepEqual(calls.map(c=>c.path),Array(3).fill('/x/v1/work-intents'));
  assert.deepEqual(calls.map(c=>c.options.body),Array(3).fill(JSON.stringify(registration)));
  let level=0;guard.forEach((delta,index)=>{level+=delta;if(index<guard.length-1)assert.ok(level>0,'identity remains locked between retries');});assert.equal(level,0);
});

test('registration stale rejection stops at three attempts; network/server/other conflict never replays',async()=>{
  for(const failure of ['stale','network','server','conflict']) {
    let calls=0;
    globalThis.fetch=async()=>{calls++;if(failure==='network')throw new TypeError('lost response');return failure==='server'?json({error:'internal_error'},500):json({error:failure==='stale'?'stale_version':'intent_conflict'},409);};
    await assert.rejects(new ApiClient({owner:'bob',token:''}).registerWork({intent_id:'stable-work'}));
    assert.equal(calls,failure==='stale'?3:1,failure);
  }
});

// Artifact bytes must survive JavaScript's unsafe-integer boundary.
test('raw artifact JSON preserves uint64 results independently of parsed presentation data',async()=>{
  const raw='{"state":{"step":1000000,"value":16159431991146531633}}\n';
  globalThis.fetch=async()=>new Response(raw,{headers:{'Content-Type':'application/json'}});
  const result=await new ApiClient({owner:'alice',token:'test-only'}).request('/x/v1/artifacts?uri=registered');
  assert.equal(result.raw,raw);
  assert.notEqual(JSON.stringify(result.data),raw.trim(),'parsed numbers are not safe artifact content');
});

test('browser sessions use same-origin cookies and bind business requests to the verified actor',async()=>{
  const calls=[];
  globalThis.fetch=async(path,options)=>{calls.push({path,options});return json({ok:true});};
  const client=new ApiClient({owner:'legacy-owner',token:'',actorId:'actor-chen'});
  await client.request('/x/v1/contributions/recall',{method:'POST',body:{}});
  await client.request('/x/v1/session');
  assert.equal(calls[0].options.credentials,'same-origin');
  assert.equal(calls[0].options.headers['X-Fabric-Request'],'browser');
  assert.equal(calls[0].options.headers['X-Fabric-Actor'],'actor-chen');
  assert.equal(calls[0].options.headers['X-Owner'],undefined);
  assert.equal(calls[0].options.headers.Authorization,undefined);
  assert.equal(calls[1].options.headers['X-Fabric-Actor'],undefined);
});

test('expired or switched sessions invalidate visible account state without replaying the request',async()=>{
  for(const [status,error] of [[401,'unauthorized'],[409,'account_changed']]){
    let invalidated=0,calls=0;
    globalThis.fetch=async()=>{calls++;return json({error},status);};
    const client=new ApiClient({owner:'alice',token:'',actorId:'alice'},()=>{},()=>true,()=>invalidated++);
    await assert.rejects(client.request('/x/v1/contributions/recall',{method:'POST',body:{}}),failure=>failure.status===status);
    assert.equal(invalidated,1);assert.equal(calls,1);
  }
});
