// Сниппеты для разбора закрытых (invite-only) индикаторов TradingView из вкладки графика,
// открытой под входом владельца (Claude in Chrome -> javascript_tool). Найдены 22.10.2026
// на индикаторах CoinMetrika, разбор: docs/research/indicators/coinmetrika-suite-2026-10-22.md
//
// Границы: зашифрованный код скриптов (поле IL ответа translate) не трогать; куки, токены
// и window.user.auth_token не читать и не печатать. Берётся только то, что владелец и так
// видит в окне настроек и на графике.
//
// Ограничения среды (каждое стоило времени):
//  * вывод javascript_tool режется на ~1000 символов, строки с "=" и "&" блокируются как
//    "query string" -> длинное класть в скрытый <article> и читать get_page_text (dump ниже);
//  * штатный exportData на тарифе Basic отключён ("Data export is not supported") ->
//    значения брать из внутренней модели графика (studyRows ниже);
//  * Basic: максимум 2 индикатора на графике. Перед заменой индикатора владельца снять
//    шаблон (saveLayout), в конце наложить (restoreLayout) — вернёт символ, ТФ и стили;
//  * функция из одного вызова javascript_tool в следующем может не существовать —
//    вставлять нужную функцию и её вызов в ОДИН вызов (или класть в window.__H = {...});
//  * один вызов javascript_tool живёт не дольше 45 с (дальше «CDP timed out», а код в странице
//    продолжает работать). Долгие обходы запускать фоном без await (window.__state,
//    результаты в window.__R) и опрашивать вызовами по ≤40 с;
//  * TradingViewApi.activeChart().setVisibleRange на основном сайте — "Not implemented";
//    историю подгружать через loadAllHistory (п. 6);
//  * Basic: 4ч-история только с 01.01.2024 (endOfDataType "limit"), 1Д/1Н/1М — вся ("end").

// 1. ID закрытых скриптов: окно «Индикаторы» -> вкладка «Доступ ограничен», затем:
//    [...document.querySelectorAll('[data-id^="PUB;"]')].map(e => [e.dataset.title, e.dataset.id])

// 2. Паспорт скрипта: настройки (тип, по умолчанию, варианты, группа, подсказка автора),
//    линии и оповещения. Строка данных индикатора: row[0] — время, row[k + 1] — plot_k.
async function scriptMeta(pubId) {
  const r = await fetch('https://pine-facade.tradingview.com/pine-facade/translate/'
    + encodeURIComponent(pubId) + '/last', { credentials: 'include' });
  const m = (await r.json()).result.metaInfo;
  const styles = m.styles || {};
  return {
    version: m.pine && m.pine.version,
    onPrice: m.is_price_study,
    inputs: m.inputs.map(i => ({ id: i.id, name: i.name, type: i.type, def: i.defval,
      options: i.options, min: i.min, max: i.max, group: i.group, tip: i.tooltip,
      hidden: i.isHidden })),
    plots: m.plots.map(p => ({ id: p.id, type: p.type, title: (styles[p.id] || {}).title })),
  };
}

// 3. Снимок макета ДО правок и возврат ПОСЛЕ (символ, ТФ, индикаторы вместе со стилями).
//    Рисунки (линии тренда) шаблон не трогает — они остаются в макете сами.
function saveLayout() {
  const c = TradingViewApi.activeChart();
  window.__tpl = c.createStudyTemplate({ saveSymbol: true, saveInterval: true });
  sessionStorage.setItem('__tpl_backup', JSON.stringify(window.__tpl));  // на случай перезагрузки
  return c.getAllStudies().map(s => s.name);
}
// Урок 22.10.2026 (пункт 1): applyStudyTemplate меняет график, но автосохранение макета
// отстаёт на десятки секунд — перезагрузка сразу после restoreLayout подняла СТАРУЮ пару
// индикаторов. После restoreLayout: Ctrl+S (сохранить макет), ~10 с, перезагрузка, сверка —
// и только потом удалять резервную копию шаблона (лучше держать её и в localStorage).
function restoreLayout() {
  const c = TradingViewApi.activeChart();
  c.applyStudyTemplate(window.__tpl || JSON.parse(sessionStorage.getItem('__tpl_backup')));
  sessionStorage.removeItem('__tpl_backup');
}

// 4. Значения индикатора по барам текущего графика: [{t, close, row}].
//    Метка plotshape бывает null там, где метки нет (Cycle Momentum), а бывает 0 на каждом
//    баре (TrendDip) — тогда события восстанавливать по линии-состоянию (у TrendDip это
//    "Position %": 0 -> 50 = BUY, рост = DIP, -> 0 = SELL).
function studyRows(namePart) {
  const m = TradingViewApi.activeChart().chartModel();
  const inner = m.model ? m.model() : m;
  const src = inner.dataSources().find(s => {
    try { return s.metaInfo().description.includes(namePart); } catch (e) { return false; }
  });
  const d = src.data();
  const bars = inner.mainSeries().bars();
  const out = [];
  for (let i = d.firstIndex(); i <= d.lastIndex(); i++) {
    const row = d.valueAt(i);
    const b = bars.valueAt(i);
    if (row && b) out.push({ t: new Date(row[0] * 1000).toISOString().slice(0, 10), close: b[4], row });
  }
  return out;
}

// 5. Длинный текст наружу: dump(JSON.stringify(x)) -> get_page_text -> убрать блок:
//    document.getElementById('dump').remove()
function dump(text) {
  let a = document.getElementById('dump');
  if (!a) {
    a = document.createElement('article');
    a.id = 'dump';
    a.style.cssText = 'position:fixed;left:-99999px;width:10px;height:10px;overflow:hidden';
    document.body.appendChild(a);
  }
  a.textContent = text;
  return text.length;
}

// ---- Добавлено 22.10.2026 замером «сколько истории нужно» (пункт 0 раздела 5 отчёта) ----

const chartModel = () => { const m = TradingViewApi.activeChart().chartModel(); return m.model ? m.model() : m; };
const studySource = part => chartModel().dataSources().find(s => {
  try { return s.metaInfo().description.includes(part); } catch (e) { return false; }
});
const sleep = t => new Promise(r => setTimeout(r, t));

// 6. Вся история текущего символа и ТФ: просить ещё баров, пока серия не скажет «конец».
//    eod "end" — начало торгов, "limit" — потолок тарифа (на Basic у 4ч это 01.01.2024).
async function loadAllHistory(maxIter = 80) {
  const ms = chartModel().mainSeries();
  let prev = -1, same = 0, eod = 'iter';
  for (let i = 0; i < maxIter; i++) {
    const n = ms.bars().size();
    if (ms.endOfData()) { eod = ms.endOfDataType(); break; }
    if (n === prev) { if (++same >= 5) { eod = 'stall'; break; } } else same = 0;
    prev = n;
    ms.requestMoreData(5000);
    await sleep(1000);
  }
  const b = ms.bars();
  return { bars: b.size(), first: new Date(b.valueAt(b.firstIndex())[0] * 1000).toISOString().slice(0, 10), eod };
}

// 7. Дождаться пересчёта индикаторов (после смены символа, ТФ, настройки или подгрузки):
//    сначала поймать isLoading, затем три опроса подряд isCompleted и данные до последнего бара.
async function waitStudies(parts, timeout = 120000) {
  const t0 = Date.now();
  while (Date.now() - t0 < 3000 && !parts.some(p => studySource(p)?.isLoading())) await sleep(250);
  let ok = 0;
  while (Date.now() - t0 < timeout) {
    const last = chartModel().mainSeries().bars().lastIndex();
    const ready = parts.every(p => {
      const s = studySource(p); if (!s) return false; if (s.isFailed()) return true;
      return s.isCompleted() && !s.isLoading() && s.data().size() > 0 && s.data().lastIndex() === last;
    });
    if (ready) { if (++ok >= 3) return true; } else ok = 0;
    await sleep(500);
  }
  return false;
}

// 8. Первый бар с числом у каждой линии ks (индексы plots из паспорта, п. 2): сдвиг в барах
//    от первой свечи графика и дата. Линия, равная 0 до сигнала (Position % у TrendDip), —
//    смотреть отдельно первый НЕнулевой бар.
function firstValues(part, ks) {
  const s = studySource(part), d = s.data();
  const b0 = chartModel().mainSeries().bars().firstIndex();
  return ks.map(k => {
    for (let i = d.firstIndex(); i <= d.lastIndex(); i++) {
      const row = d.valueAt(i), v = row && row[k + 1];
      if (typeof v === 'number' && isFinite(v)) return { k, offset: i - b0, date: new Date(row[0] * 1000).toISOString().slice(0, 10) };
    }
    return { k, offset: null };
  });
}

// 9. Поставить закрытый индикатор из вкладки «Invite-only» и менять его настройки.
//    Перед этим снять шаблон (saveLayout) и убрать лишнее: на Basic не больше 2 индикаторов.
async function addInviteOnly(titleStart) {
  const sel = '[data-name="indicators-dialog"]';
  const waitFor = async (fn, t = 6000) => { const t0 = Date.now(); while (Date.now() - t0 < t) { const v = fn(); if (v) return v; await sleep(250); } return null; };
  if (!document.querySelector(sel)) document.querySelector('#header-toolbar-indicators button').click();
  const dlg = await waitFor(() => document.querySelector(sel));
  (await waitFor(() => [...dlg.querySelectorAll('div[class*="sidebarItem"]')].find(e => /Invite-only|Доступ ограничен/.test(e.textContent)))).click();
  (await waitFor(() => [...document.querySelectorAll('[data-id^="PUB;"]')].find(e => e.dataset.title.startsWith(titleStart)))).click();
  await sleep(1500);
  // крестик окна: [data-name="close"] не находится, Escape не закрывает — кнопка с "close" в классе
  [...document.querySelector(sel).querySelectorAll('button')].find(e => /close/i.test(e.className))?.click();
}
function setStudyInput(namePart, id, value) {   // id вида 'in_0' — из паспорта (п. 2)
  const c = TradingViewApi.activeChart();
  const s = c.getAllStudies().find(x => x.name.includes(namePart));
  c.getStudyById(s.id).setInputValues([{ id, value }]);   // меняет только этот id
}

// 10. Таблица Pine (table.new) как текст: ячейки лежат в graphics().dwgtablecells().
//     Осторожно: текст ячейки бывает "n/a" — не делать "/" разделителем при разборе.
function pineTable(part) {
  const cells = [];
  [...studySource(part).graphics().dwgtablecells().values()].forEach(v => {
    if (v && typeof v.values === 'function') cells.push(...v.values()); else cells.push(v);
  });
  const rows = {};
  cells.forEach(c => { (rows[c.row] = rows[c.row] || [])[c.column] = String(c.text).trim(); });
  return Object.keys(rows).sort((a, b) => a - b).map(r => rows[r].join(' | '));
}

// ---- Добавлено 22.10.2026 разбором 7 индикаторов (пункт 1 раздела 5 отчёта) ----
// Ловушки чтения строк (row[k + 1] = plot_k):
//  * недельные линии (MA 20W/200W, EMA 21W …) на ДНЕВНОМ графике имеют число только на
//    воскресном баре, остальные null — пересечения считать по rows.filter(e => число в row);
//  * Safety Lines: рисунок "Lines" рисуется объектами, строки пусты — читать в "Waves";
//  * Pulse MTF: большие точки Confirmed LONG/SHORT (plot 7 / 9) по умолчанию выключены
//    (in_1); таблица статуса пишет "WAIT" и там, где ТФ нечем считать;
//  * у Pine-таблицы скринера статус "No" не отличает "не в зоне" от "нет данных".
// Обход «монеты × ТФ» долгий (13–90 с на сочетание) — только фоном, см. п. 7 и шапку.
// Пересечение закрытием линии k: вниз — c[i-1] >= L[i-1] и c[i] < L[i]; вверх — наоборот.
function lineCrosses(rows, k) {
  const dn = [], up = [];
  for (let i = 1; i < rows.length; i++) {
    const a = rows[i - 1], b = rows[i], la = a.row[k + 1], lb = b.row[k + 1];
    if (typeof la !== 'number' || typeof lb !== 'number') continue;
    if (a.close >= la && b.close < lb) dn.push(b.t);
    if (a.close <= la && b.close > lb) up.push(b.t);
  }
  return { dn, up };
}

// ---- Добавлено 22.10.2026 проверкой сигналов (пункт 2 раздела 5 отчёта) ----
// 11. Даты событий наружу компактно: день от 01.01.2010 в base36 (3 знака на событие), список
//     через запятую; 1053 точки CustomRSI BTC 1Д — ~4 тыс. знаков. Разбор в Python —
//     lab-data/coinmetrika-signals/check.py (dec, load_events). Точки индикатора проще брать из
//     ЛИНИЙ ОПОВЕЩЕНИЙ (alertcondition: 1 на баре события, иначе 0) — у CustomRSI это plot 7/8,
//     у Pulse MTF 17/18 (Local) и 21/22 (Global, только в режиме Global), у TrendDip 17–19,
//     у Safety Lines 5–24. Фильтрованные оповещения CustomRSI (9/10) пусты при выключенном фильтре.
const enc36 = t => Math.round((t - Date.UTC(2010, 0, 1) / 1000) / 86400).toString(36);
function alertDates(part, ks) {   // -> "k:a,b,c/k2:..."
  const d = studySource(part).data(), out = {};
  ks.forEach(k => out[k] = []);
  for (let i = d.firstIndex(); i <= d.lastIndex(); i++) {
    const r = d.valueAt(i); if (!r) continue;
    ks.forEach(k => { const v = r[k + 1]; if (typeof v === 'number' && isFinite(v) && v !== 0) out[k].push(enc36(r[0])); });
  }
  return ks.map(k => k + ':' + out[k].join(',')).join('/');
}
// 12. Обход «монеты × ТФ» фоном: window.__job = runJob([[sym, tf], ...], {part: [k...]});
//     опрашивать window.__jobState вызовами по ≤40 с, результат — window.__jobR (строка на сочетание).
//     Линии ALTs Line4–7 скрыты по умолчанию и в строках пусты — включить bool-настройки (in_1..in_3).
async function runJob(list, spec) {
  const c = TradingViewApi.activeChart(); window.__jobR = {};
  for (const [sym, tf] of list) {
    window.__jobState = sym + ' ' + tf;
    await new Promise(r => c.setSymbol(sym, r)); await sleep(500);
    await new Promise(r => c.setResolution(tf, r)); await sleep(1500);
    const eod = (await loadAllHistory()).eod, ok = await waitStudies(Object.keys(spec));
    let s = sym + '|' + tf + '|' + eod + '|' + ok;
    for (const [p, ks] of Object.entries(spec)) s += '#' + p + '>' + alertDates(p, ks);
    window.__jobR[sym + '|' + tf] = s;
  }
  window.__jobState = 'done';
}
// Макет: saveLayout() + копия шаблона в localStorage (переживает перезагрузку), в конце
// restoreLayout() -> Ctrl+S -> 10 с -> перезагрузка -> сверка символа, ТФ и настроек -> удалить копию.

// ---- Добавлено 22.10.2026 проверкой связок (пункт 3 раздела 5 отчёта) ----
// 13. Cycle Momentum (PUB;2b458dc4ae054ed2b9b0aa970af8104a): plot 2 — сама линия импульса, 9–13 —
//     оповещения BULL / вход в −30 / REC / DIV / BEAR. Ряд значений по дням наружу компактно —
//     2 знака на день: (Math.round(v) + 100).toString(36).padStart(2, '0'); 4 760 дней BTC ≈ 9,5 тыс. знаков.
//     Ловушка: бары TradingView идут не каждый день — у BITSTAMP:BTCUSD пропуск 05→09.01.2015
//     (остановка биржи). Восстанавливать дату по номеру бара нельзя: брать row[0] либо учесть пропуски
//     (проверка — последняя дата ряда должна совпасть с последним баром графика).
