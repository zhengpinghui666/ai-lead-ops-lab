'use strict';
// Bounded page work. Settles every lane before returning; first failure stops admission.
async function runPool(items,limit,run,{check=()=>{},onActivity=async()=>{},onError=()=>{}}={}){
  if(!Number.isInteger(limit)||limit<1||limit>5)throw Error('Invalid page concurrency');
  let cursor=0,active=0,peak=0,failure;
  const outcomes=new Array(items.length);
  function remember(error){
    failure ||= error;
    // Reporting failures must not admit new work or skip the other lanes' settlement.
    try{onError(error);}catch(reportError){failure ||= reportError;}
  }
  async function lane(slot){
    while(!failure){
      try{await check();}catch(e){remember(e);return;}
      if(failure||cursor>=items.length)return;
      const index=cursor++;
      active++;peak=Math.max(peak,active);
      try{
        await onActivity({active,peak,index,slot});
        outcomes[index]=await run(items[index],index,slot);
      }catch(e){remember(e);}
      finally{
        active--;
        try{await onActivity({active,peak,index,slot});}catch(e){remember(e);}
      }
    }
  }
  const lanes=await Promise.allSettled(Array.from({length:Math.min(limit,items.length)},(_,i)=>lane(i)));
  const rejected=lanes.find(r=>r.status==='rejected');
  if(failure||rejected)throw failure||rejected.reason;
  return {outcomes,peak};
}
module.exports={runPool};
