'use strict';
// Bounded local processing, not parallel platform requests. Results commit in arrival order.
class OrderedResponseQueue {
  constructor({concurrency=2,capacity=8,onError=()=>{}}={}) {
    if(!Number.isInteger(concurrency)||!Number.isInteger(capacity)||concurrency<1||capacity<concurrency)throw Error('Invalid queue limits');
    this.concurrency=concurrency;this.capacity=capacity;this.onError=onError;
    this.pending=0;this.active=0;this.waiting=[];this.tail=Promise.resolve();this.highWater=0;
  }
  submit(read,commit) {
    if(this.pending>=this.capacity)return false;
    this.pending++;this.highWater=Math.max(this.highWater,this.pending);
    // Read failures become data so an out-of-order rejection is never unhandled.
    const result=new Promise(resolve=>this.waiting.push({read,resolve}));
    this.tail=this.tail.then(async()=>{
      const outcome=await result;
      try{if(outcome.error)throw outcome.error;await commit(outcome.value);}
      catch(error){this.onError(error);}
      finally{this.pending--;}
    });
    this.pump();return true;
  }
  pump() {
    while(this.active<this.concurrency&&this.waiting.length){
      const item=this.waiting.shift();this.active++;
      Promise.resolve().then(item.read).then(value=>item.resolve({value}),error=>item.resolve({error})).finally(()=>{this.active--;this.pump();});
    }
  }
  async drain(){while(this.pending)await this.tail;}
}
module.exports={OrderedResponseQueue};
