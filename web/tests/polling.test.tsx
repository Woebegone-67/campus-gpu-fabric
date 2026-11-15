/** Passive query updates use local synthetic responses only. */
import assert from 'node:assert/strict';
import {after,afterEach,beforeEach,test} from 'node:test';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/',pretendToBeVisual:true});
for(const name of ['window','document','navigator','HTMLElement','Event'])Object.defineProperty(globalThis,name,{value:dom.window[name],configurable:true,writable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {act}=React;
const {createRoot}=await import('react-dom/client');
const {ApiError}=await import('../src/api.ts');
const {QueryActivityContext,useQuery}=await import('../src/hooks.ts');
const originalSet=window.setTimeout,originalClear=window.clearTimeout,originalNow=Date.now;
let root,container,current,requests,timers,serial,now,visibility,api;
function Harness({client=api,active=true}){return <QueryActivityContext.Provider value={active}><Read client={client}/></QueryActivityContext.Provider>;}
function Read({client}){current=useQuery(client,'/x/v1/recalls',0,2000);return <p>{JSON.stringify(current.data)}</p>;}
function client(owner='alice'){
  return {identity:{owner,token:'',actorId:owner},request:(_path,options)=>new Promise((resolve,reject)=>requests.push({owner,options,resolve,reject}))};
}
async function mount(active=true,identity=api){await act(async()=>root.render(<Harness active={active} client={identity}/>));}
async function answer(index,data){await act(async()=>{requests[index].resolve({data});});}
async function fail(index,status){await act(async()=>{requests[index].reject(new ApiError('synthetic',status));});}
async function tick(){await act(async()=>{const pending=[...timers.entries()];timers.clear();for(const [,callback] of pending)callback();});}
async function setVisibility(value){await act(async()=>{visibility=value;document.dispatchEvent(new Event('visibilitychange'));});}
beforeEach(()=>{
  document.body.innerHTML='<div id="polling"></div>';container=document.getElementById('polling');root=createRoot(container);
  current=null;requests=[];timers=new Map();serial=0;now=100;visibility='visible';api=client();
  Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>visibility});
  window.setTimeout=(callback)=>{const id=++serial;timers.set(id,callback);return id;};
  window.clearTimeout=id=>timers.delete(id);
  Date.now=()=>now;
});
afterEach(async()=>{await act(async()=>root.unmount());window.setTimeout=originalSet;window.clearTimeout=originalClear;Date.now=originalNow;});
after(()=>dom.window.close());

test('passive polling retains rendered data and does not overlap or disable controls',async()=>{
  await mount();assert.equal(requests.length,1);assert.equal(current.loading,true);
  await answer(0,{now:3});assert.equal(current.receivedAt,100);assert.equal(current.loading,false);assert.equal(timers.size,1);
  await tick();assert.equal(requests.length,2);assert.equal(current.loading,false);assert.deepEqual(current.data,{now:3});
  await tick();assert.equal(requests.length,2,'no new request while the poll is pending');
  now=200;await answer(1,{now:5});assert.equal(current.receivedAt,200);assert.deepEqual(current.data,{now:5});assert.equal(timers.size,1);
});

test('hidden documents pause polling and immediately refresh on return',async()=>{
  await mount();await answer(0,{now:1});await setVisibility('hidden');assert.equal(timers.size,0);
  await tick();assert.equal(requests.length,1);await setVisibility('visible');assert.equal(requests.length,2);assert.equal(current.loading,false);
  await setVisibility('hidden');assert(requests[1].options.signal.aborted);await answer(1,{now:999});assert.deepEqual(current.data,{now:1});
  await setVisibility('visible');assert.equal(requests.length,3);await answer(2,{now:4});assert.deepEqual(current.data,{now:4});
});

test('inactive workspaces and unmounted queries leave no scheduled work',async()=>{
  await mount(false);assert.equal(requests.length,0);await setVisibility('hidden');await setVisibility('visible');assert.equal(requests.length,0);
  await mount(true);await answer(0,{now:1});assert.equal(timers.size,1);
  await mount(false);assert.equal(timers.size,0);await tick();assert.equal(requests.length,1);
  await mount(true);assert.equal(requests.length,2);await answer(1,{now:2});await act(async()=>root.unmount());assert.equal(timers.size,0);
  root=createRoot(container);
});

test('failed polls retain the last successful timestamp; revoked access clears data',async()=>{
  await mount();await answer(0,{now:1});now=900;await tick();await fail(1,503);
  assert.deepEqual(current.data,{now:1});assert.equal(current.receivedAt,100);assert.equal(current.error.status,503);assert.equal(current.loading,false);
  await tick();assert.equal(current.error.status,503,'a new poll does not temporarily hide the stale warning');await fail(2,403);
  assert.equal(current.data,null);assert.equal(current.receivedAt,null);assert.equal(current.error.status,403);
});

test('account changes abort the old read and never show its response',async()=>{
  await mount();const bob=client('bob');await mount(true,bob);assert(requests[0].options.signal.aborted);assert.equal(current.data,null);
  await answer(1,{owner:'bob'});await answer(0,{owner:'alice'});assert.deepEqual(current.data,{owner:'bob'});assert.equal(timers.size,1);
});
