"""Explicit own-relay acceptance, with synthetic codes only and no SMS/browser."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import secrets
import sys
import time
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import login_relay


def main(check_expiry=False, result=None):
    if result is None: result={}
    spec=importlib.util.spec_from_file_location('relay_deploy', BASE/'scripts/deploy-login-relay.py')
    deployment=importlib.util.module_from_spec(spec);spec.loader.exec_module(deployment)
    deployment.require_idle()
    private=login_relay.load();origin=private['origin']
    assert origin, 'No bound relay'
    result.update({'origin':origin,'started_at':time.time(),'channel':'computer_https_synthetic',
            'network':login_relay.network(),'iphone_tested':False,'real_sms_login_tested':False,'checks':{}})

    def request(role, route, body=None):
        # Record only fixed route/role labels. Never capture payloads, headers,
        # responses or exception text in a failed live-check receipt.
        result['last_request']={'role':role or 'anonymous','route':route}
        channel=login_relay.connection(origin)
        headers={'Content-Type':'application/json'}
        if role:headers['Authorization']='Bearer '+private[role+'_token']
        try:
            channel.request('GET' if body is None else 'POST','/v1/'+route,
                            body=None if body is None else json.dumps(body).encode(),headers=headers)
            response=channel.getresponse();raw=response.read(4097)
            result['last_request']['http_status']=response.status
            assert len(raw)<=4096, 'Response too large'
            return response.status,json.loads(raw)
        finally:channel.close()

    def require(condition, name):
        if not condition:
            result['failed_check']=name
            raise ValueError('Relay acceptance failed')
        result['checks'][name]=True

    require(request(None,'health')==(401,{'error':'unauthorized'}),'anonymous_denied')
    require(request('phone','health')==(404,{'error':'not_found'}),'phone_cannot_read_backend_health')
    require(request('backend','pending')==(404,{'error':'not_found'}),'backend_cannot_use_phone_route')
    job_id=uuid.uuid4().hex
    try:
        status,created=request('backend','login',{'id':job_id,'account':'clubops-synthetic-probe'})
        require(status==201 and created.get('id')==job_id,'created_synthetic_job')
        status,pending=request('phone','pending')
        require(status==200 and set(pending)=={'id','account','created_at','expires_at'}
                and pending['id']==job_id and pending['expires_at']-pending['created_at']==180000,'metadata_and_180_seconds')
        body={'id':job_id,'code':f'{secrets.randbelow(1000000):06d}','received_at':pending['created_at']-1}
        require(request('phone','otp',body)==(409,{'error':'stale_otp'}),'stale_code_denied')
        body['received_at']=int(time.time()*1000)+60000
        require(request('phone','otp',body)==(409,{'error':'stale_otp'}),'future_code_denied')
        body['received_at']=int(time.time()*1000)
        require(request('backend','otp',body)==(404,{'error':'not_found'}),'backend_cannot_submit_phone_code')
        require(request('phone','take',{'id':job_id})==(404,{'error':'not_found'}),'phone_cannot_take_code')
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda _:request('phone','otp',body),range(2)))
        require(sorted(code for code,_ in responses)==[200,409]
                and (409,{'error':'already_received'}) in responses,'concurrent_submit_once')
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda _:request('backend','take',{'id':job_id}),range(2)))
        values=[value for status,value in responses if status==200 and value.get('status')=='received']
        require(len(values)==1 and values[0].get('code')==body['code'] and values[0].get('id')==job_id
                and (200,{'status':'taken'}) in responses,'concurrent_take_once')
        body=None;values=None;responses=None
    finally:
        status,cleared=request('backend','cancel',{'id':job_id})
        require((status,cleared)==(200,{'status':'cleared'}),'cancel_clears_job')
    require(request('phone','pending')==(200,{'id':None}),'cancelled_job_absent')
    print('Cloud role, timestamp, concurrent submit/take and cancel checks passed.',flush=True)
    if check_expiry:
        job_id=uuid.uuid4().hex
        try:
            status,created=request('backend','login',{'id':job_id,'account':'clubops-expiry-probe'})
            require(status==201 and created.get('id')==job_id,'created_expiry_probe')
            # Wait the full TTL after the creation response, independent of
            # wall-clock skew between this computer and Cloudflare.
            wait_started=time.monotonic()
            result['expiry_probe']={'ttl_seconds':180,'local_ahead_estimate_seconds':round(time.time()-(created['expires_at']/1000-180),3)}
            deadline=wait_started+182
            while time.monotonic()<deadline:
                left=max(0,deadline-time.monotonic())
                print('Expiry probe running; seconds remaining: '+str(round(left)),flush=True)
                time.sleep(min(50,left))
            result['expiry_probe']['elapsed_seconds']=round(time.monotonic()-wait_started,3)
            require(request('phone','pending')==(200,{'id':None}),'expired_job_absent')
            body={'id':job_id,'code':f'{secrets.randbelow(1000000):06d}','received_at':int(time.time()*1000)}
            require(request('phone','otp',body)==(409,{'error':'no_matching_login'}),'expired_submit_denied')
            require(request('backend','take',{'id':job_id})==(409,{'error':'no_matching_login'}),'expired_take_denied')
            body=None
        finally:
            request('backend','cancel',{'id':job_id})
    result['completed_at']=time.time();result['status']='passed'
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify',action='store_true',required=True)
    parser.add_argument('--expiry',action='store_true',help='Include the real 180-second expiry wait')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.out.exists():raise SystemExit('Choose a new evidence path')
    result={}
    try:
        main(args.expiry,result)
    except Exception as error:
        result.update(status='incomplete',completed_at=time.time(),error_type=type(error).__name__)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result['status']!='passed':raise SystemExit(2)
