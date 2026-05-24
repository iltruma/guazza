'use strict';

// Dev: ln -s ../data/output frontend/data  then: cd frontend && python3 -m http.server 8080
const DATA_URL = loc => `/data/${loc}.json`;
const TWEMOJI_OPTS = { folder: 'svg', ext: '.svg' };

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

const VERDICT_COLOR = {
  verde:  { bg: 'bg-emerald-500/[0.06]', border: 'border-emerald-500/20', text: 'text-emerald-700 dark:text-emerald-400', badge: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400', dot: 'bg-emerald-500', glow: '0 0 6px 2px rgba(16,185,129,0.55)'  },
  giallo: { bg: 'bg-amber-500/[0.06]',   border: 'border-amber-500/20',   text: 'text-amber-700 dark:text-amber-400',   badge: 'bg-amber-500/10 text-amber-700 dark:text-amber-400',   dot: 'bg-amber-500',   glow: '0 0 6px 2px rgba(245,158,11,0.55)'  },
  rosso:  { bg: 'bg-red-500/[0.06]',     border: 'border-red-500/20',     text: 'text-red-700 dark:text-red-400',     badge: 'bg-red-500/10 text-red-700 dark:text-red-400',     dot: 'bg-red-500',     glow: '0 0 6px 2px rgba(239,68,68,0.55)'   },
};

const AQ_THRESHOLDS = {
  pm10:    [20, 40],
  pm25:    [10, 20],
  no2:     [40, 160],
  o3:      [72, 144],
  co:      [2,  8],
  benzene: [1,  4],
  so2:     [70, 280],
};

const PLAY_SVG  = '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
const PAUSE_SVG = '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>';

const RV_API               = 'https://api.rainviewer.com/public/weather-maps.json';
const RV_TTL_MS            = 5 * 60 * 1000;
const RADAR_PAST_FRAMES    = 7;
const RADAR_NOWCAST_FRAMES = 6;
const RADAR_ZOOM           = 7;

// ── State ─────────────────────────────────────────────────────────────────────
let currentData         = null;
let selectedDayIdx      = 0;
let selectedModel       = 'guazza';
let selectedWeeklyModel = 'guazza';
let meteoChart          = null;
let multiDayChart       = null;
let radarMap     = null;
let radarLayers  = [];
let radarFrames  = [];
let radarIdx     = 0;
let radarTimer   = null;
let radarPlaying = false;
let radarCache   = null;

// ── Utility ───────────────────────────────────────────────────────────────────

function escHtml(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function showEl(id)  { document.getElementById(id)?.classList.remove('hidden'); }
function hideEl(id)  { document.getElementById(id)?.classList.add('hidden'); }

function showSkeleton() { showEl('skeleton-state'); }
function hideSkeleton() { hideEl('skeleton-state'); hideEl('error-state'); }

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
  return weatherIcon(
    day.forecasts.precip_mm?.p50 ?? 0,
    day.forecasts.tmax_c?.p50   ?? null,
    day.indicators.temporale?.verdict,
    day.indicators.nebbia?.verdict,
  );
}

function weatherConditionText(precipP50, tmaxP50, temporaleVerdict, nebbiaVerdict) {
  if (nebbiaVerdict === 'rosso' || nebbiaVerdict === 'giallo') return 'Nebbia';
  if (temporaleVerdict === 'rosso')  return 'Temporale';
  if (temporaleVerdict === 'giallo') return 'Possibili tuoni';
  if (precipP50 >= 10)  return 'Pioggia';
  if (precipP50 >=  3)  return 'Pioggerella';
  if (precipP50 >= 0.5) return 'Nuvoloso';
  if (tmaxP50 == null)  return 'Variabile';
  if (tmaxP50 >= 22)    return 'Soleggiato';
  if (tmaxP50 >= 15)    return 'Sereno';
  return 'Variabile';
}

function weatherIconFromCurrent(current) {
  const prec = current?.precip_mm ?? 0;
  if (prec >= 5)   return '🌧️';
  if (prec >= 1)   return '🌦️';
  if (prec >= 0.1) return '🌥️';
  return '☀️';
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

function aqColorCls(key, value) {
  if (value == null) return null;
  const [lo, hi] = AQ_THRESHOLDS[key] ?? [0, Infinity];
  if (value < lo)  return { border: 'border-emerald-500/30', text: 'text-emerald-600 dark:text-emerald-400' };
  if (value < hi)  return { border: 'border-amber-500/30',   text: 'text-amber-600 dark:text-amber-400'   };
  return             { border: 'border-red-500/30',     text: 'text-red-600 dark:text-red-400'     };
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
  // Tema scuro fisso — nessuna preference OS
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
    const cls = active
      ? 'px-3 py-1.5 rounded-full text-xs font-semibold text-white snap-center shrink-0 transition-all duration-200'
      : 'px-3 py-1.5 rounded-full text-xs font-medium text-slate-500 dark:text-white/40 bg-transparent hover:bg-slate-200/70 dark:hover:bg-white/[0.06] snap-center shrink-0 transition-all duration-200 relative overflow-hidden';
    const style = active ? 'background:rgba(59,154,108,0.85);box-shadow:0 2px 10px -2px rgba(59,154,108,0.4)' : '';
    return `<button class="${cls}" style="${style}" data-loc="${l.id}">${l.label}</button>`;
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
  const rect   = btn.getBoundingClientRect();
  const span   = document.createElement('span');
  const size   = Math.max(rect.width, rect.height) * 2;
  span.style.cssText = `position:absolute;border-radius:50%;background:rgba(59,154,108,0.25);width:${size}px;height:${size}px;left:${e.clientX - rect.left - size/2}px;top:${e.clientY - rect.top - size/2}px;transform:scale(0);pointer-events:none;transition:transform 400ms ease-out,opacity 300ms ease-out;opacity:0.5`;
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
  const el   = document.getElementById('header-meta');
  if (!el) return;
  const ageH = (Date.now() - new Date(generatedAt).getTime()) / 3600000;
  const time = new Date(generatedAt).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
  if (ageH >= 6) {
    el.innerHTML = `<span class="rounded-full bg-amber-500/10 text-amber-600 dark:text-amber-400 px-2 py-0.5 text-[10px] font-semibold border stale-pulse" style="border-color:rgba(245,158,11,0.1)">⚠ dati vecchi</span>`;
  } else {
    el.innerHTML = `<span class="tabular-nums">Aggiornato ${time}</span>`;
  }
}

// ── Hero card ─────────────────────────────────────────────────────────────────

function renderHero(data) {
  const current  = data.current;
  const todayDay = data.days.find(d => isToday(d.target_date));
  const locMeta  = LOCATIONS.find(l => l.id === data.location_id);
  const now      = new Date();

  // Icon
  let icon;
  if (current?.temp_c != null) icon = weatherIconFromCurrent(current);
  else if (todayDay)           icon = weatherIconForDay(todayDay);
  else                         icon = '⛅';
  const iconEl = document.getElementById('hero-icon');
  iconEl.textContent = icon;
  twemoji.parse(iconEl, TWEMOJI_OPTS);

  // Temperature gradient background
  const mainTemp = current?.temp_c ?? todayDay?.forecasts?.tmax_c?.p50 ?? null;
  const gradEl   = document.getElementById('hero-temp-gradient');
  if (gradEl && mainTemp != null) {
    const dark = document.documentElement.dataset.theme === 'dark';
    const mul  = dark ? 2.5 : 1;
    let color;
    if (mainTemp < 10)      color = `rgba(59,130,246,${0.08 * mul})`;
    else if (mainTemp < 22) color = `rgba(16,185,129,${0.06 * mul})`;
    else                    color = `rgba(249,115,22,${0.08 * mul})`;
    gradEl.style.background = `linear-gradient(135deg, ${color} 0%, transparent 60%)`;
  }

  // Temperature (with counter animation)
  const tempEl = document.getElementById('hero-temp');
  if (mainTemp != null) {
    tempEl.textContent = `${mainTemp.toFixed(1)}°`;
    animateCounter(tempEl, mainTemp, v => `${v.toFixed(1)}°`);
  } else {
    tempEl.textContent = '—';
  }

  // Meta row (percepita + rugiada)
  const metaEl = document.getElementById('hero-meta');
  const feelsLike = current?.feels_like_c != null ? `${current.feels_like_c.toFixed(1)}°` : null;
  const dewpoint  = current?.dewpoint_c   != null ? `${current.dewpoint_c.toFixed(1)}°`   : null;
  const ts        = current?.ts ? new Date(current.ts).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' }) : null;
  metaEl.innerHTML = [
    feelsLike ? `<span>Percepita <strong class="text-slate-700 dark:text-slate-300">${feelsLike}</strong></span>` : '',
    dewpoint  ? `<span>Rugiada <strong class="text-slate-700 dark:text-slate-300">${dewpoint}</strong></span>`   : '',
    ts ? `<span class="text-[11px] tabular-nums">SIR · ${ts}</span>` : '',
  ].filter(Boolean).join('');

  // Stats pills
  renderHeroStats(current);

  // Air quality
  renderHeroAQ(data.air_quality, data.generated_at);

  // Sun/moon
  renderHeroSun(locMeta, now);

  // Today indicators
  renderHeroIndicators(todayDay);

  // Show hero card
  const heroCard = document.getElementById('hero-card');
  heroCard.classList.remove('hidden');
  heroCard.classList.remove('anim-fade-up');
  void heroCard.offsetWidth;
  heroCard.classList.add('anim-fade-up');
  twemoji.parse(heroCard, TWEMOJI_OPTS);
}

function renderHeroStats(current) {
  const windDir = windDirLabel(current?.wind_dir_deg);
  const stats = [
    { label: 'Vento',     value: current?.wind_speed_ms != null ? `${fmtWind(current.wind_speed_ms)}${windDir ? ` ${windDir.label}` : ''}` : '—' },
    { label: 'Umidità',   value: current?.humidity_pct  != null ? `${current.humidity_pct.toFixed(0)}%`    : '—' },
    { label: 'Pioggia',   value: current?.precip_mm     != null ? `${current.precip_mm.toFixed(1)} mm`    : '—' },
    { label: 'Pressione', value: current?.pressure_hpa  != null ? `${current.pressure_hpa.toFixed(0)} hPa` : '—' },
  ];
  const el = document.getElementById('hero-stats');
  /* Le celle hanno bg semi-opaco per lavorare con il trucco gap-px del container */
  el.innerHTML = stats.map(s => `
    <div class="px-4 py-3 flex flex-col gap-0.5 bg-[#09172A] hover:bg-[#0d1e38] transition-colors duration-200">
      <div class="text-[10px] font-semibold text-white/30 uppercase tracking-widest">${s.label}</div>
      <div class="text-sm font-bold text-white tabular-nums">${s.value}</div>
    </div>`).join('');
}

function renderHeroAQ(aq, _generatedAt) {
  const items = [
    { key: 'pm10',    label: 'PM10',  value: aq?.pm10_ugm3    ?? null, unit: 'µg/m³', dec: 0 },
    { key: 'pm25',    label: 'PM2.5', value: aq?.pm25_ugm3    ?? null, unit: 'µg/m³', dec: 0 },
    { key: 'no2',     label: 'NO₂',   value: aq?.no2_ugm3     ?? null, unit: 'µg/m³', dec: 0 },
    { key: 'o3',      label: 'O₃',    value: aq?.o3_ugm3      ?? null, unit: 'µg/m³', dec: 0 },
    { key: 'co',      label: 'CO',    value: aq?.co_mgm3      ?? null, unit: 'mg/m³', dec: 1 },
    { key: 'benzene', label: 'C₆H₆', value: aq?.benzene_ugm3 ?? null, unit: 'µg/m³', dec: 1 },
    { key: 'so2',     label: 'SO₂',   value: aq?.so2_ugm3     ?? null, unit: 'µg/m³', dec: 0 },
  ];
  const el = document.getElementById('hero-aq');
  /*
   * Hero è sempre su sfondo scuro (.hero-sky) — usiamo colori espliciti
   * senza `dark:` prefix, che dipende dall'OS preference non dal background locale.
   */
  el.innerHTML = items.map(it => {
    const display = it.value != null ? it.value.toFixed(it.dec) : '—';
    const opacityCls = it.value == null ? 'opacity-30' : '';
    let borderCls = 'border-white/10';
    let textCls   = 'text-white/35';
    if (it.value != null) {
      const [lo, hi] = AQ_THRESHOLDS[it.key] ?? [0, Infinity];
      if (it.value < lo)      { borderCls = 'border-emerald-500/30'; textCls = 'text-emerald-400'; }
      else if (it.value < hi) { borderCls = 'border-amber-500/30';   textCls = 'text-amber-400';   }
      else                    { borderCls = 'border-red-500/35';     textCls = 'text-red-400';     }
    }
    return `<div class="shrink-0 w-[72px] rounded-xl py-2.5 bg-white/[0.06] border ${borderCls} ${opacityCls} text-center">
      <div class="text-[10px] font-semibold text-white/35 leading-tight">${it.label}</div>
      <div class="text-sm font-bold ${textCls} tabular-nums mt-1 leading-tight">${display}</div>
      <div class="text-[9px] text-white/20 mt-0.5 leading-tight">${it.unit}</div>
    </div>`;
  }).join('');
}

function renderHeroSun(locMeta, now) {
  const el = document.getElementById('hero-sun');
  if (!locMeta || typeof SunCalc === 'undefined') { el.innerHTML = ''; return; }
  const sunTimes  = SunCalc.getTimes(now, locMeta.lat, locMeta.lon);
  const moonPhase = SunCalc.getMoonIllumination(now).phase;
  const idx       = Math.round(moonPhase * 8) % 8;
  const moonEmoji = ['🌑','🌒','🌓','🌔','🌕','🌖','🌗','🌘'][idx];
  const moonLabel = ['Luna nuova','Luna crescente','Primo quarto','Gibbosa crescente','Luna piena','Gibbosa calante','Ultimo quarto','Luna calante'][idx];

  /* Three glassmorphic pills: sunrise · sunset · moon phase */
  el.innerHTML = `
    <div class="flex items-center gap-2 flex-wrap justify-end">
      <div class="tooltip tooltip-left flex items-center gap-2 px-3 py-1.5 rounded-full bg-white/[0.07] border border-white/[0.08]" data-tip="Alba">
        <span class="text-sm leading-none">🌅</span>
        <span class="text-xs font-semibold text-white/65 tabular-nums">${fmtSunTime(sunTimes.sunrise)}</span>
      </div>
      <div class="tooltip tooltip-left flex items-center gap-2 px-3 py-1.5 rounded-full bg-white/[0.07] border border-white/[0.08]" data-tip="Tramonto">
        <span class="text-sm leading-none">🌇</span>
        <span class="text-xs font-semibold text-white/65 tabular-nums">${fmtSunTime(sunTimes.sunset)}</span>
      </div>
      <div class="tooltip tooltip-left flex items-center gap-2 px-3 py-1.5 rounded-full bg-white/[0.07] border border-white/[0.08]" data-tip="${moonLabel}">
        <span class="text-sm leading-none">${moonEmoji}</span>
        <span class="text-xs font-medium text-white/45">${moonLabel}</span>
      </div>
    </div>`;
  twemoji.parse(el, TWEMOJI_OPTS);
}

function renderHeroIndicators(todayDay) {
  const el = document.getElementById('hero-indicators');
  if (!todayDay) { el.innerHTML = ''; return; }

  /*
   * DaisyUI tooltip usa :hover — non scatta su touch.
   * Soluzione: testo della regola inline, nascosto, toggle al click/tap.
   * Funziona su mobile (tap) e desktop (click); niente dipendenze extra.
   */
  el.innerHTML = Object.entries(todayDay.indicators).map(([id, ind], i) => {
    const meta       = INDICATOR_META[id] ?? { label: id, icon: '?' };
    const vc         = VERDICT_COLOR[ind.verdict] ?? VERDICT_COLOR.giallo;
    const verdictCap = ind.verdict.charAt(0).toUpperCase() + ind.verdict.slice(1);
    const tip        = escHtml(ind.rule_text || ind.rule_matched || '');
    return `<div data-hero-chip class="shrink-0">
      <div class="flex items-center gap-2 px-3 py-2.5 sm:gap-2.5 sm:px-3.5 sm:py-3 rounded-2xl bg-white/[0.07] border border-white/[0.1] hover:bg-white/[0.1] active:scale-95 transition-all duration-200 cursor-pointer select-none"
           style="animation:fade-up 0.35s ease-out ${i * 50}ms both">
        <span class="text-xl sm:text-2xl leading-none">${meta.icon}</span>
        <div class="text-left min-w-0">
          <div class="text-[10px] font-semibold text-white/45 leading-tight">${meta.label}</div>
          <div class="flex items-center gap-1.5 mt-1">
            <span class="w-2 h-2 rounded-full ${vc.dot} shrink-0"></span>
            <span class="text-xs font-bold text-white/85">${verdictCap}</span>
          </div>
          ${tip ? `<div class="chip-tip text-[9px] text-white/35 leading-snug mt-1 hidden">${tip}</div>` : ''}
        </div>
      </div>
    </div>`;
  }).join('');

  /* Toggle rule text on click/tap — close others when opening a new one */
  el.querySelectorAll('[data-hero-chip]').forEach(chip => {
    chip.addEventListener('click', () => {
      const tipEl = chip.querySelector('.chip-tip');
      if (!tipEl) return;
      const opening = tipEl.classList.contains('hidden');
      el.querySelectorAll('.chip-tip').forEach(t => t.classList.add('hidden'));
      if (opening) tipEl.classList.remove('hidden');
    });
  });

  twemoji.parse(el, TWEMOJI_OPTS);
}

// ── CI bar ────────────────────────────────────────────────────────────────────

function ciBar(fc, unit) {
  if (!fc) return '';
  const { p50, ci80_lo, ci80_hi, ci90_lo, ci90_hi } = fc;
  if (p50 == null || ci90_lo == null || ci90_hi == null) return '';
  const range = ci90_hi - ci90_lo;
  if (range <= 0) return '';
  const pct      = v => Math.max(0, Math.min(100, ((v - ci90_lo) / range) * 100));
  const p80l     = (ci80_lo != null ? pct(ci80_lo) : 0).toFixed(2);
  const p80w     = (ci80_lo != null && ci80_hi != null ? pct(ci80_hi) - pct(ci80_lo) : 100).toFixed(2);
  const p50pos   = pct(p50).toFixed(2);
  return `
    <div class="mt-4">
      <div class="relative h-2 rounded-full bg-slate-200 dark:bg-slate-700" style="overflow:visible">
        <div class="ci-range-90 absolute inset-0 rounded-full bg-slate-300/50 dark:bg-slate-500/30" style="transform-origin:left"></div>
        <div class="ci-range-80 absolute top-0 h-full rounded-full" style="background:rgba(59,154,108,0.35);left:${p80l}%;width:${p80w}%;transform-origin:left"></div>
        <div class="ci-median absolute w-3 h-3 rounded-full bg-white dark:bg-[#0D1E35]" style="border:2px solid #3B9A6C;top:50%;margin-left:-6px;left:${p50pos}%"></div>
      </div>
      <div class="flex justify-between mt-2 text-[11px] text-slate-400 tabular-nums">
        <span>${ci90_lo.toFixed(1)}${unit}</span>
        <span class="text-slate-500 dark:text-slate-300">${p50.toFixed(1)}${unit}</span>
        <span>${ci90_hi.toFixed(1)}${unit}</span>
      </div>
    </div>`;
}

// ── Day strip ─────────────────────────────────────────────────────────────────

function renderDayStrip(days, activeDayIdx) {
  const el = document.getElementById('day-strip');
  el.innerHTML = days.map((day, idx) => {
    const { target_date, forecasts: fc, indicators } = day;
    const active  = idx === activeDayIdx;
    const diff    = diffDays(target_date);
    const icon    = weatherIconForDay(day);
    const condText = weatherConditionText(
      fc.precip_mm?.p50 ?? 0,
      fc.tmax_c?.p50   ?? null,
      indicators.temporale?.verdict,
      indicators.nebbia?.verdict,
    );

    /* Verdict dots row */
    const dots = Object.entries(indicators).map(([id, ind]) => {
      const vc = VERDICT_COLOR[ind.verdict];
      return vc
        ? `<span class="w-2 h-2 rounded-full shrink-0 ${vc.dot}" title="${INDICATOR_META[id]?.label ?? id}: ${ind.verdict}"></span>`
        : `<span class="w-2 h-2 rounded-full shrink-0 bg-slate-200 dark:bg-white/10"></span>`;
    }).join('');

    const isToday_   = diff === 0;
    const isTomorrow = diff === 1;
    const dayLabel   = fmtDayShort(target_date);

    const cardStyle = active
      ? `border:2px solid rgba(59,154,108,0.6);box-shadow:0 0 0 4px rgba(59,154,108,0.08),0 8px 24px -6px rgba(59,154,108,0.2)`
      : '';
    const cardCls = active
      ? 'snap-center shrink-0 w-[100px] sm:w-[116px] rounded-2xl bg-white dark:bg-white/[0.09] px-3 py-3.5 text-center cursor-pointer transition-all duration-300'
      : 'snap-center shrink-0 w-[100px] sm:w-[116px] rounded-2xl bg-white/70 dark:bg-white/[0.04] border border-black/[0.05] dark:border-white/[0.06] px-3 py-3.5 text-center cursor-pointer hover:-translate-y-0.5 hover:bg-white dark:hover:bg-white/[0.08] transition-all duration-300 ease-out';

    /* Day label: "Oggi" verde accent, "Domani" normal, others dimmed */
    const labelCls  = isToday_   ? 'text-[11px] font-bold tracking-tight today-badge-anim'
                    : isTomorrow ? 'text-[11px] font-semibold text-slate-600 dark:text-white/65'
                    :              'text-[11px] font-medium text-slate-400 dark:text-white/35 capitalize';
    const labelStyle = isToday_ ? 'color:#3B9A6C' : '';

    /* Temps: max prominent, min muted — side by side */
    const tmax = fmtTemp(fc.tmax_c?.p50);
    const tmin = fmtTemp(fc.tmin_c?.p50);

    return `<div class="${cardCls}" data-idx="${idx}" style="${cardStyle}">
      <div class="${labelCls} leading-none mb-2.5" style="${labelStyle}">${dayLabel}</div>
      <span class="text-4xl leading-none block mb-1.5">${icon}</span>
      <div class="text-[10px] font-medium text-slate-400 dark:text-white/30 mb-2.5 leading-tight">${condText}</div>
      <div class="flex items-baseline justify-center gap-1.5 mb-2.5">
        <span class="text-sm font-extrabold tabular-nums text-slate-800 dark:text-white">${tmax}</span>
        <span class="text-[11px] font-medium tabular-nums text-slate-400 dark:text-white/35">${tmin}</span>
      </div>
      <div class="flex gap-1 flex-wrap justify-center">${dots}</div>
    </div>`;
  }).join('');

  el.querySelectorAll('[data-idx]').forEach(card => {
    card.addEventListener('click', () => {
      const idx = parseInt(card.dataset.idx, 10);
      if (idx === selectedDayIdx) return;
      card.style.transform = 'translateY(-2px) scale(0.97)';
      setTimeout(() => card.style.transform = '', 100);
      selectedDayIdx = idx;
      renderDayStrip(currentData.days, selectedDayIdx);
      renderDayDetail(currentData.days[selectedDayIdx]);
      card.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
      const td = currentData.days[selectedDayIdx]?.target_date;
      if (td) updateChartModel(currentData, selectedModel, td);
    });
  });

  twemoji.parse(el, TWEMOJI_OPTS);
}

// ── Day detail ────────────────────────────────────────────────────────────────

function renderDayDetail(day) {
  if (!day) return;
  const { forecasts: fc, indicators, target_date, lead_time_h } = day;
  const icon = weatherIconForDay(day);

  document.getElementById('detail-icon').textContent  = icon;
  document.getElementById('detail-title').textContent = fmtDayLabel(target_date);
  document.getElementById('detail-date').textContent  = fmtDate(target_date);
  document.getElementById('detail-lead').textContent  = `+${lead_time_h}h`;

  /* Tmax: warm orange, no arrow — number speaks for itself */
  document.getElementById('detail-tmax').innerHTML =
    `<span style="color:#F97316">${fmtTemp(fc.tmax_c?.p50)}</span>`;
  /* Tmin: cool blue-slate */
  document.getElementById('detail-tmin').innerHTML =
    `<span style="color:#60A5FA">${fmtTemp(fc.tmin_c?.p50)}</span>`;

  const precipVal = fc.precip_mm?.p50;
  document.getElementById('detail-precip-val').innerHTML = precipVal != null && precipVal > 0.05
    ? `${precipVal.toFixed(1)}<span class="text-xl font-medium text-slate-400 dark:text-white/30 ml-1.5">mm</span>`
    : '<span class="text-slate-400 dark:text-white/30">—</span>';

  document.getElementById('detail-ci-tmax').innerHTML   = ciBar(fc.tmax_c,   '°');
  document.getElementById('detail-ci-tmin').innerHTML   = ciBar(fc.tmin_c,   '°');
  document.getElementById('detail-ci-precip').innerHTML = ciBar(fc.precip_mm, ' mm');

  renderIndicatorChips(indicators);
  renderNwpList(day);

  const detailEl = document.getElementById('day-detail');
  detailEl.classList.remove('anim-fade-up');
  void detailEl.offsetWidth;
  detailEl.classList.add('anim-fade-up');

  twemoji.parse(detailEl, TWEMOJI_OPTS);
}

function renderIndicatorChips(indicators) {
  const el = document.getElementById('detail-indicators');
  /*
   * Stessa strategia degli hero indicators:
   * - flex-wrap (no overflow-x-auto → no clipping mobile)
   * - Niente DaisyUI tooltip — testo regola inline, toggle al click/tap
   */
  el.innerHTML = Object.entries(indicators).map(([id, ind], i) => {
    const meta    = INDICATOR_META[id] ?? { label: id, icon: '?' };
    const vc      = VERDICT_COLOR[ind.verdict] ?? VERDICT_COLOR.giallo;
    const verdCap = ind.verdict.charAt(0).toUpperCase() + ind.verdict.slice(1);
    const tip     = escHtml(ind.rule_text || ind.rule_matched || '');
    return `<div data-detail-chip class="shrink-0">
      <div class="flex items-center gap-2.5 px-3.5 py-3 rounded-2xl ${vc.bg} border ${vc.border} hover:scale-[1.02] active:scale-95 transition-all duration-200 cursor-pointer select-none"
           style="animation:fade-up 0.35s ease-out ${i * 50}ms both">
        <span class="text-2xl leading-none">${meta.icon}</span>
        <div class="text-left min-w-0">
          <div class="text-[10px] font-semibold ${vc.text} leading-tight opacity-75">${meta.label}</div>
          <div class="flex items-center gap-1.5 mt-1">
            <span class="w-2 h-2 rounded-full ${vc.dot} shrink-0"></span>
            <span class="text-xs font-bold ${vc.text}">${verdCap}</span>
          </div>
          ${tip ? `<div class="chip-tip text-[9px] ${vc.text} opacity-60 leading-snug mt-1 hidden">${tip}</div>` : ''}
        </div>
      </div>
    </div>`;
  }).join('');

  el.querySelectorAll('[data-detail-chip]').forEach(chip => {
    chip.addEventListener('click', () => {
      const tipEl = chip.querySelector('.chip-tip');
      if (!tipEl) return;
      const opening = tipEl.classList.contains('hidden');
      el.querySelectorAll('.chip-tip').forEach(t => t.classList.add('hidden'));
      if (opening) tipEl.classList.remove('hidden');
    });
  });

  twemoji.parse(el, TWEMOJI_OPTS);
}

function renderNwpList(day) {
  const el  = document.getElementById('nwp-list');
  const nwp = day.nwp_comparison;
  const fc  = day.forecasts;
  if (!nwp || !nwp.length) { el.innerHTML = ''; return; }

  /*
   * Layout: 3 colonne [nome-modello | min · max | precip]
   * Guazza pinnato in cima con accent border-left.
   * last_run come riga secondaria sotto il nome del modello.
   */
  const colCls = 'grid grid-cols-[1fr_auto_auto] items-start gap-x-5';

  const guazzaRow = `
    <div class="${colCls} px-5 md:px-7 py-3 mb-px" style="background:rgba(59,154,108,0.07);border-left:3px solid #3B9A6C">
      <div class="text-sm font-bold" style="color:#3B9A6C">★ Guazza ML</div>
      <div class="text-right text-sm font-semibold tabular-nums font-mono text-slate-700 dark:text-slate-200 whitespace-nowrap">
        <span style="color:#60A5FA">${fmtTemp(fc.tmin_c?.p50)}</span>
        <span class="text-slate-300 dark:text-white/20 mx-1">·</span>
        <span style="color:#F97316">${fmtTemp(fc.tmax_c?.p50)}</span>
      </div>
      <div class="text-right text-sm font-semibold tabular-nums font-mono text-sky-600 dark:text-sky-400 whitespace-nowrap">${fmtPrecip(fc.precip_mm?.p50)}</div>
    </div>`;

  /* Column sub-header */
  const subHeader = `
    <div class="${colCls} px-5 md:px-7 py-2 border-t border-b border-slate-100 dark:border-white/[0.04]">
      <div class="text-[10px] text-slate-400 dark:text-white/25 font-semibold uppercase tracking-widest">NWP</div>
      <div class="text-right text-[10px] text-slate-400 dark:text-white/25 font-semibold whitespace-nowrap">Min · Max</div>
      <div class="text-right text-[10px] text-slate-400 dark:text-white/25 font-semibold">Precip</div>
    </div>`;

  const nwpRows = nwp.map(m => `
    <div class="${colCls} px-5 md:px-7 py-3 border-t border-slate-100 dark:border-white/[0.04] hover:bg-slate-50 dark:hover:bg-white/[0.03] transition-colors duration-150">
      <div>
        <div class="text-sm font-medium text-slate-700 dark:text-slate-300 leading-tight">${escHtml(m.label)}</div>
        <div class="text-[10px] text-slate-400 dark:text-white/25 tabular-nums mt-0.5">${fmtLastRun(m.last_run)}</div>
      </div>
      <div class="text-right text-sm font-semibold tabular-nums font-mono text-slate-600 dark:text-slate-400 whitespace-nowrap self-center">
        ${m.tmin_c != null ? m.tmin_c.toFixed(1)+'°' : '—'}
        <span class="text-slate-300 dark:text-white/20 mx-1">·</span>
        ${m.tmax_c != null ? m.tmax_c.toFixed(1)+'°' : '—'}
      </div>
      <div class="text-right text-sm font-semibold tabular-nums font-mono text-slate-600 dark:text-slate-400 whitespace-nowrap self-center">${m.precip_mm != null ? m.precip_mm.toFixed(1)+' mm' : '—'}</div>
    </div>`).join('');

  el.innerHTML = guazzaRow + subHeader + nwpRows;
}

// ── Coverage badge ────────────────────────────────────────────────────────────

function renderCoverage(cov) {
  const el = document.getElementById('coverage-bar');
  if (!el) return;
  if (!cov || Object.values(cov).every(v => v === null)) {
    el.innerHTML = `<div class="rounded-2xl bg-amber-500/10 border border-amber-500/20 text-amber-600 dark:text-amber-400 px-4 py-3 text-sm">⚠️ Calibrazione in corso — copertura CI non ancora disponibile (primi 30gg)</div>`;
  } else {
    const items = [
      ['Tmin CI80', cov.tmin_ci80], ['Tmin CI90', cov.tmin_ci90],
      ['Tmax CI80', cov.tmax_ci80], ['Tmax CI90', cov.tmax_ci90],
      ['Precip CI80', cov.precip_ci80], ['Precip CI90', cov.precip_ci90],
    ].filter(([, v]) => v !== null)
     .map(([k, v]) => `<span class="text-xs">${k}: <strong>${(v * 100).toFixed(0)}%</strong></span>`)
     .join('');
    el.innerHTML = `<div class="rounded-2xl bg-emerald-500/10 border border-emerald-500/20 text-emerald-700 dark:text-emerald-400 px-4 py-3 text-sm flex gap-4 flex-wrap">📊 ${items}</div>`;
  }
  showEl('coverage-bar');
  twemoji.parse(el, TWEMOJI_OPTS);
}

// ── Model switches (segmented control) ───────────────────────────────────────

function initModelSwitch(data) {
  _buildModelSwitch(data, 'model-switch', 'model-pill', selectedModel, src => {
    selectedModel = src;
    const td = currentData.days[selectedDayIdx]?.target_date;
    if (td) updateChartModel(currentData, selectedModel, td);
  });
}

function initWeeklyModelSwitch(data) {
  _buildModelSwitch(data, 'weekly-model-switch', 'weekly-model-pill', selectedWeeklyModel, src => {
    selectedWeeklyModel = src;
    updateWeeklyChart(currentData, selectedWeeklyModel);
  });
}

function _buildModelSwitch(data, switchId, pillId, activeSource, onChange) {
  const container = document.getElementById(switchId);
  if (!container) return;
  const models = [{ source: 'guazza', label: '★ Guazza ML' }];
  (data.nwp_models_hourly || []).forEach(m => models.push({ source: m.source, label: m.label }));

  // Keep the pill span, rebuild buttons
  const pillEl = document.getElementById(pillId);
  container.innerHTML = '';
  if (pillEl) container.appendChild(pillEl);
  else {
    const s = document.createElement('span');
    s.id = pillId;
    s.className = 'absolute top-1 bottom-1 bg-white dark:bg-slate-700 rounded-full shadow-sm pointer-events-none transition-all duration-300 ease-[cubic-bezier(0.4,0,0.2,1)]';
    container.appendChild(s);
  }

  models.forEach(m => {
    const btn = document.createElement('button');
    const active = m.source === activeSource;
    btn.className = `relative z-10 px-3 py-1 text-xs transition-colors duration-200 ${active ? 'font-semibold' : 'font-medium text-slate-500 dark:text-slate-400'}`;
    btn.style.color = active ? '#3B9A6C' : '';
    btn.dataset.src = m.source;
    btn.textContent = m.label;
    container.appendChild(btn);
    btn.addEventListener('click', () => {
      container.querySelectorAll('[data-src]').forEach(b => {
        const isActive = b.dataset.src === m.source;
        b.className = `relative z-10 px-3 py-1 text-xs transition-colors duration-200 ${isActive ? 'font-semibold' : 'font-medium text-slate-500 dark:text-slate-400'}`;
        b.style.color = isActive ? '#3B9A6C' : '';
      });
      updatePillPosition(switchId, pillId, m.source);
      onChange(m.source);
    });
  });

  requestAnimationFrame(() => updatePillPosition(switchId, pillId, activeSource));
}

// ── Chart ─────────────────────────────────────────────────────────────────────

function chartPalette() {
  const dark = document.documentElement.dataset.theme === 'dark';
  return {
    grid:  dark ? 'rgba(148,163,184,0.08)' : 'rgba(148,163,184,0.06)',
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
      const ts = new Date(pt.ts.replace('Z', ''));
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
    ts: new Date(pt.ts.replace('Z', '')),
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
    const ctx  = chart.ctx;
    const x    = active[0].element.x;
    const { top, bottom } = chart.chartArea;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.lineWidth   = 1;
    ctx.strokeStyle = 'rgba(59,154,108,0.25)';
    ctx.setLineDash([]);
    ctx.stroke();
    // Glow circle around active temp point
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

function externalTooltipHandler({ chart, tooltip }) {
  const el = document.getElementById('chart-tooltip');
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

  const row = (dot, label, val) =>
    `<div class="flex items-center justify-between gap-5 py-[3px]">
       <span class="flex items-center gap-1.5 text-[11px] text-slate-400 dark:text-white/40">${dot}${label}</span>
       <span class="text-[11px] font-bold tabular-nums text-slate-800 dark:text-white/90">${val}</span>
     </div>`;
  const dot = cls => `<span class="w-1.5 h-1.5 rounded-full ${cls} shrink-0"></span>`;

  el.innerHTML = `
    <div class="flex items-baseline justify-between gap-3 mb-2 pb-2 border-b border-slate-100 dark:border-white/[0.07]">
      <span class="text-[10px] text-slate-400 dark:text-white/35 font-medium capitalize">${date}</span>
      <span class="text-sm font-bold tabular-nums text-slate-900 dark:text-white">${time}</span>
    </div>
    ${temp ? row(dot('bg-orange-500'), 'Temp', `${temp.raw.y.toFixed(1)}°C`) : ''}
    ${hum  ? row(dot('bg-sky-500'),    'Umidità', `${hum.raw.y.toFixed(0)}%`) : ''}
    ${prec && prec.raw.y > 0.05 ? row(dot('bg-blue-500'), 'Precip', `${prec.raw.y.toFixed(1)} mm`) : ''}
    ${wind ? row(dot('bg-teal-500'),   'Vento', `${wind.raw.y.toFixed(0)} km/h`) : ''}`;

  const cRect = chart.canvas.parentElement.getBoundingClientRect();
  el.style.opacity = '1';
  el.style.left    = `${Math.max(0, Math.min(tooltip.caretX - 80, cRect.width - 175))}px`;
  el.style.top     = `${Math.max(4, tooltip.caretY - 130)}px`;
}

// Populates the 4-cell stat strip above the daily chart canvas
function renderChartStatStrip(points) {
  const el = document.getElementById('chart-stat-strip');
  if (!el) return;
  if (!points.length) { el.innerHTML = ''; return; }

  const temps  = points.map(p => p.temp_c).filter(v => v != null);
  const hums   = points.map(p => p.humidity_pct).filter(v => v != null);
  const precip = points.reduce((s, p) => s + (p.precip_mm ?? 0), 0);
  const winds  = points.map(p => p.wind_speed_ms).filter(v => v != null);

  const tRange  = temps.length  ? `${Math.min(...temps).toFixed(1)}–${Math.max(...temps).toFixed(1)}°` : '—';
  const hAvg    = hums.length   ? `${(hums.reduce((a,b)=>a+b,0)/hums.length).toFixed(0)}%` : '—';
  const precipS = precip > 0.05 ? `${precip.toFixed(1)} mm` : '—';
  const wMax    = winds.length  ? `${(Math.max(...winds) * 3.6).toFixed(0)} km/h` : '—';

  const stats = [
    { label: 'Range temp',   value: tRange,  color: '#F97316' },
    { label: 'Umid. media',  value: hAvg,    color: '#0EA5E9' },
    { label: 'Precip tot.',  value: precipS, color: '#2563EB' },
    { label: 'Vento max',    value: wMax,    color: '#14B8A6' },
  ];
  el.innerHTML = stats.map(s => `
    <div class="flex flex-col items-center py-2.5 px-2 bg-slate-50/70 dark:bg-white/[0.02]">
      <div class="text-[9px] font-semibold text-slate-400 dark:text-white/20 uppercase tracking-widest mb-1 whitespace-nowrap">${s.label}</div>
      <div class="text-[13px] font-bold tabular-nums leading-none" style="color:${s.color}">${s.value}</div>
    </div>`).join('');
}

function _buildChartDatasets(canvas, points, p) {
  const ctx = canvas.getContext('2d');
  const gradTemp = ctx.createLinearGradient(0, 0, 0, 320);
  const dark = document.documentElement.dataset.theme === 'dark';
  gradTemp.addColorStop(0, dark ? 'rgba(249,115,22,0.42)' : 'rgba(249,115,22,0.18)');
  gradTemp.addColorStop(0.65, dark ? 'rgba(249,115,22,0.08)' : 'rgba(249,115,22,0.04)');
  gradTemp.addColorStop(1, 'rgba(249,115,22,0)');
  const { data: precipData, bg: precipBg } = precipDatasets(points);
  return [
    {
      type: 'line', label: 'Temperatura (°C)',
      data: points.filter(pt => pt.temp_c != null).map(pt => ({ x: pt.ts, y: pt.temp_c })),
      borderColor: p.temp, backgroundColor: gradTemp, fill: true,
      borderWidth: 3, pointRadius: 0, pointHoverRadius: 6,
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

function _baseChartOptions(p, xMin, xMax, unit, labelFn) {
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
  renderChartStatStrip(points);
}

function updateChartModel(data, model, targetDate) {
  if (!meteoChart || !targetDate) { initChart(data, model, targetDate); return; }
  const canvas = document.getElementById('meteo-chart');
  const points = buildChartPoints(data, model, targetDate);
  const p = chartPalette();
  const ds = _buildChartDatasets(canvas, points, p);
  meteoChart.data.datasets.forEach((d, i) => { d.data = ds[i].data; if (i === 2) d.backgroundColor = ds[i].backgroundColor; });
  meteoChart.update();
  renderChartStatStrip(points);
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
  const dark = document.documentElement.dataset.theme === 'dark';
  opts.plugins.tooltip = {
    backgroundColor: dark ? 'rgba(15,23,42,0.95)' : 'rgba(255,255,255,0.97)',
    titleColor:      dark ? '#94a3b8' : '#64748b',
    bodyColor:       dark ? '#e2e8f0' : '#1e293b',
    borderColor:     dark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)',
    borderWidth: 1,
    padding: 10,
    cornerRadius: 12,
    callbacks: {
      title: items => new Date(items[0].raw.x).toLocaleString('it-IT', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }),
      label: item => {
        if (item.datasetIndex === 0) return ` ${item.raw.y.toFixed(1)}°C`;
        if (item.datasetIndex === 1) return ` Umidità: ${item.raw.y.toFixed(0)}%`;
        if (item.datasetIndex === 2 && item.raw.y > 0.05) return ` Precip: ${item.raw.y.toFixed(1)} mm`;
        if (item.datasetIndex === 3) return ` Vento: ${item.raw.y.toFixed(0)} km/h`;
        return null;
      },
    },
  };
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
  radarLayers = [];
  radarFrames = [];
  radarIdx    = 0;
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
    return L.tileLayer(url, { opacity: 0, tileSize: 256, zIndex: 5, minZoom: 0, maxZoom: 7, attribution: 'RainViewer' });
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
  if (timeEl)   timeEl.textContent  = lbl;
  if (sliderEl) sliderEl.value = String(i);
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
  if (box) { box.textContent = `Radar non disponibile (${msg})`; box.classList.remove('hidden'); }
}

function buildRadarMap(locationId, host, frames) {
  if (typeof L === 'undefined') { showRadarError('libreria mappa non caricata'); return; }
  const loc = LOCATIONS.find(l => l.id === locationId);
  if (!loc)  { showRadarError('location sconosciuta'); return; }
  radarFrames = frames;
  radarMap = L.map('radar-map', {
    center: [loc.lat, loc.lon], zoom: RADAR_ZOOM,
    minZoom: 3, maxZoom: 7, zoomControl: false,
    attributionControl: true, scrollWheelZoom: false,
  });
  // Always dark basemap for radar contrast
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    minZoom: 3, maxZoom: 12, zIndex: 1,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>, &copy; <a href="https://carto.com">CARTO</a>',
  }).addTo(radarMap);

  // Sonar marker using DivIcon
  const sonarHtml = `<div style="position:relative;width:20px;height:20px">
    <div style="position:absolute;inset:0;border-radius:50%;background:#3B9A6C;opacity:0.9"></div>
    <div style="position:absolute;inset:-5px;border-radius:50%;border:2px solid #3B9A6C;animation:sonar 2s ease-out infinite;pointer-events:none"></div>
  </div>`;
  L.marker([loc.lat, loc.lon], {
    icon: L.divIcon({ className: '', html: sonarHtml, iconSize: [20, 20], iconAnchor: [10, 10] }),
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

  showEl('radar-section');
  initRadar(data.location_id);

  if (data.days.length > 0) {
    renderDayStrip(data.days, selectedDayIdx);
    renderDayDetail(day);
    showEl('forecast-section');
  }

  showEl('chart-daily-section');
  showEl('chart-weekly-section');
  initModelSwitch(data);
  initWeeklyModelSwitch(data);
  if (targetDate) initChart(data, selectedModel, targetDate);
  initWeeklyChart(data, selectedWeeklyModel);

  renderCoverage(data.coverage_empirical_30d);
}

// ── Data loading ──────────────────────────────────────────────────────────────

async function loadLocation(locId) {
  renderTabs(locId);
  selectedDayIdx      = 0;
  selectedModel       = 'guazza';
  selectedWeeklyModel = 'guazza';

  // Hide content, show skeleton
  ['hero-card','radar-section','forecast-section','chart-daily-section','chart-weekly-section','coverage-bar','error-state'].forEach(hideEl);
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
twemoji.parse(document.querySelector('header'), TWEMOJI_OPTS);
window.addEventListener('popstate', () => loadLocation(getActiveLoc()));
loadLocation(getActiveLoc());
