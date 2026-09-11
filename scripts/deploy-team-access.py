"""Deploy the explicitly requested public workbench using existing own-account authorization."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
import runtime
import team_access

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--account',required=True);parser.add_argument('--output',required=True);parser.add_argument('--pages-only',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-f0-9]{32}',args.account):raise ValueError('账号格式无效')
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    node,_=runtime.dependencies();cli=BASE/'integrations/login-relay/node_modules/wrangler/bin/wrangler.js'
    cwd=BASE/'integrations/team-access';config=cwd/'wrangler.jsonc';pages=cwd/'pages/wrangler.jsonc'
    env={**os.environ,'CLOUDFLARE_ACCOUNT_ID':args.account,'WRANGLER_SEND_METRICS':'false'}
    def run(step,arguments,payload=None):
        result=subprocess.run([node,str(cli),*arguments],cwd=cwd,input=payload,capture_output=True,text=True,encoding='utf-8',timeout=180,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        # Keep only credential-free summary fields, never raw request/debug output.
        text=result.stdout+'\n'+result.stderr
        codes=re.findall(r'\[code:\s*(\d+)\]',text)
        receipt={'step':step,'returncode':result.returncode,'codes':codes,'urls':re.findall(r'https://[a-z0-9.-]+\.pages\.dev',text),'version_ids':re.findall(r'Current Version ID:\s*([a-f0-9-]+)',text)}
        (output/(step+'.json')).write_text(json.dumps(receipt,indent=2),encoding='utf-8');print(json.dumps(receipt),flush=True)
        if result.returncode:raise RuntimeError('部署步骤未完成：'+step)
        return result.stdout
    try:private=team_access.load()
    except FileNotFoundError:
        private={'origin':'https://clubops-team-4ff187.pages.dev','token':secrets.token_urlsafe(48),'enabled':False};team_access.save(private)
    if not args.pages_only:
        run('dry-run',['deploy','--config',str(config),'--dry-run'])
        run('worker-deploy',['deploy','--config',str(config)])
        run('connector-secret',['secret','bulk','--config',str(config)],json.dumps({'CONNECTOR_TOKEN':private['token']}))
    projects=json.loads(run('pages-list',['pages','project','list','--json']))
    if not any(p.get('name')=='clubops-team-4ff187' for p in projects):
        run('pages-create',['pages','project','create','clubops-team-4ff187','--production-branch','main','--force'])
    run('pages-deploy',['pages','deploy',str(cwd/'pages/public'),'--cwd',str(pages.parent),'--project-name','clubops-team-4ff187','--branch','main','--commit-dirty=true'])
    private['enabled']=True;team_access.save(private)
    result={'origin':private['origin'],'access':'public_no_login','connector_enabled':True,'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [cwd/'gateway.mjs',BASE/'team_access.py',BASE/'server.py']}}
    (output/'deployment.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)

if __name__=='__main__':main()
