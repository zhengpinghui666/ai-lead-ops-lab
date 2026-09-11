'use strict';
const learning=require('../captcha_learning.cjs');
(async()=>{
  const command=process.argv[2]||'status';
  if(command==='enable'||command==='disable')await learning.configure(command==='enable');
  else if(command!=='status')throw Error('Use: node scripts/captcha-learning.cjs enable|disable|status');
  console.log(JSON.stringify(await learning.status(),null,2));
})().catch(error=>{console.error(error.message);process.exitCode=1;});
