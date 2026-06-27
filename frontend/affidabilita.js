// Guazza · Affidabilità
// Pagina statica self-contained: carica /data/skill.json e /data/skill_history.json
// (file globali generati dai job `guazza-skill` e `guazza-skill-history`) e mostra:
//   1) MAE Guazza vs consensus NWP per Tmin/Tmax, per orizzonte D+0..D+7
//   2) Time series forecast vs actual, finestra 7gg / 30gg / totale
//
// Riusa le classi CSS esistenti (.g-tab, .g-card, .g-skill__*, .g-chart-legend)
// e l'estetica Carbone+Iride di index.html. Niente dipendenza da app.js.

const AFF_LOCATIONS = [
  { id: 'casa_campi',    label: 'Casa Campi' },
  { id: 'lavoro_cosimo', label: 'Lav. Cosimo' },
  { id: 'lavoro_madda',  label: 'Lav. Madda' },
  { id: 'casa_cesto',    label: 'Casa Cesto' },
  { id: 'casa_nicco',    label: 'Casa Nicco' },
  { id: 'casa_cercina',  label: 'Casa Cercina' },
];

const SKILL_URL        = '/data/skill.json';
const SKILL_HISTORY_URL = '/data/skill_history.json';
// Fonti NWP nell'ordine in cui appaiono nella legenda (e nel JSON).
// Mantenuto allineato con `jobs.skill_history.NWP_SOURCES` (lato backend).
const NWP_SOURCES = [
  'open_meteo_ecmwf_ifs',
  'open_meteo_icon_eu',
  'open_meteo_icon_d2',
  'open_meteo_gfs025',
  'open_meteo_arome_france',
  'open_meteo_italia_meteo_arpae_icon_2i',
];
const NWP_LABELS = {
  'open_meteo_ecmwf_ifs':                    'ECMWF IFS',
  'open_meteo_icon_eu':                      'ICON-EU',
  'open_meteo_icon_d2':                      'ICON-D2',
  'open_meteo_gfs025':                       'GFS 0.25°',
  'open_meteo_arome_france':                 'AROME France',
  'open_meteo_italia_meteo_arpae_icon_2i':   'ARPAE ICON-2I',
};

let affChart     = null;
let affSkillData = null;     // skill.json (curva MAE per lead)
let histData     = null;     // skill_history.json (time series)
let affLocId     = 'casa_campi';
let histWindow   = 30;       // giorni, 0 = totale

// ── Utils ───────────────────────────────────────────────────────────────────

function fmtIsoDate(iso) {
  const [y, m, d] = iso.split('-');
  return `${d}/${m}/${y.slice(2)}`;
}

function fmtShortDate(iso) {
  // Per l'asse X del grafico history: "DD/MM" (no anno, basta scansione settimana).
  const [, m, d] = iso.split('-');
  return `${d}/${m}`;
}

function showEl(id) { document.getElementById(id)?.classList.remove('hidden'); }
function hideEl(id) { document.getElementById(id)?.classList.add('hidden'); }

// ── Fetch ───────────────────────────────────────────────────────────────────

async function loadSkill() {
  try {
    const r = await fetch(SKILL_URL, { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    affSkillData = await r.json();
  } catch {
    affSkillData = null;
    showEl('aff-error');
  }
}

async function loadHistory() {
  try {
    const r = await fetch(SKILL_HISTORY_URL, { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    histData = await r.json();
  } catch {
    // history è best-effort: la pagina resta valida anche senza.
    histData = null;
  }
}

// ── Render: tabs location ───────────────────────────────────────────────────

function renderLocationTabs() {
  const nav = document.getElementById('aff-locations');
  nav.innerHTML = AFF_LOCATIONS.map(l => {
    const active = l.id === affLocId;
    return `<button class="g-tab${active ? ' g-tab--active' : ''}" data-loc="${l.id}"${active ? ' aria-current="page"' : ''}>${l.label}</button>`;
  }).join('');
  nav.querySelectorAll('[data-loc]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.dataset.loc !== affLocId) {
        affLocId = btn.dataset.loc;
        renderLocationTabs();
        renderCard();
        renderHistory();
      }
    });
  });
}

// ── Render: card MAE per lead ───────────────────────────────────────────────

function renderCard() {
  const card = document.getElementById('aff-card');
  const loc = affSkillData?.locations?.[affLocId];
  if (!loc) { hideEl('aff-card'); return; }

  const tminPts = loc.tmin_c || [];
  const tmaxPts = loc.tmax_c || [];
  const hasAny = (tminPts.some(p => p.mae_ml != null)) || (tmaxPts.some(p => p.mae_ml != null));
  if (!hasAny) { hideEl('aff-card'); return; }
  showEl('aff-card');

  document.getElementById('aff-sub').textContent =
    `MAE °C · verità: stazione SIR ${loc.sir_station_id}`;

  const valid = [...tminPts, ...tmaxPts].filter(p => p.n != null);
  const nMin = valid.length ? Math.min(...valid.map(p => p.n)) : 0;
  const nMax = valid.length ? Math.max(...valid.map(p => p.n)) : 0;
  const win = `${fmtIsoDate(affSkillData.window_start)}→${fmtIsoDate(affSkillData.window_end)}`;
  document.getElementById('aff-caption').textContent =
    `Errore medio assoluto out-of-sample contro il termometro SIR primario, per orizzonte. ` +
    `Linee piene = Guazza ML; tratteggiate = consensus NWP. Caldo = T max, freddo = T min. ` +
    `Più in basso è, più Guazza è accurata. Finestra ${win} ` +
    `(${nMin}–${nMax} giorni per orizzonte): è una finestra di mesi, non lifetime.`;

  drawChart(tminPts, tmaxPts);
}

function drawChart(tminPts, tmaxPts) {
  const canvas = document.getElementById('aff-chart');
  if (!canvas || typeof Chart === 'undefined') return;
  if (affChart) { affChart.destroy(); affChart = null; }

  const css = getComputedStyle(document.documentElement);
  const v = name => css.getPropertyValue(name).trim();
  const axis   = v('--chart-axis');
  const grid   = v('--chart-grid');
  const warm   = v('--warm');
  const cold   = v('--cold');
  const warmNwp = 'rgba(249,115,22,0.45)';
  const coldNwp = 'rgba(96,165,250,0.45)';

  const leadPoints = (tminPts.length ? tminPts : tmaxPts);
  const labels = leadPoints.map(p => `D+${p.lead_h / 24}`);

  const pick = (points, key) => points.map(p => p[key]);

  function skillAfterBody(items) {
    const i = items[0].dataIndex;
    const lines = [];
    for (const it of items) {
      const ds = it.dataset;
      const targetPts = ds._target === 'tmin' ? tminPts : tmaxPts;
      const p = targetPts[i];
      if (!p || p.skill_pct == null) continue;
      const sign = p.skill_pct >= 0 ? '+' : '';
      lines.push(`${ds.label}: skill ${sign}${p.skill_pct}%  ·  n=${p.n}`);
    }
    return lines;
  }

  const datasets = [
    { label: 'NWP T max',  data: pick(tmaxPts, 'mae_nwp'),
      borderColor: warmNwp, backgroundColor: warmNwp,
      borderDash: [5, 4], borderWidth: 1.5, pointRadius: 3, spanGaps: true, tension: 0.25,
      _target: 'tmax' },
    { label: 'Guazza T max', data: pick(tmaxPts, 'mae_ml'),
      borderColor: warm, backgroundColor: warm,
      borderWidth: 2, pointRadius: 3, spanGaps: true, tension: 0.25,
      _target: 'tmax' },
    { label: 'NWP T min',  data: pick(tminPts, 'mae_nwp'),
      borderColor: coldNwp, backgroundColor: coldNwp,
      borderDash: [5, 4], borderWidth: 1.5, pointRadius: 3, spanGaps: true, tension: 0.25,
      _target: 'tmin' },
    { label: 'Guazza T min', data: pick(tminPts, 'mae_ml'),
      borderColor: cold, backgroundColor: cold,
      borderWidth: 2, pointRadius: 3, spanGaps: true, tension: 0.25,
      _target: 'tmin' },
  ];

  affChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { grid: { color: grid }, ticks: { color: axis } },
        y: { grid: { color: grid }, ticks: { color: axis, callback: x => `${x}°` },
             title: { display: true, text: 'MAE (°C)', color: axis } },
      },
      plugins: {
        legend: { labels: { color: axis, usePointStyle: true, boxWidth: 8 } },
        tooltip: { callbacks: { afterBody: skillAfterBody } },
      },
    },
  });
}

// ── Render: history time series ─────────────────────────────────────────────

function renderHistory() {
  const card = document.getElementById('hist-card');
  const loc = histData?.locations?.[affLocId];
  if (!loc || !histData) { hideEl('hist-card'); return; }

  // Determina se la location ha almeno una riga (tmin o tmax) per la finestra scelta
  const tmin = loc.tmin_c;
  const tmax = loc.tmax_c;
  const hasAny = (tmin?.dates?.length) || (tmax?.dates?.length);
  if (!hasAny) { hideEl('hist-card'); return; }
  showEl('hist-card');

  // Testo header
  const winText = histWindow === 0
    ? `tutto (dal ${fmtIsoDate(histData.min_date)})`
    : `ultimi ${histWindow}gg`;
  document.getElementById('hist-sub').textContent =
    `forecast a D-1 vs osservato a D · ${winText}`;

  // Filtra per finestra
  const sliced = sliceByWindow(tmin, histWindow);
  const slicedTmax = sliceByWindow(tmax, histWindow);
  const nDates = sliced.dates.length;
  // Conta NWP effettivamente disponibili (esclude modelli con tutti null nella finestra)
  const nNwpAvailable = NWP_SOURCES.filter(src => {
    const arr = sliced.sources[src];
    return arr && arr.some(v => v != null);
  }).length;
  document.getElementById('hist-caption').textContent =
    `${nDates} giorni · lead 24h · ${nNwpAvailable} NWP più Guazza ML contro la stazione SIR pesata della location. ` +
    `Più una linea è vicina alla riga nera (osservato), più quel modello ci ha preso. ` +
    `Finestra di append: ${fmtIsoDate(histData.min_date)}→${fmtIsoDate(histData.max_date)}.`;

  drawHist(sliced, 'hist-chart-tmax');
  drawHist(slicedTmax, 'hist-chart-tmin');
}

function sliceByWindow(series, windowDays) {
  // series: { dates: [iso, ...], actual: [...], guazza: [...], nwp_*: [...] }
  if (!series?.dates?.length) return { dates: [], actual: [], sources: {} };
  let dates = series.dates;
  let actual = series.actual;
  let slices = { guazza: series.guazza };
  for (const src of NWP_SOURCES) slices[src] = series[src];
  if (windowDays > 0 && dates.length > windowDays) {
    const start = dates.length - windowDays;
    dates = dates.slice(start);
    actual = actual.slice(start);
    for (const k of Object.keys(slices)) slices[k] = slices[k].slice(start);
  }
  return { dates, actual, sources: slices };
}

function drawHist(sliced, canvasId) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || typeof Chart === 'undefined') return;

  // Distruggi eventuale chart precedente su questo canvas
  const existing = Chart.getChart(canvas);
  if (existing) existing.destroy();

  if (!sliced.dates.length) return;

  const css = getComputedStyle(document.documentElement);
  const v = name => css.getPropertyValue(name).trim();
  const axis = v('--chart-axis');
  const grid = v('--chart-grid');
  const accent = v('--accent');
  const warm = v('--warm');
  const isTmax = canvasId.endsWith('tmax');
  const lineColor = isTmax ? warm : v('--cold');

  const labels = sliced.dates.map(fmtShortDate);

  // Dataset: actual (riferimento, nero spesso) + Guazza (accent) + 6 NWP (grigi tratteggiati)
  const datasets = [
    { label: 'Osservato', data: sliced.actual,
      borderColor: v('--text-1'), backgroundColor: v('--text-1'),
      borderWidth: 2.5, pointRadius: 0, spanGaps: true, tension: 0.2,
      order: 1 },
    { label: 'Guazza ML', data: sliced.sources.guazza,
      borderColor: accent, backgroundColor: accent,
      borderWidth: 2, pointRadius: 0, spanGaps: true, tension: 0.2,
      order: 2 },
  ];
  // NWP tratteggiati in grigio (alpha 0.55) — solo quelli con almeno un valore
  // non-null nella finestra corrente. Esclude modelli "morti" (es. GFS oggi ha
  // record orari senza temp_c nel DB).
  const nwpColor = 'rgba(148,163,174,0.55)';
  let nNwpShown = 0;
  for (const src of NWP_SOURCES) {
    const arr = sliced.sources[src];
    if (!arr || !arr.some(v => v != null)) continue;
    datasets.push({
      label: NWP_LABELS[src] || src,
      data: arr,
      borderColor: nwpColor, backgroundColor: nwpColor,
      borderDash: [3, 3], borderWidth: 1, pointRadius: 0,
      spanGaps: true, tension: 0.2,
      _isNwp: true, order: 3,
    });
    nNwpShown++;
  }

  new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { grid: { color: grid, display: false }, ticks: { color: axis, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
        y: { grid: { color: grid }, ticks: { color: axis, callback: x => `${x}°` } },
      },
      plugins: {
        legend: { display: false }, // legenda statica sopra il grafico
        tooltip: {
          callbacks: {
            filter: (item) => !item.dataset._isNwp, // nasconde i 6 NWP dal tooltip
            afterBody: (items) => {
              if (!sliced.dates.length) return '';
              const i = items[0].dataIndex;
              const lines = [];
              // Errore Guazza vs actual
              const g = sliced.sources.guazza?.[i];
              const a = sliced.actual?.[i];
              if (g != null && a != null) {
                const err = g - a;
                const sign = err >= 0 ? '+' : '';
                lines.push(`Guazza: ${sign}${err.toFixed(2)}° vs osservato`);
              }
              return lines;
            },
          },
        },
      },
    },
  });
}

// ── Filtro finestra (segmented control 7gg / 30gg / Totale) ────────────────

document.getElementById('hist-seg')?.addEventListener('click', e => {
  const btn = e.target.closest('.g-skill__seg-btn');
  if (!btn) return;
  const w = parseInt(btn.dataset.window, 10);
  if (w === histWindow) return;
  histWindow = w;
  document.querySelectorAll('#hist-seg .g-skill__seg-btn').forEach(b =>
    b.classList.toggle('is-active', parseInt(b.dataset.window, 10) === histWindow));
  renderHistory();
});

// ── Header meta: "skill aggiornato al" ──────────────────────────────────────

function renderHeaderMeta() {
  const meta = document.getElementById('header-meta');
  if (!meta) return;
  const ts = (affSkillData?.generated_at || histData?.generated_at || '').replace('T', ' ').slice(0, 16);
  if (ts) meta.textContent = `skill aggiornato ${ts}`;
}

// ── Boot ────────────────────────────────────────────────────────────────────

(async function boot() {
  await Promise.all([loadSkill(), loadHistory()]);
  if (!affSkillData && !histData) {
    showEl('aff-error');
    return;
  }
  renderHeaderMeta();
  renderLocationTabs();
  renderCard();
  renderHistory();
})();
