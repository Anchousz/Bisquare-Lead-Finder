'use strict';
/*
 * Разделы «Календарь» и «CRM» внутри окна приложения.
 *
 * Раздел занимает главную область вместо списка лидов; «‹ Лиды» или повторное
 * нажатие пункта меню возвращают обратно. Данные идут через бэкенд приложения
 * (/api/cloud/call) по токену устройства — на сервере работают те же проверки
 * прав, что и на сайте: менеджер не получит того, что ему не положено, даже
 * если кнопку как-то вызовет.
 *
 * Пользуется общим с app.js: $, $$, api, toast, S.
 */
const Views = (() => {

  /* ============================== общее ============================== */

  // Всё, что пришло из базы, вставляем только текстом: имена пишут люди.
  const mk = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  };

  async function call(path, query, body) {
    try {
      const r = await api('/api/cloud/call', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, query: query || {}, body: body === undefined ? null : body }),
      });
      return r || { ok: false, msg: 'нет ответа' };
    } catch (e) {
      return { ok: false, msg: 'нет связи с бэкендом приложения' };
    }
  }

  const rub = v => {
    const n = Number(v || 0);
    return n.toLocaleString('ru-RU', { minimumFractionDigits: n % 1 ? 2 : 0, maximumFractionDigits: 2 }) + ' ₽';
  };
  const num = v => {
    const n = parseFloat(String(v ?? '').replace(/\s/g, '').replace(',', '.'));
    return isFinite(n) ? n : 0;
  };
  const MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
                  'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
  const DOW = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
  const iso = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
                   '-' + String(d.getDate()).padStart(2, '0');
  const parse = s => { const [y, m, d] = String(s).split('-').map(Number); return new Date(y, m - 1, d); };
  const human = s => { const d = parse(s); return d.getDate() + ' ' + MONTHS[d.getMonth()].slice(0, 3); };
  const daysBetween = (a, b) => Math.round((parse(b) - parse(a)) / 86400000) + 1;
  function plural(n, one, few, many) {
    const a = n % 10, b = n % 100;
    if (a === 1 && b !== 11) return one;
    if (a >= 2 && a <= 4 && (b < 12 || b > 14)) return few;
    return many;
  }

  /* Шапка раздела: «‹ Лиды», заголовок, управление справа. */
  function head(title, sub) {
    const h = mk('div', 'vhead');
    const back = mk('button', 'vback', '‹ Лиды');
    back.type = 'button';
    back.title = 'Назад к списку лидов (Esc)';
    back.onclick = () => setMode('leads');
    h.appendChild(back);
    const t = mk('div', 'vtitle');
    t.appendChild(document.createTextNode(title + (sub ? ' · ' : '')));
    if (sub) t.appendChild(mk('b', null, sub));
    h.appendChild(t);
    const ctl = mk('div', 'vctl');
    h.appendChild(ctl);
    return { el: h, ctl, title: t };
  }

  function btn(label, cls, fn) {
    const b = mk('button', 'vbtn' + (cls ? ' ' + cls : ''), label);
    b.type = 'button';
    b.onclick = fn;
    return b;
  }

  /* Окно поверх раздела. Сначала на страницу, потом наполнение — поля ищутся
     через document. Одно окно за раз. */
  function modal({ title, note, build, onOk, okLabel, danger, wide, onDelete }) {
    closeModal();
    const ov = mk('div', 'vmodal-ov');
    const m = mk('div', 'vmodal' + (wide ? ' wide' : ''));
    m.setAttribute('role', 'dialog');
    m.setAttribute('aria-label', title);
    m.appendChild(mk('h3', null, title));
    if (note) { const p = mk('p', 'vnote', note); p.style.whiteSpace = 'pre-line'; m.appendChild(p); }
    const body = mk('div');
    m.appendChild(body);
    ov.appendChild(m);
    document.body.appendChild(ov);
    if (build) build(body);

    const msg = mk('div');
    m.appendChild(msg);
    const foot = mk('div', 'vfoot');
    if (onDelete) {
      const del = btn('Удалить', 'danger', async () => { if (await onDelete()) closeModal(); });
      del.style.marginRight = 'auto';
      foot.appendChild(del);
    }
    foot.appendChild(btn('Отмена', '', closeModal));
    const ok = btn(okLabel || 'Готово', danger ? 'danger' : 'primary', async () => {
      ok.disabled = true;
      msg.innerHTML = '';
      const err = await onOk();
      ok.disabled = false;
      if (err) { msg.appendChild(mk('div', 'verr', err)); return; }
      closeModal();
    });
    foot.appendChild(ok);
    m.appendChild(foot);
    ov.addEventListener('click', e => { if (e.target === ov) closeModal(); });
    const first = m.querySelector('input,select,textarea');
    if (first) first.focus();
  }
  function closeModal() { document.querySelectorAll('.vmodal-ov').forEach(x => x.remove()); }

  const field = (label, input) => {
    const w = mk('div', 'vfld');
    const l = mk('label', null, label);
    if (input.id) l.setAttribute('for', input.id);
    w.appendChild(l);
    w.appendChild(input);
    return w;
  };
  const inp = (id, val, ph, type) => {
    const i = mk('input');
    i.id = id; i.value = val ?? '';
    if (ph) i.placeholder = ph;
    if (type) i.type = type;
    return i;
  };

  /* ============================== режимы ============================== */

  let mode = 'leads';

  function setMode(m) {
    mode = m;
    closeModal();
    document.body.dataset.mode = m;
    $('#view-calendar').classList.toggle('hidden', m !== 'calendar');
    $('#view-crm').classList.toggle('hidden', m !== 'crm');
    $$('.rItem[data-section]').forEach(r => r.classList.toggle('active', r.dataset.section === m));
    // Список пересчитывает размер страницы по своей высоте, а пока был скрыт,
    // высота была нулевой — даём ему пересчитаться.
    if (m === 'leads') requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
  }

  function toggle(section) {
    if (mode === section) { setMode('leads'); return; }
    if (section === 'calendar') openCalendar();
    else if (section === 'crm') openCrm();
  }

  /* Права сменились на лету (heartbeat): без «Финансов» из CRM уводим. */
  function onAccess() {
    if (mode === 'crm' && !S.canFinance) { setMode('leads'); toast('Доступ к CRM закрыт'); }
  }

  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (document.querySelector('.vmodal-ov')) { closeModal(); return; }
    if (mode !== 'leads' && !S.panel) setMode('leads');
  });

  /* ============================ КАЛЕНДАРЬ ============================ */

  const C = { cur: new Date(), data: null, selected: null, justId: null };

  async function openCalendar(draft) {
    setMode('calendar');
    await loadCalendar();
    if (draft) {
      if (C.data && C.data.me && C.data.me.can_book) modalBooking(null, draft);
      else toast('Занимать даты тебе не разрешено — это выдаёт владелец');
    }
  }

  async function loadCalendar() {
    const box = $('#view-calendar');
    if (!C.data) { box.innerHTML = ''; box.appendChild(mk('div', 'vempty', 'Загружаю календарь…')); }
    const y = C.cur.getFullYear(), m = C.cur.getMonth();
    const d = await call('bookings.php', { from: iso(new Date(y, m - 1, 1)), to: iso(new Date(y, m + 2, 0)) });
    if (!d.ok) {
      box.innerHTML = '';
      const h = head('КАЛЕНДАРЬ');
      box.appendChild(h.el);
      box.appendChild(mk('div', 'vempty', d.msg || 'не удалось загрузить календарь'));
      return;
    }
    C.data = d;
    renderCalendar();
  }

  function renderCalendar() {
    const d = C.data, me = d.me || {};
    const box = $('#view-calendar');
    box.innerHTML = '';
    const y = C.cur.getFullYear(), m = C.cur.getMonth();

    const h = head('КАЛЕНДАРЬ', MONTHS[m] + ' ' + y);
    const nav = mk('div', 'vmonth');
    const prev = btn('‹', 'icon', () => { C.cur = new Date(y, m - 1, 1); C.selected = null; loadCalendar(); });
    const next = btn('›', 'icon', () => { C.cur = new Date(y, m + 1, 1); C.selected = null; loadCalendar(); });
    prev.setAttribute('aria-label', 'Предыдущий месяц');
    next.setAttribute('aria-label', 'Следующий месяц');
    nav.appendChild(prev); nav.appendChild(next);
    h.ctl.appendChild(nav);
    if (me.can_book) h.ctl.appendChild(btn('Занять период', 'primary', () => modalBooking(null)));
    // Закрывать даты может только владелец — менеджер этой кнопки не видит.
    if (me.is_owner) h.ctl.appendChild(btn('Закрыть даты', '', modalBlackout));
    box.appendChild(h.el);

    // сетка месяца
    const grid = mk('div', 'vgrid');
    DOW.forEach(w => grid.appendChild(mk('div', 'vdow', w)));
    const start = new Date(y, m, 1 - ((new Date(y, m, 1).getDay() + 6) % 7));
    const today = iso(new Date());
    const closed = new Set(d.closed || []);
    const byDay = {};
    d.bookings.forEach(b => {
      for (let t = parse(b.date_from); t <= parse(b.date_to); t.setDate(t.getDate() + 1)) {
        (byDay[iso(t)] = byDay[iso(t)] || []).push(b);
      }
    });

    for (let i = 0; i < 42; i++) {
      const day = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
      const key = iso(day);
      const here = byDay[key] || [];
      const cell = mk('button', 'vday');
      cell.type = 'button';
      if (day.getMonth() !== m) cell.classList.add('out');
      if (key === today) cell.classList.add('today');
      if (C.selected === key) cell.classList.add('sel');
      if (closed.has(key)) cell.classList.add('closed');
      else if (here.length >= (d.parallel || 2)) cell.classList.add('full');
      else if (here.length) cell.classList.add('busy');
      cell.appendChild(mk('span', 'n', day.getDate()));
      here.slice(0, 2).forEach(b => cell.appendChild(mk('span', 'who' + (b.state === 'pending' ? ' pend' : ''), b.client_name)));
      if (here.length > 2) cell.appendChild(mk('span', 'more', '+ ещё ' + (here.length - 2)));
      if (closed.has(key)) cell.appendChild(mk('span', 'more', 'закрыто'));
      cell.setAttribute('aria-label', day.getDate() + ' ' + MONTHS[day.getMonth()] +
        (here.length ? ', занято: ' + here.length : ', свободно'));
      cell.onclick = () => {
        C.selected = C.selected === key ? null : key;
        renderCalendar();
        if (C.selected) box.querySelector('.vlisthead').scrollIntoView({ behavior: 'smooth', block: 'start' });
      };
      grid.appendChild(cell);
    }
    box.appendChild(grid);

    const legend = mk('div', 'vlegend');
    legend.textContent = 'Нажми день — ниже его записи. Пунктир — ждёт подтверждения, сплошная — подтверждено.';
    box.appendChild(legend);

    // список записей
    const lh = mk('div', 'vlisthead');
    let rows;
    if (C.selected) {
      rows = d.bookings.filter(b => b.date_from <= C.selected && b.date_to >= C.selected);
      lh.appendChild(mk('b', null, 'Записи на ' + human(C.selected)));
      lh.appendChild(btn('Весь месяц', 'ghost small', () => { C.selected = null; renderCalendar(); }));
    } else {
      rows = d.bookings.filter(b => !(parse(b.date_to) < new Date(y, m, 1) || parse(b.date_from) > new Date(y, m + 1, 0)));
      lh.appendChild(mk('b', null, 'Записи месяца'));
    }
    box.appendChild(lh);

    const list = mk('div', 'vlist');
    if (!rows.length) list.appendChild(mk('div', 'vempty', C.selected ? 'На этот день записей нет' : 'В этом месяце пусто'));
    rows.sort((a, b) => a.date_from < b.date_from ? -1 : 1).forEach(b => list.appendChild(bookingCard(b, me)));
    box.appendChild(list);

    const just = box.querySelector('.vcard.just');
    if (just) just.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  const BK_STATE = { pending: 'ждёт подтверждения', confirmed: 'подтверждено', done: 'закрыто' };

  function bookingCard(b, me) {
    const mine = Number(b.user_id) === Number(me.id);
    const card = mk('div', 'vcard bk-' + b.state + (Number(C.justId) === Number(b.id) ? ' just' : ''));
    const top = mk('div', 'vcard-top');
    const left = mk('div');
    left.appendChild(mk('b', 'vname', b.client_name));
    const n = daysBetween(b.date_from, b.date_to);
    left.appendChild(mk('div', 'vwhen', human(b.date_from) + ' — ' + human(b.date_to) + ' · ' + n + ' ' + plural(n, 'день', 'дня', 'дней')));
    const meta = [b.title, b.user_name || b.user_login, b.lead_phone].filter(Boolean).join(' · ');
    if (meta) left.appendChild(mk('div', 'vmeta', meta));
    top.appendChild(left);
    top.appendChild(mk('span', 'vstate', BK_STATE[b.state] || b.state));
    card.appendChild(top);
    if (b.note) card.appendChild(mk('div', 'vcnote', b.note));

    // Кнопки — строго по правам: владелец решает, менеджер управляет только своим.
    const acts = mk('div', 'vacts');
    if (me.is_owner) {
      if (b.state === 'pending') {
        acts.appendChild(btn('Подтвердить', 'primary', () => decide(b, 'confirm', 'Подтверждено')));
        acts.appendChild(btn('Отклонить', 'danger', () => decide(b, 'reject', 'Отклонено')));
      } else if (b.state === 'confirmed') {
        acts.appendChild(btn('Работа сдана', '', () => decide(b, 'done', 'Запись закрыта')));
        acts.appendChild(btn('Снять подтверждение', '', () => decide(b, 'unconfirm', 'Снова ждёт подтверждения')));
      }
      acts.appendChild(btn('Перенести', '', () => modalBooking(b)));
      acts.appendChild(btn('Отменить бронь', 'danger', () => removeBooking(b)));
    } else if (mine) {
      if (b.state !== 'done') acts.appendChild(btn('Перенести', '', () => modalBooking(b)));
      if (b.state === 'pending') acts.appendChild(btn('Отменить бронь', 'danger', () => removeBooking(b)));
    }
    if (acts.children.length) card.appendChild(acts);
    return card;
  }

  async function decide(b, action, okText) {
    const d = await call('bookings.php', null, { action, id: b.id });
    if (!d.ok) { toast(d.msg || 'не вышло'); return; }
    C.justId = b.id;
    toast(okText);
    loadCalendar();
  }

  function removeBooking(b) {
    const n = daysBetween(b.date_from, b.date_to);
    modal({
      title: 'Отменить бронь?',
      note: b.client_name + '\n' + human(b.date_from) + ' — ' + human(b.date_to) + ' · ' + n + ' ' +
            plural(n, 'день', 'дня', 'дней') + '\n\nДаты освободятся.',
      okLabel: 'Отменить бронь', danger: true,
      onOk: async () => {
        const d = await call('bookings.php', null, { action: 'delete', id: b.id });
        if (!d.ok) return d.msg || 'не вышло';
        toast('Бронь отменена, даты свободны');
        loadCalendar();
        return null;
      },
    });
  }

  /* draft — заготовка после «Согласился» в карточке лида: клиент уже вписан. */
  function modalBooking(b, draft) {
    const isNew = !b;
    draft = draft || {};
    const me = (C.data && C.data.me) || {};
    const start = b ? b.date_from : (C.selected || iso(new Date()));
    modal({
      title: isNew ? 'Занять период' : 'Перенести запись',
      note: isNew ? 'Окно займётся сразу' + (me.is_owner ? '.' : ', но будет ждать подтверждения владельца.')
                  : (me.is_owner ? '' : 'Подтверждённая запись после переноса снова уйдёт на подтверждение.'),
      build: body => {
        body.appendChild(field('Клиент', inp('vb_client', b ? b.client_name : (draft.client || ''))));
        body.appendChild(field('Что за работа (необязательно)', inp('vb_title', b ? (b.title || '') : (draft.title || ''))));
        const two = mk('div', 'vtwo');
        const fi = inp('vb_from', start, '', 'date');
        const ti = inp('vb_to', b ? b.date_to : start, '', 'date');
        two.appendChild(field('С', fi));
        two.appendChild(field('По', ti));
        body.appendChild(two);
        const hint = mk('div', 'vhint');
        body.appendChild(hint);
        const upd = () => {
          if (ti.value < fi.value) ti.value = fi.value;
          const n = fi.value && ti.value ? daysBetween(fi.value, ti.value) : 0;
          hint.textContent = n ? n + ' ' + plural(n, 'день', 'дня', 'дней') : '';
        };
        fi.oninput = upd; ti.oninput = upd; upd();
        const note = mk('textarea'); note.id = 'vb_note'; note.value = b ? (b.note || '') : '';
        body.appendChild(field('Заметка (необязательно)', note));
        if (isNew && me.is_owner) {
          const lab = mk('label', 'vcheck');
          const cb = mk('input'); cb.type = 'checkbox'; cb.id = 'vb_confirm'; cb.checked = true;
          lab.appendChild(cb); lab.appendChild(document.createTextNode('Сразу подтвердить'));
          body.appendChild(lab);
        }
      },
      okLabel: isNew ? 'Занять' : 'Сохранить',
      onOk: async () => {
        const payload = {
          action: isNew ? 'create' : 'move',
          client_name: $('#vb_client').value.trim(),
          title: $('#vb_title').value.trim(),
          note: $('#vb_note').value.trim(),
          date_from: $('#vb_from').value,
          date_to: $('#vb_to').value,
        };
        if (!isNew) payload.id = b.id;
        const leadId = (b && b.lead_id) || draft.lead;
        if (leadId) payload.lead_id = leadId;
        const cb = $('#vb_confirm');
        if (isNew && cb) payload.confirm = cb.checked ? '1' : '0';
        const d = await call('bookings.php', null, payload);
        if (!d.ok) return d.msg || 'не вышло';
        C.justId = d.id || (b && b.id);
        if (isNew) C.selected = payload.date_from;
        toast(d.state === 'pending' ? (isNew ? 'Период занят, ждёт подтверждения' : 'Перенесено, ждёт подтверждения')
                                    : (isNew ? 'Период занят' : 'Изменения сохранены'));
        loadCalendar();
        return null;
      },
    });
  }

  function modalBlackout() {
    const d0 = C.selected || iso(new Date());
    modal({
      title: 'Закрыть даты',
      note: 'В закрытые дни менеджеры не смогут занимать окна.',
      build: body => {
        const two = mk('div', 'vtwo');
        const fi = inp('vo_from', d0, '', 'date'), ti = inp('vo_to', d0, '', 'date');
        fi.oninput = () => { if (ti.value < fi.value) ti.value = fi.value; };
        two.appendChild(field('С', fi)); two.appendChild(field('По', ti));
        body.appendChild(two);
        body.appendChild(field('Причина (увидят менеджеры)', inp('vo_reason', '', 'отпуск, занят своим')));
      },
      okLabel: 'Закрыть даты',
      onOk: async () => {
        const d = await call('bookings.php', null, { action: 'blackout', date_from: $('#vo_from').value,
          date_to: $('#vo_to').value, reason: $('#vo_reason').value.trim() });
        if (!d.ok) return d.msg || 'не вышло';
        toast('Даты закрыты');
        loadCalendar();
        return null;
      },
    });
  }

  /* =============================== CRM =============================== */

  const EST = { draft: 'черновик', sent: 'отправлена', approved: 'согласована', paid: 'оплачена', cancelled: 'отменена' };
  const R = { tab: 'estimates', period: new Date().toISOString().slice(0, 7), filter: '', prices: [], users: [], me: {} };

  async function openCrm(draft) {
    // Без «Финансов» раздела нет — пункт меню скрыт, а сервер всё равно откажет.
    if (!S.canFinance) { toast('Нет доступа к финансам — его выдаёт владелец'); return; }
    setMode('crm');
    await loadRefs();
    if (draft) { R.tab = 'estimates'; renderCrm(); editEstimate(null, draft); }
    else renderCrm();
  }

  async function loadRefs() {
    const [p, u] = await Promise.all([call('crm.php', { what: 'prices' }), call('crm.php', { what: 'users' })]);
    if (p.ok) { R.prices = p.items || []; R.me = p.me || {}; }
    if (u.ok) R.users = u.users || [];
    if (!p.ok) toast(p.msg || 'CRM недоступен');
  }

  function renderCrm() {
    const box = $('#view-crm');
    box.innerHTML = '';
    const h = head('CRM');
    const tabs = mk('div', 'vtabs');
    [['estimates', 'Сметы'], ['prices', 'Прайс'], ['payroll', 'Зарплаты'], ['summary', 'Сводка']].forEach(([k, t]) => {
      const b = btn(t, 'vtab' + (R.tab === k ? ' on' : ''), () => { R.tab = k; renderCrm(); });
      if (R.tab === k) b.setAttribute('aria-current', 'page');
      tabs.appendChild(b);
    });
    h.ctl.appendChild(tabs);
    box.appendChild(h.el);
    const body = mk('div', 'vbody');
    body.id = 'vcrm_body';
    body.appendChild(mk('div', 'vempty', 'Загружаю…'));
    box.appendChild(body);
    ({ estimates: crmEstimates, prices: crmPrices, payroll: crmPayroll, summary: crmSummary })[R.tab]();
  }

  const crmBody = () => $('#vcrm_body');
  function crmFail(d) {
    const b = crmBody(); b.innerHTML = '';
    b.appendChild(mk('div', 'vempty', d.msg || 'не удалось загрузить'));
  }
  function table(headers) {
    const wrap = mk('div', 'vtbl-wrap');
    const t = mk('table', 'vtbl');
    const tr = mk('tr');
    headers.forEach(([txt, r]) => tr.appendChild(mk('th', r ? 'r' : '', txt)));
    const th = mk('thead'); th.appendChild(tr); t.appendChild(th);
    const tb = mk('tbody'); t.appendChild(tb);
    wrap.appendChild(t);
    return { wrap, tb };
  }
  const td = (text, cls) => mk('td', cls || '', text);

  /* --------------------------- сметы --------------------------- */
  async function crmEstimates() {
    const d = await call('crm.php', { what: 'estimates', status: R.filter });
    if (!d.ok) return crmFail(d);
    const b = crmBody(); b.innerHTML = '';
    const bar = mk('div', 'vbar');
    const sel = mk('select');
    [['', 'все сметы'], ...Object.entries(EST)].forEach(([v, t]) => {
      const o = mk('option', null, t); o.value = v; if (v === R.filter) o.selected = true; sel.appendChild(o);
    });
    sel.onchange = () => { R.filter = sel.value; crmEstimates(); };
    bar.appendChild(sel);
    bar.appendChild(btn('Новая смета', 'primary push', () => editEstimate(null)));
    b.appendChild(bar);

    if (!d.estimates.length) { b.appendChild(mk('div', 'vempty', R.filter ? 'Смет с таким статусом нет' : 'Смет пока нет')); return; }
    const t = table([['Клиент'], ['Чья сделка'], ['Статус'], ['Сумма', 1], ['Обновлена', 1]]);
    d.estimates.forEach(e => {
      const tr = mk('tr', 'click' + (e.status === 'cancelled' ? ' dim' : ''));
      const c = mk('td');
      c.appendChild(mk('div', null, e.client_name));
      c.appendChild(mk('div', 'sub', (e.lines_count || 0) + ' поз.' + (Number(e.discount_pct) ? ' · скидка ' + Number(e.discount_pct) + '%' : '')));
      tr.appendChild(c);
      tr.appendChild(td(e.manager_name || e.manager_login || '—'));
      tr.appendChild(td(EST[e.status] || e.status, 'st-' + e.status));
      tr.appendChild(td(rub(e.total), 'r num'));
      tr.appendChild(td(String(e.updated_at || '').slice(0, 10), 'r sub'));
      tr.onclick = () => editEstimate(e.id);
      t.tb.appendChild(tr);
    });
    b.appendChild(t.wrap);
  }

  async function editEstimate(id, draft) {
    let e = { client_name: '', manager_id: R.me.id, status: 'draft', discount_pct: 0, note: '', lead_id: '' };
    let lines = [];
    if (id) {
      const d = await call('crm.php', { what: 'estimate', id });
      if (!d.ok) { toast(d.msg || 'не удалось открыть'); return; }
      e = d.estimate;
      lines = d.lines.map(l => ({ title: l.title, unit: l.unit, qty: Number(l.qty), price: Number(l.price) }));
    } else if (draft) {
      Object.assign(e, draft);
    }

    modal({
      title: id ? 'Смета' : 'Новая смета', wide: true, okLabel: 'Сохранить',
      build: body => {
        const g = mk('div', 'vthree');
        g.appendChild(field('Клиент', inp('ve_client', e.client_name)));
        const man = mk('select'); man.id = 've_manager';
        const none = mk('option', null, '— не указано'); none.value = ''; man.appendChild(none);
        R.users.forEach(u => {
          const o = mk('option', null, u.name || u.login); o.value = u.id;
          if (Number(u.id) === Number(e.manager_id)) o.selected = true;
          man.appendChild(o);
        });
        g.appendChild(field('Чья сделка', man));
        const st = mk('select'); st.id = 've_status';
        Object.entries(EST).forEach(([v, t]) => { const o = mk('option', null, t); o.value = v; if (v === e.status) o.selected = true; st.appendChild(o); });
        g.appendChild(field('Статус', st));
        body.appendChild(g);

        const t = mk('table', 'vlines');
        t.innerHTML = '<thead><tr><th>Услуга</th><th style="width:70px">Ед.</th><th style="width:80px">Кол-во</th>' +
                      '<th style="width:110px">Цена, ₽</th><th style="width:110px;text-align:right">Сумма</th><th></th></tr></thead>';
        const tb = mk('tbody'); tb.id = 've_lines'; t.appendChild(tb);
        body.appendChild(t);

        const tools = mk('div', 'vbar');
        const pick = mk('select');
        const p0 = mk('option', null, R.prices.length ? '+ из прайса…' : 'прайс пуст'); p0.value = ''; pick.appendChild(p0);
        R.prices.filter(p => Number(p.active)).forEach(p => {
          const o = mk('option', null, (p.category ? p.category + ' · ' : '') + p.title + ' — ' + rub(p.price) + ' / ' + p.unit);
          o.value = p.id; pick.appendChild(o);
        });
        pick.onchange = () => {
          const p = R.prices.find(x => String(x.id) === pick.value);
          if (p) addLine({ title: p.title, unit: p.unit, qty: 1, price: Number(p.price) });
          pick.value = '';
        };
        tools.appendChild(pick);
        tools.appendChild(btn('+ пустая строка', '', () => addLine({ title: '', unit: 'шт', qty: 1, price: 0 }, true)));
        body.appendChild(tools);

        const g2 = mk('div', 'vtwo');
        g2.appendChild(field('Скидка, %', inp('ve_discount', e.discount_pct || 0)));
        g2.appendChild(field('Заметка', inp('ve_note', e.note || '')));
        body.appendChild(g2);
        const tot = mk('div', 'vtotals'); tot.id = 've_totals'; body.appendChild(tot);
        lines.forEach(l => addLine(l));
        $('#ve_discount').oninput = recalc;
        recalc();
      },
      onOk: async () => {
        const d = await call('crm.php', null, {
          action: 'estimate_save', id: id || 0,
          client_name: $('#ve_client').value.trim(), manager_id: $('#ve_manager').value || 0,
          status: $('#ve_status').value, discount_pct: $('#ve_discount').value,
          note: $('#ve_note').value, lead_id: e.lead_id || '', lines: readLines(),
        });
        if (!d.ok) return d.msg || 'не удалось сохранить';
        toast('Смета сохранена · ' + rub(d.total));
        if (R.tab === 'estimates') crmEstimates();
        return null;
      },
      onDelete: id ? async () => {
        if (!confirm('Удалить смету? Отменить будет нельзя.')) return false;
        const d = await call('crm.php', null, { action: 'estimate_delete', id });
        if (!d.ok) { toast(d.msg || 'не вышло'); return false; }
        toast('Смета удалена');
        crmEstimates();
        return true;
      } : null,
    });
  }

  function addLine(l, focus) {
    const tr = mk('tr');
    const cell = (cls, val, mode) => {
      const i = mk('input', cls); i.value = val;
      if (mode) i.inputMode = mode;
      i.oninput = recalc;
      const c = mk('td'); c.appendChild(i); tr.appendChild(c);
      return i;
    };
    const t = cell('l-title', l.title);
    cell('l-unit', l.unit); cell('l-qty', l.qty, 'decimal'); cell('l-price', l.price, 'decimal');
    const s = mk('td', 'l-sum num'); s.style.textAlign = 'right'; tr.appendChild(s);
    const x = btn('✕', 'x', () => { tr.remove(); recalc(); });
    x.title = 'Убрать строку';
    const c = mk('td'); c.appendChild(x); tr.appendChild(c);
    $('#ve_lines').appendChild(tr);
    recalc();
    if (focus) t.focus();
  }
  function readLines() {
    return $$('#ve_lines tr').map(tr => ({
      title: tr.querySelector('.l-title').value.trim(),
      unit: tr.querySelector('.l-unit').value.trim() || 'шт',
      qty: num(tr.querySelector('.l-qty').value),
      price: num(tr.querySelector('.l-price').value),
    })).filter(l => l.title);
  }
  /* Итог здесь — только для глаз; сервер пересчитает сам и вернёт свою цифру. */
  function recalc() {
    let sub = 0;
    $$('#ve_lines tr').forEach(tr => {
      const s = num(tr.querySelector('.l-qty').value) * num(tr.querySelector('.l-price').value);
      tr.querySelector('.l-sum').textContent = rub(Math.round(s * 100) / 100);
      sub += s;
    });
    const disc = Math.min(100, Math.max(0, num(($('#ve_discount') || {}).value)));
    const total = Math.round(sub * (100 - disc)) / 100;
    const box = $('#ve_totals');
    if (!box) return;
    box.innerHTML = '';
    box.appendChild(mk('div', 'sub', 'Сумма позиций: ' + rub(Math.round(sub * 100) / 100)));
    if (disc) box.appendChild(mk('div', 'sub', 'Скидка ' + disc + '%: −' + rub(Math.round((sub - total) * 100) / 100)));
    box.appendChild(mk('div', 'big num', 'Итого: ' + rub(total)));
  }

  /* --------------------------- прайс --------------------------- */
  async function crmPrices() {
    await loadRefs();
    const b = crmBody(); b.innerHTML = '';
    const bar = mk('div', 'vbar');
    bar.appendChild(mk('div', 'vhint', 'Цена копируется в смету при добавлении — правка прайса старые сметы не меняет.'));
    bar.appendChild(btn('Добавить услугу', 'primary push', () => editPrice(null)));
    b.appendChild(bar);
    if (!R.prices.length) { b.appendChild(mk('div', 'vempty', 'Прайс пуст — добавь первую услугу')); return; }
    const t = table([['Услуга'], ['Раздел'], ['Ед.'], ['Цена', 1]]);
    R.prices.forEach(p => {
      const tr = mk('tr', 'click' + (Number(p.active) ? '' : ' dim'));
      const c = mk('td'); c.appendChild(mk('div', null, p.title));
      if (!Number(p.active)) c.appendChild(mk('div', 'sub', 'скрыта из выбора'));
      tr.appendChild(c);
      tr.appendChild(td(p.category || '—', 'sub'));
      tr.appendChild(td(p.unit, 'sub'));
      tr.appendChild(td(rub(p.price), 'r num'));
      tr.onclick = () => editPrice(p);
      t.tb.appendChild(tr);
    });
    b.appendChild(t.wrap);
  }

  function editPrice(p) {
    modal({
      title: p ? 'Услуга' : 'Новая услуга', okLabel: 'Сохранить',
      build: body => {
        body.appendChild(field('Название', inp('vp_title', p ? p.title : '')));
        const g = mk('div', 'vthree');
        g.appendChild(field('Раздел', inp('vp_cat', p ? p.category : '', 'сайты, SEO…')));
        g.appendChild(field('Ед.', inp('vp_unit', p ? p.unit : 'шт')));
        g.appendChild(field('Цена, ₽', inp('vp_price', p ? Number(p.price) : '')));
        body.appendChild(g);
        const lab = mk('label', 'vcheck');
        const cb = mk('input'); cb.type = 'checkbox'; cb.id = 'vp_active'; cb.checked = p ? !!Number(p.active) : true;
        lab.appendChild(cb); lab.appendChild(document.createTextNode('Показывать при составлении сметы'));
        body.appendChild(lab);
      },
      onOk: async () => {
        const d = await call('crm.php', null, {
          action: 'price_save', id: p ? p.id : 0, title: $('#vp_title').value, category: $('#vp_cat').value,
          unit: $('#vp_unit').value, price: $('#vp_price').value, active: $('#vp_active').checked ? 1 : 0,
        });
        if (!d.ok) return d.msg || 'не удалось сохранить';
        toast('Прайс обновлён');
        crmPrices();
        return null;
      },
      onDelete: p ? async () => {
        if (!confirm('Удалить услугу? В составленных сметах она останется.')) return false;
        const d = await call('crm.php', null, { action: 'price_delete', id: p.id });
        if (!d.ok) { toast(d.msg || 'не вышло'); return false; }
        toast('Услуга удалена');
        crmPrices();
        return true;
      } : null,
    });
  }

  /* -------------------------- зарплаты -------------------------- */
  function periodBar(onChange) {
    const bar = mk('div', 'vbar');
    const [y, m] = R.period.split('-').map(Number);
    const shift = dlt => { const t = new Date(y, m - 1 + dlt, 1); R.period = t.getFullYear() + '-' + String(t.getMonth() + 1).padStart(2, '0'); onChange(); };
    const prev = btn('‹', 'icon', () => shift(-1)), next = btn('›', 'icon', () => shift(1));
    prev.setAttribute('aria-label', 'Предыдущий месяц');
    next.setAttribute('aria-label', 'Следующий месяц');
    bar.appendChild(prev);
    bar.appendChild(mk('b', 'vperiod', MONTHS[m - 1] + ' ' + y));
    bar.appendChild(next);
    return bar;
  }

  async function crmPayroll() {
    const d = await call('crm.php', { what: 'payroll', period: R.period });
    if (!d.ok) return crmFail(d);
    const b = crmBody(); b.innerHTML = '';
    const bar = periodBar(crmPayroll);
    bar.appendChild(mk('div', 'vhint push', 'Процент — от оплаченных смет, где человек указан как «чья сделка».'));
    b.appendChild(bar);
    if (!d.people.length) { b.appendChild(mk('div', 'vempty', 'Сотрудников нет')); return; }
    const t = table([['Сотрудник'], ['Продажи', 1], ['Оклад', 1], ['%', 1], ['Премия − удерж.', 1], ['Итого', 1], ['Статус'], ['']]);
    d.people.forEach(p => {
      const r = p.row;
      const locked = p.self && !d.me.is_owner;   // своё начисление — только владелец
      const tr = mk('tr', p.user.access === 'revoked' ? 'dim' : '');
      const c = mk('td'); c.appendChild(mk('div', null, p.user.name || p.user.login));
      if (locked) c.appendChild(mk('div', 'sub', 'своё — начисляет владелец'));
      tr.appendChild(c);
      tr.appendChild(td(rub(r ? r.sales : p.sales_now), 'r num'));
      tr.appendChild(td(r ? rub(r.base) : '—', 'r num'));
      tr.appendChild(td(r ? Number(r.percent) + '% = ' + rub(r.commission) : '—', 'r num'));
      tr.appendChild(td(r ? '+' + rub(r.bonus) + ' − ' + rub(r.penalty) : '—', 'r num'));
      tr.appendChild(td(r ? rub(r.total) : '—', 'r num'));
      tr.appendChild(td(r ? (r.status === 'paid' ? 'выплачено' : 'начислено') : 'не начислено', r && r.status === 'paid' ? '' : 'sub'));
      const a = mk('td'); const acts = mk('div', 'vacts tight');
      if (!locked) {
        acts.appendChild(btn(r ? 'Пересчитать' : 'Начислить', 'small', () => editPayroll(p, d.period)));
        if (r) acts.appendChild(btn(r.status === 'paid' ? 'Снять «выплачено»' : 'Выплачено', r.status === 'paid' ? 'small' : 'small primary', async () => {
          const x = await call('crm.php', null, { action: 'payroll_status', id: r.id, status: r.status === 'paid' ? 'accrued' : 'paid' });
          if (!x.ok) { toast(x.msg || 'не вышло'); return; }
          toast(x.status === 'paid' ? 'Отмечено как выплачено' : 'Снова «начислено»');
          crmPayroll();
        }));
      }
      a.appendChild(acts); tr.appendChild(a);
      t.tb.appendChild(tr);
    });
    b.appendChild(t.wrap);
  }

  function editPayroll(p, period) {
    const r = p.row, sug = p.suggest || {};
    const [y, m] = period.split('-').map(Number);
    modal({
      title: 'Начисление · ' + (p.user.name || p.user.login),
      note: MONTHS[m - 1] + ' ' + y + ' · продажи за месяц: ' + rub(p.sales_now), okLabel: 'Сохранить',
      build: body => {
        const g = mk('div', 'vtwo');
        g.appendChild(field('Оклад, ₽', inp('vy_base', r ? Number(r.base) : (sug.base ? Number(sug.base) : ''))));
        g.appendChild(field('Процент от продаж', inp('vy_pct', r ? Number(r.percent) : (sug.percent ? Number(sug.percent) : ''))));
        g.appendChild(field('Премия, ₽', inp('vy_bonus', r ? Number(r.bonus) : '')));
        g.appendChild(field('Удержание, ₽', inp('vy_pen', r ? Number(r.penalty) : '')));
        body.appendChild(g);
        body.appendChild(field('Комментарий', inp('vy_note', r ? r.note : '')));
        const tot = mk('div', 'vtotals'); body.appendChild(tot);
        const upd = () => {
          const com = Math.round(num(p.sales_now) * num($('#vy_pct').value)) / 100;
          const total = Math.max(0, num($('#vy_base').value) + com + num($('#vy_bonus').value) - num($('#vy_pen').value));
          tot.innerHTML = '';
          tot.appendChild(mk('div', 'sub', 'Процент: ' + rub(com)));
          tot.appendChild(mk('div', 'big num', 'К выплате: ' + rub(Math.round(total * 100) / 100)));
        };
        ['vy_base', 'vy_pct', 'vy_bonus', 'vy_pen'].forEach(i => { $('#' + i).oninput = upd; });
        upd();
      },
      onOk: async () => {
        const d = await call('crm.php', null, {
          action: 'payroll_save', user_id: p.user.id, period, base: $('#vy_base').value, percent: $('#vy_pct').value,
          bonus: $('#vy_bonus').value, penalty: $('#vy_pen').value, note: $('#vy_note').value,
        });
        if (!d.ok) return d.msg || 'не удалось сохранить';
        toast('Начислено · ' + rub(d.total));
        crmPayroll();
        return null;
      },
      onDelete: r ? async () => {
        if (!confirm('Удалить начисление за этот месяц?')) return false;
        const d = await call('crm.php', null, { action: 'payroll_delete', id: r.id });
        if (!d.ok) { toast(d.msg || 'не вышло'); return false; }
        toast('Начисление удалено');
        crmPayroll();
        return true;
      } : null,
    });
  }

  /* --------------------------- сводка --------------------------- */
  async function crmSummary() {
    const d = await call('crm.php', { what: 'summary', period: R.period });
    if (!d.ok) return crmFail(d);
    const b = crmBody(); b.innerHTML = '';
    b.appendChild(periodBar(crmSummary));
    const k = mk('div', 'vkpis');
    const kpi = (label, val, sub, cls) => {
      const x = mk('div', 'vkpi' + (cls ? ' ' + cls : ''));
      x.appendChild(mk('div', 'l', label)); x.appendChild(mk('div', 'v num', val));
      if (sub) x.appendChild(mk('div', 's', sub));
      k.appendChild(x);
    };
    kpi('Выручка за месяц', rub(d.revenue), 'оплачено смет: ' + d.revenue_count);
    kpi('В работе', rub(d.pipeline), 'отправлено и согласовано: ' + d.pipeline_count);
    kpi('Фонд оплаты труда', rub(d.payroll), 'выплачено ' + rub(d.payroll_paid));
    kpi('Прибыль', rub(d.profit), 'выручка минус зарплаты', Number(d.profit) < 0 ? 'bad' : '');
    b.appendChild(k);
    if (!d.by_manager.length) { b.appendChild(mk('div', 'vempty', 'В этом месяце оплаченных смет нет')); return; }
    const t = table([['Чья сделка'], ['Оплачено смет', 1], ['Выручка', 1]]);
    d.by_manager.forEach(m => {
      const tr = mk('tr');
      tr.appendChild(td(m.name || m.login || 'не указано'));
      tr.appendChild(td(m.deals, 'r num'));
      tr.appendChild(td(rub(m.revenue), 'r num'));
      t.tb.appendChild(tr);
    });
    b.appendChild(t.wrap);
  }

  return { toggle, openCalendar, openCrm, onAccess, setMode };
})();
