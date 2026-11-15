from pathlib import Path
import os, subprocess, json, time
root=Path.cwd()
out=root/'evidence/reliability-placement-20261115'
python=root/'.venv/bin/python'
base_env={k:v for k,v in os.environ.items() if not k.startswith(('FABRIC_','VITE_'))}
base_env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONPATH='src:.')
base_env['PATH']='/Users/xuyang/Documents/GitHub/campus-gpu-fabric/runtime/b-tools/node/node_modules/.bin'+os.pathsep+base_env['PATH']
runs=[
 ('session-other-three', ['tests/blackbox/test_cp2_joint_session_owned.py', '-k', 'not test_two_mode_session_lifecycle_export_and_repeated_start_preserve_data']),
 ('cpu-preview-only', ['tests/blackbox/test_cp2_joint_cpu_owned.py::test_cpu_preview_factory_starts_real_paced_worker_and_cancels_safely']),
 ('remaining-regression', ['--ignore=tests/blackbox/test_cp2_joint_session_owned.py','--ignore=tests/blackbox/test_cp2_joint_cpu_owned.py']),
]
results=[]
for label,args in runs:
 log=out/f'{label}-clean.log'
 if log.exists():raise RuntimeError(f'Preserving prior evidence: {log}')
 command=[str(python),'-m','pytest',*args]
 before=time.monotonic()
 with log.open('w') as stream:
  result=subprocess.run(command,cwd=root,env=base_env,stdout=stream,stderr=subprocess.STDOUT)
 record=dict(label=label,returncode=result.returncode,seconds=round(time.monotonic()-before,2),command=command,log=log.name)
 results.append(record)
 (out/'remaining-regression-results.json').write_text(json.dumps({'environment':'New process per group; initial FABRIC_/VITE_ removed, PYTHONPATH=src:., existing .venv Python 3.12.16, Node 22. No isolation plugin and no source changes.', 'results':results},indent=2)+'\n')
 print(label,result.returncode,record['seconds'],flush=True)
 print('\n'.join(log.read_text().splitlines()[-5:]),flush=True)
