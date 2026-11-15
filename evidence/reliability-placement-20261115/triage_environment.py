from pathlib import Path
import os,subprocess,sys,tempfile,tarfile,io,json
root=Path.cwd()
out=root/'evidence/reliability-placement-20261115'
python=root/'.venv/bin/python'
sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
first='tests/blackbox/test_cp2_joint_session_owned.py::test_historical_report_configuration_survives_environment_cleanup'
second='tests/domain/test_work_display_name.py::test_invalid_name_does_not_register_work'
base_env={k:v for k,v in os.environ.items() if not k.startswith(('FABRIC_','VITE_'))}
base_env['PYTHONDONTWRITEBYTECODE']='1'
def run(label,cwd,paths,extra=None):
 env=base_env.copy();env['PYTHONPATH']=str(cwd/'src')+os.pathsep+str(cwd)
 if extra:env.update(extra)
 cmd=[str(python),'-m','pytest',*paths,'-x','-q','--tb=short','-p','no:cacheprovider']
 r=subprocess.run(cmd,cwd=cwd,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=40)
 (out/f'triage-{label}.log').write_text(r.stdout)
 print(label,r.returncode,r.stdout[-2500:])
 return {'label':label,'returncode':r.returncode,'command':cmd,'cwd':str(cwd),'log':f'triage-{label}.log'}
results=[run('clean-current',root,[second]),run('pair-current',root,[first,second])]
with tempfile.TemporaryDirectory(prefix='fabric-base-triage-') as td:
 baseline=Path(td)
 blob=subprocess.check_output(['git','archive',sha,'src','scripts','contracts','experiments','tests','pyproject.toml'])
 with tarfile.open(fileobj=io.BytesIO(blob)) as tf:tf.extractall(baseline,filter='data')
 results.append(run('pair-base-0217e45',baseline,[first,second]))
(out/'triage-baseline-results.json').write_text(json.dumps({'base':sha,'environment':'Same worktree .venv Python; clean initial FABRIC_/VITE_ environment; PYTHONPATH=archive/src:archive. No network/browser or real services.','results':results},indent=2)+'\n')
