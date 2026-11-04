/* The API owns every quantity, price comparison, and state transition. */
'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const e = escapeHTML;

// ---- Language: Indonesian default, English toggle (persisted) ----
const LANG_KEY = 'restopilot-lang';
function loadLang() { try { return window.localStorage.getItem(LANG_KEY); } catch { return null; } }
let LANG = loadLang() === 'en' ? 'en' : 'id';
const t = (idText, enText) => LANG === 'en' ? enText : idText;
const STATIC_I18N = {
  skip: {id:'Lewati ke konten', en:'Skip to content'},
  workspace_sub: {id:'Workspace resto', en:'Restaurant workspace'},
  nav_label: {id:'OPERASI ANDA', en:'YOUR OPERATIONS'},
  nav_overview: {id:'Ringkasan', en:'Overview'},
  nav_approvals: {id:'Persetujuan', en:'Approvals'},
  nav_purchases: {id:'Pembelian', en:'Purchases'},
  nav_inventory: {id:'Stok', en:'Inventory'},
  nav_rules: {id:'Peraturan', en:'Rules'},
  nav_links: {id:'Koneksi', en:'Connections'},
  nav_activity: {id:'Aktivitas', en:'Activity'},
  nav_whatsapp: {id:'WhatsApp demo', en:'WhatsApp demo'},
  nav_admin: {id:'Resto', en:'Restaurants'},
  session_active: {id:'Sesi aktif', en:'Session active'},
  session_note: {id:'Autentikasi demo.<br>Nomor WhatsApp dihubungkan admin per resto.', en:'Demo authentication.<br>WhatsApp numbers are linked per restaurant by the admin.'},
  logout: {id:'Keluar', en:'Log out'},
  workspace: {id:'Workspace', en:'Workspace'},
  connecting: {id:'Menghubungkan…', en:'Connecting…'},
  eyebrow: {id:'SEDIKIT KEJELASAN UNTUK DAPUR ANDA', en:'A LITTLE CLARITY FOR YOUR KITCHEN'},
  btn_log: {id:'Catat pembelian', en:'Log a purchase'},
  loading: {id:'Memuat dapur Anda…', en:'Loading your kitchen…'},
  footer_left: {id:'RestoPilot · Dari pembelian ke ketenangan.', en:'RestoPilot · From purchase to peace of mind.'},
  footer_right: {id:'Input manual · Parser aturan · Database lokal', en:'Manual intake · Rules parser · Persistent local database'},
  intake_title: {id:'Apa yang masuk ke dapur?', en:'What came into the kitchen?'},
  intake_intro: {id:'Tulis pembelian dalam Bahasa Indonesia, atau lampirkan foto nota pemasok. Semuanya Anda tinjau sebelum dicatat.', en:'Type your purchase in Indonesian, or attach a photo of the supplier invoice. Everything is reviewed before it is recorded.'},
  intake_msg_label: {id:'Pesan pembelian', en:'Purchase message'},
  try_it: {id:'Coba:', en:'Try:'},
  intake_photo_label: {id:'Atau lampirkan nota pemasok', en:'Or attach the supplier invoice'},
  intake_supplier_label: {id:'Pemasok', en:'Supplier'},
  intake_help: {id:'Untuk beberapa bahan, gunakan satu baris per bahan. Foto dibaca vision StepFun; yang tidak terbaca dibiarkan kosong untuk Anda isi. Voice note belum didukung.', en:'For several items, use one line per item. Photos are read by StepFun vision; anything it cannot read stays blank for you to fill in. Voice notes are not supported yet.'},
  intake_footer: {id:'Tidak ada yang tercatat tanpa persetujuan Anda.', en:'Nothing posts without your approval.'},
  btn_prepare: {id:'Siapkan draf', en:'Prepare draft'},
};
function applyStaticI18n() {
  const root = document.documentElement; if (root) root.lang = LANG;
  document.querySelectorAll('[data-i18n]').forEach(element => {
    const entry = STATIC_I18N[element.dataset.i18n];
    if (entry) element.innerHTML = LANG === 'en' ? entry.en : entry.id;
  });
  const toggle = $('#lang-toggle');
  if (toggle) { toggle.textContent = LANG === 'en' ? 'ID' : 'EN'; toggle.title = LANG === 'en' ? 'Ganti ke Bahasa Indonesia' : 'Switch to English'; }
}
function toggleLang() {
  LANG = LANG === 'en' ? 'id' : 'en';
  try { window.localStorage.setItem(LANG_KEY, LANG); } catch { /* private mode */ }
  applyStaticI18n();
  renderIdentity();
  render();
}
const rupiah = value => value == null ? '—' : `Rp${new Intl.NumberFormat('id-ID', {maximumFractionDigits: 0}).format(Number(value))}`;
const number = value => value == null ? '—' : new Intl.NumberFormat('id-ID', {maximumFractionDigits: 6}).format(Number(value));
const date = value => value ? new Intl.DateTimeFormat('en-GB', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Jakarta'}).format(new Date(value)) + ' WIB' : '';
const icons = {cabai:'🌶',telur:'🥚',beras:'🌾',minyak:'🫙',bawang:'🧅'};
const TITLES = {
  id: {overview:['Dapur Anda, dalam pandangan.','Pembelian, stok, dan keputusan yang menunggu Anda.'],approvals:['Saatnya meninjau.','Periksa detailnya. Anda yang memutuskan menjadi catatan.'],purchases:['Setiap pembelian tercatat.','Pembelian yang disetujui beserta sumber catatannya. Status pembayaran terpisah.'],inventory:['Tahu apa yang masuk.','Stok estimasi dari pembelian tercatat. Pemakaian dapur belum dilacak.'],rules:['Peraturan asisten resto Anda.','Aturan khusus per resto — asisten menaatinya, resto lain tidak terpengaruh.'],links:['Hubungkan nomor/chat resto Anda.','Satu nomor bisa aktif di WhatsApp dan Telegram sekaligus — per resto.'],activity:['Kisah di balik tiap catatan.','Jejak permanen draf, koreksi, dan keputusan pemilik.'],whatsapp:['Alur WhatsApp yang bisa dilatih.','Percakapan lokal permanen, dari catatan pembelian ke keputusan pemilik.']},
  en: {overview:['Your kitchen, in view.','Purchases, stock, and the decisions that need you.'],approvals:['A moment for your review.','Check the details. You decide what becomes a record.'],purchases:['Every purchase, accounted for.','Approved purchases and their source records. Payment status is separate.'],inventory:['Know what came in.','Estimated stock from recorded purchases. Kitchen usage is not tracked yet.'],rules:['Rules for your restaurant assistant.','Per-restaurant rules — the assistant obeys them, other restaurants are unaffected.'],links:['Connect your restaurant numbers.','One number can be active on WhatsApp and Telegram at once — per restaurant.'],activity:['The story behind each record.','A persistent trail of drafts, corrections, and owner decisions.'],whatsapp:['A WhatsApp flow you can rehearse.','A persistent local conversation, from purchase note to owner decision.']}
};
const titles = () => TITLES[LANG] || TITLES.id;
let WHATSAPP_SENDER = '';
const state = {user:null, data:null, adminList:null, adminSelected:null, adminRejected:[], adminForms:{}, draft:null, page:'overview', dirty:false, busy:false, online:false, intakeKey:null, decisions:new Map(), mutationKeys:new Map(), refreshId:0, inlineDecisions:new Set(), whatsappMessages:[], whatsappDraft:null, whatsappResult:null, whatsappLoading:false, whatsappError:'', whatsappLoaded:false, whatsappPending:null, whatsappSending:false, whatsappNumber:'', rules:[], rulesLoaded:false, rulesBusy:false, links:[], linksLoaded:false, linksBusy:false, opnameBusy:false};

async function api(path, {method='GET', body, key, timeout=15000} = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    // ngrok-skip-browser-warning keeps the free-tier interstitial from
    // hijacking fetch() calls with a non-JSON warning page.
    const response = await fetch(path, {method, signal:controller.signal, headers:{Accept:'application/json', 'ngrok-skip-browser-warning':'1', ...(body ? {'Content-Type':'application/json'} : {}), ...(key ? {'Idempotency-Key':key} : {})}, ...(body ? {body:JSON.stringify(body)} : {})});
    let payload;
    try { payload = await response.json(); }
    catch { throw new Error(t('Gateway mengembalikan halaman bukan-JSON (halaman peringatan ngrok?). Muat ulang halaman, setujui peringatannya, lalu coba lagi.','The gateway returned a non-JSON page (ngrok warning?). Reload the page, accept the warning, then retry.')); }
    if (!response.ok) {
      const error = new Error(payload.error?.message || 'The request could not be completed.'); error.status=response.status; throw error;
      }
    return payload.data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error(t('Permintaan kehabisan waktu. Ulangi aksi yang sama untuk memeriksa hasilnya dengan aman.','The request timed out. Retry the same action to check its result safely.'));
    if (error instanceof TypeError) throw new Error(t('Tidak dapat menghubungi server lokal. Pastikan server berjalan, lalu coba lagi.','Cannot reach the local server. Check it is running, then retry.'));
    throw error;
  } finally { clearTimeout(timer); }
}
document.addEventListener('change',event => {
  if(event.target.id==='whatsapp-sender'){WHATSAPP_SENDER=event.target.value;state.whatsappNumber=event.target.value;state.whatsappLoaded=false;state.whatsappMessages=[];state.whatsappDraft=null;state.whatsappResult=null;loadWhatsAppMessages();}
});

// ---- Admin console (role: admin) ----
const ADMIN_TITLES = {admin:['Restaurants you manage.','Every restaurant, its owner login, and its linked WhatsApp numbers.'],'admin-detail':['Restaurant detail.','Information, WhatsApp numbers, and the read-only owner dashboard.']};
async function loadAdminList() {
  const payload=await api('/v1/admin/restaurants',{timeout:30000});
  state.adminList=payload.restaurants || [];
  state.adminRejected=payload.rejected_attempts || [];
  if(!state.adminSelected && state.adminList.length)state.adminSelected=state.adminList[0].id;
  renderAdmin();
}
function renderAdmin() {
  const page=state.page, [title,subtitle]=ADMIN_TITLES[page] || ADMIN_TITLES.admin;
  $('#page-title').textContent=title; $('#page-subtitle').textContent=subtitle; $('#breadcrumb').textContent=page==='admin'?t('Resto','Restaurants'):t('Detail resto','Restaurant detail');
  const navCount=$('#nav-count');if(navCount)navCount.textContent='—';
  if(!state.adminList && !state.adminLoading){state.adminLoading=true;loadAdminList().finally(() => state.adminLoading=false);$('#content').innerHTML='<div class="loading-state"><span class="spinner"></span> Loading restaurants…</div>';return;}
  if(page==='admin')renderAdminList(); else renderAdminDetail();
}
function renderAdminList() {
  const rows=(state.adminList || []).map(resto => `<div class="admin-row ${resto.id===state.adminSelected?'selected':''}" data-open-resto="${e(resto.id)}" role="button" tabindex="0"><div><strong>${e(resto.name)}</strong><small>${resto.recorded_purchases} pembelian · ${resto.pending_drafts} draf menunggu · ${resto.whatsapp_numbers.length} nomor WA</small></div><span class="spacer"></span><span class="badge ${resto.pending_drafts?'amber':'green'}">${resto.pending_drafts?`${resto.pending_drafts} review`:'clear'}</span></div>`).join('');
  const rejected=state.adminRejected.length?`<section class="panel" style="margin-top:18px"><div class="panel-heading"><div><h2>Percobaan nomor tak terdaftar</h2><p>Pesan WhatsApp dari nomor yang belum di-link — ditolak otomatis.</p></div></div><div class="admin-list">${state.adminRejected.map(row => `<div class="number-row"><code>${e((row.message || '').match(/sender (\d+)/)?.[1] || '?')}</code><span>${e(row.message || '')}</span><span class="spacer"></span><small>${e(date(row.created_at))}</small></div>`).join('')}</div></section>`:'';
  $('#content').innerHTML=`<div class="admin-grid"><section class="panel"><div class="panel-heading"><div><h2>Daftar resto</h2></div><button class="button mini primary" data-admin-action="new-resto">+ Resto baru</button></div><div class="admin-list">${rows || '<div class="empty">Belum ada resto.</div>'}</div></section>${rejected}</div><dialog id="admin-dialog" class="review-dialog"><div id="admin-dialog-content"></div></dialog>`;
}
function renderAdminDetail() {
  const resto=(state.adminList || []).find(item => item.id===state.adminSelected);
  if(!resto){location.hash='#admin';return;}
  const numbers=resto.whatsapp_numbers.map(number => `<div class="number-row"><code>${e(number.phone)}</code><span class="badge ${number.platform==='telegram'?'amber':'green'}">${e(number.platform || 'whatsapp')}</span><span>${e(number.label || '')}</span><span class="spacer"></span><button class="text-button danger" data-admin-action="remove-number" data-phone="${e(number.phone)}" data-platform="${e(number.platform || 'whatsapp')}">Hapus</button></div>`).join('');
  const owners=resto.users.map(user => `<div class="number-row"><code>${e(user.username)}</code><span>${e(user.display_name)} · owner</span></div>`).join('');
  $('#content').innerHTML=`<button class="text-button" data-back-to="admin">← Semua resto</button><div class="admin-grid"><div class="column"><section class="panel"><div class="panel-heading"><div><h2>${e(resto.name)}</h2><p>${e(resto.address || 'Alamat belum diisi')}</p></div><button class="button mini" data-admin-action="edit-resto">Ubah info</button></div><div class="admin-list kv-list"><div><span>ID</span><span class="inline-id">${e(resto.id)}</span></div><div><span>Dibuat</span><span>${e(date(resto.created_at))}</span></div><div><span>Pembelian tercatat</span><span>${resto.recorded_purchases}</span></div><div><span>Draf menunggu</span><span>${resto.pending_drafts}</span></div></div></section><section class="panel"><div class="panel-heading"><div><h2>Nomor WhatsApp terhubung</h2><p>Hanya nomor di sini yang dapat mengirim pesan dan diproses sistem.</p></div><button class="button mini primary" data-admin-action="add-number">+ Nomor</button></div><div class="admin-list">${numbers || '<div class="empty">Belum ada nomor terhubung.</div>'}</div></section><section class="panel"><div class="panel-heading"><div><h2>Login pemilik</h2></div><button class="button mini" data-admin-action="reset-password">Reset password</button></div><div class="admin-list">${owners}</div></section></div><div class="column"><div class="readonly-note"><span aria-hidden="true">👁</span> Dashboard di bawah hanya-baca. Persetujuan pembelian tetap keputusan pemilik resto.</div><section class="panel" id="admin-overview-panel"><div class="loading-state"><span class="spinner"></span> Loading dashboard…</div></section></div></div><dialog id="admin-dialog" class="review-dialog"><div id="admin-dialog-content"></div></dialog>`;
  refreshAdminOverview();
}
let adminOverviewTicket=0;
async function refreshAdminOverview() {
  const panel=$('#admin-overview-panel');if(!panel)return;
  const ticket=++adminOverviewTicket;
  try {
    const data=await api(`/v1/admin/restaurants/${encodeURIComponent(state.adminSelected)}/overview`,{timeout:60000});
    if(ticket!==adminOverviewTicket || !$('#admin-overview-panel'))return;
    const drafts=data.drafts || [];
    panel.innerHTML=`<div class="panel-heading"><div><h2>Ringkasan ${e(data.restaurant.name)}</h2><p>Read-only view of the owner dashboard.</p></div></div><div class="draft-list" style="padding-top:6px">${drafts.filter(draft => ['pending_approval','needs_clarification'].includes(draft.status)).map(draftCardReadOnly).join('') || '<div class="empty"><strong>Tidak ada draf menunggu.</strong></div>'}</div><div class="panel-note">${data.metrics.recorded_purchases} pembelian tercatat · stok rendah: ${data.metrics.low_stock_count} · pembelian hari ini ${rupiah(data.metrics.purchases_today_idr)}</div>${inventoryRows((data.inventory || []).slice(0,5))}`;
  } catch(error) { if($('#admin-overview-panel'))$('#admin-overview-panel').innerHTML=`<div class="empty">${e(error.message)}</div>`; }
}
function draftCardReadOnly(draft) {
  const item=draft.items?.[0] || {};
  return `<article class="draft-card"><div class="draft-top"><span class="ingredient-icon" aria-hidden="true">▤</span><div><h3>${e(itemSummary(draft.items))}</h3><p>${e(draft.supplier || t('Pemasok tidak disebutkan','Supplier unspecified'))} · ${e(draft.items?.length===1 ? `${number(item.quantity)} ${item.unit || ''}` : `${draft.items?.length || 0} ${t('bahan','items')}`)}</p></div><div class="price money">${rupiah(draft.total_idr)}</div></div><div class="draft-bottom">${badge(draft.status)}</div></article>`;
}
function adminDialogForm(title, fields, action) {
  const dialog=$('#admin-dialog');if(!dialog)return;
  $('#admin-dialog-content').innerHTML=`<div class="dialog-heading"><div><p class="eyebrow">ADMIN</p><h2 id="admin-dialog-title">${e(title)}</h2></div><button class="icon-button" data-close="admin-dialog" aria-label="Tutup">×</button></div><form id="admin-form" data-admin-submit="${e(action)}">${fields}<div id="admin-form-error" class="notice error" role="alert" hidden></div><div class="dialog-footer"><span class="muted">Perubahan tercatat di activity resto.</span><button class="button primary" type="submit">Simpan</button></div></form>`;
  if(!dialog.open)dialog.showModal();
}
async function adminAction(action, button) {
  button = button || (event && event.target && event.target.closest('button'));
  const resto=state.adminList && state.adminList.find(item => item.id===state.adminSelected);
  try {
    if(action==='new-resto')return adminDialogForm('Resto baru', `<label for="f-name">Nama resto</label><input id="f-name" maxlength="200" required><label for="f-address">Alamat <span class="muted">(opsional)</span></label><input id="f-address" maxlength="200"><div class="form-grid"><div><label for="f-user">Username owner</label><input id="f-user" maxlength="50" required></div><div><label for="f-pass">Password owner</label><input id="f-pass" type="password" maxlength="200" required></div><div><label for="f-phone">Nomor WhatsApp pertama</label><input id="f-phone" placeholder="6281234567890"></div><div><label for="f-label">Label nomor</label><input id="f-label" maxlength="100"></div></div>`, 'create-resto');
    if(action==='edit-resto')return adminDialogForm('Ubah info resto', `<label for="f-name">Nama resto</label><input id="f-name" value="${e(resto.name)}" maxlength="200" required><label for="f-address">Alamat</label><input id="f-address" value="${e(resto.address || '')}" maxlength="200">`, 'update-resto');
    if(action==='add-number')return adminDialogForm('Hubungkan nomor/chat', `<label for="f-phone">Nomor / chat ID (digit, kode negara di depan)</label><input id="f-phone" placeholder="6281234567890 atau chat ID Telegram" required><label for="f-platform">Platform</label><select id="f-platform"><option value="whatsapp">WhatsApp</option><option value="telegram">Telegram</option></select><label for="f-label">Label <span class="muted">(opsional)</span></label><input id="f-label" maxlength="100">`, 'add-number');
    if(action==='remove-number'){
      const platform=button.dataset.platform || 'whatsapp';
      const response=await api(`/v1/admin/restaurants/${state.adminSelected}/numbers/${encodeURIComponent(button.dataset.phone)}?platform=${encodeURIComponent(platform)}`,{method:'DELETE',key:crypto.randomUUID()});
      replaceRestoInList(response);renderAdmin();toast('Koneksi dihapus. Pesan dari situ tidak lagi diproses.');return;
    }
    if(action==='reset-password')return adminDialogForm('Reset password owner', `<label for="f-user">Username owner</label><input id="f-user" value="${e(resto && resto.users[0] ? resto.users[0].username : '')}" maxlength="50" required><label for="f-pass">Password baru</label><input id="f-pass" type="password" maxlength="200" required>`, 'reset-password');
  } catch(error) { toast(error.message); }
}
function replaceRestoInList(detail) {
  state.adminList=(state.adminList || []).filter(item => item.id!==detail.id);
  state.adminList.push(detail);
}
document.addEventListener('submit',async event => {
  if(event.target.id!=='admin-form')return;
  event.preventDefault();
  const action=event.target.dataset.adminSubmit, value=name => { const el=$(`#f-${name}`); return el ? el.value.trim() : null; };
  try {
    let detail;
    if(action==='create-resto')detail=await api('/v1/admin/restaurants',{method:'POST',body:{name:value('name'),address:value('address'),owner_username:value('user'),owner_password:value('pass'),whatsapp_number:value('phone'),number_label:value('label')},key:crypto.randomUUID()});
    else if(action==='update-resto')detail=await api(`/v1/admin/restaurants/${state.adminSelected}`,{method:'PATCH',body:{name:value('name'),address:value('address')},key:crypto.randomUUID()});
    else if(action==='add-number')detail=await api(`/v1/admin/restaurants/${state.adminSelected}/numbers`,{method:'POST',body:{whatsapp_number:value('phone'),platform:value('platform'),label:value('label')},key:crypto.randomUUID()});
    else if(action==='reset-password')detail=await api(`/v1/admin/restaurants/${state.adminSelected}/reset-owner-password`,{method:'POST',body:{username:value('user'),new_password:value('pass')},key:crypto.randomUUID()});
    else return;
    replaceRestoInList(detail);
    $('#admin-dialog').close();renderAdmin();toast('Tersimpan.');
  } catch(error) { showError('admin-form-error', error.message); }
});

async function boot() {
  try {
    state.user = await api('/v1/auth/me');
    showApp(true);
    navigate();refresh();
    startAutoRefresh();
  } catch(error) { showLogin(); }
}
function showLogin() {
  showApp(false);
  $('#login-page').hidden = false;
  $('#login-error').hidden = true;
  $('#login-username')?.focus?.();
}
function showApp(visible) {
  $('#login-page').hidden = true;
  $('#sidebar').hidden = !visible;
  $('#shell').hidden = !visible;
}
async function login(event) {
  event.preventDefault();
  const button = (event.target.querySelector && event.target.querySelector('button[type="submit"]')) || null;
  if(button)button.disabled = true; showError('login-error','');
  try {
    state.user = await api('/v1/auth/login', {method:'POST', body:{username:$('#login-username').value.trim(), password:$('#login-password').value}});
    event.target.reset();
    showApp(true);
    state.page = state.user.role==='admin' ? 'admin' : 'overview';
    if(location.hash.slice(1)!==state.page)location.hash='#'+state.page; // hashchange fires navigate()
    navigate(); refresh();
    startAutoRefresh();
  } catch(error) { showError('login-error', error.message); }
  finally { if(button)button.disabled = false; }
}
async function logout() {
  try { await api('/v1/auth/logout', {method:'POST'}); } catch(error) { /* the session is gone either way */ }
  state.user = null; state.data = null; state.adminList = null; state.adminSelected = null;
  showLogin();
}
function renderIdentity() {
  const user = state.user; if(!user)return;
  document.body?.classList?.toggle('is-admin', user.role==='admin');
  $('#restaurant-name').textContent = user.role==='admin' ? 'Platform admin' : (user.restaurant_name || 'Restaurant workspace');
  $('#profile-name').textContent = user.display_name;
  $('#profile-role').textContent = user.role==='admin' ? 'Admin platform' : (user.restaurant_name || 'Pemilik resto');
  $('#profile-initials').textContent = user.display_name.split(/\s+/).map(part => part[0]).join('').slice(0,2).toUpperCase();
  const isAdmin = user.role==='admin';
  $('#nav-admin').hidden = !isAdmin;
  $('#nav-approvals').hidden = isAdmin;
  $('#nav-whatsapp').hidden = isAdmin;
  $('#nav-rules').hidden = isAdmin;
  $('#nav-links').hidden = isAdmin;
  $('#workspace-sub').textContent = isAdmin ? t('Semua resto','All restaurants') : t('Workspace resto','Restaurant workspace');
}
function toast(message) { $('#toast').textContent=message; $('#toast').hidden=false; clearTimeout(toast.timer); toast.timer=setTimeout(() => $('#toast').hidden=true, 5500); }
function showError(id, message) { const element=$(`#${id}`); element.textContent=message; element.hidden=!message; }
function pendingDrafts() { return (state.data?.drafts || []).filter(draft => ['pending_approval','needs_clarification'].includes(draft.status) && !state.inlineDecisions.has(draft.id)); }
function badge(status) { const text={pending_approval:'Menunggu persetujuan',needs_clarification:'Perlu detail',recorded:'Tercatat',rejected:'Dibatalkan'}; return `<span class="badge ${status==='recorded'?'green':status==='rejected'?'gray':'amber'}">${e(text[status] || status)}</span>`; }
const WA_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/></svg>';
function timeHM(value) { return value ? new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Jakarta'}).format(new Date(value)) : ''; }
function sourceChip(draft) {
  const source=draft.source;
  if(!source)return '<span class="provenance">Manual</span>';
  if(source.kind==='whatsapp')return `<span class="provenance wa">${WA_ICON}via WhatsApp · ${e(timeHM(source.created_at))}</span>`;
  return `<span class="provenance">via Simulator · ${e(timeHM(source.created_at))}</span>`;
}
function nameFor(item) { return state.data?.ingredients?.find(ingredient => ingredient.id===item.ingredient_id || ingredient.ingredient_id===item.ingredient_id)?.name || item.raw_name || item.ingredient_id || 'Unmatched item'; }
// Editor prefill: catalog display name when mapped, the raw name when new,
// and empty for a freshly added row so nothing is invented by default.
function editorNameFor(item) { if(!item.raw_name && !item.ingredient_id)return ''; const matched=(state.data?.ingredients || []).find(ingredient => (ingredient.id || ingredient.ingredient_id)===item.ingredient_id); return matched?.name || item.raw_name || ''; }
// The ingredient field is a searchable combo: typed text that matches a catalog
// name selects it, anything else stays a new raw name that approval adds to
// the catalog (service.db._resolve_new_ingredients).
function resolveIngredient(query,fallback) {
  const text=String(query||'').trim();
  if(text) {
    const matched=(state.data?.ingredients || []).find(ingredient => (ingredient.name||'').toLowerCase()===text.toLowerCase());
    if(matched)return {ingredient_id:matched.id || matched.ingredient_id,raw_name:matched.name};
    return {ingredient_id:null,raw_name:text.slice(0,200)};
  }
  return {ingredient_id:null,raw_name:String(fallback || '').slice(0,200)};
}
function itemSummary(items) { return (items || []).map(item => nameFor(item)).join(', ') || t('Pembelian perlu detail','Purchase needs details'); }
function warningFor(draft) { return draft.items?.find(item => item.comparison?.status==='price_increase'); }
function draftCard(draft) {
  const item=draft.items?.[0] || {}, spike=warningFor(draft), clarify=draft.status==='needs_clarification';
  return `<article class="draft-card ${spike?'alert':''}"><div class="draft-top"><span class="ingredient-icon" aria-hidden="true">${icons[item.ingredient_id] || '▤'}</span><div><h3>${e(itemSummary(draft.items))}</h3><p>${e(draft.supplier || t('Pemasok tidak disebutkan','Supplier unspecified'))} · ${e(draft.items?.length===1 ? `${number(item.quantity)} ${item.unit || ''}` : `${draft.items?.length || 0} ${t('bahan','items')}`)}</p>${sourceChip(draft)}</div><div class="price money">${rupiah(draft.total_idr)}<small>${t('Total pembelian','Purchase total')}</small></div></div>${spike ? `<div class="price-alert"><span aria-hidden="true">↗</span><span><strong>${number(Number(spike.comparison.change_percent).toFixed(1))}% ${t('kenaikan harga','price increase')}</strong> · ${rupiah(spike.unit_price_idr)}/${e(spike.canonical_unit)} vs ${rupiah(spike.comparison.baseline_unit_price_idr)}</span></div>` : clarify ? t('<div class="price-alert">Ada detail yang perlu diperhatikan sebelum dicatat.</div>','<div class="price-alert">Some details need your attention before this can be recorded.</div>') : ''}<div class="draft-bottom">${badge(draft.status)}<div class="draft-actions"><button class="text-button" data-review="${e(draft.id)}">${t('Tinjau pembelian','Review purchase')} <span aria-hidden="true">→</span></button><button class="button mini primary" data-approve="${e(draft.id)}"${clarify?' disabled title="Lengkapi detailnya lewat Review dulu"':''}>${t('Setujui','Approve')}</button><button class="button mini" data-reject="${e(draft.id)}">${t('Batalkan','Reject')}</button></div></div></article>`;
}
function purchaseTable(purchases) {
  if (!purchases.length) return `<div class="empty"><strong>${t('Riwayat pembelian Anda dimulai di sini.','Your purchase history starts here.')}</strong>${t('Pembelian yang disetujui akan muncul di sini.','Approved purchases will appear in this view.')}</div>`;
  return `<div class="table-wrap"><table><thead><tr><th>${t('Pembelian','Purchase')}</th><th>${t('Pemasok','Supplier')}</th><th>${t('Jumlah','Amount')}</th><th>${t('Status','Status')}</th></tr></thead><tbody>${purchases.map(purchase => `<tr><td><button class="text-button" data-review="${e(purchase.draft_id)}">${e(itemSummary(purchase.items))}</button><small>${e(date(purchase.created_at))}</small></td><td>${e(purchase.supplier || t('Tidak disebutkan','Unspecified'))}</td><td class="money">${rupiah(purchase.total_idr)}<small>${t('Pembayaran belum diketahui','Payment unknown')}</small></td><td>${badge('recorded')}</td></tr>`).join('')}</tbody></table></div>`;
}
function inventoryRows(inventory) {
  return `<div class="inventory-list">${inventory.map(item => `<div class="inventory-row"><span class="ingredient-icon" aria-hidden="true">${icons[item.ingredient_id] || '▦'}</span><div><strong>${e(item.name)}</strong><small>${item.low_stock?'<span class="badge red">Stok rendah</span>':'<span class="badge green">Tercatat</span>'}</small></div><div class="stock ${item.low_stock?'low':''}">${number(item.quantity)} <span>${e(item.unit)}</span><small>Threshold ${number(item.reorder_threshold)} ${e(item.unit)}</small></div></div>`).join('')}</div>`;
}
function activityRows(activity) {
  if (!activity.length) return `<div class="empty">${t('Aktivitas dapur Anda akan muncul di sini.','Your kitchen activity will appear here.')}</div>`;
  return `<div class="activity-list">${activity.map(item => `<div class="activity-row"><span class="activity-dot"></span><div><p>${e(item.message || item.event)}</p><small>${e(date(item.created_at))}</small></div></div>`).join('')}</div>`;
}
function whatsappPage() {
  const numbers=state.data?.whatsapp_numbers || [];
  return `<div class="whatsapp-layout"><section class="panel whatsapp-panel"><div class="whatsapp-topline"><div><p class="eyebrow">SIMULATED · META CLOUD API FLOW</p><h2>${e(state.user?.display_name || 'Pemilik')}</h2><p class="muted"><select id="whatsapp-sender" aria-label="Nomor WhatsApp pengirim">${numbers.map(number => `<option value="${e(number.phone)}"${number.phone===WHATSAPP_SENDER?' selected':''}>${e(number.phone)}${number.label?` · ${e(number.label)}`:''}</option>`).join('')}</select> · percakapan lokal</p></div><span class="simulated-badge">SIMULATED</span></div><div class="chat-shell"><div class="chat-date">TODAY · LOCAL DEMO</div><div id="whatsapp-messages" class="chat-messages" role="log" aria-live="polite" aria-relevant="additions text">${whatsappMessageHTML()}</div></div><div id="whatsapp-error" class="notice error" role="alert" ${state.whatsappError?'':'hidden'}>${e(state.whatsappError)}</div><form id="whatsapp-form" class="whatsapp-composer"><label class="sr-only" for="whatsapp-text">${t('Pesan untuk asisten RestoPilot simulasi','Message to the simulated RestoPilot assistant')}</label><textarea id="whatsapp-text" rows="2" maxlength="4000" required placeholder="${t('Tulis pesan pembelian…','Type a purchase message…')}" ${state.whatsappSending?'disabled':''}></textarea><button class="button primary" type="submit" ${state.whatsappSending?'disabled':''}>${state.whatsappSending?t('Mengirim…','Sending…'):t('Kirim pesan','Send message')} <span aria-hidden="true">→</span></button></form><div class="whatsapp-examples" aria-label="Quick message examples"><span>${t('Coba pesan:','Try a message:')}</span>${['beli cabai 3 kg 150rb','ya','batal','beli beras','berapa cabai yang tersisa?'].map(value=>`<button type="button" data-whatsapp-example="${e(value)}">${e(value)}</button>`).join('')}</div><p class="simulator-note">${t('Latihan ini menyimpan pesan di database demo lokal. Tidak terhubung ke WhatsApp dan tidak mengirim pesan sungguhan.','This rehearsal stores messages in the local demo database. It does not connect to WhatsApp or send a live message.')}</p></section><aside class="panel whatsapp-guide"><p class="eyebrow">${t('LATIHAN','THE REHEARSAL')}</p><h2>${t('Dari catatan menjadi catatan resmi.','From a note to a record.')}</h2><ol><li><span>1</span><div><strong>${t('Kirim catatan pembelian','Send a purchase note')}</strong><small>Try “beli cabai 3 kg 150rb”.</small></div></li><li><span>2</span><div><strong>${t('Tinjau draf terstruktur','Review the structured draft')}</strong><small>Correct the details in the dashboard.</small></div></li><li><span>3</span><div><strong>${t('Balas ya atau batal','Reply ya or batal')}</strong><small>Only an owner decision records or rejects it.</small></div></li></ol><a class="text-button" href="#approvals">${t('Buka antrean peninjauan','Open the review queue')} <span aria-hidden="true">→</span></a></aside></div>`;
}
function whatsappMessageHTML() {
  if(state.whatsappLoading && !state.whatsappMessages.length)return '<div class="chat-empty">Loading saved conversation…</div>';
  if(!state.whatsappMessages.length)return '<div class="chat-empty">Your rehearsal starts here. Choose a message below.</div>';
  return state.whatsappMessages.map(message=>`<article class="chat-message ${message.direction==='inbound'?'inbound':'outbound'}"><div class="chat-bubble"><p>${e(message.body)}</p><small>${e(message.direction==='inbound'?'You':'RestoPilot')} · ${e(message.status==='received'?'received':'simulated')} · ${e(date(message.created_at))}</small></div></article>`).join('') + whatsappOutcomeHTML();
}
function whatsappOutcomeHTML() {
  const draft=state.whatsappDraft, action=state.whatsappResult?.action;
  let status='';
  if(action==='approved')status=`<div class="chat-outcome recorded"><strong>${t('Pembelian tercatat.','Purchase recorded.')}</strong><span>${t('Stok dan riwayat pembelian diperbarui.','Inventory and purchase history have been updated.')}</span></div>`;
  else if(action==='rejected')status=`<div class="chat-outcome"><strong>${t('Draf dibatalkan.','Draft rejected.')}</strong><span>${t('Stok tidak berubah.','Inventory is unchanged.')}</span></div>`;
  else if(action==='review_required')status=`<div class="chat-outcome"><strong>${t('Perlu ditinjau lebih detail.','Needs a closer look.')}</strong><span>${t('Lengkapi detail yang kurang di dashboard sebelum memutuskan.','Complete the missing details in the dashboard before deciding.')}</span></div>`;
  else if(action==='stock_answer')status=`<div class="chat-outcome recorded"><strong>${t('Pertanyaan stok terjawab.','Stock answered.')}</strong><span>${t('Dibaca langsung dari catatan stok — tidak ada yang dicatat.','Read straight from the inventory ledger — nothing was recorded.')}</span></div>`;
  else if(action==='active_draft_exists')status=`<div class="chat-outcome"><strong>${t('Sudah ada draf menunggu.','One draft is already waiting.')}</strong><span>${t('Tinjau atau putuskan dulu sebelum memulai pembelian baru.','Review or decide on it before starting another purchase.')}</span></div>`;
  else if(action==='no_active_draft')status=`<div class="chat-outcome"><strong>${t('Tidak ada draf aktif.','No active draft.')}</strong><span>${t('Kirim catatan pembelian untuk memulai latihan baru.','Send a purchase note to start a new rehearsal.')}</span></div>`;
  else if(draft?.status==='needs_clarification')status=`<div class="chat-outcome"><strong>${t('Draf perlu detail.','Draft needs details.')}</strong><span>${t('Periksa kolomnya, lalu simpan koreksinya.','Review the fields, then save corrections.')}</span></div>`;
  else if(draft)status=`<div class="chat-outcome"><strong>${t('Draf siap ditinjau.','Draft ready for review.')}</strong><span>${t('Tidak ada yang dicatat sampai Anda konfirmasi barang diterima.','Nothing is recorded until you confirm received goods.')}</span></div>`;
  const review=draft && ['needs_clarification','pending_approval'].includes(draft.status) ? `<button class="button secondary chat-review" type="button" data-whatsapp-review="${e(draft.id)}">${t('Tinjau di dashboard','Review in dashboard')} <span aria-hidden="true">→</span></button>` : '';
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
function heroDelta(metrics) {
  const today=metrics.purchases_today_idr, yesterday=metrics.purchases_yesterday_idr;
  if(yesterday==null)return {text:'—',cls:''};
  if(!yesterday)return today?{text:'Hari pertama',cls:'neutral'}:{text:'—',cls:''};
  const diff=today-yesterday;
  return diff>=0?{text:`▲ ${rupiah(diff)} vs kemarin`,cls:'up'}:{text:`▼ ${rupiah(-diff)} vs kemarin`,cls:'down'};
}
function overview() {
  const {metrics, purchases=[], inventory=[], activity=[]}=state.data, drafts=pendingDrafts();
  const delta=heroDelta(metrics);
  const metric=(label,value,detail,symbol,accent=false) => `<article class="metric ${accent?'metric-accent':''}"><div class="metric-label">${label}<span class="metric-symbol" aria-hidden="true">${symbol}</span></div><p class="metric-value money">${value}</p><small>${detail}</small></article>`;
  const hero=`<article class="metric metric-hero"><div class="metric-label">${t('Pembelian hari ini','Purchases today')}<span class="metric-symbol" aria-hidden="true">↗</span></div><p class="metric-value money">${rupiah(metrics.purchases_today_idr)}</p><small class="hero-delta ${delta.cls}">${e(delta.text)}</small><small>${t('Nilai invoice disetujui · WIB','Approved invoice value · WIB')}</small></article>`;
  return `<div class="metrics">${hero}${metric(t('Menunggu Anda','Waiting for you'),number(drafts.length),t('Draf untuk ditinjau atau dilengkapi','Drafts to review or complete'),'☑',true)}${metric(t('Stok rendah','Low stock'),number(metrics.low_stock_count),t('Berdasar estimasi inventory','Based on estimated inventory'),'▦')}${metric(t('Pembelian tercatat','Recorded purchases'),number(metrics.recorded_purchases),t('Tersimpan di database lokal','Saved in your local database'),'▤')}</div><div class="main-grid"><div class="column"><section class="panel">${panelHeading(`${t('Perlu perhatian Anda','Needs your attention')} <span class="count-pill">${drafts.length}</span>`,t('Periksa cepat hari ini, besok lebih sedikit kejutan.','A quick check today, fewer surprises tomorrow.'), `<a class="text-button" href="#approvals">${t('Lihat semua →','View all →')}</a>`)}<div class="draft-list">${drafts.slice(0,3).map(draftCard).join('') || `<div class="empty"><strong>${t('Semua sudah beres.','You’re all caught up.')}</strong>${t('Draf pembelian baru akan muncul di sini.','New purchase drafts will appear here.')}</div>`}</div></section><section class="panel">${panelHeading(t('Pembelian terbaru','Recent purchases'),t('Terkonfirmasi, tercatat, mudah dicari.','Confirmed, recorded, and easy to find.'),`<a class="text-button" href="#purchases">${t('Semua pembelian →','All purchases →')}</a>`)}${purchaseTable(purchases.slice(0,5))}</section></div><div class="column"><section class="panel quick-panel"><div class="quick-icon" aria-hidden="true">✳</div><h2>${t('Satu pembelian. Satu pesan sederhana.','A purchase. A simple message.')}</h2><p>${t('Dapur tetap bergerak. Ubah catatan singkat menjadi pembelian siap tinjau.','Keep your kitchen moving. Turn a quick note into a purchase ready for your review.')}</p><div class="quick-sample">beli cabai 3 kg 150rb</div><button class="button" data-action="new-purchase">${t('Coba catat cepat →','Try a quick log →')}</button></section><section class="panel">${panelHeading(t('Stok dapur','Kitchen stock'),'',`<a class="text-button" href="#inventory">${t('Lihat semua →','View all →')}</a>`)}${inventoryRows(inventory.slice(0,3))}<div class="panel-note">${t('Estimasi dari nota yang disetujui. Hitungan fisik dan pemakaian dapur bisa berbeda.','Estimated from approved receipts. Physical counts and kitchen usage may differ.')}</div></section><section class="panel">${panelHeading(t('Aktivitas terbaru','Latest activity'))}${activityRows(activity.slice(0,3))}</section></div></div><div class="charts-grid"><section class="panel">${panelHeading(t('Pengeluaran 14 hari','14-day spending'),t('Total pembelian tercatat per hari (WIB).','Recorded purchase totals per day (WIB).'))}${dailySpendChart(state.data.charts?.daily_spend)}</section><section class="panel">${panelHeading(t('Bahan pengeluaran terbesar','Top ingredients by spend'),t('Dari pembelian 30 hari terakhir.','From purchases in the last 30 days.'))}${ingredientSpendChart(state.data.charts?.ingredient_spend)}</section></div>`;
}
function render() {
  renderIdentity();
  if(state.user?.role==='admin')return renderAdmin();
  const page=state.page, [title,subtitle]=titles()[page] || titles().overview;
  $('#page-title').textContent=title; $('#page-subtitle').textContent=subtitle; $('#breadcrumb').textContent=page[0].toUpperCase()+page.slice(1);
  document.querySelectorAll('[data-page]').forEach(link => { link.classList.toggle('active',link.dataset.page===page); if(link.dataset.page===page)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current'); });
  if (!state.data) return;
  $('#restaurant-name').textContent=state.data.restaurant.name;
  $('#workspace-sub').textContent=state.data.restaurant.name===state.user?.restaurant_name?'Restaurant workspace':'Restaurant workspace';
  $('#nav-count').textContent=pendingDrafts().length;
  let html='';
  if (page==='overview') html=overview();
  if (page==='approvals') html=`<section class="panel">${panelHeading(t('Antrean peninjauan','Review queue'),`${pendingDrafts().length} ${t('draf menunggu keputusan Anda','drafts waiting for your decision')}`)}<div class="full-list">${pendingDrafts().map(draftCard).join('') || `<div class="empty"><strong>${t('Semua beres.','All clear.')}</strong>${t('Setiap draf sudah punya keputusan.','Every draft has a decision.')}</div>`}</div></section>`;
  if (page==='purchases') html=`<p class="subtle-note">${t('Termasuk pembelian pembuka sintetis untuk demo perbandingan harga. Mencatat pembelian tidak menandai pemasok sebagai dibayar.','Includes synthetic opening purchases used to demonstrate price comparisons. Recording a purchase does not mark a supplier as paid.')}</p><section class="panel">${panelHeading(t('Riwayat pembelian','Purchase history'))}${purchaseTable(state.data.purchases)}</section>`;
  if (page==='inventory') html=`<section class="panel">${panelHeading(t('Estimasi stok','Estimated inventory'),t('Hanya jumlah barang yang diterima dan disetujui. Koreksi angkanya dengan stok opname lewat chat atau formulir di bawah.','Approved received quantities only. Correct the numbers with a stock opname over chat or with the form below.'))}${inventoryRows(state.data.inventory)}<div class="panel-note">${t('Ambang stok rendah adalah konfigurasi demo. Bandingkan dengan hitungan fisik sebelum memesan.','Low-stock thresholds are demo configuration. Compare these estimates with a physical count before ordering.')}</div></section>` + opnameFormPanel() + (state.data.stock_counts?.length ? `<section class="panel">${panelHeading(t('Riwayat stok opname','Stock count history'),t('Koreksi stok fisik dari chat dan dashboard. Semua angka dari database.','Physical corrections from chat and the dashboard. Every figure comes from the database.'))}${state.data.stock_counts.map(item=>`<div class="number-row"><code>${e(item.name)}</code><span>${e(item.estimated_quantity)} → ${e(item.counted_quantity)} (${t('selisih','delta')} ${e(item.delta)})</span><span class="spacer"></span><small>${e(date(item.applied_at))}</small></div>`).join('')}</section>` : '');
  if (page==='activity') html=`<section class="panel">${panelHeading(t('Riwayat aktivitas','Activity history'),t('Termasuk catatan pembuka sintetis. Semua tindakan baru tersimpan.','Includes synthetic setup records. All new actions are persisted.'))}${activityRows(state.data.activity)}</section>`;
  if (page==='whatsapp') html=whatsappPage();
  if (page==='rules') html=rulesPage();
  if (page==='links') html=linksPage();
  $('#content').innerHTML=html;
  if(page==='whatsapp' && !state.whatsappLoaded && !state.whatsappLoading)loadWhatsAppMessages();
  if(page==='rules' && !state.rulesLoaded && !state.rulesBusy)loadRules();
  if(page==='links' && !state.linksLoaded && !state.linksBusy)loadLinks();
}

function linksPage() {
  const rows=state.links.map(link=>`<div class="admin-row"><div><code>${e(link.phone)}</code><small> · ${e(date(link.created_at))}${link.label?` · ${e(link.label)}`:''}</small></div><span class="badge ${link.platform==='telegram'?'amber':'green'}">${e(link.platform)}</span><span class="spacer"></span><button class="button mini" data-link-delete="${e(link.phone)}" data-link-platform="${e(link.platform)}">Putuskan</button></div>`).join('');
  return `<section class="panel"><div class="panel-heading"><div><h2>Koneksi chat resto ini</h2><p>Hubungkan nomor/chat ke platform yang dipakai owner. Satu nomor boleh aktif di dua platform sekaligus; chat dari platform lain tidak diproses.</p></div></div><form id="links-form" class="whatsapp-composer"><label class="sr-only" for="links-phone">Nomor atau chat ID</label><input id="links-phone" placeholder="628123456789 atau chat ID Telegram" maxlength="15" required><label class="sr-only" for="links-platform">Platform</label><select id="links-platform" aria-label="Platform"><option value="whatsapp">WhatsApp</option><option value="telegram">Telegram</option></select><label class="sr-only" for="links-label">Label</label><input id="links-label" placeholder="Label (opsional)" maxlength="100"><button class="button primary" type="submit" ${state.linksBusy?'disabled':''}>Hubungkan</button></form><div id="links-error" class="notice error" role="alert" hidden></div><div class="admin-list">${rows || '<div class="empty"><strong>Belum ada koneksi.</strong>Hubungkan nomor WhatsApp atau chat Telegram agar pesan resto ini diproses.</div>'}</div><div class="panel-note">Notifikasi proaktif dikirim ke setiap koneksi sesuai platformnya masing-masing.</div></section>`;
}
async function loadLinks() {
  if(state.linksBusy)return;state.linksBusy=true;
  try { const data=await api('/v1/links'); state.links=data.links||[]; state.linksLoaded=true; if(state.page==='links')render(); }
  catch(error) { toast('Gagal memuat koneksi: '+error.message); }
  finally { state.linksBusy=false; }
}
async function addLink(phone, platform, label) {
  if(state.linksBusy)return;state.linksBusy=true;showError('links-error','');
  try { await api('/v1/links',{method:'POST',body:{whatsapp_number:phone,platform,label:label||null},key:crypto.randomUUID()}); state.linksLoaded=false; await loadLinks(); }
  catch(error) { showError('links-error',error.message); }
  finally { state.linksBusy=false; const button=document.querySelector('#links-form button[type="submit"]'); if(button)button.disabled=false; }
}
async function removeLink(phone, platform) {
  if(state.linksBusy)return;state.linksBusy=true;
  try { await api('/v1/links/'+encodeURIComponent(phone)+'?platform='+encodeURIComponent(platform),{method:'DELETE',key:crypto.randomUUID()}); state.linksLoaded=false; await loadLinks(); }
  catch(error) { toast('Gagal memutus koneksi: '+error.message); }
  finally { state.linksBusy=false; }
}

function dailySpendChart(daily) {
  if(!daily || !daily.length) return '<div class="empty">Belum ada data grafik.</div>';
  const max=Math.max(...daily.map(d=>d.total_idr), 1);
  return `<div class="chart-bars">`+daily.map(d=>`<div class="chart-col"><div class="chart-bar" style="height:${Math.max(2, Math.round(d.total_idr/max*100))}%" title="${e(d.date)} · ${rupiah(d.total_idr)}"></div><small>${e(d.date.slice(8))}</small></div>`).join('')+`</div>`;
}
function ingredientSpendChart(items) {
  if(!items || !items.length) return '<div class="empty">Belum ada data grafik.</div>';
  const max=Math.max(...items.map(i=>i.total_idr), 1);
  return `<div class="hbars">`+items.map(i=>`<div class="hbar-row"><small class="hbar-name">${e(i.name)}</small><div class="hbar-track"><div class="hbar" style="width:${Math.max(2, Math.round(i.total_idr/max*100))}%"></div></div><span class="money hbar-value">${rupiah(i.total_idr)}</span></div>`).join('')+`</div>`;
}
function opnameFormPanel() {
  const options=(state.data?.inventory || []).map(item=>`<option value="${e(item.ingredient_id)}">${e(item.name)} — sistem: ${e(number(item.quantity))} ${e(item.unit)}</option>`).join('');
  return `<section class="panel"><div class="panel-heading"><div><h2>Catat stok opname</h2><p>Hitung fisik satu bahan, lalu koreksi stok sistem. Tercatat di riwayat dengan selisihnya.</p></div></div><form id="opname-form" class="whatsapp-composer"><label class="sr-only" for="opname-ingredient">Bahan</label><select id="opname-ingredient" aria-label="Bahan">${options}</select><label class="sr-only" for="opname-counted">Hasil hitung fisik</label><input id="opname-counted" placeholder="Hasil hitung fisik, contoh: 2,5" maxlength="40" required><label class="sr-only" for="opname-note">Catatan</label><input id="opname-note" placeholder="Catatan (opsional)" maxlength="200"><button class="button primary" type="submit" ${state.opnameBusy?'disabled':''}>Terapkan</button></form><div id="opname-error" class="notice error" role="alert" hidden></div><div class="panel-note">Stok sistem langsung disamakan dengan hasil hitung fisik, dengan catatan selisih di riwayat.</div></section>`;
}
async function applyOpname() {
  if(state.opnameBusy)return;state.opnameBusy=true;showError('opname-error','');
  const ingredient=$('#opname-ingredient').value, counted=$('#opname-counted').value.trim(), note=$('#opname-note').value.trim() || null;
  const button=document.querySelector('#opname-form button[type="submit"]');if(button)button.disabled=true;
  try { await api('/v1/stock-counts',{method:'POST',body:{ingredient_id:ingredient,counted_quantity:counted,note},key:crypto.randomUUID()}); state.data=null; await refresh(); toast('Stok opname diterapkan.'); }
  catch(error) { showError('opname-error',error.message); }
  finally { state.opnameBusy=false; const b=document.querySelector('#opname-form button[type="submit"]'); if(b)b.disabled=false; }
}
function rulesPage() {
  const rows=state.rules.map(rule=>`<div class="admin-row"><div><p>${e(rule.rule)}</p><small>${rule.enabled?'Aktif':'Nonaktif'} · ${e(date(rule.created_at))}</small></div><span class="spacer"></span><button class="button mini" data-rule-toggle="${e(rule.id)}">${rule.enabled?'Nonaktifkan':'Aktifkan'}</button><button class="button mini" data-rule-delete="${e(rule.id)}">Hapus</button></div>`).join('');
  return `<section class="panel"><div class="panel-heading"><div><h2>Peraturan asisten</h2><p>Aturan ini hanya berlaku untuk resto ini. Contoh: "kabari kalau cabai di bawah 3 kg" mengubah ambang notifikasi, "jangan ingatkan stok telur" mematikan notifikasi telur, dan catatan lain diikuti asisten sebagai konteks gaya notifikasi.</p></div></div><form id="rules-form" class="whatsapp-composer"><label class="sr-only" for="rules-text">Tulis peraturan baru</label><textarea id="rules-text" rows="2" maxlength="500" required placeholder="Contoh: kabari kalau cabai di bawah 3 kg"></textarea><button class="button primary" type="submit" ${state.rulesBusy?'disabled':''}>Tambah peraturan</button></form><div id="rules-error" class="notice error" role="alert" hidden></div><div class="admin-list">${rows || '<div class="empty"><strong>Belum ada peraturan.</strong>Tanpa peraturan, asisten memakai ambang default tiap bahan.</div>'}</div><div class="panel-note">Asisten memeriksa stok secara berkala dan mengirim notifikasi stok rendah ke nomor/chat yang terhubung. Semua angka selalu dibaca dari database.</div></section>`;
}
async function loadRules() {
  if(state.rulesBusy)return;state.rulesBusy=true;
  try { const data=await api('/v1/rules'); state.rules=data.rules||[]; state.rulesLoaded=true; if(state.page==='rules')render(); }
  catch(error) { toast('Gagal memuat peraturan: '+error.message); }
  finally { state.rulesBusy=false; }
}
async function addRule(text) {
  if(state.rulesBusy)return;state.rulesBusy=true;showError('rules-error','');
  try { await api('/v1/rules',{method:'POST',body:{rule:text}}); state.rulesLoaded=false; await loadRules(); }
  catch(error) { showError('rules-error',error.message); }
  finally { state.rulesBusy=false; const button=document.querySelector('#rules-form button[type="submit"]'); if(button)button.disabled=false; }
}
async function toggleRule(id) {
  const rule=state.rules.find(item=>item.id===id); if(!rule||state.rulesBusy)return;state.rulesBusy=true;
  try { await api('/v1/rules/'+encodeURIComponent(id),{method:'PATCH',body:{enabled:!rule.enabled}}); state.rulesLoaded=false; await loadRules(); }
  catch(error) { toast('Gagal mengubah peraturan: '+error.message); }
  finally { state.rulesBusy=false; }
}
async function deleteRule(id) {
  if(state.rulesBusy)return;state.rulesBusy=true;
  try { await api('/v1/rules/'+encodeURIComponent(id),{method:'DELETE'}); state.rulesLoaded=false; await loadRules(); }
  catch(error) { toast('Gagal menghapus peraturan: '+error.message); }
  finally { state.rulesBusy=false; }
}
let autoRefreshTimer = null, refreshInFlight = false;
function startAutoRefresh() {
  // Exactly one interval no matter how many logins; each tick is skipped
  // while a request is still in flight so a slow tunnel never piles up
  // overlapping /overview requests.
  if (autoRefreshTimer) return;
  autoRefreshTimer = setInterval(async () => {
    if (!document.hidden && !state.busy && !refreshInFlight) await refresh();
  }, 5000);
}
async function refresh() {
  if (refreshInFlight || document.hidden || state.busy) return;
  if (!state.user) return; // logged out: nothing to poll
  refreshInFlight = true;
  try {
    // Admins have no restaurant of their own: refresh their console views
    // instead of the owner overview (which would 404 and clobber the page).
    if(state.user?.role==='admin'){
      if(state.page==='admin-detail')refreshAdminOverview();
      else if(state.page==='admin')loadAdminList().catch(()=>{});
      return;
    }
    const id=++state.refreshId;
    try {
      const data=await api('/v1/overview');
      if(id!==state.refreshId)return;
      state.data=data;state.online=true;
      if(!WHATSAPP_SENDER && data.whatsapp_numbers?.length)WHATSAPP_SENDER=state.whatsappNumber||data.whatsapp_numbers[0].phone;
      $('#sync-status').textContent=t(`Diperbarui ${new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`,`Updated ${new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`);
      showError('connection-error','');render();
      if(state.draft && $('#review-dialog').open && !state.busy) updateApprovalButton();
    } catch(error) {
      if(id!==state.refreshId)return;
      state.online=false;$('#sync-status').textContent=t('Koneksi terputus','Connection lost');
      showError('connection-error',`${error.message} ${state.data?t('Menampilkan data terakhir yang dimuat. Keputusan dinonaktifkan sampai terhubung kembali.','Showing the last loaded data. Decisions are disabled until reconnected.'):''}`);
      if (!state.data && state.user?.role!=='admin') $('#content').innerHTML=`<div class="empty"><strong>${t('Dapur sedang offline sementara.','The kitchen is temporarily offline.')}</strong>${t('Gunakan tombol refresh setelah server lokal berjalan.','Use the refresh button after starting the local server.')}</div>`;
      updateApprovalButton();
    }
  } finally { refreshInFlight = false; }
}
function openIntake() { showError('intake-error',''); $('#intake-dialog').showModal(); $('#purchase-text').focus(); }
async function openReview(id) {
  try { state.draft=await api(`/v1/drafts/${encodeURIComponent(id)}`);state.dirty=false;renderReview();if(!$('#review-dialog').open)$('#review-dialog').showModal(); }
  catch(error) { toast(error.message); }
}
function renderReview() {
  const draft=state.draft, editable=['pending_approval','needs_clarification'].includes(draft.status);
  const ingredients=state.data?.ingredients || [];
  const editors=draft.items.map((item,index) => `<div class="item-editor" data-item="${index}"><div><label for="item-${index}">Ingredient</label><input id="item-${index}" name="ingredient_query" list="ingredient-options" value="${e(editorNameFor(item))}" placeholder="Search or type a new ingredient" maxlength="200" autocomplete="off" ${editable?'':'disabled'}></div><div><label for="quantity-${index}">Quantity</label><input id="quantity-${index}" name="quantity" inputmode="decimal" value="${e(item.quantity)}" placeholder="e.g. 3" ${editable?'':'disabled'}></div><div><label for="unit-${index}">Unit</label><input id="unit-${index}" name="unit" value="${e(item.unit)}" placeholder="kg, g, l, ml" ${editable?'':'disabled'}></div><div><label for="total-${index}">Line total (Rp)</label><input id="total-${index}" name="line_total_idr" inputmode="numeric" value="${e(item.line_total_idr)}" placeholder="e.g. 150000" ${editable?'':'disabled'}></div></div><div class="item-evidence">${item.matched_from ? `<br>Typed name: '${e(item.matched_from)}' — confirm this is the right ingredient.` : ''}${item.unit_price_idr ? `${rupiah(item.unit_price_idr)} / ${e(item.canonical_unit)} · Normalized quantity ${number(item.canonical_quantity)} ${e(item.canonical_unit)}` : 'Complete the fields to calculate a unit price.'}${item.comparison?.baseline_purchase_id ? `<br>Previous: ${rupiah(item.comparison.baseline_unit_price_idr)} / ${e(item.canonical_unit)} · ${e(item.comparison.baseline_supplier || 'Supplier unspecified')} · ${e(date(item.comparison.baseline_created_at))}<br>Evidence: <span class="inline-id">${e(item.comparison.baseline_purchase_id)}</span>` : '<br>No comparable approved purchase in the last 30 days.'}${editable?`<br><button type="button" class="text-button danger" data-remove-item="${index}">Remove item</button>`:''}</div>${item.comparison?.status==='price_increase'?`<div class="price-alert">↗ ${number(Number(item.comparison.change_percent).toFixed(1))}% above the previous comparable purchase.</div>`:''}`).join('');
  const distinctWarnings=[...new Set((draft.warnings||[]).map(String))], visibleWarnings=distinctWarnings.slice(0,4);
  const warningMarkup=distinctWarnings.length?`<div class="notice">${visibleWarnings.map(e).join('<br>')}${distinctWarnings.length>4?'<br>Review the fields above for the remaining details.':''}<br>Review and save corrected details to validate this draft.</div>`:'';
  $('#review-content').innerHTML=`<div class="dialog-heading"><div><p class="eyebrow">OWNER REVIEW · VERSION ${e(draft.version)}</p><h2 id="review-title">${e(itemSummary(draft.items))}</h2></div><button class="icon-button" data-close="review-dialog" aria-label="Close purchase review">×</button></div>${badge(draft.status)}<p class="source-caption">Original purchase message</p><div class="source-quote">${e(draft.source_text)}</div><form id="review-form"><datalist id="ingredient-options">${ingredients.map(ingredient => `<option value="${e(ingredient.name)}">`).join('')}</datalist><label for="review-supplier">Supplier</label><input id="review-supplier" name="supplier" value="${e(draft.supplier)}" placeholder="Unspecified" maxlength="100" ${editable?'':'disabled'}>${editors}<div class="review-total"><span>Total purchase value</span><strong class="money">${rupiah(draft.total_idr)}</strong></div>${warningMarkup}<div id="review-error" class="notice error" role="alert" hidden></div>${editable?`<p id="unsaved" class="notice" hidden>Save your changes to validate the draft before approving.</p><label class="receipt-check"><input type="checkbox" id="goods-received"> <span>I confirm these goods have been received. Approval will record the purchase and add the received quantities to estimated inventory.</span></label><div class="dialog-footer review-actions"><button class="button danger" type="button" id="reject-draft">Reject</button><button class="button secondary" type="submit" id="save-draft">Save corrections</button><span class="spacer"></span><button class="button primary" type="button" id="approve-draft" disabled>Approve &amp; record</button></div>`:'<p class="muted">This decision is recorded. Purchase history remains unchanged on refresh. Payment status is unknown.</p>'}</form><p class="inline-id">Draft ${e(draft.id)}</p>`;
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
    const resolved=resolveIngredient(val('ingredient_query'),original.raw_name);
    return {ingredient_id:resolved.ingredient_id,raw_name:resolved.raw_name,quantity:val('quantity').replace(',','.') || null,unit:val('unit') || null,line_total_idr:total?Number(total):null};
  });
}
function changeItems(removeIndex) {
  if(state.busy)return;
  try {
    const items=editedItems();
    if(removeIndex===undefined){if(items.length>=20)return;items.push({ingredient_id:null,raw_name:'',quantity:null,unit:null,line_total_idr:null});}
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
  try { state.draft=await api(`/v1/drafts/${encodeURIComponent(state.draft.id)}`,{method:'PATCH',body,key:stableMutationKey(`edit:${state.draft.id}`,body)});state.dirty=false;renderReview();await refresh();toast(t('Koreksi tersimpan. Tinjau total terbaru sebelum menyetujui.','Corrections saved. Review the updated totals before approving.')); }
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
    toast(decision==='approve'?t('Pembelian tercatat. Stok dan riwayat pembelian diperbarui.','Purchase recorded. Inventory and purchase history updated.'):t('Draf dibatalkan. Stok tidak berubah.','Draft rejected. Inventory is unchanged.'));
  } catch(error) { handleReviewError(error); }
  finally {reviewBusy(false);}
}
async function inlineDecision(id, decision) {
  if(state.inlineDecisions.has(id) || !state.online)return;
  const draft=(state.data?.drafts || []).find(item => item.id===id);
  if(!draft || !['pending_approval','needs_clarification'].includes(draft.status))return;
  // Optimistic: drop the card and its count immediately; restore if the server refuses.
  state.inlineDecisions.add(id);render();
  const body={expected_version:draft.version,decision,received:decision==='approve'};
  try {
    await api(`/v1/drafts/${encodeURIComponent(id)}/decision`,{method:'POST',body,key:stableMutationKey(`inline-${decision}:${id}`,body)});
    const stored=(state.data?.drafts || []).find(item => item.id===id);
    if(stored)stored.status=decision==='approve'?'recorded':'rejected';
    toast(decision==='approve'?'Pembelian tercatat ✓':'Draf dibatalkan');
  } catch(error) { toast(error.message); }
  finally { state.inlineDecisions.delete(id);render();refresh(); }
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
  const photos=[...$('#purchase-photo').files];
  const pdfs=photos.filter(photo=>photo.type==='application/pdf' || photo.name.toLowerCase().endsWith('.pdf'));
  const images=photos.filter(photo=>!pdfs.includes(photo));
  const supplier=$('#purchase-supplier').value.trim() || null;
  const text=$('#purchase-text').value.trim();
  if(photos.length && text){showError('intake-error','Use either files or a typed message, not both.');return;}
  if(!photos.length && !text){showError('intake-error','Choose a file or type a purchase message first.');return;}
  if(pdfs.length && images.length){showError('intake-error','Send photos and PDFs separately — each becomes its own draft.');return;}
  for(const photo of images){
    if(!PHOTO_TYPES.includes(photo.type)){showError('intake-error','Use JPEG, PNG, or WEBP photos.');return;}
    if(photo.size > MAX_PHOTO_BYTES){showError('intake-error','A photo is over 5 MB. Take smaller photos.');return;}
  }
  for(const pdf of pdfs){
    if(pdf.size > 10 * 1024 * 1024){showError('intake-error','A PDF is over 10 MB.');return;}
  }
  if(photos.length > 5){showError('intake-error','Send at most 5 files at once — the rest can follow in another draft.');return;}
  const fingerprint=photos.length ? photos.map(photo=>`${photo.name}:${photo.size}:${photo.lastModified}`).join('|')+`:${supplier || ''}` : JSON.stringify({text,supplier});
  if(state.intakeKey?.fingerprint!==fingerprint)state.intakeKey={fingerprint,key:crypto.randomUUID()};
  button.disabled=true;button.textContent=photos.length>1?'Reading photos…':photos.length?'Reading photo…':'Preparing…';showError('intake-error','');
  try {
    const draft=pdfs.length === 1
      ? await api('/v1/intake/pdf',{method:'POST',body:{pdf_base64:await readPhoto(pdfs[0]),supplier},key:state.intakeKey.key,timeout:180000})
      : photos.length === 1
      ? await api('/v1/intake/image',{method:'POST',body:{image_base64:await readPhoto(photos[0]),media_type:photos[0].type,supplier},key:state.intakeKey.key,timeout:90000})
      : photos.length > 1
      ? await api('/v1/intake/images',{method:'POST',body:{images:await Promise.all(photos.map(async photo=>({image_base64:await readPhoto(photo),media_type:photo.type}))),supplier},key:state.intakeKey.key,timeout:180000})
      : await api('/v1/intake',{method:'POST',body:{text,supplier},key:state.intakeKey.key});
    $('#intake-dialog').close();event.target.reset();state.intakeKey=null;await refresh();await openReview(draft.id);
  }
  catch(error) {showError('intake-error',error.message);}
  finally {button.disabled=false;button.textContent='Prepare draft →';}
});
document.addEventListener('click',event => {
  // Admin rows are div[role=button], not <button>: match either.
  const button=event.target.closest('button,[data-open-resto],[data-back-to]');if(!button)return;
  if(button.dataset.action==='new-purchase')openIntake();
  if(button.dataset.close && !state.busy)document.getElementById(button.dataset.close).close();
  if(button.dataset.review)openReview(button.dataset.review);
  if(button.dataset.approve)inlineDecision(button.dataset.approve,'approve');
  if(button.dataset.reject)inlineDecision(button.dataset.reject,'reject');
  if(button.dataset.example){$('#purchase-text').value=button.dataset.example;$('#purchase-text').focus();}
  if(button.dataset.whatsappExample){$('#whatsapp-text').value=button.dataset.whatsappExample;$('#whatsapp-text').focus();}
  if(button.dataset.whatsappReview){location.hash='#approvals';openReview(button.dataset.whatsappReview);}
  if(button.dataset.openResto){state.adminSelected=button.dataset.openResto;location.hash='#admin-detail';window.scrollTo(0,0);}
  if(button.dataset.backTo){location.hash='#'+button.dataset.backTo;}
  if(button.dataset.adminAction)adminAction(button.dataset.adminAction,button);
  if(button.dataset.ruleDelete)deleteRule(button.dataset.ruleDelete);
  if(button.dataset.ruleToggle)toggleRule(button.dataset.ruleToggle);
  if(button.dataset.linkDelete)removeLink(button.dataset.linkDelete,button.dataset.linkPlatform);
});
document.addEventListener('submit',async event=>{
  if(event.target.id==='rules-form'){
    event.preventDefault();
    const text=$('#rules-text').value.trim();if(!text)return;
    const button=document.querySelector('#rules-form button[type="submit"]');if(button)button.disabled=true;
    $('#rules-text').value='';await addRule(text);
  }
  if(event.target.id==='opname-form'){
    event.preventDefault();
    const counted=$('#opname-counted').value.trim();if(!counted)return;
    const button=document.querySelector('#opname-form button[type="submit"]');if(button)button.disabled=true;
    await applyOpname();
  }
  if(event.target.id==='links-form'){
    event.preventDefault();
    const phone=$('#links-phone').value.trim();if(!phone)return;
    const platform=$('#links-platform').value,label=$('#links-label').value.trim();
    const button=document.querySelector('#links-form button[type="submit"]');if(button)button.disabled=true;
    $('#links-phone').value='';$('#links-label').value='';await addLink(phone,platform,label);
  }
});
document.addEventListener('submit',event=>{if(event.target.id==='whatsapp-form'){event.preventDefault();sendWhatsAppMessage($('#whatsapp-text').value);}});
$('#review-dialog').addEventListener('cancel',event => {if(state.busy)event.preventDefault();});
$('#refresh').addEventListener('click',refresh);
function navigate() {
  const hash=location.hash.slice(1);
  const known=Object.hasOwn(state.user?.role==='admin'?{...titles(),admin:'','admin-detail':''}:titles(),hash);
  state.page=known?hash:(state.user?.role==='admin'?'admin':'overview');
  render();
}
window.addEventListener('hashchange',navigate);
document.addEventListener('keydown',event => {
  // A focused admin row (role=button, tabindex=0) activates on Enter or Space.
  if((event.key==='Enter'||event.key===' ') && event.target.classList && event.target.classList.contains('admin-row')){
    event.preventDefault();event.target.click();
  }
});
document.addEventListener('submit',event => {if(event.target.id==='login-form')login(event);});
$('#login-form').addEventListener('submit',login);
$('#logout').addEventListener('click',logout);
boot();
