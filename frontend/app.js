'use strict';

// Dev: ln -s ../data/output frontend2/data  then: cd frontend2 && python3 -m http.server 8081
const DATA_URL = loc => `/data/${loc}.json`;
const TWEMOJI_OPTS = { folder: 'svg', ext: '.svg' };
const METEOCONS_BASE = 'https://cdn.jsdelivr.net/npm/@meteocons/svg@0.1.0/fill';

const LOCATIONS = [
  { id: 'casa_campi',    label: 'Casa Campi',  lat: 43.82,  lon: 11.13  },
  { id: 'lavoro_cosimo', label: 'Lav. Cosimo', lat: 43.75,  lon: 11.17  },
  { id: 'lavoro_madda',  label: 'Lav. Madda',  lat: 43.88,  lon: 11.09  },
  { id: 'casa_cesto',    label: 'Casa Cesto',  lat: 43.59,  lon: 11.46  },
  { id: 'casa_nicco',    label: 'Casa Nicco',  lat: 43.791, lon: 11.219 },
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

// Soglie qualità aria — mapping verde/giallo/rosso vs fasce ARPAT.
// [lo, hi]: value<lo→verde, lo≤value<hi→giallo, value≥hi→rosso.
const AQ_THRESHOLDS = {
  pm10:    [20, 40],
  pm25:    [10, 20],
  no2:     [80, 160],
  o3:      [72, 144],
  co:      [4,  8],
  benzene: [2,  4],
  so2:     [140, 280],
};

const PLAY_SVG  = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><polygon points="6,3 20,12 6,21"/></svg>';
const PAUSE_SVG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>';

const RV_API               = 'https://api.rainviewer.com/public/weather-maps.json';
const RV_TTL_MS            = 5 * 60 * 1000;
const RADAR_PAST_FRAMES    = 24;
const RADAR_NOWCAST_FRAMES = 12;

// ── State ─────────────────────────────────────────────────────────────────────
let currentData         = null;
let selectedDayIdx      = 0;
let selectedModel       = 'guazza';
let meteoChart          = null;
let multiDayChart       = null;
let radarMap     = null;
let radarLayers  = [];
let radarFrames  = [];
let radarIdx     = 0;
let radarTimer   = null;
let radarPlaying = false;
let radarCache   = null;
let _tipHideTimer = null;

// ── Utility ───────────────────────────────────────────────────────────────────

function escHtml(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function showEl(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('hidden');
  el.style.removeProperty('display');
}

function hideEl(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.add('hidden');
  el.style.display = 'none';
}

function showSkeleton() { showEl('skeleton-state'); }
function hideSkeleton() { hideEl('skeleton-state'); hideEl('error-state'); }

// ── Weather icon ──────────────────────────────────────────────────────────────

// Mappa codici WMO → {icon, iconName, text}. isNight rimappa i codici sereno (0/1) sulla luna.
// icon: emoji twemoji (fallback), iconName: slug Meteocons SVG, text: etichetta italiana.
// I codici non mappati esplicitamente cadono nel fallback ⛅/Variabile.
function wmoCondition(code, isNight = false) {
  if (code == null) return { icon: '⛅', iconName: 'cloudy', text: 'Variabile' };
  switch (code) {
    case 0:           return { icon: isNight ? '🌙' : '☀️',  iconName: isNight ? 'clear-night'               : 'clear-day',               text: isNight ? 'Sereno'        : 'Soleggiato' };
    case 1:           return { icon: isNight ? '🌙' : '🌤️', iconName: isNight ? 'partly-cloudy-night'        : 'partly-cloudy-day',        text: 'Sereno' };
    case 2:           return { icon: '⛅',  iconName: isNight ? 'partly-cloudy-night'        : 'partly-cloudy-day',        text: 'Parz. nuvoloso' };
    case 3:           return { icon: '☁️',  iconName: isNight ? 'overcast-night'             : 'overcast',                text: 'Coperto' };
    case 45: case 48: return { icon: '🌫️', iconName: isNight ? 'fog-night'                  : 'fog-day',                 text: 'Nebbia' };
    case 51: case 53: case 55: return { icon: '🌦️', iconName: 'drizzle',                                                  text: 'Pioviggine' };
    case 56: case 57: return { icon: '🌨️', iconName: 'sleet',                                                             text: 'Pioviggine gelata' };
    case 61:          return { icon: '🌧️', iconName: isNight ? 'partly-cloudy-night-rain'   : 'partly-cloudy-day-rain',  text: 'Pioggia debole' };
    case 63: case 65: return { icon: '🌧️', iconName: 'rain',                                                              text: 'Pioggia' };
    case 66: case 67: return { icon: '🌨️', iconName: 'sleet',                                                             text: 'Pioggia gelata' };
    case 71: case 73: case 75: case 77: return { icon: '❄️', iconName: 'snow',                                            text: 'Neve' };
    case 80:          return { icon: '🌦️', iconName: isNight ? 'partly-cloudy-night-rain'   : 'partly-cloudy-day-rain',  text: 'Rovesci' };
    case 81: case 82: return { icon: '🌧️', iconName: 'rain',                                                              text: 'Rovesci' };
    case 85: case 86: return { icon: '🌨️', iconName: 'snow',                                                              text: 'Neve a rovesci' };
    case 95:          return { icon: '⛈️', iconName: isNight ? 'thunderstorms-night'        : 'thunderstorms-day',        text: 'Temporale' };
    case 96: case 99: return { icon: '⛈️', iconName: isNight ? 'thunderstorms-night-rain'   : 'thunderstorms-day-rain',   text: 'Temporale con grandine' };
    default:          return { icon: '⛅', iconName: 'cloudy',                                                             text: 'Variabile' };
  }
}

// Le Meteocons hanno un margine trasparente fisso nel canvas 128×128: ritagliamo il
// viewBox per avvicinare il glifo al testo. 14 lascia 2px ai raggi di clear-day (che
// ruotando arrivano a 16px dal bordo) — è il vincolo più stretto del set.
const METEOCONS_CROP = '14 14 100 100';
const _weatherSvgCache = new Map();  // iconName -> Promise<string> (markup SVG ritagliato)

// Placeholder sincrono; l'SVG inline viene iniettato dopo da hydrateWeatherIcons.
// Serve l'inline (non <img>) per poter riscrivere il viewBox e togliere il margine.
function weatherIconHtml(iconName, emojiFallback, cls) {
  return `<span class="g-wicon ${cls}" data-wicon="${iconName}" data-wicon-fb="${emojiFallback}"></span>`;
}

function loadWeatherSvg(iconName) {
  let pending = _weatherSvgCache.get(iconName);
  if (!pending) {
    pending = fetch(`${METEOCONS_BASE}/${iconName}.svg`)
      .then(r => { if (!r.ok) throw new Error(`${r.status}`); return r.text(); })
      .then(txt => txt.replace(/viewBox="[^"]*"/, `viewBox="${METEOCONS_CROP}"`))
      .catch(err => { _weatherSvgCache.delete(iconName); throw err; });
    _weatherSvgCache.set(iconName, pending);
  }
  return pending;
}

// Idrata i placeholder [data-wicon] dentro container con l'SVG inline animato.
// Fallback a emoji nativa se il CDN non risponde (twemoji.parse è già passato qui,
// quindi l'emoji resta nativa — leggibile comunque).
function hydrateWeatherIcons(container) {
  if (!container) return;
  container.querySelectorAll('[data-wicon]:not([data-wicon-done])').forEach(el => {
    el.setAttribute('data-wicon-done', '');
    loadWeatherSvg(el.getAttribute('data-wicon'))
      .then(svg => { el.innerHTML = svg; })
      .catch(() => {
        el.textContent = el.getAttribute('data-wicon-fb') || '';
        el.classList.add('g-wicon-fallback');
      });
  });
}

function weatherIconForDay(day, cls) {
  const c = wmoCondition(day.weather_code ?? null);
  return weatherIconHtml(c.iconName, c.icon, cls);
}

function isNightAt(locMeta, now) {
  if (!locMeta || typeof SunCalc === 'undefined') return false;
  const t = SunCalc.getTimes(now, locMeta.lat, locMeta.lon);
  return now < t.sunrise || now > t.sunset;
}

// ── Formatting ────────────────────────────────────────────────────────────────

function fmtDate(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('it-IT', { weekday: 'short', day: 'numeric', month: 'short' });
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
  const wd = target.toLocaleDateString('it-IT', { weekday: 'long' });
  return wd.charAt(0).toUpperCase() + wd.slice(1);
}

function fmtDayShort(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  const todayMid = new Date(); todayMid.setHours(0, 0, 0, 0);
  const diff = Math.round((new Date(y, m - 1, d) - todayMid) / 86400000);
  if (diff === 0) return 'Oggi';
  if (diff === 1) return 'Domani';
  const wd = new Date(y, m - 1, d).toLocaleDateString('it-IT', { weekday: 'short' });
  return wd.charAt(0).toUpperCase() + wd.slice(1);
}

function fmtDateTime(iso) {
  return new Date(iso).toLocaleString('it-IT', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function fmtTemp(v)   { return v != null ? `${v.toFixed(1)}°` : '—'; }
function fmtPrecip(v) { return v != null ? `${v.toFixed(1)} mm` : '—'; }
function fmtWind(v)   { return v != null ? `${(v * 3.6).toFixed(0)} km/h` : '—'; }

function windDirLabel(deg) {
  if (deg == null) return null;
  const dirs   = ['N','NE','E','SE','S','SO','O','NO'];
  const arrows = ['↓','↙','←','↖','↑','↗','→','↘'];
  const i = Math.round(deg / 45) % 8;
  return { label: dirs[i], arrow: arrows[i] };
}

function fmtSunTime(d) {
  if (!d || isNaN(d)) return '—';
  return d.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
}

function fmtLastRun(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('it-IT', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function aqVerdictForValue(key, value) {
  if (value == null) return null;
  const [lo, hi] = AQ_THRESHOLDS[key] ?? [0, Infinity];
  if (value < lo) return 'verde';
  if (value < hi) return 'giallo';
  return 'rosso';
}

// ── Indicator tooltip ─────────────────────────────────────────────────────────

function showIndicatorTooltip(anchorEl, text) {
  if (!text) return;
  clearTimeout(_tipHideTimer);
  const tip = document.getElementById('indicator-tooltip');
  if (!tip) return;
  tip.textContent = text;
  tip.style.left = '0px';
  tip.style.top  = '0px';
  tip.classList.remove('hidden');
  requestAnimationFrame(() => {
    const r  = anchorEl.getBoundingClientRect();
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let top  = r.bottom + 8;
    if (top + th > window.innerHeight - 8) top = r.top - th - 8;
    let left = r.left + r.width / 2 - tw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
    tip.style.top  = `${top}px`;
    tip.style.left = `${left}px`;
  });
}

function hideIndicatorTooltip() {
  document.getElementById('indicator-tooltip')?.classList.add('hidden');
}

function _wireChipTooltip(el, selector) {
  el.querySelectorAll(selector).forEach(chip => {
    const tip = chip.dataset.tip;
    if (!tip) return;
    chip.addEventListener('mouseenter', () => {
      clearTimeout(_tipHideTimer);
      showIndicatorTooltip(chip, tip);
    });
    chip.addEventListener('mouseleave', () => {
      _tipHideTimer = setTimeout(hideIndicatorTooltip, 150);
    });
    chip.addEventListener('click', e => {
      e.stopPropagation();
      const t = document.getElementById('indicator-tooltip');
      if (!t || t.classList.contains('hidden')) showIndicatorTooltip(chip, tip);
      else hideIndicatorTooltip();
    });
  });
}

function isToday(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  const now = new Date();
  return y === now.getFullYear() && m === (now.getMonth() + 1) && d === now.getDate();
}

function diffDays(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  const todayMid = new Date(); todayMid.setHours(0, 0, 0, 0);
  return Math.round((new Date(y, m - 1, d) - todayMid) / 86400000);
}

// ── Dark mode ─────────────────────────────────────────────────────────────────

function initDarkMode() {
  document.documentElement.dataset.theme = 'dark';
  document.documentElement.classList.add('dark');
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
  nav.innerHTML = LOCATIONS.map(l => {
    const active = l.id === activeLoc;
    return `<button class="g-tab${active ? ' g-tab--active' : ''}" data-loc="${l.id}">${escHtml(l.label)}</button>`;
  }).join('');

  nav.querySelectorAll('[data-loc]').forEach(btn => {
    btn.addEventListener('click', e => {
      if (btn.dataset.loc !== activeLoc) {
        addRipple(btn, e);
        navTo(btn.dataset.loc);
      }
    });
  });
}

function addRipple(btn, e) {
  const rect = btn.getBoundingClientRect();
  const span = document.createElement('span');
  const size = Math.max(rect.width, rect.height) * 2;
  span.style.cssText = `position:absolute;border-radius:50%;background:rgba(59,164,194,0.15);width:${size}px;height:${size}px;left:${e.clientX - rect.left - size/2}px;top:${e.clientY - rect.top - size/2}px;transform:scale(0);pointer-events:none;transition:transform 400ms ease-out,opacity 300ms ease-out;opacity:0.5;overflow:hidden`;
  btn.style.overflow = 'hidden';
  btn.style.position = 'relative';
  btn.appendChild(span);
  requestAnimationFrame(() => { span.style.transform = 'scale(1)'; span.style.opacity = '0'; });
  setTimeout(() => span.remove(), 500);
}

// ── Counter animation ─────────────────────────────────────────────────────────

function animateCounter(el, targetVal, format, duration = 600) {
  const start = performance.now();
  const from  = targetVal - 5;
  const easeOutQuint = t => 1 - Math.pow(1 - t, 5);
  const tick = now => {
    const t = Math.min((now - start) / duration, 1);
    const val = from + (targetVal - from) * easeOutQuint(t);
    el.textContent = format(val);
    if (t < 1) requestAnimationFrame(tick);
    else el.textContent = format(targetVal);
  };
  requestAnimationFrame(tick);
}

// ── Sliding pill for segmented controls ───────────────────────────────────────

function updatePillPosition(switchId, pillId, activeSource) {
  const container = document.getElementById(switchId);
  const pill      = document.getElementById(pillId);
  if (!container || !pill) return;
  const activeBtn = container.querySelector(`[data-src="${activeSource}"]`);
  if (!activeBtn) return;
  pill.style.left  = `${activeBtn.offsetLeft}px`;
  pill.style.width = `${activeBtn.offsetWidth}px`;
}

// ── Header meta ───────────────────────────────────────────────────────────────

function renderHeaderMeta(generatedAt) {
  const el = document.getElementById('header-meta');
  if (!el) return;
  const ageH = (Date.now() - new Date(generatedAt).getTime()) / 3600000;
  const time = new Date(generatedAt).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
  if (ageH >= 6) {
    el.innerHTML = `<span class="g-stale-pill">dati vecchi</span>`;
  } else {
    el.innerHTML = `<span class="tabular-nums font-mono">Aggiornato ${time}</span>`;
  }
}

// ── Hero ──────────────────────────────────────────────────────────────────────

function renderHero(data) {
  const current  = data.current;
  const todayDay = data.days.find(d => isToday(d.target_date));
  const locMeta  = LOCATIONS.find(l => l.id === data.location_id);

  // Location name
  const locNameEl = document.getElementById('hero-loc-name');
  if (locNameEl) {
    locNameEl.innerHTML = `<span class="g-hero__loc-pulse"></span><span>${escHtml(locMeta?.label ?? data.location_id)}</span>`;
  }

  // Temperature
  const mainTemp = current?.temp_c ?? todayDay?.forecasts?.tmax_c?.p50 ?? null;
  const tempEl = document.getElementById('hero-temp');
  if (mainTemp != null) {
    tempEl.textContent = `${mainTemp.toFixed(1)}°`;
    animateCounter(tempEl, mainTemp, v => `${v.toFixed(1)}°`);
  } else {
    tempEl.textContent = '—';
  }

  // Condizione live: weather_code WMO da current (NWP ora più vicina a now) con
  // fallback al codice giornaliero di todayDay. Override pioggia se il SIR segnala
  // precipitazione in corso (evita discrepanza tra dato realtime e codice forecast).
  // Di notte i codici sereno (0/1) usano la luna.
  const isNight = isNightAt(locMeta, new Date());
  const heroCode = (() => {
    // Override: pioggia realtime osservata → forza codice pioggia debole (61)
    if (current?.precip_mm != null && current.precip_mm > 0.2) return 61;
    // Sorgente primaria: weather_code dal current NWP (ora corrente)
    if (current?.weather_code != null) return current.weather_code;
    // Fallback: codice giornaliero del giorno corrente
    return todayDay?.weather_code ?? null;
  })();
  const cond = wmoCondition(heroCode, isNight);

  const iconEl = document.getElementById('hero-icon');
  if (iconEl) {
    iconEl.innerHTML = weatherIconHtml(cond.iconName, cond.icon, 'g-wicon--hero');
    hydrateWeatherIcons(iconEl);
  }

  const condEl = document.getElementById('hero-condition');
  const condText = cond.text;
  if (condEl) {
    const meta = [
      current?.feels_like_c != null ? `percepita ${current.feels_like_c.toFixed(1)}°` : null,
      current?.ts ? `dati SIR ${new Date(current.ts).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })}` : null,
    ].filter(Boolean).join(' · ');
    condEl.innerHTML = `<span>${escHtml(condText)}</span>${meta ? `<span class="g-hero__condition-meta">${escHtml(meta)}</span>` : ''}`;
  }

  // Stats inline
  renderHeroStatsInline(current);

  // DLE indicators (today)
  renderHeroIndicators(todayDay);

  const heroCard = document.getElementById('hero-card');
  heroCard.classList.remove('hidden');
  heroCard.style.removeProperty('display');
  heroCard.classList.remove('anim-fade-up');
  void heroCard.offsetWidth;
  heroCard.classList.add('anim-fade-up');
  twemoji.parse(heroCard, TWEMOJI_OPTS);
}

function renderHeroStatsInline(current) {
  const el = document.getElementById('hero-stats');
  if (!el) return;
  const windDir = windDirLabel(current?.wind_dir_deg);
  const stats = [
    { icon: '💨', lbl: 'Vento',     val: current?.wind_speed_ms != null ? `${fmtWind(current.wind_speed_ms)}${windDir ? ` ${windDir.label}` : ''}` : '—' },
    { icon: '💧', lbl: 'Umidità',   val: current?.humidity_pct  != null ? `${current.humidity_pct.toFixed(0)}%` : '—' },
    { icon: '🌧️', lbl: 'Pioggia',   val: current?.precip_mm     != null ? `${current.precip_mm.toFixed(1)} mm` : '—' },
    { icon: '🌡️', lbl: 'Pressione', val: current?.pressure_hpa  != null ? `${current.pressure_hpa.toFixed(0)} hPa` : '—' },
  ];
  el.innerHTML = stats.map(s => `
    <div class="g-hero__stat">
      <span class="g-hero__stat-icon">${s.icon}</span>
      <span class="g-hero__stat-lbl">${s.lbl}</span>
      <span class="g-hero__stat-val">${escHtml(s.val)}</span>
    </div>`).join('');
  twemoji.parse(el, TWEMOJI_OPTS);
}

function renderHeroIndicators(todayDay) {
  const el = document.getElementById('hero-indicators');
  if (!el) return;
  if (!todayDay) { el.innerHTML = ''; return; }

  el.innerHTML = Object.entries(todayDay.indicators).map(([id, ind], i) => {
    const meta    = INDICATOR_META[id] ?? { label: id, icon: '?' };
    const verdict = ind.verdict ?? 'giallo';
    const verdCap = verdict.charAt(0).toUpperCase() + verdict.slice(1);
    const tip     = escHtml(ind.rule_text || ind.rule_matched || '');
    return `<button class="g-pill g-pill--${verdict}"${tip ? ` data-hero-chip data-tip="${tip}"` : ' data-hero-chip'}
                    style="animation:fade-up 0.35s ease-out ${i * 50}ms both">
      <span class="g-pill__icon">${meta.icon}</span>
      <span class="g-pill__label">${meta.label}</span>
      <span class="g-pill__verdict">${verdCap}</span>
    </button>`;
  }).join('');

  _wireChipTooltip(el, '[data-hero-chip]');
  twemoji.parse(el, TWEMOJI_OPTS);
}

// ── AQ (targets #aq-section / #aq-grid) ──────────────────────────────────────

function renderAQ(aq) {
  const section = document.getElementById('aq-section');
  const grid    = document.getElementById('aq-grid');
  if (!section || !grid) return;

  const items = [
    { key: 'pm10',    label: 'PM10',  value: aq?.pm10_ugm3    ?? null, unit: 'µg/m³', dec: 0 },
    { key: 'pm25',    label: 'PM2.5', value: aq?.pm25_ugm3    ?? null, unit: 'µg/m³', dec: 0 },
    { key: 'no2',     label: 'NO₂',   value: aq?.no2_ugm3     ?? null, unit: 'µg/m³', dec: 0 },
    { key: 'o3',      label: 'O₃',    value: aq?.o3_ugm3      ?? null, unit: 'µg/m³', dec: 0 },
    { key: 'co',      label: 'CO',    value: aq?.co_mgm3      ?? null, unit: 'mg/m³', dec: 1 },
    { key: 'benzene', label: 'C₆H₆', value: aq?.benzene_ugm3 ?? null, unit: 'µg/m³', dec: 1 },
    { key: 'so2',     label: 'SO₂',   value: aq?.so2_ugm3     ?? null, unit: 'µg/m³', dec: 0 },
  ];

  grid.innerHTML = items.map(it => {
    const display = it.value != null ? it.value.toFixed(it.dec) : '—';
    const verdict = aqVerdictForValue(it.key, it.value);
    const valCls  = verdict ? `g-aq-cell__val--${verdict}` : 'g-aq-cell__val--null';
    return `<div class="g-aq-cell">
      <span class="g-aq-cell__label">${it.label}</span>
      <span class="g-aq-cell__val ${valCls}">${display}</span>
      <span class="g-aq-cell__unit">${it.unit}</span>
    </div>`;
  }).join('');
  section.classList.remove('hidden');
  section.style.removeProperty('display');
}

// ── Sun/moon (targets #hero-sun inside coverage bar) ─────────────────────────

function renderHeroSun(locMeta, now) {
  const el = document.getElementById('hero-sun');
  if (!el) return;
  if (!locMeta || typeof SunCalc === 'undefined') { el.innerHTML = ''; return; }
  const sunTimes  = SunCalc.getTimes(now, locMeta.lat, locMeta.lon);
  const moonPhase = SunCalc.getMoonIllumination(now).phase;
  const idx       = Math.round(moonPhase * 8) % 8;
  const moonEmoji = ['🌑','🌒','🌓','🌔','🌕','🌖','🌗','🌘'][idx];
  const moonLabel = ['Luna nuova','Luna crescente','Primo quarto','Gibbosa crescente','Luna piena','Gibbosa calante','Ultimo quarto','Luna calante'][idx];

  el.innerHTML = `
    <div class="g-sun-pill" title="Alba"><span>🌅</span><span>${fmtSunTime(sunTimes.sunrise)}</span></div>
    <div class="g-sun-pill" title="Tramonto"><span>🌇</span><span>${fmtSunTime(sunTimes.sunset)}</span></div>
    <div class="g-sun-pill" title="${escHtml(moonLabel)}"><span>${moonEmoji}</span><span>${escHtml(moonLabel)}</span></div>`;
  twemoji.parse(el, TWEMOJI_OPTS);
}

// ── CI bar with label ─────────────────────────────────────────────────────────

function ciBar(fc, unit) {
  if (!fc) return '';
  const { p50, mean, ci80_lo, ci80_hi, ci90_lo, ci90_hi } = fc;
  const anchor = mean ?? p50;
  if (anchor == null || ci90_lo == null || ci90_hi == null) return '';
  const range = ci90_hi - ci90_lo;
  if (range <= 0) return '';
  const pct = v => Math.max(0, Math.min(100, ((v - ci90_lo) / range) * 100));
  const p80l    = (ci80_lo != null ? pct(ci80_lo) : 0).toFixed(2);
  const p80w    = (ci80_lo != null && ci80_hi != null ? pct(ci80_hi) - pct(ci80_lo) : 100).toFixed(2);
  const anchorPos = pct(anchor).toFixed(2);
  return `
    <div class="g-ci-bar">
      <div class="g-ci-bar__track">
        <div class="g-ci-bar__range-90"></div>
        <div class="g-ci-bar__range-80" style="left:${p80l}%;width:${p80w}%"></div>
        <div class="g-ci-bar__median" style="left:${anchorPos}%"></div>
      </div>
      <div class="g-ci-bar__labels">
        <span class="g-ci-bar__lo">${ci90_lo.toFixed(1)}${unit}</span>
        <span class="g-ci-bar__note">CI 80%</span>
        <span class="g-ci-bar__hi">${ci90_hi.toFixed(1)}${unit}</span>
      </div>
    </div>`;
}

// ── Day strip ─────────────────────────────────────────────────────────────────

function renderDayStrip(days, activeDayIdx) {
  const el = document.getElementById('day-strip');
  el.innerHTML = days.map((day, idx) => {
    const { target_date, forecasts: fc, indicators } = day;
    const active = idx === activeDayIdx;
    const diff   = diffDays(target_date);
    const icon   = weatherIconForDay(day, 'g-wicon--strip');
    const tmax   = fmtTemp(fc.tmax_c?.p50);
    const tmin   = fmtTemp(fc.tmin_c?.p50);
    const precipVal = (fc.precip_mm?.mean ?? fc.precip_mm?.p50) ?? 0;
    const precipPct = Math.min(100, (precipVal / 20) * 100).toFixed(1);
    const precipDisplay = precipVal > 0.05 ? `${precipVal.toFixed(1)} mm` : '—';
    const dayLabel  = fmtDayShort(target_date);
    const isToday_  = diff === 0;

    const labelCls = isToday_ ? 'g-strip__label g-strip__label--today today-badge-anim' : 'g-strip__label';

    const dotsHtml = indicators && Object.keys(indicators).length
      ? `<div class="g-strip__dots">${Object.entries(indicators).map(([, ind]) => {
          const v = ind.verdict ?? 'unknown';
          return `<span class="g-strip__dot g-strip__dot--${v}"></span>`;
        }).join('')}</div>`
      : '';

    return `<button class="g-strip__card${active ? ' g-strip__card--active' : ''}" data-idx="${idx}" aria-pressed="${active}">
      <span class="${labelCls}">${dayLabel}</span>
      <span class="g-strip__icon">${icon}</span>
      <div class="g-strip__temps">
        <span class="g-strip__tmax">${tmax}</span>
        <span class="g-strip__tmin">${tmin}</span>
      </div>
      <div class="g-strip__precip-bar">
        <span class="g-strip__precip-fill" style="width:${precipPct}%"></span>
      </div>
      <span class="g-strip__precip-val">${precipDisplay}</span>
      ${dotsHtml}
    </button>`;
  }).join('');

  el.querySelectorAll('[data-idx]').forEach(card => {
    card.addEventListener('click', () => {
      const idx = parseInt(card.dataset.idx, 10);
      if (idx === selectedDayIdx) return;
      selectedDayIdx = idx;
      renderDayStrip(currentData.days, selectedDayIdx);
      renderDayDetail(currentData.days[selectedDayIdx]);
      card.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
      const td = currentData.days[selectedDayIdx]?.target_date;
      if (td) updateChartModel(currentData, selectedModel, td);
    });
  });

  twemoji.parse(el, TWEMOJI_OPTS);
  hydrateWeatherIcons(el);
}

// ── Day detail ────────────────────────────────────────────────────────────────

function renderDayDetail(day) {
  if (!day) return;
  const { forecasts: fc, indicators, target_date, lead_time_h } = day;
  const icon = weatherIconForDay(day, 'g-wicon--detail');

  // Head
  const iconEl = document.getElementById('detail-icon');
  if (iconEl) { iconEl.innerHTML = icon; hydrateWeatherIcons(iconEl); }
  const titleEl = document.getElementById('detail-title');
  if (titleEl) titleEl.textContent = fmtDayLabel(target_date);
  const dateEl = document.getElementById('detail-date');
  if (dateEl) dateEl.textContent = fmtDate(target_date);
  const leadEl = document.getElementById('detail-lead');
  if (leadEl) leadEl.textContent = `+${lead_time_h}h`;
  const emessaEl = document.getElementById('detail-emessa');
  if (emessaEl && currentData?.generated_at) {
    const t = new Date(currentData.generated_at).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
    emessaEl.textContent = `prev. emessa ${t}`;
  }

  // Metrics
  const tmaxEl = document.getElementById('detail-tmax');
  if (tmaxEl) tmaxEl.textContent = fmtTemp(fc.tmax_c?.p50);

  const tminEl = document.getElementById('detail-tmin');
  if (tminEl) tminEl.textContent = fmtTemp(fc.tmin_c?.p50);

  const precipVal = fc.precip_mm?.mean ?? fc.precip_mm?.p50;
  const precipEl = document.getElementById('detail-precip-val');
  if (precipEl) {
    precipEl.textContent = precipVal != null && precipVal > 0.05
      ? `${precipVal.toFixed(1)} mm`
      : '—';
  }

  // Intraday correction D+0
  const intraday = day.intraday;
  const intradayEl = document.getElementById('detail-intraday');
  if (intraday) {
    if (intraday.tmin_corrected_c != null && tminEl)
      tminEl.textContent = fmtTemp(intraday.tmin_corrected_c);
    if (intraday.tmax_corrected_c != null && tmaxEl)
      tmaxEl.textContent = fmtTemp(intraday.tmax_corrected_c);
    if (intraday.precip_remaining_mm != null && precipEl) {
      const rem = intraday.precip_remaining_mm;
      precipEl.textContent = rem > 0.05 ? `${rem.toFixed(1)} mm` : '—';
    }
    if (intradayEl) {
      const obs = intraday.precip_observed_mm;
      const rem = intraday.precip_remaining_mm;
      if (obs != null) {
        const obsStr = obs > 0.05 ? `🌧️ ${obs.toFixed(1)} mm osservati` : 'Nessuna pioggia osservata';
        const remStr = rem != null && rem > 0.05 ? ` · ~${rem.toFixed(1)} mm attesi` : '';
        intradayEl.innerHTML = `<span class="g-intraday-badge">${obsStr}${remStr}</span>`;
        twemoji.parse(intradayEl, TWEMOJI_OPTS);
      } else {
        intradayEl.innerHTML = '';
      }
    }
  } else if (intradayEl) {
    intradayEl.innerHTML = '';
  }

  // CI bars
  const ciTmax = document.getElementById('detail-ci-tmax');
  if (ciTmax) ciTmax.innerHTML = ciBar(fc.tmax_c, '°');
  const ciTmin = document.getElementById('detail-ci-tmin');
  if (ciTmin) ciTmin.innerHTML = ciBar(fc.tmin_c, '°');
  const ciPrecip = document.getElementById('detail-ci-precip');
  const precipCi = (intraday?.precip_corrected) ?? fc.precip_mm;
  if (ciPrecip) ciPrecip.innerHTML = ciBar(precipCi, ' mm');

  // Indicators + NWP
  renderIndicatorChips(indicators);
  renderNwpList(day);

  // Re-animate
  const detailEl = document.getElementById('day-detail');
  detailEl.classList.remove('anim-fade-up');
  void detailEl.offsetWidth;
  detailEl.classList.add('anim-fade-up');

  twemoji.parse(detailEl, TWEMOJI_OPTS);
}

function renderIndicatorChips(indicators) {
  const el = document.getElementById('detail-indicators');
  if (!el) return;
  el.innerHTML = Object.entries(indicators).map(([id, ind], i) => {
    const meta    = INDICATOR_META[id] ?? { label: id, icon: '?' };
    const verdict = ind.verdict ?? 'giallo';
    const verdCap = verdict.charAt(0).toUpperCase() + verdict.slice(1);
    const tip     = escHtml(ind.rule_text || ind.rule_matched || '');
    return `<button class="g-pill g-pill--${verdict}"${tip ? ` data-detail-chip data-tip="${tip}"` : ' data-detail-chip'}
                    style="animation:fade-up 0.35s ease-out ${i * 50}ms both">
      <span class="g-pill__icon">${meta.icon}</span>
      <span class="g-pill__label">${meta.label}</span>
      <span class="g-pill__verdict">${verdCap}</span>
    </button>`;
  }).join('');

  _wireChipTooltip(el, '[data-detail-chip]');
  twemoji.parse(el, TWEMOJI_OPTS);
}

// ── NWP table with deltas ─────────────────────────────────────────────────────

function computeNwpDelta(modelVal, guazzaVal, kind) {
  if (modelVal == null || guazzaVal == null) return { text: '', cls: '' };
  const delta = modelVal - guazzaVal;
  const absD  = Math.abs(delta);
  const sign  = delta >= 0 ? '+' : '';
  const fmt   = delta.toFixed(1);
  if (kind === 'temp') {
    return delta > 0
      ? { text: `${sign}${fmt}°`, cls: 'g-delta--warm' }
      : { text: `${sign}${fmt}°`, cls: 'g-delta--cold' };
  }
  // precip
  return delta > 0
    ? { text: `${sign}${fmt}`, cls: 'g-delta--wet' }
    : { text: `${sign}${fmt}`, cls: 'g-delta--dry' };
}

function nwpDailyStats(source, targetDate) {
  const mdl = (currentData?.nwp_models_hourly || []).find(x => x.source === source);
  if (!mdl) return { hum: null, wind: null };
  const [y, mo, d] = targetDate.split('-').map(Number);
  const dayStart = new Date(y, mo - 1, d, 0, 0, 0);
  const dayEnd   = new Date(y, mo - 1, d, 23, 59, 59);
  const pts = (mdl.data || []).filter(pt => {
    const ts = new Date(pt.ts);
    return ts >= dayStart && ts <= dayEnd;
  });
  if (!pts.length) return { hum: null, wind: null };
  const hums  = pts.map(p => p.humidity_pct).filter(v => v != null);
  const winds = pts.map(p => p.wind_speed_ms).filter(v => v != null);
  return {
    hum:  hums.length  ? hums.reduce((a, b) => a + b, 0) / hums.length : null,
    wind: winds.length ? Math.max(...winds) : null,
  };
}

function renderNwpList(day) {
  const el  = document.getElementById('nwp-list');
  const nwp = day.nwp_comparison;
  const fc  = day.forecasts;
  if (!nwp || !nwp.length) { el.innerHTML = ''; return; }

  const gTmin = fc.tmin_c?.p50;
  const gTmax = fc.tmax_c?.p50;
  const gPrec = fc.precip_mm?.mean ?? fc.precip_mm?.p50;

  const thead = `<thead><tr>
    <th>Modello</th>
    <th>T min</th>
    <th>T max</th>
    <th>Precip TOT</th>
    <th>Um. Media</th>
    <th>Vento Max</th>
    <th>Run</th>
  </tr></thead>`;

  const gHourly = day.hourly || [];
  const gHums   = gHourly.map(h => h.humidity_pct).filter(v => v != null);
  const gWinds  = gHourly.map(h => h.wind_speed_ms).filter(v => v != null);
  const gHum    = gHums.length  ? gHums.reduce((a, b) => a + b, 0) / gHums.length : null;
  const gWind   = gWinds.length ? Math.max(...gWinds) : null;

  const guazzaRow = `<tr class="g-nwp__row g-nwp__row--guazza" data-source="guazza">
    <td><span class="g-nwp__name g-nwp__name--guazza">★ Guazza ML</span></td>
    <td>${gTmin != null ? gTmin.toFixed(1)+'°' : '—'}</td>
    <td>${gTmax != null ? gTmax.toFixed(1)+'°' : '—'}</td>
    <td>${gPrec != null ? gPrec.toFixed(1)+' mm' : '—'}</td>
    <td>${gHum  != null ? gHum.toFixed(0)+'%'                   : '—'}</td>
    <td>${gWind != null ? (gWind * 3.6).toFixed(0)+' km/h'      : '—'}</td>
    <td style="color:var(--text-3);font-size:10px">${currentData?.generated_at
      ? new Date(currentData.generated_at).toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'})
      : '—'}</td>
  </tr>`;

  const nwpRows = nwp.map(m => {
    const dTmin = computeNwpDelta(m.tmin_c, gTmin, 'temp');
    const dTmax = computeNwpDelta(m.tmax_c, gTmax, 'temp');
    const dPrec = computeNwpDelta(m.precip_mm, gPrec, 'precip');
    const st    = nwpDailyStats(m.source, day.target_date);
    const hum   = st.hum  != null ? st.hum.toFixed(0)+'%'                  : '—';
    const wind  = st.wind != null ? (st.wind * 3.6).toFixed(0)+' km/h'     : '—';
    return `<tr class="g-nwp__row" data-source="${escHtml(m.source)}">
      <td>
        <span class="g-nwp__name">${escHtml(m.label)}</span>
        <span class="g-nwp__run">${fmtLastRun(m.last_run)}</span>
      </td>
      <td class="g-nwp__val">${m.tmin_c != null ? m.tmin_c.toFixed(1)+'°' : '—'}${dTmin.text ? `<span class="g-delta ${dTmin.cls}">${dTmin.text}</span>` : ''}</td>
      <td class="g-nwp__val">${m.tmax_c != null ? m.tmax_c.toFixed(1)+'°' : '—'}${dTmax.text ? `<span class="g-delta ${dTmax.cls}">${dTmax.text}</span>` : ''}</td>
      <td class="g-nwp__val">${m.precip_mm != null ? m.precip_mm.toFixed(1)+' mm' : '—'}${dPrec.text ? `<span class="g-delta ${dPrec.cls}">${dPrec.text}</span>` : ''}</td>
      <td class="g-nwp__val">${hum}</td>
      <td class="g-nwp__val">${wind}</td>
      <td style="color:var(--text-3);font-size:10px">${m.last_run ? new Date(m.last_run).toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'}) : '—'}</td>
    </tr>`;
  }).join('');

  el.innerHTML = `<table class="g-nwp__table">${thead}<tbody>${guazzaRow}${nwpRows}</tbody></table>`;
  highlightNwpRow(selectedModel);
}

function highlightNwpRow(source) {
  document.querySelectorAll('#nwp-list .g-nwp__row[data-source]').forEach(row => {
    row.classList.toggle('g-nwp__row--active', row.dataset.source === source);
  });
}

// ── Coverage ──────────────────────────────────────────────────────────────────

function renderCoverage(cov) {
  const el = document.getElementById('coverage-numbers');
  if (!el) return;
  if (!cov || Object.values(cov).every(v => v === null)) {
    el.innerHTML = `<span class="g-coverage__calibrating">Calibrazione in corso — copertura CI non ancora disponibile (primi 30gg)</span>`;
  } else {
    const items = [
      ['Tmin CI80', cov.tmin_ci80], ['Tmin CI90', cov.tmin_ci90],
      ['Tmax CI80', cov.tmax_ci80], ['Tmax CI90', cov.tmax_ci90],
      ['Precip CI80', cov.precip_ci80], ['Precip CI90', cov.precip_ci90],
    ].filter(([, v]) => v !== null)
     .map(([k, v]) => `<span>${k}: <strong>${(v * 100).toFixed(0)}%</strong></span>`)
     .join('');
    el.innerHTML = `<div class="g-coverage__numbers">${items}</div>`;
  }
  showEl('coverage-bar');
}

// ── Model switches (segmented control) ───────────────────────────────────────

function setActiveModel(src) {
  selectedModel = src;

  // sync entrambi i switch UI
  ['model-switch', 'weekly-model-switch'].forEach(switchId => {
    const pillId = switchId === 'model-switch' ? 'model-pill' : 'weekly-model-pill';
    const container = document.getElementById(switchId);
    if (!container) return;
    container.querySelectorAll('[data-src]').forEach(b => {
      b.className = `g-model-switch__btn${b.dataset.src === src ? ' g-model-switch__btn--active' : ''}`;
    });
    updatePillPosition(switchId, pillId, src);
  });

  const td = currentData?.days[selectedDayIdx]?.target_date;
  if (td) updateChartModel(currentData, selectedModel, td);
  updateWeeklyChart(currentData, selectedModel);
  highlightNwpRow(src);
}

function initModelSwitch(data) {
  _buildModelSwitch(data, 'model-switch', 'model-pill', selectedModel, setActiveModel);
}

function initWeeklyModelSwitch(data) {
  _buildModelSwitch(data, 'weekly-model-switch', 'weekly-model-pill', selectedModel, setActiveModel);
}

function _buildModelSwitch(data, switchId, pillId, activeSource, onChange) {
  const container = document.getElementById(switchId);
  if (!container) return;
  const models = [{ source: 'guazza', label: '★ Guazza ML' }];
  (data.nwp_models_hourly || []).forEach(m => models.push({ source: m.source, label: m.label }));

  const pillEl = document.getElementById(pillId);
  container.innerHTML = '';
  if (pillEl) {
    container.appendChild(pillEl);
  } else {
    const s = document.createElement('span');
    s.id = pillId;
    s.className = 'g-model-switch__pill';
    container.appendChild(s);
  }

  models.forEach(m => {
    const btn = document.createElement('button');
    const active = m.source === activeSource;
    btn.className = `g-model-switch__btn${active ? ' g-model-switch__btn--active' : ''}`;
    btn.dataset.src = m.source;
    btn.textContent = m.label;
    container.appendChild(btn);
    btn.addEventListener('click', () => {
      container.querySelectorAll('[data-src]').forEach(b => {
        const isActive = b.dataset.src === m.source;
        b.className = `g-model-switch__btn${isActive ? ' g-model-switch__btn--active' : ''}`;
      });
      updatePillPosition(switchId, pillId, m.source);
      onChange(m.source);
    });
  });

  requestAnimationFrame(() => updatePillPosition(switchId, pillId, activeSource));
}

// ── Chart ─────────────────────────────────────────────────────────────────────

function chartPalette() {
  return {
    grid:  'rgba(148,163,184,0.08)',
    label: '#94A3B8',
    temp:  '#F97316',
    hum:   '#0EA5E9',
    wind:  '#14B8A6',
  };
}

function buildChartPoints(data, model, targetDate) {
  const [y, m, d] = targetDate.split('-').map(Number);
  const dayStart = new Date(y, m - 1, d, 0, 0, 0);
  const dayEnd   = new Date(y, m - 1, d, 23, 59, 59);
  const points   = [];
  if (model === 'guazza') {
    (data.days.find(day => day.target_date === targetDate)?.hourly || []).forEach(h => {
      points.push({ ts: new Date(y, m - 1, d, h.hour, 0, 0),
                    temp_c: h.temp_c, humidity_pct: h.humidity_pct,
                    precip_mm: h.precip_mm, precip_prob: h.precip_prob,
                    wind_speed_ms: h.wind_speed_ms });
    });
  } else {
    const mdl = (data.nwp_models_hourly || []).find(x => x.source === model);
    (mdl?.data || []).forEach(pt => {
      const ts = new Date(pt.ts);
      if (ts >= dayStart && ts <= dayEnd)
        points.push({ ts, temp_c: pt.temp_c, humidity_pct: pt.humidity_pct,
                      precip_mm: pt.precip_mm, precip_prob: null, wind_speed_ms: pt.wind_speed_ms });
    });
  }
  return points.sort((a, b) => a.ts - b.ts);
}

function buildWeeklyPoints(data, model) {
  if (model === 'guazza') {
    const points = [];
    data.days.forEach(day => {
      const [y, m, d] = day.target_date.split('-').map(Number);
      (day.hourly || []).forEach(h => points.push({
        ts: new Date(y, m - 1, d, h.hour, 0, 0),
        temp_c: h.temp_c, humidity_pct: h.humidity_pct,
        precip_mm: h.precip_mm, precip_prob: h.precip_prob, wind_speed_ms: h.wind_speed_ms,
      }));
    });
    return points.sort((a, b) => a.ts - b.ts);
  }
  const mdl = (data.nwp_models_hourly || []).find(x => x.source === model);
  return (mdl?.data || []).map(pt => ({
    ts: new Date(pt.ts),
    temp_c: pt.temp_c, humidity_pct: pt.humidity_pct,
    precip_mm: pt.precip_mm, precip_prob: null, wind_speed_ms: pt.wind_speed_ms,
  })).sort((a, b) => a.ts - b.ts);
}

function precipDatasets(points) {
  return {
    data: points.map(pt => ({ x: pt.ts, y: (pt.precip_mm ?? 0) < 0.05 ? 0 : (pt.precip_mm ?? 0) })),
    bg:   points.map(pt => {
      const y = pt.precip_mm ?? 0;
      if (y < 0.05) return 'rgba(37,99,235,0.06)';
      const prob = pt.precip_prob ?? 0.8;
      return `rgba(37,99,235,${(0.5 + prob * 0.45).toFixed(2)})`;
    }),
  };
}

const crosshairPlugin = {
  id: 'crosshair',
  afterDraw(chart) {
    const active = chart.tooltip?._active ?? [];
    if (!active.length) return;
    const ctx = chart.ctx;
    const x   = active[0].element.x;
    const { top, bottom } = chart.chartArea;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.lineWidth   = 1;
    ctx.strokeStyle = 'rgba(59,164,194,0.20)';
    ctx.setLineDash([]);
    ctx.stroke();
    const tempPt = active.find(a => a.datasetIndex === 0);
    if (tempPt) {
      ctx.beginPath();
      ctx.arc(tempPt.element.x, tempPt.element.y, 12, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(249,115,22,0.08)';
      ctx.fill();
    }
    ctx.restore();
  },
};

function _makeTooltipHandler(elId) {
  return function({ chart, tooltip }) {
    const el = document.getElementById(elId);
    if (!el) return;
    if (!tooltip.opacity) { el.style.opacity = '0'; return; }
    const items = tooltip.dataPoints ?? [];
    if (!items.length) return;

    const ts   = new Date(items[0].raw.x);
    const date = ts.toLocaleDateString('it-IT', { weekday: 'short', day: 'numeric', month: 'short' });
    const time = ts.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
    const temp = items.find(i => i.datasetIndex === 0);
    const hum  = items.find(i => i.datasetIndex === 1);
    const prec = items.find(i => i.datasetIndex === 2);
    const wind = items.find(i => i.datasetIndex === 3);

    const row = (dotColor, label, val) =>
      `<div style="display:flex;align-items:center;justify-content:space-between;gap:20px;padding:3px 0">
         <span style="display:flex;align-items:center;gap:6px;font-size:11px;font-family:var(--ff-mono);color:var(--text-3)"><span style="width:6px;height:6px;border-radius:50%;background:${dotColor};flex-shrink:0;display:inline-block"></span>${label}</span>
         <span style="font-size:11px;font-weight:700;font-variant-numeric:tabular-nums;font-family:var(--ff-mono);color:var(--text-1)">${val}</span>
       </div>`;

    el.innerHTML = `
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--border-1)">
        <span style="font-size:10px;font-family:var(--ff-mono);text-transform:capitalize;color:var(--text-3)">${date}</span>
        <span style="font-size:13px;font-weight:700;font-family:var(--ff-mono);font-variant-numeric:tabular-nums;color:var(--text-1)">${time}</span>
      </div>
      ${temp ? row('#F97316', 'Temp',    `${temp.raw.y.toFixed(1)}°C`)   : ''}
      ${hum  ? row('#0EA5E9', 'Umidità', `${hum.raw.y.toFixed(0)}%`)     : ''}
      ${prec && prec.raw.y > 0.05 ? row('#2563EB', 'Precip', `${prec.raw.y.toFixed(1)} mm`) : ''}
      ${wind ? row('#14B8A6', 'Vento',   `${wind.raw.y.toFixed(0)} km/h`) : ''}`;

    const cRect = chart.canvas.parentElement.getBoundingClientRect();
    el.style.opacity = '1';
    el.style.left    = `${Math.max(0, Math.min(tooltip.caretX - 80, cRect.width - 175))}px`;
    el.style.top     = `${Math.max(4, tooltip.caretY - 130)}px`;
  };
}

const externalTooltipHandler       = _makeTooltipHandler('chart-tooltip');
const externalTooltipHandlerWeekly = _makeTooltipHandler('chart-weekly-tooltip');


function _buildChartDatasets(canvas, points, p) {
  const { data: precipData, bg: precipBg } = precipDatasets(points);
  return [
    {
      type: 'line', label: 'Temperatura (°C)',
      data: points.filter(pt => pt.temp_c != null).map(pt => ({ x: pt.ts, y: pt.temp_c })),
      borderColor: p.temp, backgroundColor: 'transparent', fill: false,
      borderWidth: 2.5, pointRadius: 0, pointHoverRadius: 6,
      pointHoverBackgroundColor: '#fff', pointHoverBorderColor: p.temp, pointHoverBorderWidth: 3,
      yAxisID: 'yTemp', tension: 0.4, order: 1,
    },
    {
      type: 'line', label: 'Umidità (%)',
      data: points.filter(pt => pt.humidity_pct != null).map(pt => ({ x: pt.ts, y: pt.humidity_pct })),
      borderColor: p.hum, backgroundColor: 'transparent',
      borderWidth: 2, borderDash: [6, 4], pointRadius: 0, pointHoverRadius: 6,
      pointHoverBackgroundColor: '#fff', pointHoverBorderColor: p.hum, pointHoverBorderWidth: 3,
      yAxisID: 'yHum', tension: 0.4, order: 2,
    },
    {
      type: 'bar', label: 'Precipitazioni (mm)',
      data: precipData, backgroundColor: precipBg,
      yAxisID: 'yTemp', barPercentage: 0.9, categoryPercentage: 1.0,
      borderRadius: 2, order: 3,
    },
    {
      type: 'line', label: 'Vento (km/h)',
      data: points.filter(pt => pt.wind_speed_ms != null).map(pt => ({ x: pt.ts, y: pt.wind_speed_ms * 3.6 })),
      borderColor: p.wind, backgroundColor: 'transparent',
      borderWidth: 1.5, borderDash: [3, 3], pointRadius: 0, pointHoverRadius: 6,
      pointHoverBackgroundColor: '#fff', pointHoverBorderColor: p.wind, pointHoverBorderWidth: 3,
      yAxisID: 'yWind', tension: 0.4, order: 4,
    },
  ];
}

function _baseChartOptions(p, xMin, xMax, unit) {
  return {
    responsive: true, maintainAspectRatio: false,
    hover: { mode: 'index', intersect: false },
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: { enabled: false, external: externalTooltipHandler },
    },
    scales: {
      x: {
        type: 'time', min: xMin, max: xMax,
        time: { unit, displayFormats: { hour: 'HH', day: 'dd/MM' } },
        grid: { color: p.grid, borderColor: 'transparent' },
        ticks: { color: p.label, maxTicksLimit: 13, font: { size: 11 } },
      },
      yTemp: {
        position: 'left',
        grid: { color: p.grid, borderColor: 'transparent' },
        ticks: { color: p.temp, callback: v => `${v}°`, font: { size: 11 } },
      },
      yHum: {
        position: 'right', min: 0, max: 100,
        grid: { drawOnChartArea: false },
        ticks: { color: p.hum, callback: v => `${v}`, font: { size: 11 } },
      },
      yWind: {
        position: 'right', min: 0,
        grid: { drawOnChartArea: false },
        ticks: { color: p.wind, callback: v => `${v}`, font: { size: 11 } },
      },
    },
  };
}

function initChart(data, model, targetDate) {
  const canvas = document.getElementById('meteo-chart');
  if (!canvas || !targetDate) return;
  if (meteoChart) { meteoChart.destroy(); meteoChart = null; }
  const [y, m, d] = targetDate.split('-').map(Number);
  const xMin = new Date(y, m - 1, d, 0, 0, 0);
  const xMax = new Date(y, m - 1, d, 23, 0, 0);
  const points = buildChartPoints(data, model, targetDate);
  const p = chartPalette();
  meteoChart = new Chart(canvas.getContext('2d'), {
    plugins: [crosshairPlugin],
    data: { datasets: _buildChartDatasets(canvas, points, p) },
    options: _baseChartOptions(p, xMin, xMax, 'hour'),
  });
}

function updateChartModel(data, model, targetDate) {
  if (!meteoChart || !targetDate) { initChart(data, model, targetDate); return; }
  const canvas = document.getElementById('meteo-chart');
  const [y, m, d] = targetDate.split('-').map(Number);
  meteoChart.options.scales.x.min = new Date(y, m - 1, d, 0, 0, 0);
  meteoChart.options.scales.x.max = new Date(y, m - 1, d, 23, 0, 0);
  const points = buildChartPoints(data, model, targetDate);
  const p = chartPalette();
  const ds = _buildChartDatasets(canvas, points, p);
  meteoChart.data.datasets.forEach((d, i) => { d.data = ds[i].data; if (i === 2) d.backgroundColor = ds[i].backgroundColor; });
  meteoChart.update();
}

function initWeeklyChart(data, model) {
  const canvas = document.getElementById('multiday-chart');
  if (!canvas || !data.days.length) return;
  if (multiDayChart) { multiDayChart.destroy(); multiDayChart = null; }
  const first = data.days[0].target_date.split('-').map(Number);
  const last  = data.days[data.days.length - 1].target_date.split('-').map(Number);
  const xMin  = new Date(first[0], first[1] - 1, first[2], 0, 0, 0);
  const xMax  = new Date(last[0],  last[1] - 1,  last[2],  23, 0, 0);
  const points = buildWeeklyPoints(data, model);
  const p = chartPalette();
  const opts = _baseChartOptions(p, xMin, xMax, 'day');
  opts.plugins.tooltip = { enabled: false, external: externalTooltipHandlerWeekly };
  multiDayChart = new Chart(canvas.getContext('2d'), {
    plugins: [crosshairPlugin],
    data: { datasets: _buildChartDatasets(canvas, points, p) },
    options: opts,
  });
}

function updateWeeklyChart(data, model) {
  if (!multiDayChart) { initWeeklyChart(data, model); return; }
  const canvas = document.getElementById('multiday-chart');
  const points = buildWeeklyPoints(data, model);
  const p = chartPalette();
  const ds = _buildChartDatasets(canvas, points, p);
  multiDayChart.data.datasets.forEach((d, i) => { d.data = ds[i].data; if (i === 2) d.backgroundColor = ds[i].backgroundColor; });
  multiDayChart.update();
}

// ── Radar ─────────────────────────────────────────────────────────────────────

function destroyRadar() {
  if (radarTimer) { clearInterval(radarTimer); radarTimer = null; }
  if (radarMap)   { radarMap.remove(); radarMap = null; }
  radarLayers  = [];
  radarFrames  = [];
  radarIdx     = 0;
  radarPlaying = false;
}

async function fetchRadarFrames() {
  const now = Date.now();
  if (radarCache && (now - radarCache.fetchedAt) < RV_TTL_MS) return radarCache;
  const ctrl = new AbortController();
  const to   = setTimeout(() => ctrl.abort(), 8000);
  let r;
  try { r = await fetch(RV_API, { cache: 'no-store', signal: ctrl.signal }); }
  finally { clearTimeout(to); }
  if (!r.ok) throw new Error(`RainViewer HTTP ${r.status}`);
  const j      = await r.json();
  const past   = (j.radar?.past    ?? []).slice(-RADAR_PAST_FRAMES);
  const nowcst = (j.radar?.nowcast ?? []).slice(0, RADAR_NOWCAST_FRAMES);
  const frames = [
    ...past.map(f => ({ time: f.time, path: f.path, kind: 'past' })),
    ...nowcst.map(f => ({ time: f.time, path: f.path, kind: 'nowcast' })),
  ];
  if (!frames.length) throw new Error('nessun frame disponibile');
  radarCache = { host: j.host, frames, fetchedAt: now };
  return radarCache;
}

function buildRadarLayers(host, frames) {
  radarLayers = frames.map(f => {
    const url = `${host}${f.path}/256/{z}/{x}/{y}/4/1_1.png`;
    return L.tileLayer(url, { opacity: 0, tileSize: 256, zIndex: 5, minZoom: 0, maxZoom: 18, maxNativeZoom: 7, attribution: 'RainViewer' });
  });
  radarLayers.forEach(l => l.addTo(radarMap));
}

function showRadarFrame(i) {
  const opacity = f => f.kind === 'nowcast' ? 0.55 : 0.7;
  radarLayers.forEach((l, k) => l.setOpacity(k === i ? opacity(radarFrames[k]) : 0));
  radarIdx = i;
  const f = radarFrames[i];
  if (!f) return;
  const t   = new Date(f.time * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
  const lbl = f.kind === 'nowcast' ? `${t} (prev.)` : t;
  const timeEl   = document.getElementById('radar-time');
  const sliderEl = document.getElementById('radar-slider');
  if (timeEl)   timeEl.textContent = lbl;
  if (sliderEl) sliderEl.value = String(i);
  const badgeEl = document.getElementById('radar-frame-badge');
  if (badgeEl) {
    if (f.kind === 'nowcast') {
      badgeEl.textContent   = 'Previsione';
      badgeEl.style.cssText = 'background:rgba(59,164,194,0.15);color:#3BA4C2';
    } else {
      badgeEl.textContent   = 'LIVE';
      badgeEl.style.cssText = 'background:rgba(52,211,153,0.15);color:#34D399';
    }
    badgeEl.classList.remove('hidden');
  }
}

function startRadarAnimation() {
  if (radarTimer) clearInterval(radarTimer);
  radarTimer = setInterval(() => {
    if (!radarPlaying || document.hidden) return;
    showRadarFrame((radarIdx + 1) % radarFrames.length);
  }, 500);
}

function wireRadarControls() {
  const slider  = document.getElementById('radar-slider');
  const playBtn = document.getElementById('radar-play');
  if (slider) {
    slider.max = String(radarFrames.length - 1);
    slider.addEventListener('input', () => {
      radarPlaying = false;
      if (playBtn) playBtn.innerHTML = PLAY_SVG;
      showRadarFrame(parseInt(slider.value, 10));
    });
  }
  if (playBtn) {
    playBtn.innerHTML = PLAY_SVG;
    playBtn.addEventListener('click', () => {
      radarPlaying = !radarPlaying;
      playBtn.innerHTML = radarPlaying ? PAUSE_SVG : PLAY_SVG;
    });
  }
  document.getElementById('radar-zoom-in')?.addEventListener('click',  () => radarMap?.zoomIn());
  document.getElementById('radar-zoom-out')?.addEventListener('click', () => radarMap?.zoomOut());
}

function showRadarError(msg) {
  const box = document.getElementById('radar-error');
  if (!box) return;
  box.innerHTML = `
    <div style="width:40px;height:40px;border-radius:12px;background:rgba(255,255,255,0.04);border:1px solid var(--border-1);display:flex;align-items:center;justify-content:center">
      <span style="font-size:20px;line-height:1">⚠️</span>
    </div>
    <p style="font-size:13px;font-weight:600;color:var(--text-3);margin-top:10px">Radar non disponibile</p>
    <p style="font-size:11px;color:var(--text-3);opacity:0.6;margin-top:4px">${escHtml(msg)}</p>`;
  twemoji.parse(box, TWEMOJI_OPTS);
  box.classList.remove('hidden');
}

function buildRadarMap(locationId, host, frames) {
  if (typeof L === 'undefined') { showRadarError('libreria mappa non caricata'); return; }
  const loc = LOCATIONS.find(l => l.id === locationId);
  if (!loc)  { showRadarError('location sconosciuta'); return; }
  radarFrames = frames;
  radarMap = L.map('radar-map', {
    center: [loc.lat, loc.lon], zoom: 11,
    minZoom: 3, maxZoom: 12, zoomControl: false,
    attributionControl: false, scrollWheelZoom: false,
  });
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    minZoom: 3, maxZoom: 12, zIndex: 1,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>, &copy; <a href="https://carto.com">CARTO</a>',
  }).addTo(radarMap);

  L.control.attribution({
    position: 'bottomright'
  }).addTo(radarMap);

  const sonarHtml = `<div style="width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 2px rgba(59,164,194,0.35)"></div>`;
  L.marker([loc.lat, loc.lon], {
    icon: L.divIcon({ className: '', html: sonarHtml, iconSize: [8, 8], iconAnchor: [4, 4] }),
  }).addTo(radarMap);

  buildRadarLayers(host, frames);
  const startIdx = Math.min(RADAR_PAST_FRAMES - 1, frames.length - 1);
  wireRadarControls();
  showRadarFrame(startIdx);
  startRadarAnimation();
  setTimeout(() => radarMap?.invalidateSize(), 0);
}

function initRadar(locationId) {
  const errBox = document.getElementById('radar-error');
  if (errBox) errBox.classList.add('hidden');
  fetchRadarFrames()
    .then(({ host, frames }) => buildRadarMap(locationId, host, frames))
    .catch(err => showRadarError(err.message));
}


// ── Main render ───────────────────────────────────────────────────────────────

function render(data) {
  if (selectedDayIdx >= data.days.length) selectedDayIdx = 0;
  const day = data.days[selectedDayIdx] ?? data.days[0];
  const targetDate = day?.target_date;

  if (meteoChart)    { meteoChart.destroy();    meteoChart    = null; }
  if (multiDayChart) { multiDayChart.destroy(); multiDayChart = null; }
  destroyRadar();

  hideSkeleton();
  hideEl('error-state');

  renderHeaderMeta(data.generated_at);
  renderHero(data);
  renderAQ(data.air_quality);

  if (data.days.length > 0) {
    renderDayStrip(data.days, selectedDayIdx);
    renderDayDetail(day);

    const fs = document.getElementById('forecast-section');
    if (fs) { fs.classList.remove('hidden'); fs.style.display = 'flex'; }

    showEl('chart-weekly-section');
    initModelSwitch(data);
    initWeeklyModelSwitch(data);
    if (targetDate) initChart(data, selectedModel, targetDate);
    initWeeklyChart(data, selectedModel);
  }

  renderCoverage(data.coverage_empirical_30d);
  renderHeroSun(LOCATIONS.find(l => l.id === data.location_id), new Date());
  initRadar(data.location_id);

  twemoji.parse(document.querySelector('.g-header'), TWEMOJI_OPTS);
}

// ── Data loading ──────────────────────────────────────────────────────────────

async function loadLocation(locId) {
  renderTabs(locId);
  selectedDayIdx      = 0;
  selectedModel = 'guazza';

  const fs = document.getElementById('forecast-section');
  if (fs) { fs.classList.add('hidden'); fs.style.display = 'none'; }
  ['hero-card','aq-section','chart-weekly-section','coverage-bar','error-state'].forEach(hideEl);
  showSkeleton();

  if (meteoChart)    { meteoChart.destroy();    meteoChart    = null; }
  if (multiDayChart) { multiDayChart.destroy(); multiDayChart = null; }

  try {
    const r = await fetch(DATA_URL(locId));
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    currentData = await r.json();
    render(currentData);
  } catch (e) {
    hideSkeleton();
    const msgEl = document.getElementById('error-msg');
    if (msgEl) msgEl.textContent = `Errore: ${e.message}`;
    showEl('error-state');
    currentData = null;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

initDarkMode();

if (typeof Chart !== 'undefined') {
  Chart.defaults.font.family = "'JetBrains Mono', ui-monospace, monospace";
  Chart.defaults.font.size   = 11;
}

document.addEventListener('click', hideIndicatorTooltip);
window.addEventListener('popstate', () => loadLocation(getActiveLoc()));

loadLocation(getActiveLoc());
