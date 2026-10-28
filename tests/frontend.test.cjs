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
  // UI request/toast timers must not hold this unit harness open when a stubbed
  // fetch deliberately never resolves (the admin preview uses a 60s timeout).
  const context=vm.createContext({console,Intl,Date,Number,String,Object,AbortController,TypeError,Map,Promise,crypto,FormData:class {constructor(form){this.values=form.values;}get(key){return this.values[key]??null;}},setTimeout:(...args)=>setTimeout(...args).unref(),clearTimeout,setInterval:()=>{},location:{hash},window:{addEventListener(){}},document:{hidden:false,querySelector:element,querySelectorAll:()=>[],addEventListener(){},createElement:()=>element('new-element')},fetch:fetchImpl});
  // Omit boot calls; no network request or timer is required by these tests.
  // Omit boot: the real app boots via boot() (auth check) which no test needs implicitly.
  const source=fs.readFileSync('frontend/app.js','utf8').replace('boot();','');
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
  h.run(`WHATSAPP_SENDER='628123456789'`);
  await h.run(`sendWhatsAppMessage('beli cabai 3 kg 150rb')`);
  await h.run(`sendWhatsAppMessage('beli cabai 3 kg 150rb')`);
  assert.equal(requests.length,2);
  assert.equal(requests[0].path,'/v1/whatsapp/simulate');
  assert.equal(requests[0].options.headers['Idempotency-Key'],requests[1].options.headers['Idempotency-Key']);
  assert.equal(requests[0].options.headers['Idempotency-Key'],requests[0].options.headers['Idempotency-Key']);
  const first=JSON.parse(requests[0].options.body), second=JSON.parse(requests[1].options.body);
  assert.deepEqual(first,second);
  assert.equal(first.text,'beli cabai 3 kg 150rb');
  assert.equal(first.sender,'628123456789');
  assert.match(first.event_id,/^[0-9a-f-]{36}$/i);
});

test('message history GET uses the restaurant-linked sender number',async()=>{
  let request;
  const h=harness({fetchImpl:async(path,options)=>{request={path,options};return {ok:true,json:async()=>({data:{thread:{sender:'628123456789'},messages:[]}})}}});
  h.run(`WHATSAPP_SENDER='628123456789';state.user={display_name:'Bu Sari',role:'owner'};state.data={restaurant:{name:'Demo kitchen'},drafts:[],whatsapp_numbers:[{phone:'628123456789',label:'Bu Sari'}],metrics:{purchases_today_idr:0,low_stock_count:0,recorded_purchases:0},purchases:[],inventory:[],activity:[],ingredients:[]};location.hash='#whatsapp';navigate()`);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(request.path,'/v1/whatsapp/messages?sender=628123456789');
});

test('unauthenticated boot shows the login page and hides the shell',async()=>{
  let requested;
  const h=harness({fetchImpl:async(path,options)=>{requested=path;return {ok:false,json:async()=>({error:{code:'unauthorized',message:'Log in to continue.'}})}}});
  await h.run('boot()');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requested,'/v1/auth/me');
  assert.equal(h.element('#login-page').hidden,false);
  assert.equal(h.element('#sidebar').hidden,true);
  assert.equal(h.element('#shell').hidden,true);
});

test('admin login navigates to the restaurants console and hides owner nav',async()=>{
  const requests=[];
  const h=harness({fetchImpl:async(path,options)=>{
    requests.push(path);
    return {ok:true,json:async()=>({data:{id:'u1',username:'admin',display_name:'Platform admin',role:'admin',restaurant_id:null,restaurant_name:null}})};
  }});
  await h.run(`login({preventDefault(){},target:{reset(){}}})`);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests[0],'/v1/auth/login');
  assert.equal(h.element('#nav-admin').hidden,false);
  assert.equal(h.element('#nav-approvals').hidden,true);
  assert.equal(h.element('#nav-whatsapp').hidden,true);
  assert.equal(h.element('#sidebar').hidden,false);
  assert.match(h.element('#page-title').textContent,/Restaurants/);
});

test('admin row data attributes drive the detail navigation',()=>{
  const h=harness({hash:'#admin-detail'});
  h.run(`state.user={id:'u1',username:'admin',display_name:'Platform admin',role:'admin',restaurant_id:null,restaurant_name:null};state.adminSelected='resto-1';state.adminList=[{id:'resto-1',name:'Warung Bu Sari',address:null,created_at:'2026-10-27T00:00:00Z',whatsapp_numbers:[],users:[],pending_drafts:1,recorded_purchases:2}];navigate()`);
  assert.equal(h.element('#page-title').textContent,'Restaurant detail.');
  assert.match(h.element('#content').innerHTML,/Warung Bu Sari/);
  assert.match(h.element('#content').innerHTML,/data-open-resto="resto-1"|Nomor WhatsApp terhubung/);
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

test('ingredient combo resolves catalog matches and keeps typed new names',()=>{
  const h=harness();
  h.run(`state.data={ingredients:[{id:'cabai',name:'Cabai merah'},{ingredient_id:'kecap_manis',name:'Kecap manis'}]};`);
  const matched=h.run(`resolveIngredient('cabai merah','old')`);
  assert.equal(matched.ingredient_id,'cabai');
  assert.equal(matched.raw_name,'Cabai merah');
  const custom=h.run(`resolveIngredient('KECAP manis','old')`);
  assert.equal(custom.ingredient_id,'kecap_manis');
  assert.equal(custom.raw_name,'Kecap manis');
  const created=h.run(`resolveIngredient('  Terasi  ','old')`);
  assert.equal(created.ingredient_id,null);
  assert.equal(created.raw_name,'Terasi');
  const empty=h.run(`resolveIngredient('','original raw')`);
  assert.equal(empty.ingredient_id,null);
  assert.equal(empty.raw_name,'original raw');
});

test('review editor prefills mapped names and leaves new rows blank',()=>{
  const h=harness();
  h.run(`state.data={ingredients:[{id:'cabai',name:'Cabai merah'}]};state.draft={version:1,status:'needs_clarification',items:[{ingredient_id:'cabai',raw_name:'cabai',quantity:'3',unit:'kg',line_total_idr:150000},{ingredient_id:null,raw_name:'terasi',quantity:'1',unit:'kg',line_total_idr:49000},{ingredient_id:null,raw_name:'',quantity:null,unit:null,line_total_idr:null}],supplier:null,source_text:'demo',warnings:[]};renderReview()`);
  const html=h.element('#review-content').innerHTML;
  assert.ok(html.includes('value="Cabai merah"'));
  assert.ok(html.includes('value="terasi"'));
  assert.ok(html.includes('name="ingredient_query"'));
  assert.ok(html.includes('list="ingredient-options"'));
  assert.ok(html.includes('datalist id="ingredient-options"'));
  assert.ok(!html.includes('<select'));
  assert.ok(!html.includes('value="Unmatched item"'));
});

test('inventory cards escape ledger content and distinguish forecast evidence sources',()=>{
  const h=harness();
  h.run(`state.data={inventory:[{ingredient_id:'rice',name:'<img src=x>',quantity:'10',unit:'kg',reorder_threshold:'2'},{ingredient_id:'oil',name:'Oil',quantity:'4',unit:'l',reorder_threshold:'1'}]};inventoryState.loaded=true;inventoryState.data={settings:{lead_days:2,safety_days:1,history_start:'2026-10-01',history_end:'2026-10-07'},forecasts:[{ingredient_id:'rice',source:'manual',daily_usage:'2',days_left:'5',reorder_in_days:'2'},{ingredient_id:'oil',source:'recorded',daily_usage:'1',days_left:'4',reorder_in_days:'1',evidence_days:7}],events:[{kind:'usage',note:'<script>alert(1)</script>',created_at:'2026-10-20T00:00:00Z',items:[]}]};`);
  const html=h.run('inventoryWorkspace()');
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script>'));
  assert.match(html,/&lt;img/);
  assert.match(html,/your estimate/);
  assert.match(html,/7 recorded days/);
  assert.match(html,/2026-10-01/);
});

function inventoryMutationHarness(kind,values){
  const h=harness();
  const button={disabled:false,textContent:''};
  h.context.testForm={dataset:{kind},values,querySelector:()=>button,querySelectorAll:()=>[]};
  h.run(`inventoryState.formId='test-form';inventoryState.snapshot=[{ingredient_id:'rice',quantity:'10',unit:'kg'}];var sent=[];api=async(path,options)=>{sent.push({path,...options});throw new Error('Temporary failure — retry');};`);
  return h;
}

test('history settings require confirmation and send the complete-history flag',async()=>{
  const values={lead_days:'2',safety_days:'1',history_start:'2026-10-01',history_end:'2026-10-07',history_confirmed:''};
  const h=inventoryMutationHarness('settings',values);
  await h.run(`saveInventoryForm({preventDefault(){},target:testForm})`);
  assert.equal(h.run('sent.length'),0);
  assert.match(h.element('#inventory-form-error').textContent,/confirm/);
  values.history_confirmed='on';
  await h.run(`saveInventoryForm({preventDefault(){},target:testForm})`);
  const payload=JSON.parse(h.run('JSON.stringify(sent[0].body)'));
  assert.equal(payload.history_complete,true);
  assert.equal(payload.history_start,'2026-10-01');
  assert.equal(payload.history_end,'2026-10-07');
  assert.equal(h.run('sent[0].path'),'/v1/inventory/settings');
});

test('stock count uses the opened balance and retry retains the same mutation key',async()=>{
  const h=inventoryMutationHarness('count',{ingredient_id:'rice',quantity:'0.5',pack_size:'12',note:'Physical count',confirmed:'on'});
  h.run(`state.data={inventory:[{ingredient_id:'rice',quantity:'99'}]};`);
  await h.run(`saveInventoryForm({preventDefault(){},target:testForm})`);
  await h.run(`saveInventoryForm({preventDefault(){},target:testForm})`);
  assert.equal(h.run('sent.length'),2);
  assert.equal(h.run('sent[0].body.items[0].expected_quantity'),'10');
  assert.equal(h.run('sent[0].body.items[0].quantity'),'0.5');
  assert.equal(h.run('sent[0].body.items[0].pack_size'),'12');
  assert.equal(h.run('sent[0].key'),h.run('sent[1].key'));
});

test('unmapped sales preserve portions and revenue without inventing ingredient deductions',async()=>{
  const h=inventoryMutationHarness('sale',{recipe_id:'',portions:'12',revenue_idr:'150000',note:'Lunch service',confirmed:'on'});
  await h.run(`saveInventoryForm({preventDefault(){},target:testForm})`);
  const payload=JSON.parse(h.run('JSON.stringify(sent[0].body)'));
  assert.equal(payload.portions,'12');
  assert.equal(payload.revenue_idr,150000);
  assert.deepEqual(payload.items,[]);
  assert.equal(payload.recipe_id,undefined);
});

test('recipe sales submit the reviewed version even after a background refresh',async()=>{
  const h=inventoryMutationHarness('sale',{recipe_id:'recipe1',portions:'2',revenue_idr:'',note:'Lunch',confirmed:'on'});
  h.run(`inventoryState.recipesSnapshot=[{id:'recipe1',version:1,items:[]}];inventoryState.data={recipes:[{id:'recipe1',version:2,items:[]}]};`);
  await h.run(`saveInventoryForm({preventDefault(){},target:testForm})`);
  assert.equal(h.run('sent[0].body.expected_recipe_version'),1);
});
