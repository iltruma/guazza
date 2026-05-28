---
name: Guazza
description: ML weather forecasting dashboard for Tuscan microclimates
colors:
  surface-base: "#0C0C0C"
  surface-panel: "#131313"
  surface-raised: "#1A1A1A"
  surface-deep: "#212121"
  accent: "#6B7FD4"
  text-primary: "oklch(98% 0 0)"
  verde: "#34D399"
  giallo: "#FBBF24"
  rosso: "#F87171"
  warm: "#F97316"
  cold: "#60A5FA"
typography:
  display:
    fontFamily: "Geist, system-ui, sans-serif"
    fontSize: "clamp(3.5rem, 9vw, 5rem)"
    fontWeight: 700
    lineHeight: 0.9
    letterSpacing: "-0.025em"
  headline:
    fontFamily: "Geist, system-ui, sans-serif"
    fontSize: "clamp(1.75rem, 4vw, 2.5rem)"
    fontWeight: 700
    lineHeight: 1.05
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Geist, system-ui, sans-serif"
    fontSize: "clamp(1.6rem, 3.5vw, 2.2rem)"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.02em"
  body:
    fontFamily: "Geist, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "10px"
    fontWeight: 700
    letterSpacing: "0.12em"
  data:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 600
    letterSpacing: "normal"
    fontFeature: "tnum"
rounded:
  sm: "4px"
  md: "8px"
  lg: "12px"
  xl: "16px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
components:
  tab-active:
    backgroundColor: "rgba(107,127,212,0.10)"
    textColor: "{colors.accent}"
    rounded: "{rounded.md}"
    padding: "5px 12px"
    typography: "label"
  tab-default:
    backgroundColor: "transparent"
    textColor: "oklch(98% 0 0 / 0.48)"
    rounded: "{rounded.md}"
    padding: "5px 12px"
    typography: "label"
  pill-verde:
    backgroundColor: "rgba(52,211,153,0.10)"
    textColor: "{colors.verde}"
    rounded: "8px"
    padding: "8px 12px"
  pill-giallo:
    backgroundColor: "rgba(251,191,36,0.10)"
    textColor: "{colors.giallo}"
    rounded: "8px"
    padding: "8px 12px"
  pill-rosso:
    backgroundColor: "rgba(248,113,113,0.10)"
    textColor: "{colors.rosso}"
    rounded: "8px"
    padding: "8px 12px"
  card:
    backgroundColor: "{colors.surface-panel}"
    rounded: "{rounded.xl}"
    padding: "20px 24px"
---

# Design System: Guazza

## 1. Overview

**Creative North Star: "Carbone e Iride"**

Guazza è uno strumento scientifico personale. Il design lo rispecchia: superfici carbone neutre senza alcun hue, che non imitano né la pietra né il cielo ma il materiale grezzo degli strumenti di misura. Un unico accento iris, blu-indaco profondo, si riserva per i dati vivi e lo stato attivo. L'oscurità non è "dark mode": è il buio di chi guarda fuori dalla finestra alle 7 di mattina per decidere se prendere il motorino.

La doppia velocità è il principio organizzativo centrale. Ogni sezione funziona a 3 secondi (indicatori DLE a colori, temperatura hero, CI bar) e a 30 secondi (tabella NWP, confronto modelli, coverage empirica). La gerarchia visiva non è decorativa: il dato operativo è sempre più grande del dato tecnico, sempre.

L'interfaccia rifiuta esplicitamente tre categorie: il cheerful consumer-weather con icone a colori e pubblicità, il crypto-dark con neon e glow eccessivo, il SaaS analytics con hero-metric grid identici e blu navy piatto. Ogni scelta di colore, spaziatura e motion qui è una presa di posizione contro quei pattern.

**Key Characteristics:**
- Carbone puro con 4 livelli di superficie, zero hue nelle superfici
- Testo bianco puro `oklch(98% 0 0)` — massimo contrasto, nessuna tinta
- Accento iris `#6B7FD4` riservato agli elementi vivi (LIVE pulse, tab attivo, CI bar, lead badge)
- Doppia tipografia: Geist per display e titoli, JetBrains Mono per tutti i dati numerici
- Elevazione tonal senza ombre — la profondità viene dalla differenza di superficie
- Grain overlay (`opacity: 0.022`, `mix-blend-mode: overlay`) che rompe la piattezza digitale
- Animazioni con scopo preciso: CI expand, fade-up, sonar radar — nessuna ornamentale

## 2. Colors: Carbone e Iride

Quattro livelli di carbone, un accento iris, cinque segnali semantici.

### Primary
- **Iris** (`#6B7FD4`): l'unico colore saturato del sistema. Usato esclusivamente per elementi che comunicano stato attivo o dati vivi: tab selezionato, pulse dot LIVE, CI bar range 80%, mediana CI bar, radar sonar ring, slider radar, lead badge `+Nh`. Il suo background tint è `rgba(107,127,212,0.10)`, il suo bordo semitrasparente è `rgba(107,127,212,0.35)`.

### Semantic
- **Verde Operativo** (`#34D399`): segnale "sicuro, procedi". Esclusivo nei pill indicatore DLE, valori qualità aria fascia bassa, dot strip card verde. Background tint `rgba(52,211,153,0.10)`, bordo `rgba(52,211,153,0.22)`.
- **Giallo Cautela** (`#FBBF24`): segnale intermedio. Solo nei pill DLE, AQ, dot strip card giallo.
- **Rosso Allerta** (`#F87171`): segnale critico. Pill DLE, valori AQ alti, error state, delta NWP wet. Background `rgba(248,113,113,0.10)`.
- **Warm** (`#F97316`): delta temperatura positivo nella NWP table, classe `g-metric__value--warm` sul Tmax bento. Background `rgba(249,115,22,0.08)`.
- **Cold** (`#60A5FA`): delta temperatura negativo nella NWP table, classe `g-metric__value--cool` sul Tmin bento. Background `rgba(96,165,250,0.08)`.

### Neutral
- **Carbone Base** (`#0C0C0C`): fondo del body. Puro carbone senza hue. Punto zero della scala.
- **Carbone Panel** (`#131313`): superficie di card, hero band, collapsibili. Livello 1.
- **Carbone Rialzato** (`#1A1A1A`): hover state dei day card, riga Guazza nella NWP table. Livello 2.
- **Carbone Profondo** (`#212121`): hover della riga Guazza in NWP, superficie più elevata. Livello 3.
- **Testo Primario** (`oklch(98% 0 0)`): bianco puro. Tutti i valori importanti, titoli, temperatura hero.
- **Testo Secondario** (`oklch(98% 0 0 / 0.62)`): testo descrittivo, date, valori NWP non-Guazza.
- **Testo Terziario** (`oklch(98% 0 0 / 0.48)`): label uppercase mono, kicker, unità, metadati.
- **Bordo Sottile** (`rgba(255,255,255,0.07)`): divisori tra celle, bordi card.
- **Bordo Medio** (`rgba(255,255,255,0.10)`): bordo su elementi in stato active/hover.

### Named Rules
**The One Accent Rule.** L'iris (`#6B7FD4`) appare su meno del 15% di qualsiasi schermata. Usarlo per ornamento svuota il sistema.

**The Carbone Progression Rule.** I quattro livelli si usano in ordine stretto: surface-0 → surface-1 → surface-2 → surface-3. Non saltare livelli. Non inventare un quinto senza aggiornare la scala.

**The Semantic Separation Rule.** I colori warm/cold esistono solo per i delta di temperatura NWP e i valori Tmax/Tmin nel bento. Verde/giallo/rosso esistono solo per i segnali DLE e AQ. L'iris esiste solo per lo stato attivo. Nessuna contaminazione tra ruoli.

## 3. Typography

**Display Font:** Geist (con `system-ui, sans-serif` come fallback)
**Data Font:** JetBrains Mono (con `ui-monospace, monospace` come fallback)

**Character:** Il pairing è funzionale, non estetico. Geist porta i numeri grandi e i titoli con personalità moderna. JetBrains Mono prende tutto il resto: ogni valore numerico, ogni label uppercase, ogni timestamp. La distinzione è assoluta: se è un dato, è mono.

### Hierarchy
- **Display** (700, `clamp(3.5rem, 9vw, 5rem)`, line-height 0.9, tracking -0.025em): temperatura hero corrente. Solo qui.
- **Headline** (700, `clamp(1.75rem, 4vw, 2.5rem)`, line-height 1.05, tracking -0.02em): titolo del day detail (nome giorno).
- **Title** (700, `clamp(1.6rem, 3.5vw, 2.2rem)`, line-height 1, tracking -0.02em, tabular-nums): valori metrici nel bento (Tmin, Tmax, precip).
- **Body** (400, 14px, line-height 1.5): testo descrittivo, condizione meteo testuale. Max 65ch.
- **Label** (JetBrains Mono, 10-11px, weight 700, uppercase, tracking 0.10-0.14em, `text-3`): kicker di sezione, intestazioni colonna, unità.
- **Data** (JetBrains Mono, 12-13px, weight 600, tabular-nums): valori numerici NWP table, stat strip, hero stats.

### Named Rules
**The Mono-for-Numbers Rule.** Se il valore è numerico, è JetBrains Mono con `font-variant-numeric: tabular-nums`. Nessuna eccezione.

## 4. Elevation

Questo sistema è flat per principio. La profondità viene interamente dalla progressione di superficie (`surface-0` → `surface-3`) e dai bordi semitrasparenti. Nessuna `box-shadow` sui componenti a riposo.

Due sole eccezioni funzionali: il chart tooltip e l'indicator tooltip usano ombra profonda `0 8px 32px rgba(8,8,8,0.6)` perché devono galleggiare sopra al contenuto. L'ombra è scura, profonda, senza spread.

Il grain overlay sul body (`opacity: 0.022`, `mix-blend-mode: overlay`) rompe la piattezza digitale del carbone puro.

### Named Rules
**The Flat-By-Default Rule.** Le superfici non hanno ombra a riposo. Hover non aggiunge shadow: aggiunge `surface-raised` come background.

## 5. Components

### Navigation Tabs
- **Shape:** 8px (`--r-md`)
- **Default:** Background trasparente, testo `text-3`, bordo trasparente
- **Hover:** Background `rgba(255,255,255,0.05)`, testo `text-2`
- **Active:** Background `rgba(107,127,212,0.10)`, bordo `rgba(107,127,212,0.35)`, testo accent, weight 600
- **Tipografia:** JetBrains Mono 12px, weight 500 default → 600 active
- **Touch target:** min-height 36px su dispositivi touch

### Indicator Pills (DLE Verdict)
- **Shape:** 8px radius
- **Verde/Giallo/Rosso:** Background tinted 10% + bordo 22% del colore segnale. Verdict text usa il colore pieno.
- **Hover:** `opacity: 0.82`
- **Press:** `scale(0.95)`
- **Width:** Full-width nel contesto DLE grid

### Day Strip Cards
- **Shape:** 12px (`--r-lg`)
- **Default:** `surface-1` background, `border-1` border
- **Hover:** `surface-2` background, `translateY(-2px)` lift
- **Active:** `surface-2` + `border-2` + linea accent 2px in basso, centrata, 28px
- **Dots indicatori:** riga di pallini 8px in fondo alla card, uno per indicatore, colorati verde/giallo/rosso

### CI Bar (Confidence Interval)
- **Track:** 6px, `rgba(255,255,255,0.07)`, border-radius 3px
- **Range 90%:** `rgba(255,255,255,0.10)`
- **Range 80%:** `rgba(107,127,212,0.35)` — accent iris semitrasparente
- **Median dot:** 10px circle, border 2px accent su `surface-1`
- **Animazione:** `ci-expand` 400ms ease-out, `ci-pop` 400ms spring

### NWP Comparison Table
- **Header:** Mono 10px uppercase, tracking 0.10em, `text-3`, border-bottom
- **Row default:** padding 9px 12px, `text-2`, border-top, hover `rgba(255,255,255,0.025)`
- **Row Guazza:** Background `surface-2`, `text-1`, weight 600, nome in accent
- **Delta chips:** 4px radius, 10px — warm `#F97316`, cold `#60A5FA`, wet rosso, dry verde
- **Mobile:** Prima colonna sticky left

### Metric Bento (Tmax / Tmin / Precip)
- **Tmax:** `g-metric__value--warm` → `color: var(--warm)` (#F97316)
- **Tmin:** `g-metric__value--cool` → `color: var(--cold)` (#60A5FA)
- **Precip:** `g-metric__value` → `text-1`

### Lead Badge (`+Nh`)
- Background `rgba(107,127,212,0.10)`, bordo `rgba(107,127,212,0.35)`, testo accent
- Indica l'orizzonte di previsione selezionato

### Collapsible Panels (Radar, AQ)
- **Shape:** 16px (`--r-xl`), overflow hidden
- **Header hover:** `rgba(255,255,255,0.02)`
- **Chevron:** Rotazione 180° su `details[open]`, `var(--dur-base)` ease-out

### Chart Tooltip
- **Background:** `rgba(19,19,19,0.97)` — surface-1 quasi opaco
- **Shadow:** `0 8px 32px rgba(8,8,8,0.6)`
- **Blur:** `backdrop-filter: blur(12px)`

## 6. Do's and Don'ts

### Do:
- **Do** usare JetBrains Mono con `font-variant-numeric: tabular-nums` per ogni valore numerico.
- **Do** mantenere l'accento iris `#6B7FD4` riservato a stati vivi e dati in tempo reale.
- **Do** comunicare la profondità cambiando livello di superficie, non aggiungendo shadow su hover.
- **Do** usare `clamp()` per le dimensioni dei display numerici.
- **Do** animare con scopo: CI expand, fade-up, sonar radar. Animazioni ornamentali vanno rimosse.
- **Do** mostrare l'incertezza: CI bar, lead time badge, coverage empirica sono obbligatori.
- **Do** rispettare `prefers-reduced-motion`: tutte le animazioni a `0.01ms` via media query.

### Don't:
- **Don't** usare `border-left` o `border-right` colorato come accent. Le uniche linee accent sono orizzontali.
- **Don't** usare gradient text (`background-clip: text`).
- **Don't** usare warm/cold per elementi non-temperatura e non-delta-NWP.
- **Don't** usare verde/giallo/rosso fuori dai pill DLE, AQ, e dot strip. Sono segnali operativi riservati.
- **Don't** aggiungere un quinto livello di superficie senza aggiornare la scala `--surface-*`.
- **Don't** usare Geist per valori numerici tabellari.
- **Don't** colorare elementi neutri con l'iris. Se è accent, è perché quel dato è vivo o quello stato è attivo.
