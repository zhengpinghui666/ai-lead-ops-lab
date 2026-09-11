'use strict';
// Mirrors the small wire-protocol selection contract in candidate_pool.py.
function select(rows,limit,policy){
  if(!policy)return rows.slice(0,limit);
  if(policy.version!=='candidate-rotation-v1'||!Number.isInteger(limit)||limit<1||limit>5||rows.length>50)throw Error('Invalid candidate policy');
  const history=new Map(policy.history.map(row=>[row.video_id,row]));
  const candidates=rows.map((row,index)=>({row,index})),selected=[];
  const last=item=>history.get(item.row.video_id)?.last_selected_ms??-1;
  const priority=item=>history.get(item.row.video_id)?.priority_ms??policy.as_of_ms;
  if(candidates.length&&(limit>1||policy.round%3===0)){
    const fair=[...candidates].sort((a,b)=>last(a)-last(b)||a.index-b.index)[0];
    selected.push(fair);candidates.splice(candidates.indexOf(fair),1);
  }
  candidates.sort((a,b)=>priority(a)-priority(b)||a.index-b.index);
  return selected.concat(candidates.slice(0,limit-selected.length)).map(item=>item.row);
}
module.exports={select};
