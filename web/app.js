'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
// секрет от Electron: без него бэкенд не пускает к /api/*
const TOKEN = new URLSearchParams(location.search).get('t') || '';
const api = (u, opt = {}) => {
  const o = Object.assign({}, opt);
  if (TOKEN) o.headers = Object.assign({}, o.headers, { 'X-Bisquare-Token': TOKEN });
  return fetch(u, o).then(r => r.json());
};
// ссылки приходят из внешних источников и импортированных файлов — пускаем только http(s),
// иначе javascript:/file:-ссылка выполнилась бы по клику прямо в приложении
const safeUrl = u => (typeof u === 'string' && /^https?:\/\//i.test(u.trim())) ? u.trim() : null;

const FIELDS = [
  ['cities', 'text'], ['gis_api_key', 'text'], ['yandex_api_key', 'text'], ['vk_token', 'text'],
  ['max_leads', 'num'], ['max_pages', 'num'], ['api_budget', 'num'],
  ['rubrics_filter', 'text'], ['analyze_sites', 'bool'], ['concurrency', 'num'],
  ['exclude', 'text'], ['proxies', 'text'], ['pagespeed_key', 'text'], ['out_dir', 'text'],
  ['headless', 'bool'], ['cleanup_on_finish', 'bool'],
];

const S = { view: 'all', sort: 'score', dir: 'desc', q: '', status: '', show: '', minScore: 0, running: false, seq: -1,
  page: 0, pageSize: 8, leads: [], maxLeads: 2000, panel: null, leadId: null };

/* сколько карточек влезает в область списка — страница ровно по высоте, без внутренней прокрутки */
const CARD_H = 95;            // высота карточки (87) + зазор (8)
function calcPageSize() {
  const h = ($('#leads') || {}).clientHeight || 0;
  return h ? Math.max(3, Math.floor(h / CARD_H)) : (S.pageSize || 6);
}
/* 1 … 4 5 6 … 20 */
function pageList(pages, cur) {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i);
  const out = [0];
  const a = Math.max(1, cur - 1), b = Math.min(pages - 2, cur + 1);
  if (a > 1) out.push('…');
  for (let i = a; i <= b; i++) out.push(i);
  if (b < pages - 2) out.push('…');
  out.push(pages - 1);
  return out;
}

const KNOB_C = 2 * Math.PI * 34; // длина окружности диска

function readForm() {
  const o = {};
  for (const [id, t] of FIELDS) {
    const el = document.getElementById(id); if (!el) continue;
    o[id] = t === 'bool' ? el.checked : (t === 'num' ? Number(el.value || 0) : el.value);
  }
  o.sources = $$('#sources input[data-src]').filter(c => c.checked).map(c => c.dataset.src);
  S.maxLeads = Number(o.max_leads) || 2000;
  return o;
}
function applyConfig(cfg) {
  for (const [id, t] of FIELDS) {
    const el = document.getElementById(id); if (!el || !(id in cfg)) continue;
    if (t === 'bool') el.checked = !!cfg[id]; else el.value = cfg[id];
  }
  S.maxLeads = Number(cfg.max_leads) || 2000;
  const srcs = cfg.sources || ['2gis', 'osm'];
  $$('#sources input[data-src]').forEach(c => { c.checked = srcs.includes(c.dataset.src); c.closest('.src').classList.toggle('on', c.checked); });
  $('#stageTitle').innerHTML = 'LEADS · <b>' + esc((cfg.cities || '—').split('\n')[0] || '—') + '</b>';
  renderKeys(S.auth);
}

function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

/* ---------- запуск / статус ---------- */
async function toggleRun() {
  if (S.running) { await api('/api/stop', { method: 'POST' }); return; }
  const cfg = readForm();
  const r = await api('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg) });
  if (!r.ok) toast(r.msg || 'не удалось запустить'); else toast('Сбор запущен');
}
function setState(status, added) {
  const knob = $('#knob');
  const busy = status === 'running', err = status === 'error';
  knob.classList.toggle('busy', busy);
  knob.classList.toggle('err', err);
  $('#runWord').textContent = busy ? 'RUNNING' : (err ? 'ERROR' : 'IDLE');
  $('#knobCenter').textContent = busy ? '■' : (err ? '!' : '▶');
  const p = busy ? Math.min(1, (added || 0) / (S.maxLeads || 2000)) : (err ? 1 : 0);
  $('#knobArc').style.strokeDashoffset = String(KNOB_C * (1 - p));
  S.running = busy;
}

/* ---------- доступ: вход, блокировка, самоудаление ----------
   Приложение не работает без разрешения сервера. Экран входа и экран
   блокировки перекрывают окно целиком, чтобы нельзя было «просто продолжить». */
function applyAuth(a, wipe) {
  if (!a) return;

  // Доступ отозван: данные уже стёрты бэкендом, окно закрывается следом.
  if (wipe || a.access === 'revoked') {
    show('#gate', false);
    show('#blocked', true);
    $('#blockedTitle').textContent = 'Доступ отозван';
    $('#blockedNote').textContent =
      (a.note || 'Доступ к приложению закрыт.') +
      ' Данные удалены, программа будет удалена с компьютера.';
    $('#blockedRetry').classList.add('hidden');
    return;
  }

  if (!a.logged_in) {
    show('#blocked', false);
    show('#gate', true);
    show('#tlogout', false);
    return;
  }

  // Вошёл, но работать нельзя: пауза, общий стоп или нет связи.
  if (a.access === 'paused' || (!a.ok && a.access !== 'active')) {
    show('#gate', false);
    show('#blocked', true);
    $('#blockedTitle').textContent = a.ok ? 'Работа приостановлена' : 'Нет связи с сервером';
    $('#blockedNote').textContent = a.note || a.search_reason || '';
    $('#blockedRetry').classList.remove('hidden');
    return;
  }

  show('#gate', false);
  show('#blocked', false);
  show('#tlogout', true);
  // CRM видят только владелец и те, кому выдано право «Финансы».
  S.canFinance = !!a.can_finance;
  S.canBook = !!a.can_book;
  S.isOwner = !!a.is_owner;
  show('#railCrm', S.canFinance);
  for (const id of ['#btnDemo', '#btnPurge', '#btnClear']) show(id, false);
  show('#serverDbNote', true);
  // права могли отобрать, пока раздел открыт — тогда возвращаемся к лидам
  if (typeof Views !== 'undefined') Views.onAccess();
  S.auth = a;
  renderKeys(a);
  if (!S.exportWait) setExportWait(false);

  // Почему нельзя запускать поиск — видно прямо в титулбаре, а не только
  // в момент нажатия кнопки.
  const st = $('#tstate');
  if (st) {
    if (a.search_allowed) {
      st.textContent = a.user && (a.user.name || a.user.login) ? (a.user.name || a.user.login) : '';
      st.classList.remove('warn');
    } else {
      st.textContent = a.search_reason || 'поиск недоступен';
      st.classList.add('warn');
    }
  }
}
function show(sel, on) { const el = $(sel); if (el) el.classList.toggle('hidden', !on); }

// Ключи в настройках: какие заведены на сервере и можно ли вписать свой.
// Сами серверные ключи сюда не приходят — только «есть/нет».
function renderKeys(a) {
  if (!a || !a.logged_in) return;
  const mode = a.keys_mode || 'own_or_server';
  const srv = a.server_sources || {};
  const note = $('#keysNote');
  note.textContent = mode === 'server'
    ? 'Ключи даёт сервер — свои вписывать не нужно, так настроил владелец.'
    : mode === 'own'
      ? 'Владелец включил режим «только свои ключи»: без своего ключа источник не работает.'
      : 'Пустое поле — работает ключ сервера, если он заведён. Свой ключ тратит свою квоту, а не общую.';
  note.classList.remove('hidden');
  $$('.keyf').forEach(f => {
    const has = !!srv[f.dataset.key];
    const inp = f.querySelector('input');
    const own = inp.value.trim() !== '';
    f.classList.toggle('srvonly', mode === 'server');
    let hint;
    if (mode === 'server') hint = has ? '✓ ключ сервера' : '✗ на сервере ключа нет — источник не работает';
    else if (own) hint = '✓ свой ключ';
    else if (mode === 'own') hint = has ? '✗ нужен свой ключ — на сервере есть, но режим «только свои» его не отдаёт'
                                        : '✗ нужен свой ключ';
    else hint = has ? '✓ пусто — работает ключ сервера' : '✗ на сервере ключа нет — впиши свой';
    const h = f.querySelector('.khint');
    h.textContent = hint;
    h.classList.toggle('bad', hint.startsWith('✗'));
    inp.placeholder = mode !== 'own' && has ? 'пусто = ключ сервера' : 'вставь свой ключ';
  });
}

async function doLogin(e) {
  if (e) e.preventDefault();
  const btn = $('#gateBtn');
  btn.disabled = true;
  $('#gateMsg').textContent = '';
  try {
    const r = await api('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      // Адрес сервера не передаём: он зашит в приложении, менеджеру его знать
      // и вводить не нужно.
      body: JSON.stringify({
        login: $('#g_login').value.trim(),
        password: $('#g_pass').value,
      }),
    });
    if (!r.ok) { $('#gateMsg').textContent = r.msg || 'не удалось войти'; return; }
    $('#g_pass').value = '';
    await poll();
    await loadLeads();
  } catch (err) {
    $('#gateMsg').textContent = 'нет связи с сервером';
  } finally {
    btn.disabled = false;
  }
}

/* Выход. База лежит в памяти, поэтому сначала досылаем собранное на сервер —
   иначе выход посреди прогона потерял бы то, что ещё не ушло. */
async function doLogout() {
  if (S.running && !confirm('Идёт сбор. Выйти и остановить его?')) return;
  if (!confirm('Выйти из учётной записи?\n\nСобранное сначала уйдёт на сервер.')) return;

  const btn = $('#tlogout');
  btn.disabled = true;
  btn.textContent = 'выход…';
  try {
    const r = await api('/api/auth/logout', { method: 'POST' });
    if (r && r.synced === false) {
      alert('Связи с сервером нет, часть собранного могла не уйти.');
    }
  } catch (e) {}
  location.reload();
}

async function poll() {
  try {
    const s = await api('/api/state');
    applyAuth(s.auth, s.wipe);
    setState(s.status || (s.running ? 'running' : 'idle'), s.added);
    renderStats(s.stats);
    // Итог прогона: какой источник кончился и почему. Показываем один раз;
    // при открытии окна старое сообщение не всплывает.
    const n = s.notice || {};
    if (S.noticeSeq !== undefined && n.seq !== S.noticeSeq && n.text) toast(n.text, 9000);
    S.noticeSeq = n.seq;
    if (s.seq !== S.seq) { $('#log').textContent = (s.log || []).join('\n'); logScroll(); S.seq = s.seq; }
    if (s.running && ((poll._t = (poll._t || 0) + 1) % 3 === 0)) loadLeads();
  } catch (e) {}
}
function logScroll() { const el = $('#log'); if (el && S.panel === 'notifications') el.scrollTop = el.scrollHeight; }

/* ---------- статистика ---------- */
function renderStats(st) {
  if (!st) return;
  $('#c_all').textContent = st.total;
  $('#c_fav').textContent = st.favorites;
  $('#c_unchecked').textContent = st.unchecked;
  $('#c_deleted').textContent = st.deleted;
  const us = $('#c_unchecked_sites'); if (us) us.textContent = st.unchecked_sites ?? '—';
  // Количество у каждого пункта фильтров: «(0)» — фильтр работает, просто таких нет.
  const counts = Object.assign({ hot: st.hot, warm: st.warm, cold: st.cold }, st.show_counts || {});
  for (const o of document.querySelectorAll('#statusFilter option, #showFilter option')) {
    if (!o.value) continue;
    o.dataset.label = o.dataset.label || o.textContent;
    if (counts[o.value] !== undefined) o.textContent = `${o.dataset.label} (${counts[o.value]})`;
  }
  // Сортировать по рейтингу/отзывам можно, только если они у кого-то есть:
  // у OSM их нет вовсе, и выбор «ничего не делал».
  for (const [val, n] of [['rating', st.with_rating], ['reviews', st.with_reviews]]) {
    const o = $(`#sortBy option[value="${val}"]`);
    if (!o) continue;
    const none = !n;
    o.disabled = none;
    o.textContent = (val === 'rating' ? 'рейтинг' : 'отзывы') + (none ? ' — нет данных' : '');
    if (none && S.sort === val) { S.sort = 'score'; $('#sortBy').value = 'score'; }
  }
  $('#dbStats').innerHTML = [
    ['всего', st.total], ['hot', st.hot], ['warm', st.warm], ['cold', st.cold],
    ['есть номер', st.with_phone ?? 0],
    ['избранные', st.favorites], ['не проверено', st.unchecked], ['корзина', st.deleted],
  ].map(([k, v]) => `<span class="dbchip">${k}<b>${v}</b></span>`).join('');
}

/* ---------- фильтры: «показать» и «скор от» помнятся между запусками ---------- */
function saveFilters() {
  try { localStorage.setItem('bq_filters', JSON.stringify({ show: S.show, min: S.minScore })); } catch (e) {}
}
function restoreFilters() {
  let f = {};
  try { f = JSON.parse(localStorage.getItem('bq_filters') || '{}') || {}; } catch (e) {}
  if ($(`#showFilter option[value="${f.show || ''}"]`)) { S.show = f.show || ''; $('#showFilter').value = S.show; }
  const m = Number(f.min) || 0;
  if ($(`#minScore option[value="${m}"]`)) { S.minScore = m; $('#minScore').value = String(m); }
}

/* ---------- список + пагинация ---------- */
async function loadLeads() {
  const p = new URLSearchParams({ view: S.view, sort: S.sort, dir: S.dir, q: S.q, status: S.status,
                                  show: S.show, min: S.minScore });
  const r = await api('/api/leads?' + p.toString());
  renderStats(r.stats);
  S.leads = r.leads || [];
  if (S.page > Math.ceil(S.leads.length / S.pageSize) - 1) S.page = 0;
  renderPage();
}
function renderPage() {
  const box = $('#leads');
  // подстроить размер страницы под высоту окна, удержав текущую позицию в списке
  const ps = calcPageSize();
  if (ps !== S.pageSize) {
    const first = S.page * S.pageSize;
    S.pageSize = ps;
    S.page = Math.floor(first / ps);
  }
  const total = S.leads.length, pages = Math.max(1, Math.ceil(total / S.pageSize));
  if (S.page >= pages) S.page = pages - 1;
  if (S.page < 0) S.page = 0;
  if (!total) {
    // пустое состояние живёт ВНУТРИ списка, чтобы его высота не схлопывалась
    box.innerHTML = '<div class="empty">' + (S.view === 'deleted' ? 'Корзина пуста.'
      : '<div><b>Пусто.</b><br>Открой ⚙ Settings, выбери источники и город, затем нажми круглую кнопку ▶ справа вверху.<br>Или ▤ Database → «Загрузить демо-данные».</div>') + '</div>';
  } else {
    const slice = S.leads.slice(S.page * S.pageSize, S.page * S.pageSize + S.pageSize);
    box.innerHTML = slice.map(cardHTML).join('');
    $$('[data-act]', box).forEach(b => b.onclick = e => { e.stopPropagation(); leadAction(b.dataset.id, b.dataset.act); });
    // клик по плашке — подробная карточка (кроме кнопок действий и ссылок)
    $$('.lead', box).forEach(card => card.onclick = e => {
      if (e.target.closest('[data-act]') || e.target.closest('a')) return;
      openLead(card.dataset.id);
    });
  }
  renderPager(pages, total);
  // высота списка известна только после раскладки — один безопасный пересчёт
  if (!renderPage._pending) {
    renderPage._pending = true;
    requestAnimationFrame(() => {
      renderPage._pending = false;
      if (calcPageSize() !== S.pageSize) renderPage();
    });
  }
}
function renderPager(pages, total) {
  $('#pnums').innerHTML = pageList(pages, S.page).map(x =>
    x === '…' ? '<span class="pdots">…</span>'
      : `<button class="pnum ${x === S.page ? 'active' : ''}" data-p="${x}">${x + 1}</button>`).join('');
  $$('#pnums .pnum').forEach(b => b.onclick = () => { S.page = +b.dataset.p; renderPage(); });
  $('#prev').disabled = S.page <= 0;
  $('#next').disabled = S.page >= pages - 1;
  $('#pager').classList.toggle('hidden', pages <= 1);
  const from = total ? S.page * S.pageSize + 1 : 0, to = Math.min(total, (S.page + 1) * S.pageSize);
  $('#footInfo').textContent = total ? `${from}–${to} из ${total}` : 'нет записей';
}
function goPage(delta) {
  const pages = Math.max(1, Math.ceil(S.leads.length / S.pageSize));
  const next = S.page + delta;
  if (next >= 0 && next < pages) { S.page = next; renderPage(); }
}
function cardHTML(l) {
  const st = l.status || 'cold';
  const lab = st === 'hot' ? 'HOT' : (st === 'warm' ? 'WARM' : 'COLD');
  const w = safeUrl(l.website);
  const site = w
    ? `<a href="${esc(w)}" target="_blank" rel="noopener">${esc(w.replace(/^https?:\/\//, ''))}</a>`
    : `<span class="no">${l.website ? esc(l.website) : 'нет сайта'}</span>`;
  const rating = l.rating ? `★ ${l.rating}${l.reviews_count ? ' · ' + l.reviews_count : ''}` : '';
  const del = l.deleted;
  const actions = del
    ? `<button class="act" data-id="${l.id}" data-act="restore" title="Восстановить">↩</button>`
    : `<button class="act ${l.favorite ? 'on' : ''}" data-id="${l.id}" data-act="${l.favorite ? 'unfav' : 'fav'}" title="Избранное">${l.favorite ? '★' : '☆'}</button>
       <button class="act ${l.checked ? 'on' : ''}" data-id="${l.id}" data-act="${l.checked ? 'uncheck' : 'check'}" title="Проверен">✓</button>
       <button class="act" data-id="${l.id}" data-act="delete" title="В корзину">🗑</button>`;
  return `<div class="lead ${del ? 'del' : ''}" data-id="${l.id}" title="Открыть карточку">
    <div class="score ${st}" title="${esc((l.reasons && l.reasons.length) ? l.reasons.join('\n') : 'скор ' + (l.score ?? 0))}"><span class="n">${l.score ?? 0}</span><span class="lab">${lab}</span></div>
    <div class="lmain">
      <div class="lname">${esc(l.name || 'без названия')}${l.industry ? `<span class="ind">${esc(l.industry)}</span>` : ''}</div>
      <div class="lmeta">
        ${l.city ? `<span>◍ ${esc(l.city)}</span>` : ''}
        ${l.phone ? `<span>☎ ${esc(l.phone)}</span>` : ''}
        <span>⚇ ${site}</span>${rating ? `<span>${esc(rating)}</span>` : ''}
      </div>
      ${l.recommendation ? `<div class="lrec">${esc(l.recommendation)}</div>` : ''}
    </div>
    <div class="lactions">${actions}</div>
  </div>`;
}
/* ---------- подробная карточка: всё, что собрал бот ---------- */
const _fmt = v => (v === null || v === undefined || v === '') ? null : v;
const _yn = v => v === true ? 'да' : (v === false ? 'нет' : null);
/* value — уже безопасный HTML (на местах вызова plain-текст прогоняем через esc) */
function drow(label, value) {
  const v = _fmt(value);
  return `<div class="drow"><span>${esc(label)}</span><b class="${v === null ? 'dim' : ''}">${v === null ? '—' : v}</b></div>`;
}
/* ---------- связь с клиентом: звонок → исход → календарь ----------
   Статус звонка живёт на сервере и общий для всех: «в работе» у одного —
   значит, второй менеджер этому бизнесу не звонит. */
const CALL_WORD = {
  new: 'не звонили', in_progress: 'на связи — отметь исход', no_answer: 'недозвон',
  callback: 'перезвонить', interested: 'согласился', deal: 'согласился', refused: 'отказался',
};

function contactBlockHTML(l) {
  const cs = l.call_status || 'new';
  const busy = l.busy_by_other;
  let h = `<div class="dsec">Связь с клиентом</div>
    <div class="drow"><span>Статус</span><b class="cstate ${cs}">${esc(CALL_WORD[cs] || cs)}${
      busy && l.owner_name ? ' · ведёт ' + esc(l.owner_name) : ''}</b></div>`;

  if (busy) {
    h += `<div class="dnote">Этот клиент в работе у другого менеджера — не звони ему, чтобы не дублировать.</div>`;
    return h;
  }
  if (cs === 'refused') {
    h += `<div class="dnote">Отказался — в стоп-листе, повторно в поиск не попадёт.</div>`;
    return h;
  }

  if (cs === 'in_progress') {
    h += `<div class="dact">
        <button class="pbtn primary" data-c="agreed">✓ Согласился</button>
        <button class="pbtn danger" data-c="refused">✕ Отказался</button>
      </div>
      <div class="dact">
        <button class="pbtn" data-c="no_answer">Недозвон</button>
        <button class="pbtn" data-c="callback">Перезвонить</button>
        <button class="pbtn" data-c="cancel">Отменить</button>
      </div>`;
  } else if (cs === 'deal' || cs === 'interested') {
    h += `<div class="dact">
        <button class="pbtn primary" data-w="calendar">▦ Занять даты</button>
        ${S.canFinance ? '<button class="pbtn" data-w="crm">₽ Составить смету</button>' : ''}
      </div>`;
  } else {
    h += `<div class="dact"><button class="pbtn primary" data-c="start">☎ Связываюсь с клиентом</button></div>`;
  }
  return h;
}

async function contactAction(id, act) {
  const l = S.leads.find(x => x.id === id);
  if (act === 'refused' &&
      !confirm('Клиент отказался?\n\nОн уйдёт в стоп-лист и больше не появится в поиске — ни у кого.')) return;

  const r = await api('/api/contact', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lead_id: id, action: act }),
  });
  if (!r || !r.ok) { toast((r && r.msg) || 'не получилось'); return; }

  const words = {
    start: 'Клиент твой — отметь исход после разговора', agreed: 'Согласился — открываю календарь',
    refused: 'Отказ записан, клиент больше не появится', no_answer: 'Недозвон записан',
    callback: 'Отмечено: перезвонить', cancel: 'Клиент снова свободен',
  };
  toast(words[act] || 'Готово');

  // Согласился — сразу в календарь, с уже вписанным клиентом.
  if (act === 'agreed') {
    const d = r.booking_draft || {};
    closePanels();
    Views.openCalendar({ client: d.client_name || (l && l.name) || '',
                         lead: id, title: d.title || (l && l.industry) || '' });
  }
  await loadLeads();
  if (S.panel === 'lead' && S.leadId === id) {
    if (S.leads.some(x => x.id === id)) openLead(id); else closePanels();
  }
}

function leadDetailHTML(l) {
  const s = l.site || {};
  const hasSite = Object.keys(s).filter(k => k !== '_done').length > 0;
  const st = l.status || 'cold';
  const lab = st === 'hot' ? 'HOT' : (st === 'warm' ? 'WARM' : 'COLD');
  const link = u => {
    const s = safeUrl(u);
    if (s) return `<a href="${esc(s)}" target="_blank" rel="noopener">${esc(s)}</a>`;
    return u ? esc(String(u)) : null;      // не-http значение показываем текстом, не ссылкой
  };
  const list = a => (a && a.length) ? a.map(x => link(x)).join('<br>') : null;

  let h = `<div class="dhead">
      <div class="score ${st}"><span class="n">${l.score ?? 0}</span><span class="lab">${lab}</span></div>
      <div><div class="dname">${esc(l.name || 'без названия')}</div>
        <div class="dind">${esc(l.industry || '—')}</div></div>
    </div>`;

  h += contactBlockHTML(l);

  h += `<div class="dsec">Контакты</div>`;
  h += drow('Телефон', esc(l.phone || ''));
  h += drow('E-mail', esc(l.email || ''));
  h += drow('Сайт', link(l.website));
  h += drow('Соцсети', list(l.socials));
  h += drow('Мессенджеры', list(l.messengers));

  h += `<div class="dsec">Организация</div>`;
  h += drow('Город', esc(l.city || ''));
  h += drow('Адрес', esc(l.address || ''));
  h += drow('Рейтинг', l.rating);
  h += drow('Отзывов', l.reviews_count);
  h += drow('Часы работы', esc(l.hours || ''));
  h += drow('Соцактивность', esc(l.social_activity || ''));

  h += `<div class="dsec">Сайт — что проверил бот</div>`;
  if (!hasSite) {
    h += `<div class="dnote">${l.website
      ? 'Сайт ещё не анализировался. Включи «Анализировать сайты» в Settings и запусти сбор.'
      : 'Сайта нет — проверять нечего (это и даёт +35 к скору).'}</div>`;
  } else {
    h += drow('Открывается', _yn(s.reachable));
    h += drow('HTTPS / SSL', _yn(s.ssl));
    h += drow('Мобильная версия', _yn(s.mobile));
    h += drow('Время загрузки', s.speed_ms ? s.speed_ms + ' мс' : null);
    h += drow('PageSpeed', s.pagespeed);
    h += drow('CMS / движок', esc(s.cms || ''));
    h += drow('Форма заявки', _yn(s.has_form));
    h += drow('Онлайн-запись', _yn(s.has_booking));
    h += drow('SEO (title + description)', _yn(s.seo));
    h += drow('Кнопка связи', _yn(s.cta));
    if (s.final_url) h += drow('Итоговый адрес', link(s.final_url));
    if (s.error) h += drow('Ошибка', esc(s.error));
  }

  h += `<div class="dsec">Скоринг</div>`;
  h += drow('Скор', `${l.score ?? 0} / 100 · ${lab}`);
  h += drow('Рекомендация', esc(l.recommendation || ''));
  if (l.reasons && l.reasons.length) h += `<ul class="dwhy">${l.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>`;

  h += `<div class="dsec">Служебное</div>`;
  h += drow('Источник', esc(l.source || ''));
  h += drow('Собрано', esc(l.scraped_at || ''));
  h += drow('ID', esc(l.id || ''));

  h += `<div class="dact">
      <button class="pbtn" data-d="${l.favorite ? 'unfav' : 'fav'}">${l.favorite ? '★ Из избранного' : '☆ В избранное'}</button>
      <button class="pbtn" data-d="${l.checked ? 'uncheck' : 'check'}">${l.checked ? '✓ Снять отметку' : '✓ Проверен'}</button>
    </div>
    <div class="dact">
      <button class="pbtn ${l.deleted ? '' : 'danger'}" data-d="${l.deleted ? 'restore' : 'delete'}">${l.deleted ? '↩ Восстановить' : '🗑 В корзину'}</button>
    </div>`;
  return h;
}
function openLead(id) {
  const l = S.leads.find(x => x.id === id);
  if (!l) { closePanels(); return; }
  // openPanel внутри вызывает closePanels(), который обнуляет leadId — поэтому сначала панель, потом состояние
  if (S.panel !== 'lead') openPanel('lead');
  S.leadId = id;
  $('#leadTitle').textContent = l.name || 'Карточка лида';
  $('#leadDetail').innerHTML = leadDetailHTML(l);
  $$('#leadDetail [data-d]').forEach(b => b.onclick = () => leadAction(id, b.dataset.d));
  $$('#leadDetail [data-c]').forEach(b => b.onclick = () => contactAction(id, b.dataset.c));
  $$('#leadDetail [data-w]').forEach(b => b.onclick = () => {
    closePanels();
    if (b.dataset.w === 'crm') Views.openCrm({ client_name: l.name || '', lead_id: id });
    else Views.openCalendar({ client: l.name || '', lead: id, title: l.industry || '' });
  });
}

async function leadAction(id, act) {
  await api(`/api/lead?id=${id}&action=${act}`, { method: 'POST' });
  await loadLeads();
  if (S.panel === 'lead' && S.leadId === id) {
    if (S.leads.some(x => x.id === id)) openLead(id);
    else { closePanels(); toast('Готово — лид больше не в этом списке'); }
  }
}

/* ---------- экспорт ---------- */
/* ---------- выгрузка: только с согласия руководителя, на каждый раз ---------- */
const EXPORT_NOTE_OWNER = 'Твои выгрузки скачиваются сразу и пишутся в журнал.';
const EXPORT_NOTE_MGR = 'Выгрузка — только с согласия руководителя: он получит запрос и нажмёт «Разрешить», файл скачается сам. Одно согласие — один файл.';

async function exportNow(fmt) {
  if (S.exportWait) { toast('Уже ждём согласия руководителя на выгрузку', 4000); return; }
  const f = { format: fmt, view: S.view, sort: S.sort, dir: S.dir, q: S.q, status: S.status,
              show: S.show, min: S.minScore };
  const r = await api('/api/export/request', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                body: JSON.stringify(f) });
  if (!r.ok) { toast(r.msg || 'не удалось запросить выгрузку', 5000); return; }
  if (r.state === 'approved') { claimExport(r.id); return; }

  toast('Запрос отправлен руководителю. Файл скачается сам, когда он разрешит.', 7000);
  setExportWait(true);
  const until = Date.now() + (Number(r.ttl_min) || 15) * 60000;
  const tick = async () => {
    let s = {};
    try { s = await api('/api/export/status?id=' + encodeURIComponent(r.id)); } catch (e) {}
    if (s.state === 'approved') { setExportWait(false); claimExport(r.id); return; }
    if (s.state === 'rejected') { setExportWait(false); toast('Руководитель отклонил выгрузку', 6000); return; }
    // Связь моргнула — не сдаёмся, ждём до конца срока запроса.
    if (s.state === 'expired' || s.state === 'used' || Date.now() > until) {
      setExportWait(false); toast('Руководитель не ответил — запрос истёк. Можно запросить снова.', 6000); return;
    }
    S.exportTimer = setTimeout(tick, 4000);
  };
  S.exportTimer = setTimeout(tick, 4000);
}

function setExportWait(on) {
  S.exportWait = on;
  for (const id of ['#expJson', '#expCsv']) { const b = $(id); if (b) b.disabled = on; }
  const n = $('#exportNote');
  if (n) n.textContent = on ? '⏳ Ждём согласия руководителя… Файл скачается сам.'
                            : (S.isOwner ? EXPORT_NOTE_OWNER : EXPORT_NOTE_MGR);
}

async function claimExport(id) {
  const c = await api('/api/export/claim', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                             body: JSON.stringify({ id }) });
  if (!c.ok) { toast(c.msg || 'выгрузка не разрешена', 5000); return; }
  // скачивание идёт обычной ссылкой — заголовок не поставить, поэтому токен в query
  const a = document.createElement('a');
  a.href = '/api/export?' + new URLSearchParams({ key: c.key, token: TOKEN }).toString();
  a.download = '';
  document.body.appendChild(a); a.click(); a.remove();
  toast('Файл скачивается, копия — в папке экспорта', 5000);
}

/* ---------- панели (взаимоисключающие, обратимые) ---------- */
function openPanel(name) {
  if (S.panel === name) { closePanels(); return; }
  closePanels();
  const el = $('#panel-' + name); if (!el) return;
  el.classList.add('on'); $('#scrim').classList.add('on');
  $$('.rItem[data-panel]').forEach(r => r.classList.toggle('active', r.dataset.panel === name));
  S.panel = name;
  if (name === 'notifications') logScroll();
}
function closePanels() {
  $$('.panel').forEach(p => p.classList.remove('on'));
  $('#scrim').classList.remove('on');
  $$('.rItem[data-panel]').forEach(r => r.classList.remove('active'));
  S.panel = null;
  S.leadId = null;
}

/* ---------- тема ---------- */
function setTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  $('#dmState').textContent = t === 'dark' ? 'ON' : 'OFF';
  try { localStorage.setItem('bq_theme', t); } catch (e) {}
}

/* ---------- тост ---------- */
let toastT;
function toast(msg, ms = 2600) {
  let t = $('#toast'); if (!t) { t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.textContent = msg; t.style.opacity = '1'; clearTimeout(toastT);
  toastT = setTimeout(() => t.style.opacity = '0', ms);
}

/* ---------- события ---------- */
function bind() {
  // запуск — только круглая кнопка
  $('#knob').onclick = toggleRun;

  // чекбоксы источников (подсветка выбранных)
  $$('#sources input[data-src]').forEach(c => c.onchange = () => c.closest('.src').classList.toggle('on', c.checked));

  // вкладки
  $$('#tabs .tab').forEach(t => t.onclick = () => {
    $('#tabs .active').classList.remove('active'); t.classList.add('active');
    S.view = t.dataset.view; S.page = 0; loadLeads();
  });

  // фильтры
  let dq; $('#search').oninput = e => { clearTimeout(dq); dq = setTimeout(() => { S.q = e.target.value.trim(); S.page = 0; loadLeads(); }, 250); };
  $('#statusFilter').onchange = e => { S.status = e.target.value; S.page = 0; loadLeads(); };
  $('#showFilter').onchange = e => { S.show = e.target.value; S.page = 0; saveFilters(); loadLeads(); };
  $('#minScore').onchange = e => { S.minScore = Number(e.target.value) || 0; S.page = 0; saveFilters(); loadLeads(); };
  restoreFilters();
  $('#sortBy').onchange = e => { S.sort = e.target.value; loadLeads(); };
  $('#sortDir').onclick = () => { S.dir = S.dir === 'desc' ? 'asc' : 'desc'; $('#sortDir').textContent = S.dir === 'desc' ? '▼' : '▲'; loadLeads(); };

  // пагинация
  $('#prev').onclick = () => goPage(-1);
  $('#next').onclick = () => goPage(1);
  // перелистывание стрелками клавиатуры (когда не печатаем и не открыта панель)
  window.addEventListener('keydown', e => {
    const t = e.target, typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    if (typing || S.panel) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); goPage(-1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); goPage(1); }
  });
  // пересчитать «страницу» при изменении размера окна
  let rz; window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(renderPage, 150); });

  // правое меню -> панели (обратимо: повторный клик закрывает)
  $$('.rItem').forEach(r => r.onclick = () =>
    r.dataset.section ? Views.toggle(r.dataset.section) : openPanel(r.dataset.panel));
  $$('[data-close]').forEach(b => b.onclick = closePanels);
  $('#scrim').onclick = closePanels;
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closePanels(); });

  // экспорт
  $('#expJson').onclick = () => exportNow('json');
  $('#expCsv').onclick = () => exportNow('csv');

  // база
  $$('.keyf input').forEach(i => i.addEventListener('input', () => renderKeys(S.auth)));
  $('#btnAnalyze').onclick = async () => {
    const r = await api('/api/analyze', { method: 'POST' });
    toast(r.ok ? 'Проверка сайтов запущена — ход в журнале (Notifications)' : (r.msg || 'не удалось запустить'), 4000);
    poll();
  };
  $('#btnProbe').onclick = async () => { toast('Проверяю ключ…'); const r = await api('/api/probe', { method: 'POST' }); toast(r.msg || 'проверено'); };
  $('#btnDemo').onclick = async () => { const r = await api('/api/demo', { method: 'POST' }); loadLeads(); toast(r.ok ? 'Демо-данные загружены' : r.msg); };
  $('#btnImport').onclick = () => $('#importFile').click();
  $('#importFile').onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    try {
      const data = JSON.parse(await f.text());
      const r = await api('/api/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
      if (r.ok) { loadLeads(); toast(`Импорт: +${r.added} новых, обновлено ${r.updated}` + (r.skipped ? `, пропущено ${r.skipped}` : '')); }
      else toast(r.msg || 'не удалось импортировать');
    } catch (err) { toast('Ошибка чтения JSON: ' + (err.message || err)); }
    e.target.value = '';
  };
  $('#btnPurge').onclick = async () => { const r = await api('/api/purge', { method: 'POST' }); loadLeads(); toast(r.ok ? 'Корзина очищена' : r.msg); };
  $('#btnCleanup').onclick = async () => { await api('/api/cleanup', { method: 'POST' }); toast('Очистка следов запущена'); };
  $('#btnClearLog').onclick = async () => {
    await api('/api/log/clear', { method: 'POST' });
    $('#log').textContent = ''; S.seq = -1;
    toast('Журнал очищен');
  };
  $('#btnClear').onclick = async () => { if (confirm('Удалить ВСЕ лиды из базы? (действие необратимо)')) { await api('/api/clear', { method: 'POST' }); loadLeads(); } };

  // сохранить настройки (в панели и на рейле)
  const save = async () => { await api('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(readForm()) }); applyConfig(readForm()); toast('Настройки сохранены'); };
  $('#saveCfg2').onclick = save;

  // тема
  $('#dmToggle').onclick = () => setTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
}

/* ---------- старт ---------- */
(async function init() {
  try { setTheme(localStorage.getItem('bq_theme') || 'dark'); } catch (e) { setTheme('dark'); }
  $('#knobArc').style.strokeDashoffset = String(KNOB_C);
  bind();
  $('#gateForm').addEventListener('submit', doLogin);
  $('#tlogout').onclick = doLogout;
  $('#blockedRetry').onclick = async () => {
    await api('/api/auth/refresh', { method: 'POST' });
    await poll();
  };
  try { applyConfig(await api('/api/config')); } catch (e) {}
  await loadLeads();
  await poll();
  setInterval(poll, 1200);
})();
