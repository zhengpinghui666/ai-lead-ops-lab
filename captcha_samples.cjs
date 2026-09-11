'use strict';
// Explicit, expiring local development capture; disabled without the private flag.
const fs=require('node:fs/promises'),path=require('node:path'),{createHash}=require('node:crypto');
async function save(challenge,config,{dataDir=process.env.CLUBOPS_DATA_DIR||path.join(__dirname,'data'),now=Date.now()}={}){
  const root=path.resolve(dataDir);
  if(!config.profile_dir||path.resolve(config.profile_dir)!==path.join(root,'browser-profile')||challenge.payload?.method!=='same_shape_pair')return false;
  try{
    const flag=JSON.parse(await fs.readFile(path.join(root,'private/captcha-sampling.json'),'utf8'));
    if(flag.enabled!==true||!Number.isFinite(flag.created_at)||!Number.isFinite(flag.expires_at)||
       now<flag.created_at||now>=flag.expires_at||flag.expires_at-flag.created_at>15*60*1000||
       !Number.isInteger(flag.limit)||flag.limit<1||flag.limit>8||!/^sample-[a-z0-9-]{1,50}$/.test(flag.id))return false;
    const directory=path.join(root,'private',flag.id);
    await fs.mkdir(directory,{recursive:true});
    const image=Buffer.from(challenge.payload.image,'base64');
    if(image.length>1024*1024||image.length<24)return false;
    const id=createHash('sha256').update(image).digest('hex');
    const files=await fs.readdir(directory);
    if(files.includes(id+'.png')||files.filter(n=>/^[a-f0-9]{64}\.png$/.test(n)).length>=flag.limit)return false;
    await fs.writeFile(path.join(directory,id+'.png'),image,{flag:'wx'});
    await fs.writeFile(path.join(directory,id+'.json'),JSON.stringify({image_sha256:id,prompt:'same_shape_pair',
      captured_at:new Date(now).toISOString(),task_id:Number.isInteger(config.id)?config.id:null}),{flag:'wx'});
    return true;
  }catch{return false;} // Sample storage must never change collection/verification semantics.
}
module.exports={save};
