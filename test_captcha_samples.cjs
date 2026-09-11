'use strict';
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path'),assert=require('node:assert/strict');
const {save}=require('./captcha_samples.cjs');
(async()=>{
  const dataDir=await fs.mkdtemp(path.join(os.tmpdir(),'clubops-samples-'));
  const config={profile_dir:path.join(dataDir,'browser-profile'),id:34};
  const image=await fs.readFile(path.join(__dirname,'tests/fixtures/captcha/point-pair.png'));
  const challenge={payload:{method:'same_shape_pair',image:image.toString('base64')},secret:'DO_NOT_STORE'};
  const settings={dataDir,now:1000};
  assert.equal(await save(challenge,config,settings),false);
  await fs.mkdir(path.join(dataDir,'private'));
  async function flag(extra){await fs.writeFile(path.join(dataDir,'private/captcha-sampling.json'),JSON.stringify({enabled:true,created_at:900,expires_at:1500,limit:1,id:'sample-test',...extra}));}
  await flag({expires_at:1000});assert.equal(await save(challenge,config,settings),false);
  await flag({id:'../escape'});assert.equal(await save(challenge,config,settings),false);
  await flag({});assert.equal(await save(challenge,{...config,profile_dir:'synthetic-only'},settings),false);
  assert.equal(await save(challenge,config,settings),true);
  assert.equal(await save(challenge,config,settings),false);
  const other={payload:{method:'same_shape_pair',image:Buffer.concat([image,Buffer.from('different')]).toString('base64')}};
  assert.equal(await save(other,config,settings),false);
  const files=await fs.readdir(path.join(dataDir,'private/sample-test'));
  assert.equal(files.length,2);
  const meta=JSON.parse(await fs.readFile(path.join(dataDir,'private/sample-test',files.find(n=>n.endsWith('.json'))),'utf8'));
  assert.deepEqual(Object.keys(meta).sort(),['captured_at','image_sha256','prompt','task_id']);
  console.log('PASS: disabled, expiry, path validation, profile isolation, save, deduplication, limit and metadata whitelist');
  // Only the fresh directory created above, never a user-supplied recursive target.
  assert.ok(path.resolve(dataDir).startsWith(path.resolve(os.tmpdir())+path.sep));
  await fs.rm(dataDir,{recursive:true,force:true});
})().catch(error=>{console.error(error);process.exitCode=1;});
