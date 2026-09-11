'use strict';
// Local, bounded evidence collection. Feedback never changes model weights or
// relaxes verification gates. Unsupported captures retain metadata only.
const fs=require('node:fs/promises'),path=require('node:path'),{createHash}=require('node:crypto');
const hash=raw=>createHash('sha256').update(raw).digest('hex');
const UUID=/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i;
const SHA=/^[a-f0-9]{64}$/;
const SCHEMA='clubops-captcha-learning-v1';
const DEFAULTS={enabled:false,max_cases:200,max_attempts:1000,max_bytes:128*1024*1024};
const dataRoot=options=>path.resolve(options?.dataDir||process.env.CLUBOPS_DATA_DIR||path.join(__dirname,'data'));
const paths=root=>({settings:path.join(root,'private/captcha-learning.json'),archive:path.join(root,'private/captcha-learning')});
async function settings(root){
  let raw;try{raw=await fs.readFile(paths(root).settings,'utf8');}catch(error){if(error.code==='ENOENT')return {...DEFAULTS};throw error;}
  const value=JSON.parse(raw);
  if(typeof value.enabled!=='boolean'||!Number.isInteger(value.max_cases)||value.max_cases<1||value.max_cases>500||
     !Number.isInteger(value.max_attempts)||value.max_attempts<1||value.max_attempts>2000||
     !Number.isInteger(value.max_bytes)||value.max_bytes<1024||value.max_bytes>256*1024*1024)throw Error('invalid_settings');
  return value;
}
async function configure(enabled,options={}){
  if(typeof enabled!=='boolean')throw Error('invalid_settings');
  const root=dataRoot(options);let previous;try{previous=await settings(root);}catch{previous=DEFAULTS;}
  const value={...previous,enabled};
  await fs.mkdir(path.join(root,'private'),{recursive:true});
  await fs.writeFile(paths(root).settings,JSON.stringify(value));return value;
}
async function withLock(archive,operation){
  await fs.mkdir(archive,{recursive:true});
  if((await fs.lstat(archive)).isSymbolicLink())return null;
  const file=path.join(archive,'.lock');let lock;
  try{lock=await fs.open(file,'wx');}
  catch(error){
    if(error.code!=='EEXIST')return null;
    // Recover only a lock whose recorded process has exited. Live/unknown owners
    // are never displaced, and concurrent capture may simply skip evidence.
    try{
      const raw=await fs.readFile(file,'utf8'),owner=JSON.parse(raw);
      if(!Number.isInteger(owner.pid)||owner.pid<1)return null;
      try{process.kill(owner.pid,0);return null;}catch(error){if(error.code!=='ESRCH')return null;}
      if(await fs.readFile(file,'utf8')!==raw)return null;
      await fs.unlink(file);lock=await fs.open(file,'wx');
    }catch{return null;}
  }
  try{
    await lock.writeFile(JSON.stringify({pid:process.pid}));
    for(const name of ['cases','attempts']){
      const directory=path.join(archive,name);await fs.mkdir(directory,{recursive:true});
      if((await fs.lstat(directory)).isSymbolicLink())return null;
    }
    return await operation();
  }finally{await lock.close();await fs.unlink(file).catch(()=>{});}
}
async function inventory(archive){
  const cases=(await fs.readdir(path.join(archive,'cases')).catch(()=>[]));
  const attempts=(await fs.readdir(path.join(archive,'attempts')).catch(()=>[])).filter(n=>UUID.test(n.replace(/\.json$/,''))&&n.endsWith('.json'));
  let bytes=0;for(const file of cases)bytes+=(await fs.lstat(path.join(archive,'cases',file))).size;
  return {cases:cases.filter(n=>/^[a-f0-9]{64}\.json$/.test(n)).length,attempts,bytes};
}
function images(challenge){
  const p=challenge?.payload||challenge?.evidence_payload,method=p?.method;
  if(!['slide_match','same_shape_pair','observed_images'].includes(method))return {method:'unsupported',items:[]};
  if(method==='observed_images'&&(!Array.isArray(p.images)||!p.images.length||p.images.length>2))throw Error('invalid_image');
  const entries=method==='slide_match'?[['target',p.target_image],['background',p.background_image]]:
    method==='observed_images'?p.images.map((image,index)=>['image_'+index,image]):[['image',p.image]];
  const items=entries.map(([role,encoded])=>{
    if(typeof encoded!=='string'||encoded.length>1398104||!/^[A-Za-z0-9+/]*={0,2}$/.test(encoded))throw Error('invalid_image');
    const raw=Buffer.from(encoded,'base64');
    if(raw.length<24||raw.length>1024*1024||!raw.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])))throw Error('invalid_image');
    const width=raw.readUInt32BE(16),height=raw.readUInt32BE(20);
    if(!width||!height||width*height>4000000||Math.max(width,height)>4096)throw Error('invalid_image');
    return {role,raw,sha256:hash(raw),width,height,bytes:raw.length};
  });
  return {method,items};
}
const atom=value=>typeof value==='string'&&/^[a-zA-Z0-9_-]{1,100}$/.test(value)?value:undefined;
let sourceVersion;
async function version(){
  if(!sourceVersion){
    const names=['captcha_engine.py','captcha_point.py','captcha_slider.py','captcha_browser.cjs','captcha_point_browser.cjs','collector_verification.cjs','captcha_verdict.cjs','captcha_learning.cjs','requirements-captcha.txt'];
    const files={};for(const name of names)files[name]=hash(await fs.readFile(path.join(__dirname,name)));
    sourceVersion={files,digest:hash(JSON.stringify(files))};
  }
  return sourceVersion;
}
async function begin(challenge,config,attemptId,options={}){
  const root=dataRoot(options);
  if(!UUID.test(attemptId||'')||!Number.isInteger(config.id)||config.id<1||!config.profile_dir||
     path.resolve(config.profile_dir)!==path.join(root,'browser-profile'))return null;
  try{
    const limits=await settings(root);if(!limits.enabled)return null;
    const sample=images(challenge),archive=paths(root).archive;
    return await withLock(archive,async()=>{
      const count=await inventory(archive);
      if(count.attempts.length>=limits.max_attempts)return {skipped:true,reason:'attempt_capacity_reached'};
      const caseId=sample.items.length?hash(JSON.stringify([sample.method,...sample.items.map(i=>[i.role,i.sha256])])):null;
      const caseFile=caseId&&path.join(archive,'cases',caseId+'.json');
      let exists=false;if(caseFile)try{await fs.access(caseFile);exists=true;}catch{}
      if(caseId&&!exists){
        const imageBytes=sample.items.reduce((n,i)=>n+i.bytes,0);
        if(count.cases>=limits.max_cases||count.bytes+imageBytes+4096>limits.max_bytes)return {skipped:true,reason:'image_capacity_reached'};
        const record={schema:SCHEMA,case_id:caseId,method:sample.method,images:sample.items.map(({raw,...i})=>({...i,file:caseId+'-'+i.role+'.png'}))};
        for(const item of sample.items){
          const file=path.join(archive,'cases',caseId+'-'+item.role+'.png');
          try{await fs.writeFile(file,item.raw,{flag:'wx'});}
          catch(error){if(error.code!=='EEXIST'||hash(await fs.readFile(file))!==item.sha256)throw error;}
        }
        await fs.writeFile(caseFile,JSON.stringify(record),{flag:'wx'});
      }
      const record={schema:SCHEMA,attempt_id:attemptId,task_id:config.id,case_id:caseId,method:sample.method,
        adapter:atom(challenge?.adapter),captured_at:new Date().toISOString(),source_version:await version(),
        outcome:'pending',human_label:null,label_basis:'workflow_feedback_only'};
      await fs.writeFile(path.join(archive,'attempts',attemptId+'.json'),JSON.stringify(record),{flag:'wx'});
      return {root,attemptId};
    })||{skipped:true,reason:'storage_busy'};
  }catch{return {skipped:true,reason:'storage_unavailable'};} // Collection can continue with a diagnostic.
}
function predictionFields(value){
  if(!value||!['predicted','needs_review'].includes(value.status))return undefined;
  const result={status:value.status,reason:atom(value.reason),coordinate_type:atom(value.coordinate_type),prediction_basis:atom(value.prediction_basis),image_sha256:SHA.test(value.image_sha256||'')?value.image_sha256:undefined};
  for(const key of ['matching_route','solver_version'])result[key]=atom(value[key]);
  for(const key of ['elapsed_ms','match_score','peak_margin','pair_score','pair_margin'])if(Number.isFinite(value[key]))result[key]=value[key];
  for(const key of ['background_size','target_size','image_size']){
    const size=value[key];if(Array.isArray(size)&&size.length===2&&size.every(n=>Number.isInteger(n)&&n>0&&n<=4096))result[key]=size;
  }
  for(const key of ['target','points']){
    const p=value.result?.[key];
    const valid=point=>Array.isArray(point)&&point.length===2&&point.every(n=>Number.isFinite(n)&&n>=0&&n<=4096);
    if(key==='target'&&valid(p))result.target=p;
    if(key==='points'&&Array.isArray(p)&&p.length===2&&p.every(valid))result.points=p;
  }
  return result;
}
async function finish(receipt,value){
  if(!receipt||!UUID.test(receipt.attemptId||''))return false;
  try{
    const archive=paths(receipt.root).archive;
    return !!await withLock(archive,async()=>{
      const file=path.join(archive,'attempts',receipt.attemptId+'.json');
      const record=JSON.parse(await fs.readFile(file,'utf8'));
      if(record.schema!==SCHEMA||record.outcome!=='pending')return false;
      const confirmed=value.platformVerdict?.status==='passed'&&['visible_platform_result','platform_response_message'].includes(value.platformVerdict?.source)&&value.submissions===1;
      const recovered=value.outcome==='read_recovered'&&confirmed&&value.readRecovered===true;
      record.outcome=recovered?'read_recovered':value.outcome==='needs_review'?'needs_review':'interrupted';
      record.platform_verdict=confirmed?'passed':value.platformVerdict?.status==='failed'?'failed':'unknown';
      record.verdict_source=atom(value.platformVerdict?.source)||'none';
      record.read_recovered=value.readRecovered===true;
      record.passed=confirmed&&recovered;
      record.reason=atom(value.reason);record.submissions=value.submissions===1?1:0;
      record.elapsed_ms=Math.min(900000,Math.max(0,Math.round(value.elapsed_ms||0)));
      record.prediction=predictionFields(value.prediction);record.finished_at=new Date().toISOString();
      const temp=file+'.tmp';await fs.writeFile(temp,JSON.stringify(record));await fs.rename(temp,file);
      if(SHA.test(record.case_id||'')){
        const caseFile=path.join(archive,'cases',record.case_id+'.json'),sample=JSON.parse(await fs.readFile(caseFile,'utf8'));
        sample.latest_result=record.passed?'passed':'not_passed';sample.latest_attempt_id=record.attempt_id;
        sample.latest_platform_verdict=record.platform_verdict;sample.latest_reason=record.reason;sample.updated_at=record.finished_at;
        await fs.writeFile(caseFile+'.tmp',JSON.stringify(sample));await fs.rename(caseFile+'.tmp',caseFile);
      }
      return true;
    });
  }catch{return false;}
}
async function status(options={}){
  const root=dataRoot(options);let limits;try{limits=await settings(root);}catch{limits=DEFAULTS;}
  const archive=paths(root).archive,count=await inventory(archive),methods={},outcomes={},reasons={};
  for(const file of count.attempts){
    try{
      const r=JSON.parse(await fs.readFile(path.join(archive,'attempts',file),'utf8'));if(r.schema!==SCHEMA)continue;
      methods[r.method]=(methods[r.method]||0)+1;outcomes[r.outcome]=(outcomes[r.outcome]||0)+1;
      if(r.reason)reasons[r.reason]=(reasons[r.reason]||0)+1;
    }catch{}
  }
  return {...limits,cases:count.cases,attempts:count.attempts.length,image_bytes:count.bytes,methods,outcomes,reasons,
    capacity_reached:count.cases>=limits.max_cases||count.attempts.length>=limits.max_attempts||count.bytes+4096>=limits.max_bytes};
}
async function recalled(challenge,config,options={}){
  const root=dataRoot(options);
  if(!config.profile_dir||path.resolve(config.profile_dir)!==path.join(root,'browser-profile'))return null;
  try{
    if(!(await settings(root)).enabled)return null;
    const sample=images(challenge);
    if(!['slide_match','same_shape_pair'].includes(sample.method))return null;
    const caseId=hash(JSON.stringify([sample.method,...sample.items.map(i=>[i.role,i.sha256])])),archive=paths(root).archive;
    const summary=JSON.parse(await fs.readFile(path.join(archive,'cases',caseId+'.json'),'utf8'));
    if(summary.latest_result!=='passed')return null;
    const count=await inventory(archive),matches=[];
    for(const file of count.attempts){
      const r=JSON.parse(await fs.readFile(path.join(archive,'attempts',file),'utf8')),p=r.prediction;
      if(r.schema!==SCHEMA||r.case_id!==caseId||r.adapter!==challenge.adapter||r.outcome!=='read_recovered'||r.passed!==true||r.platform_verdict!=='passed'||r.read_recovered!==true||r.submissions!==1||p?.status!=='predicted')continue;
      const target=sample.items.find(i=>i.role==='target'),background=sample.items.find(i=>i.role==='background');
      if(sample.method==='slide_match'){
        if(p.coordinate_type!=='center_xy_in_image_pixels'||!p.target||
           JSON.stringify(p.background_size)!==JSON.stringify([background.width,background.height])||
           JSON.stringify(p.target_size)!==JSON.stringify([target.width,target.height]))continue;
        matches.push({status:'predicted',result:{target:p.target},coordinate_type:p.coordinate_type,
          background_size:p.background_size,target_size:p.target_size,image_sha256:hash(Buffer.concat([target.raw,background.raw]))});
      }else{
        const image=sample.items[0];
        if(p.coordinate_type!=='xy_in_image_pixels'||!p.points||JSON.stringify(p.image_size)!==JSON.stringify([image.width,image.height]))continue;
        matches.push({status:'predicted',result:{points:p.points},coordinate_type:p.coordinate_type,image_size:p.image_size,image_sha256:image.sha256});
      }
    }
    if(!matches.length||new Set(matches.map(m=>JSON.stringify(m))).size!==1)return null;
    return {...matches[0],prediction_basis:'exact_image_platform_confirmed'};
  }catch{return null;}
}
module.exports={begin,finish,configure,status,recalled};
