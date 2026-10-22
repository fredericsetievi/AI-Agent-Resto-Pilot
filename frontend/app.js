/* The API owns every quantity, price comparison, and state transition. */
'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const e = escapeHTML;
const rupiah = value => value == null ? '—' : `Rp${new Intl.NumberFormat('id-ID', {maximumFractionDigits: 0}).format(Number(value))}`;
const number = value => value == null ? '—' : new Intl.NumberFormat('id-ID', {maximumFractionDigits: 6}).format(Number(value));
const date = value => value ? new Intl.DateTimeFormat('en-GB', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Jakarta'}).format(new Date(value)) + ' WIB' : '';
const icons = {cabai:'🌶',telur:'🥚',beras:'🌾',minyak:'🫙',bawang:'🧅'};
const titles = {overview:['Your kitchen, in view.','Purchases, stock, and the decisions that need you.'],approvals:['A moment for your review.','Check the details. You decide what becomes a record.'],purchases:['Every purchase, accounted for.','Approved purchases and their source records. Payment status is separate.'],inventory:['Know what came in.','Estimated stock from recorded purchases. Kitchen usage is not tracked yet.'],activity:['The story behind each record.','A persistent trail of drafts, corrections, and owner decisions.'],whatsapp:['A WhatsApp flow you can rehearse.','A persistent local conversation, from purchase note to owner decision.']};
const WHATSAPP_SENDER = 'demo-owner';
const state = {data:null, draft:null, page:'overview', dirty:false, busy:false, online:false, intakeKey:null, decisions:new Map(), mutationKeys:new Map(), refreshId:0, whatsappMessages:[], whatsappDraft:null, whatsappResult:null, whatsappLoading:false, whatsappError:'', whatsappLoaded:false, whatsappPending:null, whatsappSending:false};

async function api(path, {method='GET', body, key, timeout=15000} = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {method, signal:controller.signal, headers:{Accept:'application/json', ...(body ? {'Content-Type':'application/json'} : {}), ...(key ? {'Idempotency-Key':key} : {})}, ...(body ? {body:JSON.stringify(body)} : {})});
    const payload = await response.json();
    if (!response.ok) { const error = new Error(payload.error?.message || 'The request could not be completed.'); error.status=response.status; throw error; }
    return payload.data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('The request timed out. Retry the same action to check its result safely.');
    if (error instanceof TypeError) throw new Error('Cannot reach the local server. Check it is running, then retry.');
    throw error;
  } finally { clearTimeout(timer); }
}
function toast(message) { $('#toast').textContent=message; $('#toast').hidden=false; clearTimeout(toast.timer); toast.timer=setTimeout(() => $('#toast').hidden=true, 5500); }
function showError(id, message) { const element=$(`#${id}`); element.textContent=message; element.hidden=!message; }
function pendingDrafts() { return (state.data?.drafts || []).filter(draft => ['pending_approval','needs_clarification'].includes(draft.status)); }
function badge(status) { const text={pending_approval:'Needs approval',needs_clarification:'Needs details',recorded:'Recorded',rejected:'Rejected'}; return `<span class="badge ${status==='recorded'?'green':status==='rejected'?'gray':'amber'}">${e(text[status] || status)}</span>`; }
function nameFor(item) { return state.data?.ingredients?.find(ingredient => ingredient.id===item.ingredient_id || ingredient.ingredient_id===item.ingredient_id)?.name || item.raw_name || item.ingredient_id || 'Unmatched item'; }
function itemSummary(items) { return (items || []).map(item => nameFor(item)).join(', ') || 'Purchase needs details'; }
function warningFor(draft) { return draft.items?.find(item => item.comparison?.status==='price_increase'); }
function draftCard(draft) {
  const item=draft.items?.[0] || {}, spike=warningFor(draft);
  return `<article class="draft-card ${spike?'alert':''}"><div class="draft-top"><span class="ingredient-icon" aria-hidden="true">${icons[item.ingredient_id] || '▤'}</span><div><h3>${e(itemSummary(draft.items))}</h3><p>${e(draft.supplier || 'Supplier unspecified')} · ${e(draft.items?.length===1 ? `${number(item.quantity)} ${item.unit || ''}` : `${draft.items?.length || 0} items`)}</p></div><div class="price money">${rupiah(draft.total_idr)}<small>Purchase total</small></div></div>${spike ? `<div class="price-alert"><span aria-hidden="true">↗</span><span><strong>${number(Number(spike.comparison.change_percent).toFixed(1))}% price increase</strong> · ${rupiah(spike.unit_price_idr)}/${e(spike.canonical_unit)} vs ${rupiah(spike.comparison.baseline_unit_price_idr)}</span></div>` : draft.status==='needs_clarification' ? '<div class="price-alert">Some details need your attention before this can be recorded.</div>' : ''}<div class="draft-bottom">${badge(draft.status)}<button class="text-button" data-review="${e(draft.id)}">Review purchase <span aria-hidden="true">→</span></button></div></article>`;
}
function purchaseTable(purchases) {
  if (!purchases.length) return '<div class="empty"><strong>Your purchase history starts here.</strong>Approved purchases will appear in this view.</div>';
  return `<div class="table-wrap"><table><thead><tr><th>Purchase</th><th>Supplier</th><th>Amount</th><th>Status</th></tr></thead><tbody>${purchases.map(purchase => `<tr><td><button class="text-button" data-review="${e(purchase.draft_id)}">${e(itemSummary(purchase.items))}</button><small>${e(date(purchase.created_at))}</small></td><td>${e(purchase.supplier || 'Unspecified')}</td><td class="money">${rupiah(purchase.total_idr)}<small>Payment unknown</small></td><td>${badge('recorded')}</td></tr>`).join('')}</tbody></table></div>`;
}
function inventoryRows(inventory) {
  return `<div class="inventory-list">${inventory.map(item => `<div class="inventory-row"><span class="ingredient-icon" aria-hidden="true">${icons[item.ingredient_id] || '▦'}</span><div><strong>${e(item.name)}</strong><small>${item.low_stock ? 'Below stock threshold' : 'Recorded stock'}</small></div><div class="stock ${item.low_stock?'low':''}">${number(item.quantity)} <span>${e(item.unit)}</span><small>Threshold ${number(item.reorder_threshold)} ${e(item.unit)}</small></div></div>`).join('')}</div>`;
}
function activityRows(activity) {
  if (!activity.length) return '<div class="empty">Your kitchen activity will appear here.</div>';
  return `<div class="activity-list">${activity.map(item => `<div class="activity-row"><span class="activity-dot"></span><div><p>${e(item.message || item.event)}</p><small>${e(date(item.created_at))}</small></div></div>`).join('')}</div>`;
}
function whatsappPage() {
  return `<div class="whatsapp-layout"><section class="panel whatsapp-panel"><div class="whatsapp-topline"><div><p class="eyebrow">SIMULATED · META CLOUD API FLOW</p><h2>Demo owner</h2><p class="muted">${e(WHATSAPP_SENDER)} · local conversation</p></div><span class="simulated-badge">SIMULATED</span></div><div class="chat-shell"><div class="chat-date">TODAY · LOCAL DEMO</div><div id="whatsapp-messages" class="chat-messages" role="log" aria-live="polite" aria-relevant="additions text">${whatsappMessageHTML()}</div></div><div id="whatsapp-error" class="notice error" role="alert" ${state.whatsappError?'':'hidden'}>${e(state.whatsappError)}</div><form id="whatsapp-form" class="whatsapp-composer"><label class="sr-only" for="whatsapp-text">Message to the simulated RestoPilot assistant</label><textarea id="whatsapp-text" rows="2" maxlength="4000" required placeholder="Type a purchase message…" ${state.whatsappSending?'disabled':''}></textarea><button class="button primary" type="submit" ${state.whatsappSending?'disabled':''}>${state.whatsappSending?'Sending…':'Send message'} <span aria-hidden="true">→</span></button></form><div class="whatsapp-examples" aria-label="Quick message examples"><span>Try a message:</span>${['beli cabai 3 kg 150rb','ya','batal','beli beras'].map(value=>`<button type="button" data-whatsapp-example="${e(value)}">${e(value)}</button>`).join('')}</div><p class="simulator-note">This rehearsal stores messages in the local demo database. It does not connect to WhatsApp or send a live message.</p></section><aside class="panel whatsapp-guide"><p class="eyebrow">THE REHEARSAL</p><h2>From a note to a record.</h2><ol><li><span>1</span><div><strong>Send a purchase note</strong><small>Try “beli cabai 3 kg 150rb”.</small></div></li><li><span>2</span><div><strong>Review the structured draft</strong><small>Correct the details in the dashboard.</small></div></li><li><span>3</span><div><strong>Reply ya or batal</strong><small>Only an owner decision records or rejects it.</small></div></li></ol><a class="text-button" href="#approvals">Open the review queue <span aria-hidden="true">→</span></a></aside></div>`;
}
function whatsappMessageHTML() {
  if(state.whatsappLoading && !state.whatsappMessages.length)return '<div class="chat-empty">Loading saved conversation…</div>';
  if(!state.whatsappMessages.length)return '<div class="chat-empty">Your rehearsal starts here. Choose a message below.</div>';
  return state.whatsappMessages.map(message=>`<article class="chat-message ${message.direction==='inbound'?'inbound':'outbound'}"><div class="chat-bubble"><p>${e(message.body)}</p><small>${e(message.direction==='inbound'?'You':'RestoPilot')} · ${e(message.status==='received'?'received':'simulated')} · ${e(date(message.created_at))}</small></div></article>`).join('') + whatsappOutcomeHTML();
}
function whatsappOutcomeHTML() {
  const draft=state.whatsappDraft, action=state.whatsappResult?.action;
  let status='';
  if(action==='approved')status='<div class="chat-outcome recorded"><strong>Purchase recorded.</strong><span>Inventory and purchase history have been updated.</span></div>';
  else if(action==='rejected')status='<div class="chat-outcome"><strong>Draft rejected.</strong><span>Inventory is unchanged.</span></div>';
  else if(action==='review_required')status='<div class="chat-outcome"><strong>Needs a closer look.</strong><span>Complete the missing details in the dashboard before deciding.</span></div>';
  else if(action==='active_draft_exists')status='<div class="chat-outcome"><strong>One draft is already waiting.</strong><span>Review or decide on it before starting another purchase.</span></div>';
  else if(action==='no_active_draft')status='<div class="chat-outcome"><strong>No active draft.</strong><span>Send a purchase note to start a new rehearsal.</span></div>';
  else if(draft?.status==='needs_clarification')status='<div class="chat-outcome"><strong>Draft needs details.</strong><span>Review the fields, then save corrections.</span></div>';
  else if(draft)status='<div class="chat-outcome"><strong>Draft ready for review.</strong><span>Nothing is recorded until you confirm received goods.</span></div>';
  const review=draft && ['needs_clarification','pending_approval'].includes(draft.status) ? `<button class="button secondary chat-review" type="button" data-whatsapp-review="${e(draft.id)}">Review in dashboard <span aria-hidden="true">→</span></button>` : '';
  return status || review ? `<div class="chat-next">${status}${review}</div>` : '';
}
async function loadWhatsAppMessages() {
  if(state.whatsappLoading)return;
  state.whatsappLoading=true;state.whatsappError='';
  if($('#whatsapp-messages'))$('#whatsapp-messages').innerHTML=whatsappMessageHTML();
  try {
    const thread=await api(`/v1/whatsapp/messages?sender=${encodeURIComponent(WHATSAPP_SENDER)}`);
    state.whatsappMessages=Array.isArray(thread.messages)?thread.messages:[];
    state.whatsappDraft=thread.draft || null;state.whatsappLoaded=true;state.whatsappError='';
  } catch(error) {state.whatsappError=error.message;}
  finally {state.whatsappLoading=false;renderWhatsAppChat();}
}
function renderWhatsAppChat() {
  const messages=$('#whatsapp-messages');if(messages)messages.innerHTML=whatsappMessageHTML();
  const error=$('#whatsapp-error');if(error){error.textContent=state.whatsappError;error.hidden=!state.whatsappError;}
  const form=$('#whatsapp-form');if(form){$('#whatsapp-text').disabled=state.whatsappSending;form.querySelector('button[type="submit"]').disabled=state.whatsappSending;form.querySelector('button[type="submit"]').textContent=state.whatsappSending?'Sending…':'Send message →';}
}
async function sendWhatsAppMessage(text) {
  const clean=String(text || '').trim();if(!clean || state.whatsappSending)return;
  const fingerprint=JSON.stringify({text:clean,sender:WHATSAPP_SENDER});
  if(state.whatsappPending?.fingerprint!==fingerprint)state.whatsappPending={fingerprint,event_id:crypto.randomUUID(),key:crypto.randomUUID()};
  const pending=state.whatsappPending, body={text:clean,sender:WHATSAPP_SENDER,event_id:pending.event_id};
  state.whatsappSending=true;state.whatsappError='';renderWhatsAppChat();
  try {
    const result=await api('/v1/whatsapp/simulate',{method:'POST',body,key:pending.key});
    state.whatsappPending=null;state.whatsappMessages=Array.isArray(result.messages)?result.messages:state.whatsappMessages;state.whatsappDraft=result.draft || null;state.whatsappResult=result.result || null;state.whatsappLoaded=true;
    $('#whatsapp-text').value='';
  } catch(error) {state.whatsappError=error.message;}
  finally {state.whatsappSending=false;renderWhatsAppChat();}
}
function panelHeading(title, subtitle='', action='') { return `<div class="panel-heading"><div><h2>${title}</h2>${subtitle?`<p>${subtitle}</p>`:''}</div>${action}</div>`; }
function overview() {
  const {metrics, purchases=[], inventory=[], activity=[]}=state.data, drafts=pendingDrafts();
  const metric=(label,value,detail,symbol,accent=false) => `<article class="metric ${accent?'metric-accent':''}"><div class="metric-label">${label}<span class="metric-symbol" aria-hidden="true">${symbol}</span></div><p class="metric-value money">${value}</p><small>${detail}</small></article>`;
  return `<div class="metrics">${metric('Purchases today',rupiah(metrics.purchases_today_idr),'Approved invoice value · WIB','↗')}${metric('Waiting for you',number(drafts.length),'Drafts to review or complete','☑',true)}${metric('Low stock',number(metrics.low_stock_count),'Based on estimated inventory','▦')}${metric('Recorded purchases',number(metrics.recorded_purchases),'Saved in your local database','▤')}</div><div class="main-grid"><div class="column"><section class="panel">${panelHeading(`Needs your attention <span class="count-pill">${drafts.length}</span>`,'A quick check today, fewer surprises tomorrow.', '<a class="text-button" href="#approvals">View all →</a>')}<div class="draft-list">${drafts.slice(0,3).map(draftCard).join('') || '<div class="empty"><strong>You’re all caught up.</strong>New purchase drafts will appear here.</div>'}</div></section><section class="panel">${panelHeading('Recent purchases','Confirmed, recorded, and easy to find.','<a class="text-button" href="#purchases">All purchases →</a>')}${purchaseTable(purchases.slice(0,5))}</section></div><div class="column"><section class="panel quick-panel"><div class="quick-icon" aria-hidden="true">✳</div><h2>A purchase. A simple message.</h2><p>Keep your kitchen moving. Turn a quick note into a purchase ready for your review.</p><div class="quick-sample">beli cabai 3 kg 150rb</div><button class="button" data-action="new-purchase">Try a quick log <span aria-hidden="true">→</span></button></section><section class="panel">${panelHeading('Kitchen stock','','<a class="text-button" href="#inventory">View all →</a>')}${inventoryRows(inventory.slice(0,3))}<div class="panel-note">Estimated from approved receipts. Physical counts and kitchen usage may differ.</div></section><section class="panel">${panelHeading('Latest activity')}${activityRows(activity.slice(0,3))}</section></div></div>`;
}
function render() {
  const page=state.page, [title,subtitle]=titles[page];
  $('#page-title').textContent=title; $('#page-subtitle').textContent=subtitle; $('#breadcrumb').textContent=page[0].toUpperCase()+page.slice(1);
  document.querySelectorAll('[data-page]').forEach(link => { link.classList.toggle('active',link.dataset.page===page); if(link.dataset.page===page)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current'); });
  if (!state.data) return;
  $('#restaurant-name').textContent=state.data.restaurant.name;
  $('#nav-count').textContent=pendingDrafts().length;
  let html='';
  if (page==='overview') html=overview();
  if (page==='approvals') html=`<section class="panel">${panelHeading('Review queue',`${pendingDrafts().length} drafts waiting for your decision`)}<div class="full-list">${pendingDrafts().map(draftCard).join('') || '<div class="empty"><strong>All clear.</strong>Every draft has a decision.</div>'}</div></section>`;
  if (page==='purchases') html=`<p class="subtle-note">Includes synthetic opening purchases used to demonstrate price comparisons. Recording a purchase does not mark a supplier as paid.</p><section class="panel">${panelHeading('Purchase history')}${purchaseTable(state.data.purchases)}</section>`;
  if (page==='inventory') html=`<section class="panel">${panelHeading('Estimated inventory','Approved received quantities only. No consumption or physical-count adjustments yet.')}${inventoryRows(state.data.inventory)}<div class="panel-note">Low-stock thresholds are demo configuration. Compare these estimates with a physical count before ordering.</div></section>`;
  if (page==='activity') html=`<section class="panel">${panelHeading('Activity history','Includes synthetic setup records. All new actions are persisted.')}${activityRows(state.data.activity)}</section>`;
  if (page==='whatsapp') html=whatsappPage();
  $('#content').innerHTML=html;
  if(page==='whatsapp' && !state.whatsappLoaded && !state.whatsappLoading)loadWhatsAppMessages();
}
async function refresh() {
  const id=++state.refreshId;
  try {
    const data=await api('/v1/overview');
    if(id!==state.refreshId)return;
    state.data=data;state.online=true;
    $('#sync-status').textContent=`Updated ${new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;
    showError('connection-error','');render();
    if(state.draft && $('#review-dialog').open && !state.busy) updateApprovalButton();
  } catch(error) {
    if(id!==state.refreshId)return;
    state.online=false;$('#sync-status').textContent='Connection lost';
    showError('connection-error',`${error.message} ${state.data?'Showing the last loaded data. Decisions are disabled until reconnected.':''}`);
    if (!state.data) $('#content').innerHTML='<div class="empty"><strong>The kitchen is temporarily offline.</strong>Use the refresh button after starting the local server.</div>';
    updateApprovalButton();
  }
}
function openIntake() { showError('intake-error',''); $('#intake-dialog').showModal(); $('#purchase-text').focus(); }
async function openReview(id) {
  try { state.draft=await api(`/v1/drafts/${encodeURIComponent(id)}`);state.dirty=false;renderReview();if(!$('#review-dialog').open)$('#review-dialog').showModal(); }
  catch(error) { toast(error.message); }
}
function renderReview() {
  const draft=state.draft, editable=['pending_approval','needs_clarification'].includes(draft.status);
  const ingredients=state.data?.ingredients || [];
  const editors=draft.items.map((item,index) => `<div class="item-editor" data-item="${index}"><div><label for="item-${index}">Ingredient</label><select id="item-${index}" name="ingredient_id" ${editable?'':'disabled'}><option value="">Select ingredient</option>${ingredients.map(ingredient => `<option value="${e(ingredient.id || ingredient.ingredient_id)}" ${(ingredient.id || ingredient.ingredient_id)===item.ingredient_id?'selected':''}>${e(ingredient.name)}</option>`).join('')}</select></div><div><label for="quantity-${index}">Quantity</label><input id="quantity-${index}" name="quantity" inputmode="decimal" value="${e(item.quantity)}" placeholder="e.g. 3" ${editable?'':'disabled'}></div><div><label for="unit-${index}">Unit</label><input id="unit-${index}" name="unit" value="${e(item.unit)}" placeholder="kg, g, l, ml" ${editable?'':'disabled'}></div><div><label for="total-${index}">Line total (Rp)</label><input id="total-${index}" name="line_total_idr" inputmode="numeric" value="${e(item.line_total_idr)}" placeholder="e.g. 150000" ${editable?'':'disabled'}></div></div><div class="item-evidence">${item.unit_price_idr ? `${rupiah(item.unit_price_idr)} / ${e(item.canonical_unit)} · Normalized quantity ${number(item.canonical_quantity)} ${e(item.canonical_unit)}` : 'Complete the fields to calculate a unit price.'}${item.comparison?.baseline_purchase_id ? `<br>Previous: ${rupiah(item.comparison.baseline_unit_price_idr)} / ${e(item.canonical_unit)} · ${e(item.comparison.baseline_supplier || 'Supplier unspecified')} · ${e(date(item.comparison.baseline_created_at))}<br>Evidence: <span class="inline-id">${e(item.comparison.baseline_purchase_id)}</span>` : '<br>No comparable approved purchase in the last 30 days.'}${editable?`<br><button type="button" class="text-button danger" data-remove-item="${index}">Remove item</button>`:''}</div>${item.comparison?.status==='price_increase'?`<div class="price-alert">↗ ${number(Number(item.comparison.change_percent).toFixed(1))}% above the previous comparable purchase.</div>`:''}`).join('');
  const distinctWarnings=[...new Set((draft.warnings||[]).map(String))], visibleWarnings=distinctWarnings.slice(0,4);
  const warningMarkup=distinctWarnings.length?`<div class="notice">${visibleWarnings.map(e).join('<br>')}${distinctWarnings.length>4?'<br>Review the fields above for the remaining details.':''}<br>Review and save corrected details to validate this draft.</div>`:'';
  $('#review-content').innerHTML=`<div class="dialog-heading"><div><p class="eyebrow">OWNER REVIEW · VERSION ${e(draft.version)}</p><h2 id="review-title">${e(itemSummary(draft.items))}</h2></div><button class="icon-button" data-close="review-dialog" aria-label="Close purchase review">×</button></div>${badge(draft.status)}<p class="source-caption">Original purchase message</p><div class="source-quote">${e(draft.source_text)}</div><form id="review-form"><label for="review-supplier">Supplier</label><input id="review-supplier" name="supplier" value="${e(draft.supplier)}" placeholder="Unspecified" maxlength="100" ${editable?'':'disabled'}>${editors}<div class="review-total"><span>Total purchase value</span><strong class="money">${rupiah(draft.total_idr)}</strong></div>${warningMarkup}<div id="review-error" class="notice error" role="alert" hidden></div>${editable?`<p id="unsaved" class="notice" hidden>Save your changes to validate the draft before approving.</p><label class="receipt-check"><input type="checkbox" id="goods-received"> <span>I confirm these goods have been received. Approval will record the purchase and add the received quantities to estimated inventory.</span></label><div class="dialog-footer review-actions"><button class="button danger" type="button" id="reject-draft">Reject</button><button class="button secondary" type="submit" id="save-draft">Save corrections</button><span class="spacer"></span><button class="button primary" type="button" id="approve-draft" disabled>Approve &amp; record</button></div>`:'<p class="muted">This decision is recorded. Purchase history remains unchanged on refresh. Payment status is unknown.</p>'}</form><p class="inline-id">Draft ${e(draft.id)}</p>`;
  if(editable) {
    const add=document.createElement('button');add.type='button';add.className='text-button';add.textContent='+ Add an item';add.id='add-item';add.disabled=draft.items.length>=20;
    $('#review-form').insertBefore(add,$('.review-total'));
    add.addEventListener('click',() => changeItems());
    document.querySelectorAll('[data-remove-item]').forEach(button => button.addEventListener('click',() => changeItems(Number(button.dataset.removeItem))));
    $('#review-form').addEventListener('input', event => { if(event.target.id==='goods-received') {updateApprovalButton();return;}state.dirty=true;$('#unsaved').hidden=false;updateApprovalButton(); });
    $('#review-form').addEventListener('submit',saveCorrections);
    $('#approve-draft').addEventListener('click',() => decide('approve'));
    $('#reject-draft').addEventListener('click',() => decide('reject'));
    updateApprovalButton();
    $('#unsaved').hidden=!state.dirty;
  }
}
function updateApprovalButton() { const button=$('#approve-draft'); if(button)button.disabled=state.busy || !state.online || state.dirty || state.draft?.status!=='pending_approval' || !$('#goods-received')?.checked; }
function reviewBusy(busy) { state.busy=busy;$('#review-form')?.querySelectorAll('button,input,select').forEach(control => control.disabled=busy);$('#review-dialog').querySelector('[data-close]')?.toggleAttribute('disabled',busy);if($('#add-item'))$('#add-item').disabled=busy || (state.draft?.items.length ?? 0)>=20;updateApprovalButton(); }
function stableMutationKey(operation,body) { const fingerprint=operation+JSON.stringify(body);if(!state.mutationKeys.has(fingerprint))state.mutationKeys.set(fingerprint,crypto.randomUUID());return state.mutationKeys.get(fingerprint); }
function editedItems() {
  return [...document.querySelectorAll('[data-item]')].map(row => {
    const original=state.draft.items[Number(row.dataset.item)], val=name => $(`[name="${name}"]`,row).value.trim();
    const total=val('line_total_idr');
    if(total && !/^\d+$/.test(total))throw new Error('Enter the line total in whole rupiah, without separators.');
    const ingredient=state.data.ingredients.find(item => (item.id || item.ingredient_id)===val('ingredient_id'));
    return {ingredient_id:val('ingredient_id') || null,raw_name:ingredient?.name || String(original.raw_name || 'Manual item').slice(0,200),quantity:val('quantity').replace(',','.') || null,unit:val('unit') || null,line_total_idr:total?Number(total):null};
  });
}
function changeItems(removeIndex) {
  if(state.busy)return;
  try {
    const items=editedItems();
    if(removeIndex===undefined){if(items.length>=20)return;items.push({ingredient_id:null,raw_name:'Manual item',quantity:null,unit:null,line_total_idr:null});}
    else items.splice(removeIndex,1);
    state.draft={...state.draft,items,supplier:$('#review-supplier').value.trim() || null,total_idr:null};state.dirty=true;renderReview();
  } catch(error) {showError('review-error',error.message);}
}
async function saveCorrections(event) {
  event.preventDefault(); if(state.busy)return;
  showError('review-error','');
  let items;
  try { items=editedItems(); } catch(error) {showError('review-error',error.message);return;}
  const body={expected_version:state.draft.version,items,supplier:$('#review-supplier').value.trim() || null};
  reviewBusy(true);
  try { state.draft=await api(`/v1/drafts/${encodeURIComponent(state.draft.id)}`,{method:'PATCH',body,key:stableMutationKey(`edit:${state.draft.id}`,body)});state.dirty=false;renderReview();await refresh();toast('Corrections saved. Review the updated totals before approving.'); }
  catch(error) { handleReviewError(error); }
  finally {reviewBusy(false);}
}
function handleReviewError(error) { showError('review-error',error.status===409 ? `${error.message} Close and reopen this purchase to review the latest version.` : error.message);if(error.status===409) {state.dirty=true;updateApprovalButton();} }
async function decide(decision) {
  if(state.busy)return;
  if(decision==='approve' && (!$('#goods-received').checked || state.dirty || !state.online))return;
  const body={expected_version:state.draft.version,decision,received:decision==='approve'};
  reviewBusy(true);showError('review-error','');
  try {
    await api(`/v1/drafts/${encodeURIComponent(state.draft.id)}/decision`,{method:'POST',body,key:stableMutationKey(`${decision}:${state.draft.id}`,body)});
    $('#review-dialog').close();state.draft=null;await refresh();
    toast(decision==='approve'?'Purchase recorded. Inventory and purchase history updated.':'Draft rejected. Inventory is unchanged.');
  } catch(error) { handleReviewError(error); }
  finally {reviewBusy(false);}
}
const PHOTO_TYPES = ['image/jpeg', 'image/png', 'image/webp'];
const MAX_PHOTO_BYTES = 5 * 1024 * 1024;
function readPhoto(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('The photo could not be read. Try another file.'));
    reader.onload = () => {
      const data = String(reader.result || '').split(',')[1];
      data ? resolve(data) : reject(new Error('The photo could not be read. Try another file.'));
    };
    reader.readAsDataURL(file);
  });
}
$('#intake-form').addEventListener('submit',async event => {
  event.preventDefault();const button=$('button[type="submit"]',event.target);if(button.disabled)return;
  const photo=$('#purchase-photo').files[0] || null;
  const supplier=$('#purchase-supplier').value.trim() || null;
  const text=$('#purchase-text').value.trim();
  if(photo && text){showError('intake-error','Use either a photo or a typed message, not both.');return;}
  if(!photo && !text){showError('intake-error','Choose a photo or type a purchase message first.');return;}
  if(photo && !PHOTO_TYPES.includes(photo.type)){showError('intake-error','Use a JPEG, PNG, or WEBP photo.');return;}
  if(photo && photo.size > MAX_PHOTO_BYTES){showError('intake-error','That photo is over 5 MB. Take a smaller photo.');return;}
  const fingerprint=photo ? `photo:${photo.name}:${photo.size}:${photo.lastModified}:${supplier || ''}` : JSON.stringify({text,supplier});
  if(state.intakeKey?.fingerprint!==fingerprint)state.intakeKey={fingerprint,key:crypto.randomUUID()};
  button.disabled=true;button.textContent=photo?'Reading photo…':'Preparing…';showError('intake-error','');
  try {
    const draft=photo
      ? await api('/v1/intake/image',{method:'POST',body:{image_base64:await readPhoto(photo),media_type:photo.type,supplier},key:state.intakeKey.key,timeout:90000})
      : await api('/v1/intake',{method:'POST',body:{text,supplier},key:state.intakeKey.key});
    $('#intake-dialog').close();event.target.reset();state.intakeKey=null;await refresh();await openReview(draft.id);
  }
  catch(error) {showError('intake-error',error.message);}
  finally {button.disabled=false;button.textContent='Prepare draft →';}
});
document.addEventListener('click',event => {
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.action==='new-purchase')openIntake();
  if(button.dataset.close && !state.busy)document.getElementById(button.dataset.close).close();
  if(button.dataset.review)openReview(button.dataset.review);
  if(button.dataset.example){$('#purchase-text').value=button.dataset.example;$('#purchase-text').focus();}
  if(button.dataset.whatsappExample){$('#whatsapp-text').value=button.dataset.whatsappExample;$('#whatsapp-text').focus();}
  if(button.dataset.whatsappReview){location.hash='#approvals';openReview(button.dataset.whatsappReview);}
});
document.addEventListener('submit',event=>{if(event.target.id==='whatsapp-form'){event.preventDefault();sendWhatsAppMessage($('#whatsapp-text').value);}});
$('#review-dialog').addEventListener('cancel',event => {if(state.busy)event.preventDefault();});
$('#refresh').addEventListener('click',refresh);
function navigate() {state.page=Object.hasOwn(titles,location.hash.slice(1))?location.hash.slice(1):'overview';render();}
window.addEventListener('hashchange',navigate);
navigate();refresh();
setInterval(() => {if(!document.hidden && !state.busy)refresh();},5000);
