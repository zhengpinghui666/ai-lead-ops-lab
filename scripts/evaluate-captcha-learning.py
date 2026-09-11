"""Offline replay of the private evidence store; never submits to a platform.

Earlier recovered coordinates are weak workflow feedback, not independent human
labels. Disagreement requires review; an offline prediction is not a pass rate.
"""
import argparse,base64,hashlib,itertools,json,math,re,sys,time
from datetime import datetime
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
SCHEMA='clubops-captcha-learning-v1'

def distance(a,b):
    if not a or not b or len(a)!=len(b):return None
    return min(max(math.dist(x,y) for x,y in zip(a,order)) for order in itertools.permutations(b))

def evaluate(root):
    from captcha_engine import solve
    archive=root/'private/captcha-learning';references={};invalid_attempts=0
    for file in sorted((archive/'attempts').glob('*.json'))[:2000]:
        try:row=json.loads(file.read_text(encoding='utf-8'))
        except (ValueError,OSError):invalid_attempts+=1;continue
        if not isinstance(row,dict):invalid_attempts+=1;continue
        if row.get('schema')!=SCHEMA or row.get('outcome')!='read_recovered' or row.get('passed') is not True or row.get('platform_verdict')!='passed' or row.get('read_recovered') is not True or row.get('submissions')!=1:continue
        p=row.get('prediction') or {}
        coordinates=p.get('points') or ([p['target']] if p.get('target') else [])
        if coordinates:references.setdefault(row.get('case_id'),[]).append(coordinates)
    rows=[]
    for file in sorted((archive/'cases').glob('*.json'))[:500]:
        if not re.fullmatch(r'[a-f0-9]{64}\.json',file.name):continue
        case_id=file.stem;row={'case_id':case_id,'method':'unknown'}
        try:
            case=json.loads(file.read_text(encoding='utf-8'));row['method']=case.get('method')
            if case.get('schema')!=SCHEMA or case.get('case_id')!=case_id:raise ValueError('invalid_case')
            images={}
            for item in case['images']:
                if not re.fullmatch(case_id+r'-(?:image|target|background|image_[01])\.png',item['file']):raise ValueError('invalid_image_path')
                target=archive/'cases'/item['file']
                if target.is_symlink() or target.stat().st_size>1024*1024:raise ValueError('invalid_image')
                raw=target.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('image_hash_mismatch')
                images[item['role']]=base64.b64encode(raw).decode('ascii')
            if case['method']=='slide_match':payload={'method':'slide_match','target_image':images['target'],'background_image':images['background']}
            elif case['method']=='same_shape_pair':payload={'method':'same_shape_pair','image':images['image']}
            else:
                row.update(status='needs_review',reason='unsupported_type',assessment='adapter_needed');rows.append(row);continue
            start=time.monotonic();prediction=solve(payload)
            row.update(status=prediction['status'],reason=prediction.get('reason'),elapsed_ms=round(1000*(time.monotonic()-start)))
            value=prediction.get('result') or {};coordinates=value.get('points') or ([value['target']] if value.get('target') else [])
            prior=references.get(case_id,[])
            row['has_recovered_reference']=bool(prior)
            if prior:
                differences=[distance(coordinates,reference) for reference in prior]
                row['reference_max_distance_pixels']=max(differences) if all(d is not None for d in differences) else None
                row['assessment']='reference_agreement' if all(d is not None and d<=2 for d in differences) else 'reference_disagreement_needs_review'
            else:row['assessment']='candidate_needs_platform_validation' if prediction['status']=='predicted' else 'unresolved'
        except (ValueError,KeyError,OSError,TypeError,AttributeError):
            row.update(status='needs_review',reason='invalid_case',assessment='invalid_evidence')
        rows.append(row)
    disagreements=sum(r['assessment']=='reference_disagreement_needs_review' for r in rows)
    return {'evaluated_at':datetime.now().astimezone().isoformat(),'scope':'offline private replay; no platform requests or weight updates',
        'label_basis':'earlier explicit platform pass plus read recovery; not independent ground truth','cases':len(rows),'invalid_attempt_records':invalid_attempts,
        'locally_predicted':sum(r['status']=='predicted' for r in rows),'reference_disagreements':disagreements,
        'promotion_gate':'review_required' if disagreements or invalid_attempts or not references or any(r['assessment']=='invalid_evidence' for r in rows) else 'references_preserved',
        'platform_pass_rate':None,'source_sha256':{n:hashlib.sha256((BASE/n).read_bytes()).hexdigest() for n in ('captcha_engine.py','captcha_point.py','captcha_slider.py')},'rows':rows}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',type=Path,default=BASE/'data');parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    def restrict(event,_):
        if event in ('socket.connect','socket.connect_ex','subprocess.Popen'):raise RuntimeError('Offline evaluator has no transport')
    sys.addaudithook(restrict)
    report=evaluate(args.data_dir.resolve());target=args.report or args.data_dir/'private/captcha-learning/evaluation.json'
    target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('rows','source_sha256')},ensure_ascii=False))
