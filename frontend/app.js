'use strict';

// Dev: ln -s ../data/output frontend/data  then: cd frontend && python3 -m http.server 8080
const DATA_URL = loc => `/data/${loc}.json`;

const LOCATIONS = [
  { id: 'casa_campi',    label: 'Casa Campi' },
  { id: 'lavoro_cosimo', label: 'Lav. Cosimo' },
  { id: 'lavoro_madda',  label: 'Lav. Madda' },
  { id: 'casa_cesto',    label: 'Casa Cesto' },
  { id: 'casa_nicco',    label: 'Casa Nicco' },
];

const INDICATOR_META = {
  panni:    { label: 'Panni',     icon: '🧺' },
  motorino: { label: 'Motorino',  icon: '🛵' },
  gelata:   { label: 'Gelata',    icon: '❄️' },
  temporale:{ label: 'Temporale', icon: '⛈️' },
  nebbia:   { label: 'Nebbia',    icon: '🌫️' },
  bisenzio: { label: 'Bisenzio',  icon: '🌊' },
  annaffia: { label: 'Annaffia',  icon: '💧' },
};

const VERDICT_CLS = {
  verde:  { ind: 'ind-verde',  dot: 'bg-success' },
  giallo: { ind: 'ind-giallo', dot: 'bg-warning' },
  rosso:  { ind: 'ind-rosso',  dot: 'bg-error'   },
};

// ── Weather icon ──────────────────────────────────────────────────────────────

function weatherIcon(precipP50, tmaxP50, temporaleVerdict, nebbiaVerdict) {
  if (nebbiaVerdict === 'rosso' || nebbiaVerdict === 'giallo') return '🌫️';
  if (temporaleVerdict === 'rosso')  return '⛈️';
  if (temporaleVerdict === 'giallo') return '🌩️';
  if (precipP50 >= 10)  return '🌧️';
  if (precipP50 >=  3)  return '🌦️';
  if (precipP50 >= 0.5) return '🌥️';
  if (tmaxP50 == null)  return '⛅';
  if (tmaxP50 >= 22)    return '☀️';
  if (tmaxP50 >= 15)    return '🌤️';
  return '⛅';
}

function weatherIconForDay(day) {
  const precip = day.forecasts.precip_mm?.p50 ?? 0;
  const tmax   = day.forecasts.tmax_c?.p50   ?? null;
  return weatherIcon(precip, tmax,
    day.indicators.temporale?.verdict,
    day.indicators.nebbia?.verdict,
  );
}

function weatherIconFromCurrent(current) {
  const prec = current?.precip_mm ?? 0;
  if (prec >= 5)   return '🌧️';
  if (prec >= 1)   return '🌦️';
  if (prec >= 0.1) return '🌥️';
  return '☀️';
}

let currentData          = null;
let selectedDayIdx       = 0;
let selectedModel        = 'guazza';
let selectedWeeklyModel  = 'guazza';
let meteoChart           = null;
let multiDayChart        = null;

// ── Dark mode ─────────────────────────────────────────────────────────────────

function initDarkMode() {
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const apply = dark => { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; };
  mq.addEventListener('change', e => {
    apply(e.matches);
    if (currentData) {
      if (meteoChart) { meteoChart.destroy(); meteoChart = null; }
      if (multiDayChart) { multiDayChart.destroy(); multiDayChart = null; }
      initChart(currentData, selectedModel);
    }
  });
  apply(mq.matches);
}

// ── URL routing ───────────────────────────────────────────────────────────────

function getActiveLoc() {
  const p = new URLSearchParams(location.search).get('loc');
  return LOCATIONS.some(l => l.id === p) ? p : 'casa_campi';
}

function navTo(locId) {
  history.pushState({}, '', `?loc=${locId}`);
  loadLocation(locId);
}

// ── Tabs ──────────────────────────────────────────────────────────────────────

function renderTabs(activeLoc) {
  const nav = document.getElementById('tabs');
  nav.innerHTML = LOCATIONS.map(l =>
    `<button class="btn btn-sm ${l.id === activeLoc ? 'btn-primary' : 'btn-ghost'}" data-loc="${l.id}">${l.label}</button>`
  ).join('');
  nav.querySelectorAll('[data-loc]').forEach(btn =>
    btn.addEventListener('click', () => navTo(btn.dataset.loc))
  );
}

// ── Data loading ──────────────────────────────────────────────────────────────

async function loadLocation(locId) {
  renderTabs(locId);
  selectedDayIdx = 0;
  selectedModel  = 'guazza';
  if (meteoChart) { meteoChart.destroy(); meteoChart = null; }
  const app = document.getElementById('app');
  app.innerHTML = '<div class="flex items-center justify-center p-12 gap-3 text-base-content/60"><span class="loading loading-spinner loading-md"></span>Caricamento…</div>';
  try {
    const r = await fetch(DATA_URL(locId));
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    currentData = await r.json();
    render(app, currentData);
  } catch (e) {
    app.innerHTML = `<div class="alert alert-error flex-col items-start mt-4"><span class="font-medium">Errore: ${e.message}</span><span class="text-sm opacity-70">Assicurati che il server stia servendo i JSON da /data/</span></div>`;
    currentData = null;
  }
}

// ── Formatting ────────────────────────────────────────────────────────────────

function fmtDate(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('it-IT', {
    weekday: 'short', day: 'numeric', month: 'short',
  });
}

function fmtDateShort(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('it-IT', { day: 'numeric', month: 'short' });
}

function fmtDayLabel(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  const todayMid = new Date(); todayMid.setHours(0, 0, 0, 0);
  const target = new Date(y, m - 1, d);
  const diff = Math.round((target - todayMid) / 86400000);
  if (diff === 0) return 'Oggi';
  if (diff === 1) return 'Domani';
  return target.toLocaleDateString('it-IT', { weekday: 'long' });
}

function fmtDayShort(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  const todayMid = new Date(); todayMid.setHours(0, 0, 0, 0);
  const target = new Date(y, m - 1, d);
  const diff = Math.round((target - todayMid) / 86400000);
  if (diff === 0) return 'Oggi';
  if (diff === 1) return 'Domani';
  const wd = target.toLocaleDateString('it-IT', { weekday: 'short' });
  return wd.charAt(0).toUpperCase() + wd.slice(1);
}

function fmtDateTime(iso) {
  return new Date(iso).toLocaleString('it-IT', {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

function fmtTemp(v)   { return v != null ? `${v.toFixed(1)}°` : '—'; }
function fmtPrecip(v) { return v != null ? `${v.toFixed(1)} mm` : '—'; }
function fmtWind(v)   { return v != null ? `${(v * 3.6).toFixed(0)} km/h` : '—'; }

// Soglie ARPAT livelli 1–2 = verde, 3–4 = giallo, 5–7 = rosso
const AQ_THRESHOLDS = {
  pm10:    [20, 40],   // µg/m³: <20 verde, 20–40 giallo, ≥40 rosso
  pm25:    [10, 20],   // µg/m³
  no2:     [40, 160],  // µg/m³
  o3:      [72, 144],  // µg/m³
  co:      [2,  8],    // mg/m³: livelli ARPAT <2 verde, 2–8 giallo, ≥8 rosso
  benzene: [1,  4],    // µg/m³
  so2:     [70, 280],  // µg/m³
};

function aqColorCls(key, value) {
  if (value == null) return 'text-base-content';
  const [lo, hi] = AQ_THRESHOLDS[key] ?? [0, Infinity];
  if (value < lo)  return 'text-success';
  if (value < hi)  return 'text-warning';
  return 'text-error';
}

function renderAirQuality(aq) {
  if (!aq) return '';
  const items = [
    { key: 'pm10',    label: 'PM10',   value: aq.pm10_ugm3,    unit: 'µg/m³', dec: 0 },
    { key: 'pm25',    label: 'PM2.5',  value: aq.pm25_ugm3,    unit: 'µg/m³', dec: 0 },
    { key: 'no2',     label: 'NO₂',    value: aq.no2_ugm3,     unit: 'µg/m³', dec: 0 },
    { key: 'o3',      label: 'O₃',     value: aq.o3_ugm3,      unit: 'µg/m³', dec: 0 },
    { key: 'co',      label: 'CO',     value: aq.co_mgm3,      unit: 'mg/m³', dec: 1 },
    { key: 'benzene', label: 'C₆H₆',  value: aq.benzene_ugm3, unit: 'µg/m³', dec: 1 },
    { key: 'so2',     label: 'SO₂',    value: aq.so2_ugm3,     unit: 'µg/m³', dec: 0 },
  ].filter(it => it.value != null);
  if (items.length === 0) return '';

  const cards = items.map(it => `
    <div class="bg-base-200 rounded-lg p-2.5 text-center">
      <div class="text-xs text-base-content/60 mb-0.5">${it.label}</div>
      <div class="font-semibold text-sm ${aqColorCls(it.key, it.value)}">${it.value.toFixed(it.dec)}</div>
      <div class="text-xs text-base-content/40">${it.unit}</div>
    </div>`).join('');

  return `
    <div class="mt-3">
      <div class="text-xs text-base-content/40 mb-1">Qualità aria</div>
      <div class="grid gap-2" style="grid-template-columns:repeat(${items.length},minmax(0,1fr))">${cards}</div>
    </div>`;
}

function isToday(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  const now = new Date();
  return y === now.getFullYear() && m === (now.getMonth() + 1) && d === now.getDate();
}

function staleWarning(generatedAt) {
  const ageH = (Date.now() - new Date(generatedAt).getTime()) / 3600000;
  if (ageH < 6) return '';
  return ` <span class="ml-2 font-medium text-warning" title="Dati generati ${ageH.toFixed(0)}h fa">⚠️ dati vecchi</span>`;
}

// ── Coverage badge ────────────────────────────────────────────────────────────

function coverageBadge(cov) {
  if (!cov || Object.values(cov).every(v => v === null)) {
    return '<div class="alert alert-warning text-sm mb-4">⚠️ Calibrazione in corso — copertura CI non ancora disponibile (primi 30gg di operatività)</div>';
  }
  const items = [
    ['Tmin CI80', cov.tmin_ci80], ['Tmin CI90', cov.tmin_ci90],
    ['Tmax CI80', cov.tmax_ci80], ['Tmax CI90', cov.tmax_ci90],
    ['Precip CI80', cov.precip_ci80], ['Precip CI90', cov.precip_ci90],
  ].filter(([, v]) => v !== null)
   .map(([k, v]) => `<span class="ml-3">${k}: <strong>${(v * 100).toFixed(0)}%</strong></span>`)
   .join('');
  return `<div class="alert alert-success text-sm mb-4">📊 Copertura empirica 30gg: ${items}</div>`;
}

// ── CI bar ────────────────────────────────────────────────────────────────────

function ciBar(fc, unit) {
  const { p50, ci80_lo, ci80_hi, ci90_lo, ci90_hi } = fc;
  const range = ci90_hi - ci90_lo;
  if (range <= 0) return `<div class="ci-detail">—</div>`;

  const pct      = v => Math.max(0, Math.min(100, ((v - ci90_lo) / range) * 100)).toFixed(1);
  const p80lo    = pct(ci80_lo);
  const p80width = (pct(ci80_hi) - pct(ci80_lo)).toFixed(1);
  const p50pos   = pct(p50);

  return `
    <div class="my-1">
      <div class="ci-bar-track">
        <div class="ci-range-80" style="left:${p80lo}%;width:${p80width}%"></div>
        <div class="ci-p50" style="left:${p50pos}%"></div>
      </div>
      <div class="ci-labels">
        <span>${ci90_lo.toFixed(1)}${unit}</span>
        <span>${ci90_hi.toFixed(1)}${unit}</span>
      </div>
    </div>
    <div class="ci-detail">
      p50 ${p50.toFixed(1)}${unit} &middot; CI80 [${ci80_lo.toFixed(1)}, ${ci80_hi.toFixed(1)}]${unit}
    </div>`;
}

// ── Indicators grid ───────────────────────────────────────────────────────────

function renderIndicatorsGrid(indicators) {
  return `<div class="grid grid-cols-3 gap-2 sm:grid-cols-9">
    ${Object.entries(indicators).map(([id, ind]) => {
      const meta = INDICATOR_META[id] ?? { label: id, icon: '?' };
      const cls  = VERDICT_CLS[ind.verdict];
      return `<div class="flex flex-col items-center gap-0.5 p-2 rounded-lg ${cls ? cls.ind : 'bg-base-200 text-base-content'}" title="${ind.rule_matched}">
        <span class="text-xl leading-none">${meta.icon}</span>
        <span class="font-medium text-xs leading-tight">${meta.label}</span>
        <span class="text-xs font-bold uppercase tracking-wide mt-0.5">${ind.verdict}</span>
      </div>`;
    }).join('')}
  </div>`;
}

// ── Sezione A: Condizioni attuali ─────────────────────────────────────────────

function renderCurrentPanel(data) {
  const current  = data.current;
  const todayDay = data.days.find(d => isToday(d.target_date));

  // Icona meteo: da realtime se disponibile, altrimenti da previsione di oggi
  let icon, iconLabel;
  if (current && current.temp_c != null) {
    icon      = weatherIconFromCurrent(current);
    iconLabel = null;
  } else if (todayDay) {
    icon      = weatherIconForDay(todayDay);
    iconLabel = `<span class="badge badge-ghost badge-xs ml-1">previsione</span>`;
  } else {
    icon      = '⛅';
    iconLabel = null;
  }

  // Temperatura principale: realtime se disponibile, altrimenti tmax p50 di oggi
  const mainTemp = current?.temp_c ?? todayDay?.forecasts?.tmax_c?.p50 ?? null;
  const tempStr  = mainTemp != null ? `${mainTemp.toFixed(1)}°` : '—';

  // Campi derivati (solo da realtime)
  const feelsLike = current?.feels_like_c  != null ? `${current.feels_like_c.toFixed(1)}°` : null;
  const dewpoint  = current?.dewpoint_c    != null ? `${current.dewpoint_c.toFixed(1)}°`   : null;
  const wind      = current?.wind_speed_ms != null ? fmtWind(current.wind_speed_ms)         : null;
  const hum       = current?.humidity_pct  != null ? `${current.humidity_pct.toFixed(0)}%`  : null;
  const prec      = current?.precip_mm     != null ? `${current.precip_mm.toFixed(1)} mm`   : null;
  const ts        = current?.ts ? new Date(current.ts).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' }) : null;

  const metaRow = (feelsLike || dewpoint) ? `
    <div class="flex gap-4 text-sm text-base-content/70 mt-1 flex-wrap">
      ${feelsLike ? `<span>Percepita <strong>${feelsLike}</strong></span>` : ''}
      ${dewpoint  ? `<span>Rugiada <strong>${dewpoint}</strong></span>`   : ''}
    </div>` : '';

  const statsRow = `
    <div class="grid grid-cols-3 gap-3 mt-4">
      <div class="bg-base-200 rounded-lg p-2.5 text-center">
        <div class="text-xs text-base-content/60 mb-0.5">💨 Vento</div>
        <div class="font-semibold text-sm">${wind ?? '—'}</div>
      </div>
      <div class="bg-base-200 rounded-lg p-2.5 text-center">
        <div class="text-xs text-base-content/60 mb-0.5">💧 Umidità</div>
        <div class="font-semibold text-sm">${hum ?? '—'}</div>
      </div>
      <div class="bg-base-200 rounded-lg p-2.5 text-center">
        <div class="text-xs text-base-content/60 mb-0.5">🌧 Pioggia</div>
        <div class="font-semibold text-sm">${prec ?? '—'}</div>
      </div>
    </div>`;

  const aqSection = renderAirQuality(data.air_quality);

  const noRealtimeNote = !current
    ? `<p class="text-xs text-base-content/50 mt-3 italic">Dati realtime non disponibili — indicatori calcolati su previsione</p>`
    : `<p class="text-xs text-base-content/40 mt-3">SIR/Netatmo${ts ? ` · ${ts}` : ''}</p>`;

  const indSection = todayDay ? `
    <div class="border-t border-base-300 mt-4 pt-4">
      <div class="text-xs font-semibold uppercase tracking-widest text-base-content/50 mb-2">Indicatori oggi</div>
      ${renderIndicatorsGrid(todayDay.indicators)}
    </div>` : '';

  return `
    <section class="card card-bordered bg-base-100 shadow-sm mb-4">
      <div class="card-body p-5">
        <div class="flex items-start gap-4">
          <span class="text-6xl leading-none mt-1">${icon}${iconLabel ?? ''}</span>
          <div class="flex-1">
            <div class="text-5xl font-bold tracking-tight leading-none">${tempStr}</div>
            ${metaRow}
          </div>
        </div>
        ${statsRow}
        ${aqSection}
        ${noRealtimeNote}
        ${indSection}
      </div>
    </section>`;
}

// ── Day cards strip ───────────────────────────────────────────────────────────

function renderDayCards(days, activeDayIdx) {
  const cards = days.map((day, idx) => {
    const { target_date, forecasts: fc, indicators } = day;
    const dots = Object.entries(indicators).map(([id, ind]) => {
      const meta = INDICATOR_META[id] ?? { label: id };
      const cls  = VERDICT_CLS[ind.verdict];
      return `<span class="inline-block w-2.5 h-2.5 rounded-full ${cls ? cls.dot : 'bg-base-300'}" title="${meta.label}: ${ind.verdict}"></span>`;
    }).join('');
    const hasRain = fc.precip_mm.p50 != null && fc.precip_mm.p50 >= 0.1;
    const icon = weatherIconForDay(day);

    return `<div class="card card-compact bg-base-100 border border-base-300 shadow-sm cursor-pointer shrink-0 min-w-20${idx === activeDayIdx ? ' ring-2 ring-primary' : ''}" data-idx="${idx}">
      <div class="card-body p-2.5 items-center text-center gap-0.5">
        <div class="text-sm font-semibold capitalize">${fmtDayShort(target_date)}</div>
        <div class="text-xs text-base-content/50 capitalize">${fmtDateShort(target_date)}</div>
        <span class="text-2xl leading-none my-0.5">${icon}</span>
        <div class="flex flex-col gap-0">
          <span class="text-base font-bold tracking-tight">${fmtTemp(fc.tmax_c.p50)}</span>
          <span class="text-sm text-base-content/60">${fmtTemp(fc.tmin_c.p50)}</span>
        </div>
        ${hasRain ? `<div class="text-xs text-blue-500 font-medium min-h-4">${fmtPrecip(fc.precip_mm.p50)}</div>` : '<div class="min-h-4"></div>'}
        <div class="flex gap-0.5 flex-wrap justify-center mt-0.5">${dots}</div>
      </div>
    </div>`;
  }).join('');

  return `<section class="flex gap-2 overflow-x-auto p-1 mb-3" style="-webkit-overflow-scrolling:touch;scrollbar-width:thin">${cards}</section>`;
}

// ── NWP comparison table ──────────────────────────────────────────────────────

function fmtLastRun(iso) {
  if (!iso) return '—';
  const d = new Date(iso);   // naive ISO → local time
  return d.toLocaleString('it-IT', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function renderNwpComparison(day) {
  const nwp = day.nwp_comparison;
  const fc  = day.forecasts;
  if (!nwp || nwp.length === 0) return '';

  const nwpRows = nwp.map(m => `
    <tr>
      <td class="font-medium">${m.label}</td>
      <td class="text-right tabular-nums">${m.tmin_c != null ? m.tmin_c.toFixed(1) + '°' : '—'}</td>
      <td class="text-right tabular-nums">${m.tmax_c != null ? m.tmax_c.toFixed(1) + '°' : '—'}</td>
      <td class="text-right tabular-nums">${m.precip_mm != null ? m.precip_mm.toFixed(1) + ' mm' : '—'}</td>
      <td class="text-right text-xs text-base-content/50 tabular-nums">${fmtLastRun(m.last_run)}</td>
    </tr>`).join('');

  return `
    <div class="mt-4 pt-4 border-t border-base-300">
      <h4 class="text-xs text-base-content/60 font-semibold uppercase tracking-wider mb-2">Confronto modelli</h4>
      <table class="table table-sm">
        <thead>
          <tr><th>Modello</th><th class="text-right">Tmin</th><th class="text-right">Tmax</th><th class="text-right">Precip</th><th class="text-right">Ultimo run</th></tr>
        </thead>
        <tbody>
          ${nwpRows}
          <tr class="font-semibold bg-base-200">
            <td class="text-primary">★ Guazza ML</td>
            <td class="text-right tabular-nums">${fmtTemp(fc.tmin_c.p50)}</td>
            <td class="text-right tabular-nums">${fmtTemp(fc.tmax_c.p50)}</td>
            <td class="text-right tabular-nums">${fmtPrecip(fc.precip_mm.p50)}</td>
            <td></td>
          </tr>
        </tbody>
      </table>
    </div>`;
}

// ── Sezione B: Previsioni giornaliere (striscia + dettaglio) ──────────────────

function renderDayExpanded(day) {
  const { forecasts: fc, indicators, target_date, lead_time_h } = day;
  const icon = weatherIconForDay(day);

  return `
    <section id="day-expanded" class="card card-bordered bg-base-100 shadow-sm mb-4">
      <div class="card-body p-5">
        <div class="flex items-center gap-3 mb-4 flex-wrap">
          <span class="text-4xl leading-none">${icon}</span>
          <div class="flex items-baseline gap-2 flex-wrap">
            <span class="text-xl font-bold capitalize">${fmtDayLabel(target_date)}</span>
            <span class="text-sm text-base-content/60 capitalize">${fmtDate(target_date)}</span>
            <span class="badge badge-ghost badge-sm">+${lead_time_h}h</span>
          </div>
        </div>
        <div class="grid grid-cols-3 gap-3 mb-5">
          <div class="bg-base-200 border border-base-300 rounded-lg p-3.5">
            <div class="text-xs text-base-content/60 mb-1">🌡 Tmin</div>
            <div class="text-3xl font-bold mb-2 tracking-tight">${fmtTemp(fc.tmin_c.p50)}</div>
            ${ciBar(fc.tmin_c, '°')}
          </div>
          <div class="bg-base-200 border border-base-300 rounded-lg p-3.5">
            <div class="text-xs text-base-content/60 mb-1">🌡 Tmax</div>
            <div class="text-3xl font-bold mb-2 tracking-tight">${fmtTemp(fc.tmax_c.p50)}</div>
            ${ciBar(fc.tmax_c, '°')}
          </div>
          <div class="bg-base-200 border border-base-300 rounded-lg p-3.5">
            <div class="text-xs text-base-content/60 mb-1">🌧 Precip</div>
            <div class="text-3xl font-bold mb-2 tracking-tight">${fmtPrecip(fc.precip_mm.p50)}</div>
            ${ciBar(fc.precip_mm, ' mm')}
          </div>
        </div>
        ${renderIndicatorsGrid(indicators)}
        ${renderNwpComparison(day)}
      </div>
    </section>`;
}

// ── Model switch ──────────────────────────────────────────────────────────────

function renderModelSwitch(data) {
  const models = [{ source: 'guazza', label: '★ Guazza ML' }];
  (data.nwp_models_hourly || []).forEach(m => models.push({ source: m.source, label: m.label }));
  return `<div class="flex gap-1 flex-wrap" id="model-switch">
    ${models.map(m =>
      `<button class="btn btn-xs ${m.source === selectedModel ? 'btn-primary' : 'btn-outline'}" data-src="${m.source}">${m.label}</button>`
    ).join('')}
  </div>`;
}

// ── Sezione C: Grafico unico multi-giorno ─────────────────────────────────────

function buildChartPoints(data, model, targetDate) {
  const [y, m, d] = targetDate.split('-').map(Number);
  const dayStart = new Date(y, m - 1, d, 0, 0, 0);
  const dayEnd   = new Date(y, m - 1, d, 23, 59, 59);
  const points = [];

  if (model === 'guazza') {
    const hourlyData = data.days.find(day => day.target_date === targetDate)?.hourly || [];
    hourlyData.forEach(h => {
      points.push({ ts: new Date(y, m - 1, d, h.hour, 0, 0),
                    temp_c: h.temp_c, humidity_pct: h.humidity_pct,
                    precip_mm: h.precip_mm, precip_prob: h.precip_prob,
                    wind_speed_ms: h.wind_speed_ms });
    });
  } else {
    const modelData = (data.nwp_models_hourly || []).find(mdl => mdl.source === model);
    (modelData?.data || []).forEach(pt => {
      const ts = new Date(pt.ts.replace('Z', ''));
      if (ts >= dayStart && ts <= dayEnd) {
        points.push({ ts, temp_c: pt.temp_c, humidity_pct: pt.humidity_pct,
                      precip_mm: pt.precip_mm, precip_prob: null,
                      wind_speed_ms: pt.wind_speed_ms });
      }
    });
  }
  return points.sort((a, b) => a.ts - b.ts);
}

function chartPalette() {
  const dark = document.documentElement.dataset.theme === 'dark';
  return {
    grid:  dark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)',
    label: dark ? '#94a3b8' : '#64748b',
    temp:  '#f97316',
    hum:   '#3b82f6',
    wind:  '#10b981',
  };
}

function precipDatasets(points) {
  return {
    data: points.map(pt => ({ x: pt.ts, y: (pt.precip_mm ?? 0) < 0.05 ? 0 : (pt.precip_mm ?? 0) })),
    bg:   points.map(pt => {
      const y = (pt.precip_mm ?? 0);
      if (y < 0.05) return 'rgba(59,130,246,0.08)';
      // Opacità proporzionale all'intensità, mai sotto 0.6
      const prob = pt.precip_prob ?? 0.8;
      return `rgba(37,99,235,${(0.55 + prob * 0.45).toFixed(2)})`;
    }),
  };
}

// Crosshair verticale inline
const crosshairPlugin = {
  id: 'crosshair',
  afterDraw(chart) {
    const active = chart.tooltip?._active ?? [];
    if (!active.length) return;
    const ctx = chart.ctx;
    const x = active[0].element.x;
    const { top, bottom } = chart.chartArea;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(148,163,184,0.55)';
    ctx.setLineDash([3, 3]);
    ctx.stroke();
    ctx.restore();
  },
};

function initChart(data, model, targetDate) {
  const canvas = document.getElementById('meteo-chart');
  if (!canvas) return;
  if (meteoChart) { meteoChart.destroy(); meteoChart = null; }

  const [y, m, d] = targetDate.split('-').map(Number);
  const xMin = new Date(y, m - 1, d, 0, 0, 0);
  const xMax = new Date(y, m - 1, d, 23, 0, 0);

  const points = buildChartPoints(data, model, targetDate);
  canvas.style.display = '';
  document.getElementById('chart-no-data')?.classList.add('hidden');

  const p = chartPalette();
  const { data: precipData, bg: precipBg } = precipDatasets(points);

  meteoChart = new Chart(canvas.getContext('2d'), { plugins: [crosshairPlugin],
    data: {
      datasets: [
        {
          type: 'line',
          label: 'Temperatura (°C)',
          data: points.filter(pt => pt.temp_c != null).map(pt => ({ x: pt.ts, y: pt.temp_c })),
          borderColor: p.temp,
          backgroundColor: 'transparent',
          borderWidth: 2,
          pointRadius: 0,
          yAxisID: 'yTemp',
          tension: 0.3,
          order: 1,
        },
        {
          type: 'line',
          label: 'Umidità (%)',
          data: points.filter(pt => pt.humidity_pct != null).map(pt => ({ x: pt.ts, y: pt.humidity_pct })),
          borderColor: p.hum,
          backgroundColor: 'transparent',
          borderWidth: 1.5,
          borderDash: [4, 3],
          pointRadius: 0,
          yAxisID: 'yHum',
          tension: 0.3,
          order: 2,
        },
        {
          type: 'bar',
          label: 'Precipitazioni (mm)',
          data: precipData,
          backgroundColor: precipBg,
          yAxisID: 'yTemp',
          barPercentage: 0.9,
          categoryPercentage: 1.0,
          order: 3,
        },
        {
          type: 'line',
          label: 'Vento (km/h)',
          data: points.filter(pt => pt.wind_speed_ms != null)
                      .map(pt => ({ x: pt.ts, y: pt.wind_speed_ms * 3.6 })),
          borderColor: p.wind,
          backgroundColor: 'transparent',
          borderWidth: 1.5,
          borderDash: [2, 2],
          pointRadius: 0,
          yAxisID: 'yWind',
          tension: 0.3,
          order: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      hover: { mode: 'index', intersect: false },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: items => new Date(items[0].raw.x).toLocaleString('it-IT', {
              weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
            }),
            label: item => {
              if (item.datasetIndex === 0) return ` ${item.raw.y.toFixed(1)}°C`;
              if (item.datasetIndex === 1) return ` Umidità: ${item.raw.y.toFixed(0)}%`;
              if (item.datasetIndex === 2 && item.raw.y > 0.05) return ` Precip: ${item.raw.y.toFixed(1)} mm`;
              if (item.datasetIndex === 3) return ` Vento: ${item.raw.y.toFixed(0)} km/h`;
              return null;
            },
          },
        },
      },
      scales: {
        x: {
          type: 'time',
          min: xMin,
          max: xMax,
          time: { unit: 'hour', displayFormats: { hour: 'HH' } },
          grid: { color: p.grid },
          ticks: { color: p.label, maxTicksLimit: 13, stepSize: 2, font: { size: 9 } },
        },
        yTemp: {
          position: 'left',
          grid: { color: p.grid },
          ticks: { color: p.temp, callback: v => `${v}°`, font: { size: 9 } },
        },
        yHum: {
          position: 'right',
          min: 0,
          max: 100,
          grid: { drawOnChartArea: false },
          ticks: { color: p.hum, callback: v => `${v}%`, font: { size: 9 } },
        },
        yWind: {
          position: 'right',
          min: 0,
          grid: { drawOnChartArea: false },
          ticks: { color: p.wind, callback: v => `${v}`, font: { size: 9 } },
          display: true,
        },
      },
    },
  });
}

function updateChartModel(data, model, targetDate) {
  if (!meteoChart) { initChart(data, model, targetDate); return; }
  const points = buildChartPoints(data, model, targetDate);
  const { data: precipData, bg: precipBg } = precipDatasets(points);
  meteoChart.data.datasets[0].data = points.filter(pt => pt.temp_c != null).map(pt => ({ x: pt.ts, y: pt.temp_c }));
  meteoChart.data.datasets[1].data = points.filter(pt => pt.humidity_pct != null).map(pt => ({ x: pt.ts, y: pt.humidity_pct }));
  meteoChart.data.datasets[2].data = precipData;
  meteoChart.data.datasets[2].backgroundColor = precipBg;
  meteoChart.data.datasets[3].data = points.filter(pt => pt.wind_speed_ms != null)
                                            .map(pt => ({ x: pt.ts, y: pt.wind_speed_ms * 3.6 }));
  meteoChart.update();
}

// ── Weekly combined chart ─────────────────────────────────────────────────────

function buildWeeklyPoints(data, model) {
  if (model === 'guazza') {
    const points = [];
    data.days.forEach(day => {
      const [y, m, d] = day.target_date.split('-').map(Number);
      (day.hourly || []).forEach(h => {
        points.push({ ts: new Date(y, m - 1, d, h.hour, 0, 0),
                      temp_c: h.temp_c, humidity_pct: h.humidity_pct,
                      precip_mm: h.precip_mm, precip_prob: h.precip_prob,
                      wind_speed_ms: h.wind_speed_ms });
      });
    });
    return points.sort((a, b) => a.ts - b.ts);
  }
  const modelData = (data.nwp_models_hourly || []).find(mdl => mdl.source === model);
  return (modelData?.data || []).map(pt => ({
    ts: new Date(pt.ts.replace('Z', '')),
    temp_c: pt.temp_c, humidity_pct: pt.humidity_pct,
    precip_mm: pt.precip_mm, precip_prob: null,
    wind_speed_ms: pt.wind_speed_ms,
  })).sort((a, b) => a.ts - b.ts);
}

function renderWeeklyModelSwitch(data, model) {
  const models = [{ source: 'guazza', label: '★ Guazza ML' }];
  (data.nwp_models_hourly || []).forEach(m => models.push({ source: m.source, label: m.label }));
  return `<div class="flex gap-1 flex-wrap" id="weekly-model-switch">
    ${models.map(m => `<button class="btn btn-xs ${m.source === model ? 'btn-primary' : 'btn-outline'}" data-src="${m.source}">${m.label}</button>`).join('')}
  </div>`;
}

function initWeeklyChart(data, model) {
  const canvas = document.getElementById('multiday-chart');
  if (!canvas) return;
  if (multiDayChart) { multiDayChart.destroy(); multiDayChart = null; }
  if (!data.days.length) return;

  const first = data.days[0].target_date.split('-').map(Number);
  const last  = data.days[data.days.length - 1].target_date.split('-').map(Number);
  const xMin  = new Date(first[0], first[1] - 1, first[2], 0, 0, 0);
  const xMax  = new Date(last[0],  last[1] - 1,  last[2],  23, 0, 0);

  const points = buildWeeklyPoints(data, model);
  const p = chartPalette();
  const { data: precipData, bg: precipBg } = precipDatasets(points);

  multiDayChart = new Chart(canvas.getContext('2d'), { plugins: [crosshairPlugin],
    data: {
      datasets: [
        { type: 'line', label: 'Temperatura (°C)',
          data: points.filter(pt => pt.temp_c != null).map(pt => ({ x: pt.ts, y: pt.temp_c })),
          borderColor: p.temp, backgroundColor: 'transparent',
          borderWidth: 2, pointRadius: 0, yAxisID: 'yTemp', tension: 0.3, order: 1 },
        { type: 'line', label: 'Umidità (%)',
          data: points.filter(pt => pt.humidity_pct != null).map(pt => ({ x: pt.ts, y: pt.humidity_pct })),
          borderColor: p.hum, backgroundColor: 'transparent',
          borderWidth: 1.5, borderDash: [4, 3], pointRadius: 0, yAxisID: 'yHum', tension: 0.3, order: 2 },
        { type: 'bar', label: 'Precipitazioni (mm)',
          data: precipData, backgroundColor: precipBg,
          yAxisID: 'yTemp', barPercentage: 0.9, categoryPercentage: 1.0, order: 3 },
        { type: 'line', label: 'Vento (km/h)',
          data: points.filter(pt => pt.wind_speed_ms != null)
                      .map(pt => ({ x: pt.ts, y: pt.wind_speed_ms * 3.6 })),
          borderColor: p.wind, backgroundColor: 'transparent',
          borderWidth: 1.5, borderDash: [2, 2], pointRadius: 0, yAxisID: 'yWind', tension: 0.3, order: 4 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      hover: { mode: 'index', intersect: false },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: items => new Date(items[0].raw.x).toLocaleString('it-IT', {
              weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
            }),
            label: item => {
              if (item.datasetIndex === 0) return ` ${item.raw.y.toFixed(1)}°C`;
              if (item.datasetIndex === 1) return ` Umidità: ${item.raw.y.toFixed(0)}%`;
              if (item.datasetIndex === 2 && item.raw.y > 0.05) return ` Precip: ${item.raw.y.toFixed(1)} mm`;
              if (item.datasetIndex === 3) return ` Vento: ${item.raw.y.toFixed(0)} km/h`;
              return null;
            },
          },
        },
      },
      scales: {
        x: { type: 'time', min: xMin, max: xMax,
             time: { unit: 'day', displayFormats: { day: 'dd/MM' } },
             grid: { color: p.grid },
             ticks: { color: p.label, font: { size: 9 } } },
        yTemp: { position: 'left', grid: { color: p.grid },
                 ticks: { color: p.temp, callback: v => `${v}°`, font: { size: 9 } } },
        yHum:  { position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false },
                 ticks: { color: p.hum, callback: v => `${v}%`, font: { size: 9 } } },
        yWind: { position: 'right', min: 0, grid: { drawOnChartArea: false },
                 ticks: { color: p.wind, callback: v => `${v}`, font: { size: 9 } }, display: true },
      },
    },
  });
}

function updateWeeklyChart(data, model) {
  if (!multiDayChart) { initWeeklyChart(data, model); return; }
  const points = buildWeeklyPoints(data, model);
  const { data: precipData, bg: precipBg } = precipDatasets(points);
  multiDayChart.data.datasets[0].data = points.filter(pt => pt.temp_c != null).map(pt => ({ x: pt.ts, y: pt.temp_c }));
  multiDayChart.data.datasets[1].data = points.filter(pt => pt.humidity_pct != null).map(pt => ({ x: pt.ts, y: pt.humidity_pct }));
  multiDayChart.data.datasets[2].data = precipData;
  multiDayChart.data.datasets[2].backgroundColor = precipBg;
  multiDayChart.data.datasets[3].data = points.filter(pt => pt.wind_speed_ms != null)
                                               .map(pt => ({ x: pt.ts, y: pt.wind_speed_ms * 3.6 }));
  multiDayChart.update();
}

// ── Render ────────────────────────────────────────────────────────────────────

function render(container, data) {
  if (selectedDayIdx >= data.days.length) selectedDayIdx = 0;
  const day = data.days[selectedDayIdx];
  const targetDate = day?.target_date ?? data.days[0]?.target_date;

  if (meteoChart) { meteoChart.destroy(); meteoChart = null; }
  if (multiDayChart) { multiDayChart.destroy(); multiDayChart = null; }

  container.innerHTML = `
    <div class="text-xs text-base-content/50 mb-3">Aggiornato: ${fmtDateTime(data.generated_at)}${staleWarning(data.generated_at)}</div>

    ${renderCurrentPanel(data)}

    ${data.days.length > 0 ? `
    <p class="text-xs font-semibold uppercase tracking-widest text-base-content/40 mb-2 px-1">Previsioni</p>
    ${renderDayCards(data.days, selectedDayIdx)}
    ${day ? renderDayExpanded(day) : ''}
    ` : ''}

    <section class="card card-bordered bg-base-100 shadow-sm mb-4">
      <div class="card-body p-4">
        <div class="flex items-center justify-between flex-wrap gap-2 mb-3">
          <h3 class="text-sm text-base-content/60 font-medium">Tendenza meteo</h3>
          ${renderModelSwitch(data)}
        </div>
        <div class="combined-chart-wrap">
          <div id="chart-container" class="relative" style="height:220px;min-width:700px">
            <canvas id="meteo-chart"></canvas>
            <div id="chart-no-data" class="hidden text-sm text-base-content/60 p-4">Dati grafici non disponibili per il modello selezionato</div>
          </div>
        </div>
        <div class="flex gap-5 mt-2 text-xs text-base-content/50 flex-wrap">
          <span style="color:#f97316">— Temperatura</span>
          <span style="color:#3b82f6">‐ ‐ Umidità</span>
          <span style="color:rgba(59,130,246,0.6)">▪ Precipitazioni</span>
          <span style="color:#10b981">‥ Vento km/h</span>
        </div>
      </div>
    </section>

    <section class="card card-bordered bg-base-100 shadow-sm mb-4">
      <div class="card-body p-4">
        <div class="flex items-center justify-between flex-wrap gap-2 mb-3">
          <h3 class="text-sm text-base-content/60 font-medium">Tendenza settimanale</h3>
          ${renderWeeklyModelSwitch(data, selectedWeeklyModel)}
        </div>
        <div class="combined-chart-wrap">
          <div id="multiday-chart-container" class="relative" style="height:260px;min-width:700px">
            <canvas id="multiday-chart"></canvas>
          </div>
        </div>
        <div class="flex gap-5 mt-2 text-xs text-base-content/50 flex-wrap">
          <span style="color:#f97316">— Temperatura</span>
          <span style="color:#3b82f6">‐ ‐ Umidità</span>
          <span style="color:rgba(59,130,246,0.6)">▪ Precipitazioni</span>
          <span style="color:#10b981">‥ Vento km/h</span>
        </div>
      </div>
    </section>

    ${coverageBadge(data.coverage_empirical_30d)}
  `;

  // trigger fade-in
  container.classList.remove('anim-fade-in');
  void container.offsetWidth;
  container.classList.add('anim-fade-in');

  if (targetDate) initChart(data, selectedModel, targetDate);
  initWeeklyChart(data, selectedWeeklyModel);

  container.querySelectorAll('[data-idx]').forEach(card => {
    card.addEventListener('click', () => {
      selectedDayIdx = parseInt(card.dataset.idx, 10);
      render(container, data);
      const expanded = container.querySelector('#day-expanded');
      if (expanded) {
        const headerH = document.querySelector('header')?.offsetHeight ?? 0;
        window.scrollTo({ top: expanded.getBoundingClientRect().top + window.scrollY - headerH - 8, behavior: 'smooth' });
      }
    });
  });

  document.getElementById('model-switch')?.querySelectorAll('[data-src]').forEach(btn => {
    btn.addEventListener('click', () => {
      selectedModel = btn.dataset.src;
      document.getElementById('model-switch')?.querySelectorAll('[data-src]').forEach(b => {
        b.className = `btn btn-xs ${b.dataset.src === selectedModel ? 'btn-primary' : 'btn-outline'}`;
      });
      updateChartModel(data, selectedModel, targetDate);
    });
  });

  document.getElementById('weekly-model-switch')?.querySelectorAll('[data-src]').forEach(btn => {
    btn.addEventListener('click', () => {
      selectedWeeklyModel = btn.dataset.src;
      document.getElementById('weekly-model-switch')?.querySelectorAll('[data-src]').forEach(b => {
        b.className = `btn btn-xs ${b.dataset.src === selectedWeeklyModel ? 'btn-primary' : 'btn-outline'}`;
      });
      updateWeeklyChart(data, selectedWeeklyModel);
    });
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────

initDarkMode();
window.addEventListener('popstate', () => loadLocation(getActiveLoc()));
loadLocation(getActiveLoc());
