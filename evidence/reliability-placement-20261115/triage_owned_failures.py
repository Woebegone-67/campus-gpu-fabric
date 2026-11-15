from pathlib import Path
import os, subprocess, tempfile, tarfile, io, json, time
root = Path.cwd()
out = root / 'evidence/reliability-placement-20261115'
python = root / '.venv/bin/python'
sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
nodebin = '/Users/xuyang/Documents/GitHub/campus-gpu-fabric/runtime/b-tools/node/node_modules/.bin'
cases = {
 'cpu': 'tests/blackbox/test_cp2_joint_cpu_owned.py::test_external_p_cpu_recall_restart_resume_auto_settlement_and_persistence',
 'session': 'tests/blackbox/test_cp2_joint_session_owned.py::test_two_mode_session_lifecycle_export_and_repeated_start_preserve_data',
}
results=[]
def run(label, cwd, test):
 env={k:v for k,v in os.environ.items() if not k.startswith(('FABRIC_', 'VITE_'))}
 env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(cwd/'src')+os.pathsep+str(cwd))
 env['PATH']=nodebin+os.pathsep+env['PATH']
 with tempfile.TemporaryDirectory(prefix='fabric-owned-pytest-') as td:
  cmd=[str(python),'-m','pytest',test,'-q','--tb=short','-p','no:cacheprovider','--basetemp',str(Path(td)/'pytest')]
  start=time.monotonic()
  with (out/f'triage-owned-{label}.log').open('w') as log:
   p=subprocess.run(cmd,cwd=cwd,env=env,text=True,stdout=log,stderr=subprocess.STDOUT,timeout=150)
  # Only preserve noncredential worker process output; never save manifests/auth/accounts or raw state.
  processlogs=[]
  for i, worker in enumerate(Path(td).rglob('process.log')):
   text=worker.read_text(errors='replace')
   processlogs.append({'path_suffix':str(worker.relative_to(td)), 'bytes':worker.stat().st_size, 'output':text})
  result={'label':label,'returncode':p.returncode,'seconds':round(time.monotonic()-start,2),'command':cmd,'cwd':str(cwd),'log':f'triage-owned-{label}.log','worker_process_logs':processlogs}
  results.append(result)
  (out/'triage-owned-results.json').write_text(json.dumps({'base':sha,'environment':'Same Python 3.12.16 .venv, Node 22, clean initial FABRIC_/VITE_; current versus git archive baseline with same web node_modules symlink. Owned throwaway localhost services only, cleanup by test finally. No product changes.','results':results},indent=2)+'\n')
  print(label,p.returncode,round(time.monotonic()-start,2),flush=True)
  print((out/f'triage-owned-{label}.log').read_text()[-400:],flush=True)
for name,test in cases.items(): run('current-'+name,root,test)
with tempfile.TemporaryDirectory(prefix='fabric-owned-base-') as td:
 baseline=Path(td)
 blob=subprocess.check_output(['git','archive',sha,'src','scripts','contracts','experiments','tests','configs','web','pyproject.toml'])
 with tarfile.open(fileobj=io.BytesIO(blob)) as tf: tf.extractall(baseline,filter='data')
 (baseline/'web/node_modules').symlink_to(root/'web/node_modules',target_is_directory=True)
 for name,test in cases.items():run('base-0217e45-'+name,baseline,test)
