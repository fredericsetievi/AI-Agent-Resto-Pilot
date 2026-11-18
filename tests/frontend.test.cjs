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
  const context=vm.createContext({console,Intl,Date,Number,String,Object,AbortController,TypeError,Map,Promise,crypto,FormData:class {constructor(form){this.values=form.values;}get(key){return this.values[key]??null;}},setTimeout:(...args)=>setTimeout(...args).unref(),clearTimeout,setInterval:()=>{},location:{hash},window:{addEventListener(){}},localStorage:{getItem:()=>null,setItem(){}},document:{hidden:false,documentElement:{lang:''},querySelector:element,querySelectorAll:()=>[],addEventListener(){},createElement:()=>element('new-element')},fetch:fetchImpl});
  // Omit boot calls; no network request or timer is required by these tests.
  // Omit boot: the real app boots via boot() (auth check) which no test needs implicitly.
  // i18n.js loads first so t() exists; tests assert the English strings.
  vm.runInContext(fs.readFileSync('frontend/i18n.js','utf8'),context);
  vm.runInContext("window.setLang('en')",context);
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

test('purchase totals panel renders rows, totals, and period buttons',()=>{
  const h=harness();
  assert.ok(h.run('purchaseTotalsPanel()').includes('Loading purchase totals'));
  h.run(`purchaseTotalsState.data={total_idr:1240000,purchase_count:6,label:'bulan ini',items:[{ingredient_id:'gas_12',name:'<script>x</script>',quantity:8,unit:'tabung',total_idr:1240000,purchase_count:6}]}`);
  const panel=h.run('purchaseTotalsPanel()');
  assert.ok(!panel.includes('<script>'));
  assert.ok(panel.includes('8 tabung'));
  assert.ok(panel.includes('Rp1.240.000'));
  assert.ok(panel.includes('6'));
  assert.ok(panel.includes('data-pt-period="this_month"'));
  assert.ok(panel.includes('data-pt-period="last_month"'));
});

test('inventory workspace restores sales, recipes, and order planning tools',()=>{
  const h=harness();
  h.run(`state.data={inventory:[{ingredient_id:'cabai',name:'Cabai merah',quantity:3,unit:'kg',low_stock:false,reorder_threshold:2}]};inventoryState.data={recipes:[],events:[{kind:'sale',note:'Lunch',portions:4,recipe:{name:'Nasi goreng'},items:[]}],forecasts:[],settings:{lead_days:2,safety_days:1}};inventoryState.loaded=true;`);
  const html=h.run('inventoryWorkspace()');
  assert.ok(html.includes('data-inventory-action="sale"'));
  assert.ok(html.includes('data-inventory-action="recipe"'));
  assert.ok(html.includes('data-inventory-action="settings"'));
  assert.ok(html.includes('Log sales'));
  assert.ok(html.includes('Nasi goreng'));
  assert.ok(html.includes('4 portions'));
  assert.ok(html.includes('data-count-stock="cabai"'));
  assert.ok(!html.includes('disabled')); // workspace loaded → toolbar clickable
  assert.ok(html.includes('Stock count history') || html.includes('Estimated stock'));
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
  const h=harness();h.run('LANG="id"');assert.equal(h.run('itemSummary([])'),'Pembelian perlu detail');
  h.run('LANG="en"');
  assert.equal(h.run('itemSummary([])'),'Purchase needs details');
  h.run(`state.data={drafts:[{status:'rejected'},{status:'recorded'},{status:'needs_clarification'},{status:'pending_approval'}]}`);
  assert.equal(h.run('pendingDrafts().length'),2);
});

test('superseded failing refresh cannot discard a newer successful refresh',async()=>{
  const h=harness();
  h.run(`state.user={role:'owner'};var rejectOld;api=()=>new Promise((_,reject)=>{rejectOld=reject});var oldRefresh=refresh();`);
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

test('chat assistant page renders the requested route and follows the language toggle',()=>{
  const h=harness({hash:'#whatsapp'});
  h.run('LANG="id"');
  h.run(`state.data={restaurant:{name:'Demo kitchen'},drafts:[]};state.whatsappLoaded=true;navigate()`);
  // Default language: Indonesian.
  assert.equal(h.element('#page-title').textContent,'Chat dengan asisten, Bos.');
  assert.match(h.element('#content').innerHTML,/ASISTEN PEMBELIAN/);
  assert.match(h.element('#content').innerHTML,/diproses langsung oleh RestoPilot/);
  // Toggle to English: same page, translated.
  h.run('LANG="en";applyStaticI18n();render()');
  assert.equal(h.element('#page-title').textContent,'Chat with your assistant, Boss.');
  assert.match(h.element('#content').innerHTML,/processed directly by RestoPilot/);
  const index=fs.readFileSync('frontend/index.html','utf8');
  assert.match(index,/#whatsapp[\s\S]*?nav_whatsapp">Chat</);
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

test('chat offers inline decisions for pending drafts only',()=>{
  const h=harness();
  h.run('state.online=true;state.whatsappResult=null;state.whatsappDraft={id:"d1",version:2,status:"pending_approval"}');
  const pending=h.run('whatsappOutcomeHTML()');
  assert.match(pending,/data-chat-approve="d1"/);
  assert.match(pending,/data-chat-reject="d1"/);
  assert.match(pending,/data-whatsapp-review="d1"/);
  h.run('state.whatsappDraft={id:"d2",version:1,status:"needs_clarification"}');
  const clarify=h.run('whatsappOutcomeHTML()');
  assert.ok(!clarify.includes('data-chat-approve'));
  assert.match(clarify,/data-whatsapp-review="d2"/);
  h.run('state.whatsappDraft={id:"d3",version:1,status:"recorded"}');
  assert.ok(!h.run('whatsappOutcomeHTML()').includes('data-chat-approve'));
  h.run('state.whatsappDraft={id:"d1",version:2,status:"pending_approval"};state.online=false');
  assert.ok(!h.run('whatsappOutcomeHTML()').includes('data-chat-approve'));
});

test('chat decisions post expected_version and reuse the mutation key on retry',async()=>{
  const requests=[];
  const fetchImpl=async(path,options)=>{
    if(options.method==='POST')requests.push({path,options});
    if(requests.length===1)return {ok:false,json:async()=>({error:{message:'Try again'}})};
    return {ok:true,json:async()=>({data:{}})};
  };
  const h=harness({fetchImpl});
  h.run('state.online=true;state.whatsappDraft={id:"d1",version:3,status:"pending_approval"}');
  await h.run(`chatDecision('d1','approve')`);
  await h.run(`chatDecision('d1','approve')`);
  assert.equal(requests.length,2);
  assert.equal(requests[0].path,'/v1/drafts/d1/decision');
  const body=JSON.parse(requests[0].options.body);
  assert.equal(body.expected_version,3);
  assert.equal(body.decision,'approve');
  assert.equal(body.received,true);
  assert.equal(requests[0].options.headers['Idempotency-Key'],requests[1].options.headers['Idempotency-Key']);
});

test('chat decisions ignore drafts that are not awaiting approval',async()=>{
  let sent=false;
  const h=harness({fetchImpl:async()=>{sent=true;return {ok:true,json:async()=>({data:{}})}}});
  h.run('state.online=true;state.whatsappDraft={id:"d2",version:1,status:"needs_clarification"}');
  await h.run(`chatDecision('d2','approve')`);
  assert.equal(sent,false);
  h.run('state.whatsappDraft={id:"d1",version:1,status:"pending_approval"}');
  await h.run(`chatDecision('other','approve')`);
  assert.equal(sent,false);
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
    requests.push({path,body:options.body?JSON.parse(options.body):null});
    return {ok:true,json:async()=>({data:{id:'u1',username:'admin',display_name:'Platform admin',role:'admin',restaurant_id:null,restaurant_name:null}})};
  }});
  h.element('#login-identifier').value='admin';h.element('#login-password').value='admin123';
  await h.run(`login({preventDefault(){},target:{reset(){}}})`);
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(requests[0],{path:'/v1/auth/login',body:{username:'admin',password:'admin123'}});
  assert.equal(h.element('#nav-admin').hidden,false);
  assert.equal(h.element('#nav-approvals').hidden,true);
  assert.equal(h.element('#nav-whatsapp').hidden,true);
  assert.equal(h.element('#sidebar').hidden,false);
  assert.match(h.element('#page-title').textContent,/Restaurants/);
});

test('the flexible identifier field posts username/password and opens the dashboard',async()=>{
  const requests=[];
  const h=harness({fetchImpl:async(path,options)=>{
    requests.push({path,body:JSON.parse(options.body)});
    return {ok:true,json:async()=>({data:{id:'u1',role:'owner',restaurant_id:'kitchen-beta'}})};
  }});
  h.run('navigate=()=>{};refresh=()=>{}');
  h.element('#login-identifier').value='0812 345 6789';h.element('#login-password').value='secret-password';
  await h.run('login({preventDefault(){},target:{reset(){}}})');
  assert.deepEqual(requests,[{path:'/v1/auth/login',body:{username:'0812 345 6789',password:'secret-password'}}]);
  assert.equal(h.run('state.user.restaurant_id'),'kitchen-beta');
  assert.equal(h.run('location.hash'),'#overview');
  assert.equal(h.element('#login-page').hidden,true);
});

test('login failures show the server message in the login error slot',async()=>{
  const h=harness();
  h.run("api=async()=>{var err=new Error('Unknown username or wrong password.');err.status=401;throw err}");
  h.element('#login-identifier').value='owner';h.element('#login-password').value='bad';
  await h.run('login({preventDefault(){},target:{reset(){}}})');
  assert.match(h.element('#login-error').textContent,/Unknown username or wrong password/);
});

test('signup sends phone/password and new restaurant fields without existing-tenant claims',async()=>{
  const requests=[];
  const h=harness({fetchImpl:async(path,options)=>{
    requests.push({path,body:JSON.parse(options.body)});
    return {ok:true,json:async()=>({data:{id:'new-owner',role:'owner',restaurant_id:'new-restaurant'}})};
  }});
  h.run("navigate=()=>{};refresh=()=>{}");
  h.element('#register-phone').value='08123456789';
  h.element('#signup-password').value='secret-password';h.element('#signup-password-confirm').value='secret-password';
  h.element('#signup-name').value='Owner';h.element('#signup-restaurant').value='My Kitchen';
  await h.run('submitPhoneRegister({preventDefault(){}})');
  assert.deepEqual(requests,[{path:'/v1/auth/phone/register',body:{phone:'08123456789',password:'secret-password',restaurant_name:'My Kitchen',display_name:'Owner'}}]);
  assert.equal(h.run('state.user.restaurant_id'),'new-restaurant');
  assert.equal(h.element('#signup-password-confirm').value,'');
});

test('register confirmation and busy guards prevent unintended requests',async()=>{
  const h=harness();
  h.run("var requests=0,resolveRequest;api=()=>{requests++;return new Promise(resolve=>resolveRequest=resolve)}");
  h.element('#signup-password').value='secret-password';h.element('#signup-password-confirm').value='different';
  await h.run('submitPhoneRegister({preventDefault(){}})');
  assert.equal(h.run('requests'),0);
  assert.match(h.element('#phone-register-error').textContent,/passwords must match/);
  h.element('#signup-password-confirm').value='secret-password';
  const pending=h.run('submitPhoneRegister({preventDefault(){}})');
  assert.equal(h.run('requests'),1);
  await h.run('submitPhoneRegister({preventDefault(){}})');
  assert.equal(h.run('requests'),1);
  h.run("navigate=()=>{};refresh=()=>{};resolveRequest({role:'owner',restaurant_id:'one'})");
  await pending;
});

test('register errors follow ID/EN and never leak server details',async()=>{
  const h=harness();
  h.run("api=async()=>{var err=new Error('private details');err.code='phone_account_conflict';err.status=409;throw err}");
  h.element('#signup-password').value='wrong';h.element('#signup-password-confirm').value='wrong';
  await h.run('submitPhoneRegister({preventDefault(){}})');
  assert.match(h.element('#phone-register-error').textContent,/could not be created or connected/);
  assert.equal(h.element('#signup-password').value,'');
  h.run("window.setLang('id')");
  assert.match(h.element('#phone-register-error').textContent,/tidak dapat dibuat atau dihubungkan/);
  assert.doesNotMatch(h.element('#phone-register-error').textContent,/private details/);
});

test('auth reset clears every login and signup field',()=>{
  const h=harness();
  h.element('#login-identifier').value='owner';h.element('#login-password').value='private';
  h.element('#register-phone').value='08123456789';h.element('#signup-password').value='private';
  h.run("resetAuthForms()");
  assert.equal(h.element('#login-identifier').value,'');
  assert.equal(h.element('#login-password').value,'');
  assert.equal(h.element('#register-phone').value,'');
  assert.equal(h.element('#signup-password').value,'');
  const html=fs.readFileSync('frontend/index.html','utf8');
  assert.match(html,/id="login-identifier"[^>]*autocomplete="username"/);
  assert.match(html,/id="login-password"[^>]*type="password"/);
  assert.match(html,/id="signup-password"[^>]*type="password"[^>]*minlength="8"/);
  assert.match(html,/id="register-view" hidden/);
  assert.match(html,/id="login-view"/);
  assert.doesNotMatch(html,/id="phone-login-form"|id="auth-method-toggle"|id="existing-login-phone"|id="password-login"|id="login-code"/);
});

test('admin row data attributes drive the detail navigation',()=>{
  const h=harness({hash:'#admin-detail'});
  h.run(`state.user={id:'u1',username:'admin',display_name:'Platform admin',role:'admin',restaurant_id:null,restaurant_name:null};state.adminSelected='resto-1';state.adminList=[{id:'resto-1',name:'Warung Bu Sari',address:null,created_at:'2026-10-27T00:00:00Z',whatsapp_numbers:[],users:[],pending_drafts:1,recorded_purchases:2}];navigate()`);
  assert.equal(h.element('#page-title').textContent,'Restaurant detail.');
  assert.match(h.element('#content').innerHTML,/Warung Bu Sari/);
  assert.match(h.element('#content').innerHTML,/Connected WhatsApp numbers|Nomor WhatsApp terhubung/);
});

test('review keeps every distinct warning visible',()=>{
  const h=harness();
  h.run(`state.data={ingredients:[]};state.draft={version:1,status:'recorded',items:[],supplier:null,source_text:'demo',warnings:['First warning','First warning','Second warning','Third warning','Fourth warning','Fifth warning']};renderReview()`);
  const html=h.element('#review-content').innerHTML;
  assert.equal((html.match(/First warning/g)||[]).length,1);
  assert.ok(html.includes('Fourth warning'));
  assert.ok(html.includes('Fifth warning'));
  assert.ok(!html.includes('Review the fields above for the remaining details.'));
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

test('daily spend bars expose localized dates and amounts, with visible values only for nonzero days', () => {
  const {run} = harness();
  const chart = `dailySpendChart([
    {date:'2026-11-01', total_idr: 50000},
    {date:'2026-11-02', total_idr: 100000},
    {date:'2026-11-03', total_idr: 0}
  ])`;
  const html = run(chart);
  assert.match(html, /height:50%/);
  assert.match(html, /height:100%/);
  assert.match(html, /height:0%/);
  assert.match(html, /aria-label="2 November 2026 · Rp100\.000"/);
  assert.match(html, /aria-label="3 November 2026 · Rp0"/);
  assert.equal((html.match(/class="chart-value(?: chart-value-peak)?"/g) || []).length, 2);
  assert.match(html, /class="chart-date"[^>]*>01<\/small>/);
  run("LANG='id'");
  assert.match(run(`dailySpendChart([{date:'2026-08-02',total_idr:1}])`), /aria-label="2 Agustus 2026 · Rp1"/);
});

test('small charts show only the first peak value and keep labels horizontal', () => {
  const {run} = harness();
  const html = run(`dailySpendChart([
    {date:'2026-11-01',total_idr:50000},
    {date:'2026-11-02',total_idr:100000},
    {date:'2026-11-03',total_idr:100000}
  ])`);
  assert.equal((html.match(/chart-value-peak/g) || []).length, 1);
  assert.match(html, /aria-label="2 November 2026 · Rp100\.000"[^>]*><\/div>\s*<span class="chart-value chart-value-peak"/);
  assert.doesNotMatch(run(`dailySpendChart([{date:'2026-11-01',total_idr:0}])`), /chart-value/);
  const css = fs.readFileSync('frontend/styles.css','utf8');
  const mobile = css.split('@media (max-width:480px) {')[1].split('.hbars')[0];
  assert.match(mobile, /\.chart-value\s*\{\s*display:none/);
  assert.match(mobile, /\.chart-value-peak[^{}]*\{[^{}]*display:block/);
  for (const rule of css.match(/[^{}]*\.chart-value[^{}]*\{[^{}]*\}/g)) {
    assert.doesNotMatch(rule, /writing-mode|rotate/);
  }
});

test('ingredient spend shows escaped names, amounts, and shares of the displayed total', () => {
  const {run} = harness();
  const chart = `ingredientSpendChart([{name:'<b>Cabai merah</b>', total_idr:100000},{name:'Bawang merah', total_idr:50000}])`;
  const html = run(chart);
  assert.match(html, /&lt;b&gt;Cabai merah&lt;\/b&gt;/);
  assert.match(html, /Rp100\.000 <small>66\.7%/);
  assert.match(html, /Rp50\.000 <small>33\.3%/);
  assert.match(html, /width:100%/);
  assert.match(html, /width:50%/);
  run("LANG='id'");
  assert.match(run(chart), /66,7%/);
  assert.match(run(`ingredientSpendChart([{name:'Cabai',total_idr:0}])`), /<small>0%/);
});

test('both charts localize their empty state', () => {
  const {run} = harness();
  for (const chart of ['dailySpendChart([])', 'ingredientSpendChart(null)']) {
    run("LANG='id'");
    assert.match(run(chart), /Belum ada data grafik/);
    run("LANG='en'");
    assert.match(run(chart), /No chart data yet/);
  }
});

test('activity localizes audit events and escapes original messages for unknown events', () => {
  const {run} = harness();
  const feed = `activityRows([
    {event:'purchase_recorded',message:'Purchase recorded for Rp 150,000; received stock added. Payment status unknown.'},
    {event:'draft_edited',message:'Draft reviewed and edited.'},
    {event:'whatsapp_message_processed',message:'WhatsApp message processed: media_unsupported.'},
    {event:'future_event',message:'<img src=x>'}
  ])`;
  run("LANG='id'");
  const id = run(feed);
  assert.match(id, /Pembelian Rp150\.000 dicatat/);
  assert.match(id, /Draf ditinjau dan diedit/);
  assert.match(id, /Foto\/audio belum bisa diproses/);
  assert.doesNotMatch(id, /Purchase recorded|Draft reviewed|<img/);
  assert.match(id, /&lt;img src=x&gt;/);
  run("LANG='en'");
  assert.match(run(feed), /Purchase of Rp150\.000 recorded/);
  assert.match(run(feed), /Draft reviewed and edited/);
});

test('api calls send the ngrok skip header and report non-JSON gateway pages clearly', async () => {
  let capturedHeaders;
  const h = harness({fetchImpl: async (path, options) => {
    capturedHeaders = options.headers;
    return {ok:false, status:403, json:async () => { throw new SyntaxError('ngrok gateway error page'); }};
  }});
  h.run('LANG="id"');
  await assert.rejects(h.run(`api('/v1/links')`), /Gateway mengembalikan halaman bukan-JSON/);
  assert.equal(capturedHeaders['ngrok-skip-browser-warning'], '1');
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

test('inventory search filters stock cards by name and reports the match count',()=>{
  const h=harness();
  h.run(`inventoryState.data={forecasts:[]};inventoryState.search='';state.data={inventory:[
    {ingredient_id:'a',name:'Bolkus jumbo',unit:'pcs',quantity:'12',reorder_threshold:'5',low_stock:false},
    {ingredient_id:'b',name:'Beras premium',unit:'kg',quantity:'3',reorder_threshold:'5',low_stock:true}]}`);
  const all=h.run('inventoryCardsHtml()');
  assert.ok(all.includes('Bolkus jumbo')&&all.includes('Beras premium'));
  assert.equal(h.run('inventorySearchCount()'),'');
  h.run(`inventoryState.search='bol'`);
  const filtered=h.run('inventoryCardsHtml()');
  assert.ok(filtered.includes('Bolkus jumbo'));
  assert.ok(!filtered.includes('Beras premium'));
  assert.equal(h.run('inventorySearchCount()'),'1/2');
  h.run(`inventoryState.search='KEMIRI'`);
  assert.equal(h.run('inventorySearchCount()'),'0/2');
  const empty=h.run('inventoryCardsHtml()');
  assert.equal(typeof empty,'string');
});

test('supplier and insights pages render owner analytics',()=>{
  const h=harness();
  const supplierHtml=h.run(`state.user={role:'owner',restaurant_id:'r1'};supplierState.data={suppliers:[{name:'Pak Budi',contact_phone:'6281',contact_platform:'wa',purchase_count:2,unpaid_count:0}],quotes:[{ingredient_name:'Cabai merah',supplier_name:'Pak Budi',unit_price_idr:30000,created_at:'2026-11-18T10:00:00',note:null}]};suppliersPage()`);
  assert.ok(supplierHtml.includes('Pak Budi'));
  assert.ok(supplierHtml.includes('Rp30.000'));
  assert.ok(supplierHtml.includes('Best price per ingredient'));
  const insightsHtml=h.run(`insightsState.days=7;insightsState.data={days:7,start:'2026-11-12',end:'2026-11-19',menus:[{name:'Nasi goreng',portions:10,revenue:150000,events:2},{name:'Mie ayam',portions:4,revenue:60000,events:1}],total_portions:14,total_revenue:210000,previous_total_portions:8,previous_total_revenue:120000,delta_percent:75};insightsPage()`);
  assert.ok(insightsHtml.includes('Nasi goreng'));
  assert.ok(insightsHtml.indexOf('Nasi goreng')<insightsHtml.indexOf('Mie ayam'));  // ranked by portions
  assert.ok(insightsHtml.includes('Rp210.000'));
  assert.ok(insightsHtml.includes('+75%'));
});

test('approved tokens ship before styles and the translated dashboard scripts', () => {
  const tokens = fs.readFileSync('frontend/tokens.css');
  assert.equal(crypto.createHash('sha256').update(tokens).digest('hex'), '87b57d13a1cfb96aecda44d57164953d2402b07d7b235deaf856bd90e7a92e25');
  const html = fs.readFileSync('frontend/index.html', 'utf8');
  assert.match(html, /\/assets\/tokens\.css[^]*\/assets\/styles\.css[^]*\/assets\/i18n\.js[^]*\/assets\/app\.js/);
  const plugin = fs.readFileSync('deploy/atoms/frontend/plugins/restopilot-dashboard.ts', 'utf8');
  assert.ok(plugin.includes("'/assets/tokens.css': { file: path.join(frontendDir, 'tokens.css')"));
  assert.match(plugin, /generateBundle\(\)[^]*Object.entries\(allAssets\(\)\)[^]*this.emitFile/);
});

test('base typography consumes approved tokens and preserves the legacy canvas', () => {
  const css = fs.readFileSync('frontend/styles.css', 'utf8');
  assert.ok(css.includes('font-family:var(--font-display)'));
  assert.ok(css.includes('--legacy-paper:#f6f7f2'));
  assert.ok(css.includes('--green-dark:var(--ink)'));
});

test('dialogs and primary actions share the approved dimensions', () => {
  const css = fs.readFileSync('frontend/styles.css', 'utf8');
  assert.match(css, /dialog \{ border-radius:var\(--modal-radius\); padding:var\(--modal-inset\)/);
  assert.match(css, /\.button\.primary, \.login-submit \{ min-height:var\(--button-min-height\)/);
});

test('approval badge belongs to its icon and the four owner destinations remain available', () => {
  const html = fs.readFileSync('frontend/index.html', 'utf8');
  assert.match(html, /id="nav-approvals"[^]*?<\/svg><span id="nav-count"[^]*?<\/span><\/span>/);
  for (const page of ['overview', 'approvals', 'inventory', 'more']) assert.ok(html.includes('data-page="'+page+'"'));
});

test('overview keeps scoped stock estimates and flexible two-column phone metrics', () => {
  const h = harness();
  h.run("state.data={metrics:{low_stock_count:9},restaurant:{name:'Test'}}");
  assert.match(h.run('overview()'), /Low stock[^]*?>9<[^]*?Based on estimated inventory/);
  const css = fs.readFileSync('frontend/styles.css','utf8');
  assert.match(css, /\.metrics \{ grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/);
});

test('every draft warning is visible and escaped before the decision buttons', () => {
  const h = harness();
  const html = h.run("draftCard({id:'d',items:[],warnings:['a','b','c','d','<flag>'],status:'pending_approval'})");
  assert.ok(html.indexOf('&lt;flag&gt;') >= 0 && html.indexOf('&lt;flag&gt;') < html.indexOf('data-approve'));
  h.run("window.setLang('id')");
  assert.match(h.run('recipeLine()'), /Jumlah per porsi/);
});
test('landing preserves its bilingual copy and groups the entry action before proof', () => {
  const html = fs.readFileSync('landing/index.html','utf8');
  assert.match(html, /data-i18n="hero.title"[^]*?data-i18n-html="hero.lede"[^]*?class="hero-actions"[^]*?class="hero-channels"[^]*?class="trust-strip"/);
  assert.ok(html.includes('Asisten AI'));
  assert.ok(html.indexOf('/assets/tokens.css') < html.indexOf('/landing-assets/styles.css'));
});
