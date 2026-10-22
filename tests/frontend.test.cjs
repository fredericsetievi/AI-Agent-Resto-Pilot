// These tests exercise UI logic in a minimal DOM harness, not a real browser.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const crypto = require('node:crypto');

function harness({fetchImpl=()=>new Promise(()=>{}),hash='#overview'}={}) {
  const elements=new Map();
  function element(selector) {
    if(!elements.has(selector))elements.set(selector,{textContent:'',innerHTML:'',hidden:true,disabled:false,checked:false,open:false,value:'',dataset:{},classList:{toggle(){}},addEventListener(){},querySelector:element,querySelectorAll:()=>[],setAttribute(){},removeAttribute(){},toggleAttribute(){},insertBefore(){}});
    return elements.get(selector);
  }
  const context=vm.createContext({console,Intl,Date,Number,String,Object,AbortController,TypeError,Map,Promise,crypto,setTimeout,clearTimeout,setInterval:()=>{},location:{hash},window:{addEventListener(){}},document:{hidden:false,querySelector:element,querySelectorAll:()=>[],addEventListener(){},createElement:()=>element('new-element')},fetch:fetchImpl});
  // Omit boot calls; no network request or timer is required by these tests.
  const source=fs.readFileSync('frontend/app.js','utf8').replace('navigate();refresh();','');
  vm.runInContext(source,context);
  return {context,element,run:code=>vm.runInContext(code,context)};
}

test('draft card escapes source-derived markup and unknown totals are not zero',()=>{
  const h=harness();
  const html=h.run(`draftCard({id:'safe',status:'needs_clarification',supplier:'<img src=x onerror=alert(1)>',items:[{raw_name:'<script>bad()</script>',quantity:null,unit:null}],total_idr:null})`);
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(html.includes('—'));
  assert.ok(!html.includes('Rp0'));
});

test('approval requires current validated draft, received goods, online state, and saved edits',()=>{
  const h=harness();h.run(`state.draft={status:'pending_approval'};state.online=true;`);
  h.run('updateApprovalButton()');assert.equal(h.element('#approve-draft').disabled,true);
  h.element('#goods-received').checked=true;
  h.run('updateApprovalButton()');assert.equal(h.element('#approve-draft').disabled,false);
  for(const setup of ['state.dirty=true','state.busy=true','state.online=false',"state.draft.status='needs_clarification'","state.draft.status='recorded'"]) {
    h.run(`state.dirty=false;state.busy=false;state.online=true;state.draft.status='pending_approval';${setup};updateApprovalButton()`);
    assert.equal(h.element('#approve-draft').disabled,true,setup);
  }
});

test('retries reuse keys and distinct decisions never share keys',()=>{
  const h=harness();
  const first=h.run(`stableMutationKey('approve:d1',{expected_version:1,decision:'approve',received:true})`);
  assert.equal(first,h.run(`stableMutationKey('approve:d1',{expected_version:1,decision:'approve',received:true})`));
  assert.notEqual(first,h.run(`stableMutationKey('reject:d1',{expected_version:1,decision:'reject',received:false})`));
  assert.notEqual(first,h.run(`stableMutationKey('approve:d1',{expected_version:2,decision:'approve',received:true})`));
});

test('no-item extraction has a useful title and rejection is never shown in review queue',()=>{
  const h=harness();assert.equal(h.run('itemSummary([])'),'Purchase needs details');
  h.run(`state.data={drafts:[{status:'rejected'},{status:'recorded'},{status:'needs_clarification'},{status:'pending_approval'}]}`);
  assert.equal(h.run('pendingDrafts().length'),2);
});

test('superseded failing refresh cannot discard a newer successful refresh',async()=>{
  const h=harness();
  h.run(`var rejectOld;api=()=>new Promise((_,reject)=>{rejectOld=reject});var oldRefresh=refresh();`);
  h.run(`state.refreshId++;state.online=true;rejectOld(new Error('Old network failure'));`);
  await h.run('oldRefresh');
  assert.equal(h.run('state.online'),true);
});

test('bad line total is shown as a form error without sending a mutation',async()=>{
  const h=harness();
  h.run(`editedItems=()=>{throw new Error('Enter the line total in whole rupiah, without separators.')};var sent=false;api=()=>{sent=true};`);
  await h.run(`saveCorrections({preventDefault(){}})`);
  assert.equal(h.run('sent'),false);
  assert.equal(h.element('#review-error').hidden,false);
  assert.match(h.element('#review-error').textContent,/whole rupiah/);
});

test('decision cleanup tolerates the closed and cleared review state',()=>{
  const h=harness();h.run('state.draft=null;state.busy=true;reviewBusy(false)');
  assert.equal(h.run('state.busy'),false);
  assert.equal(h.element('#approve-draft').disabled,true);
});

test('simulated chat history escapes untrusted message text',()=>{
  const h=harness();
  h.run(`state.whatsappMessages=[{direction:'inbound',body:'<img src=x onerror=alert(1)> & hi',status:'received',created_at:'2026-10-17T10:00:00Z'}]`);
  const html=h.run('whatsappMessageHTML()');
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img'));
  assert.ok(html.includes('&amp; hi'));
  assert.ok(html.includes('received'));
});

test('WhatsApp simulator renders the requested route and a clear non-live label',()=>{
  const h=harness({hash:'#whatsapp'});
  h.run(`state.data={restaurant:{name:'Demo kitchen'},drafts:[]};state.whatsappLoaded=true;navigate()`);
  assert.equal(h.element('#page-title').textContent,'A WhatsApp flow you can rehearse.');
  assert.match(h.element('#content').innerHTML,/SIMULATED/);
  assert.match(h.element('#content').innerHTML,/META CLOUD API FLOW/);
  assert.match(h.element('#content').innerHTML,/does not connect to WhatsApp or send a live message/);
  const index=fs.readFileSync('frontend/index.html','utf8');
  assert.match(index,/#whatsapp[^>]*>.*WhatsApp demo/s);
});

test('simulator POST uses the requested body and reuses event id and idempotency key on retry',async()=>{
  const requests=[];
  const fetchImpl=async(path,options)=>{
    requests.push({path,options});
    if(requests.length===1)return {ok:false,json:async()=>({error:{message:'Try again'}})};
    return {ok:true,json:async()=>({data:{messages:[],draft:null,result:{action:'no_active_draft'}}})};
  };
  const h=harness({fetchImpl});
  await h.run(`sendWhatsAppMessage('beli cabai 3 kg 150rb')`);
  await h.run(`sendWhatsAppMessage('beli cabai 3 kg 150rb')`);
  assert.equal(requests.length,2);
  assert.equal(requests[0].path,'/v1/whatsapp/simulate');
  assert.equal(requests[0].options.headers['Idempotency-Key'],requests[1].options.headers['Idempotency-Key']);
  assert.equal(requests[0].options.headers['Idempotency-Key'],requests[0].options.headers['Idempotency-Key']);
  const first=JSON.parse(requests[0].options.body), second=JSON.parse(requests[1].options.body);
  assert.deepEqual(first,second);
  assert.equal(first.text,'beli cabai 3 kg 150rb');
  assert.equal(first.sender,'demo-owner');
  assert.match(first.event_id,/^[0-9a-f-]{36}$/i);
});

test('message history GET uses the fixed demo sender',async()=>{
  let request;
  const h=harness({fetchImpl:async(path,options)=>{request={path,options};return {ok:true,json:async()=>({data:{thread:{sender:'demo-owner'},messages:[]}})}}});
  h.run(`state.data={restaurant:{name:'Demo kitchen'},drafts:[],metrics:{purchases_today_idr:0,low_stock_count:0,recorded_purchases:0},purchases:[],inventory:[],activity:[],ingredients:[]};location.hash='#whatsapp';navigate()`);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(request.path,'/v1/whatsapp/messages?sender=demo-owner');
});

test('review warnings are deduplicated and capped at four visible details',()=>{
  const h=harness();
  h.run(`state.data={ingredients:[]};state.draft={version:1,status:'recorded',items:[],supplier:null,source_text:'demo',warnings:['First warning','First warning','Second warning','Third warning','Fourth warning','Fifth warning']};renderReview()`);
  const html=h.element('#review-content').innerHTML;
  assert.equal((html.match(/First warning/g)||[]).length,1);
  assert.ok(html.includes('Fourth warning'));
  assert.ok(!html.includes('Fifth warning'));
  assert.ok(html.includes('Review the fields above for the remaining details.'));
});
