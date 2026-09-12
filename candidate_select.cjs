'use strict';
// Mirrors the small wire-protocol selection contract in candidate_pool.py.
function select(rows,limit,policy){
  if(!policy)return rows.slice(0,limit);
  if(!['candidate-rotation-v1','candidate-vertical-rotation-v2'].includes(policy.version)||!Number.isInteger(limit)||limit<1||limit>5||rows.length>50)throw Error('Invalid candidate policy');
  const history=new Map(policy.history.map(row=>[row.video_id,row]));
  const candidates=rows.map((row,index)=>({row,index}));
  const last=item=>history.get(item.row.video_id)?.last_selected_ms??-1;
  const priority=item=>history.get(item.row.video_id)?.priority_ms??policy.as_of_ms;
  function take(pool,budget){
    const remaining=[...pool],chosen=[];
    if(budget&&remaining.length&&(budget>1||policy.round%3===0)){
      const fair=[...remaining].sort((a,b)=>last(a)-last(b)||a.index-b.index)[0];
      chosen.push(fair);remaining.splice(remaining.indexOf(fair),1);
    }
    remaining.sort((a,b)=>priority(a)-priority(b)||a.index-b.index);
    return chosen.concat(remaining.slice(0,budget-chosen.length));
  }
  const known=new Set(policy.version==='candidate-vertical-rotation-v2'?(policy.vertical_ids||[]):[]);
  const vertical=candidates.filter(item=>known.has(item.row.video_id)),ordinary=candidates.filter(item=>!known.has(item.row.video_id));
  let selected;
  if(vertical.length&&ordinary.length){
    const budget=limit===1?Number(policy.round%3!==2):Math.min(limit-1,Math.floor((limit*2+2)/3));
    selected=take(vertical,budget).concat(take(ordinary,limit-budget));
    const used=new Set(selected.map(item=>item.index));
    selected=selected.concat(take(candidates.filter(item=>!used.has(item.index)),limit-selected.length));
  }else selected=take(candidates,limit);
  return selected.map(item=>item.row);
}
function evidence(rows,selected,policy){
  const known=new Set(policy.version==='candidate-vertical-rotation-v2'?(policy.vertical_ids||[]):[]);
  return {priority_basis:policy.version==='candidate-vertical-rotation-v2'?'dispatch_asset_snapshot':'read_history_only',vertical_candidates:rows.filter(r=>known.has(r.video_id)).length,
    selected_vertical:selected.filter(r=>known.has(r.video_id)).length,
    vertical_snapshot_truncated:Boolean(policy.vertical_snapshot_truncated)};
}
module.exports={select,evidence};
