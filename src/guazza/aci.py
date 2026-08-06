"""Adaptive Conformal Inference (ACI) per l'aggiustamento online degli intervalli.

Implementa l'algoritmo di Gibbs & Candès (2021): aggiusta alpha_t ad ogni feedback
di copertura per mantenere la garanzia long-run di copertura = 1-α anche sotto
distribution shift (drift climatico, cambio modello NWP).

Usato da guazza-forecast: dopo ogni prediction, carica lo state ACI dal DB,
aggiusta i bound CI, e aggiorna lo state per il prossimo run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from guazza.storage import DuckDBClient

# Learning rate ACI (γ): D-019 specifica 0.005. Il drift da correggere è
# stagionale (scala mesi), non giornaliero. γ=0.02 sarebbe 4× troppo aggressivo
# — inseguirebbe il rumore meteo invece del trend di calibrazione.
ACI_LEARNING_RATE: float = 0.005

# Fattore di correzione ACI clampato a [MIN, MAX] per evitare bande patologiche
# quando alpha_t si avvicina al clamp eps=0.01 (drift prolungato). Senza clamp,
# f = alpha_target/alpha_t può arrivare a 10+ → banda ±30°C inutili.
# 0.5..2.0 = la correzione può stringere al massimo del 50% o allargare al massimo
# del 100% rispetto al CQR baseline. Oltre è rumore.
ACI_CORRECTION_FACTOR_MIN: float = 0.5
ACI_CORRECTION_FACTOR_MAX: float = 2.0

# Cold start: prime N osservazioni prima che ACI sia affidabile. CQR statico
# (calcolato da train_all) è più conservativo e ci protegge. Dopo N obs,
# l'alpha_t corrente è già stabile.
ACI_COLD_START_N: int = 30


class AdaptiveConformalizer:
    """ACI (Gibbs & Candès 2021) per singolo livello α.

    Aggiusta `alpha_t` online ad ogni feedback di copertura, mantenendo la
    garanzia long-run marginal di copertura = 1-α anche sotto distribution
    shift (drift climatico, cambio modello). Il CQR statico fallisce in questi
    casi (vedi KI-023 — drift già in atto sui fold recenti).

    Algoritmo: alpha_{t+1} = clip(alpha_t + γ * (α_target − err_t), ε, 1−ε)
    dove err_t = 1 se miscoverage al tempo t, 0 altrimenti.

    Args:
        alpha_target: livello target (es. 0.10 per CI 90%, 0.20 per CI 80%).
        learning_rate: γ. Default ACI_LEARNING_RATE=0.005 (D-019). Il drift da
            correggere è stagionale (mesi); γ più alto inseguirebbe rumore giornaliero.
        eps: clamping per evitare alpha degeneri (0 o 1).
    """

    def __init__(
        self,
        alpha_target: float,
        learning_rate: float = ACI_LEARNING_RATE,
        eps: float = 0.01,
    ) -> None:
        self.alpha_target = alpha_target
        self.gamma = learning_rate
        self.eps = eps
        self.alpha_t = alpha_target
        self.n_updates = 0
        self._err_sum = 0  # somma err_t per diagnostics

    def update(self, covered: bool) -> float:
        """Registra feedback di copertura al tempo t e aggiorna alpha_t.

        Returns:
            Nuovo alpha_t (per ispezione / logging).
        """
        err = 0 if covered else 1
        self._err_sum += err
        self.alpha_t = max(
            self.eps,
            min(1.0 - self.eps, self.alpha_t + self.gamma * (self.alpha_target - err)),
        )
        self.n_updates += 1
        return self.alpha_t

    def correct(self, offset: float) -> float:
        """Restituisce l'offset CQR-equivalente per il livello alpha_t corrente.

        `offset` è l'offset CQR calcolato al baseline (alpha_target). ACI lo
        aggiusta: alpha_t più alto → CI più stretto (offset più piccolo),
        alpha_t più basso → CI più largo. Approssimazione lineare attorno
        al baseline.

        Nota: correct() è usato solo nei test sintetici (test_models.py).
        In produzione il mapping alpha_t→CI passa da apply_aci_correction
        (scaling moltiplicativo del half-width CQR, più robusto).
        """
        delta_alpha = self.alpha_target - self.alpha_t
        return offset * (1.0 + 5.0 * delta_alpha)

    def to_dict(self) -> dict[str, float | int]:
        return {
            "alpha_target": self.alpha_target,
            "alpha_t": self.alpha_t,
            "n_updates": self.n_updates,
            "err_rate": self._err_sum / self.n_updates if self.n_updates else 0.0,
        }

    @classmethod
    def from_state(
        cls,
        alpha_target: float,
        alpha_t: float,
        n_updates: int,
        err_sum: int,
        learning_rate: float = ACI_LEARNING_RATE,
        eps: float = 0.01,
    ) -> AdaptiveConformalizer:
        """Ricostruisce ACI da state persistito (DuckDB o dizionario).

        Usato dopo `db.get_aci_state()` per ricaricare lo state al startup
        di guazza-forecast. Se n_updates == 0, alpha_t == alpha_target (cold start).
        """
        aci = cls(alpha_target=alpha_target, learning_rate=learning_rate, eps=eps)
        aci.alpha_t = max(eps, min(1.0 - eps, alpha_t))
        aci.n_updates = n_updates
        aci._err_sum = int(err_sum)
        return aci

    @property
    def err_rate(self) -> float:
        return self._err_sum / self.n_updates if self.n_updates else 0.0

    @property
    def err_sum(self) -> int:
        return self._err_sum


def get_aci_pair(
    db: DuckDBClient,
    target: str,
    lead_bucket: str,
    learning_rate: float = ACI_LEARNING_RATE,
) -> tuple[AdaptiveConformalizer, AdaptiveConformalizer]:
    """Carica (o crea) la coppia ACI per (target, lead_bucket) ai livelli 80%/90%.

    Returns:
        (aci_80, aci_90): istanze pronte per update/correct. Se assenti in DB,
        hanno alpha_t == alpha_target (cold start, CQR statico farà da fallback
        pratico finché n_updates < ACI_COLD_START_N).
    """
    state = db.get_aci_state(target, lead_bucket)
    if state is None:
        return (
            AdaptiveConformalizer(alpha_target=0.20, learning_rate=learning_rate),
            AdaptiveConformalizer(alpha_target=0.10, learning_rate=learning_rate),
        )
    return (
        AdaptiveConformalizer.from_state(
            alpha_target=0.20,
            alpha_t=state["alpha_t_80"],
            n_updates=state["n_updates"],
            err_sum=state["err_sum_80"],
            learning_rate=learning_rate,
        ),
        AdaptiveConformalizer.from_state(
            alpha_target=0.10,
            alpha_t=state["alpha_t_90"],
            n_updates=state["n_updates"],
            err_sum=state["err_sum_90"],
            learning_rate=learning_rate,
        ),
    )


def apply_aci_correction(
    ci80_lo: float,
    ci80_hi: float,
    ci90_lo: float,
    ci90_hi: float,
    aci_80: AdaptiveConformalizer,
    aci_90: AdaptiveConformalizer,
) -> tuple[float, float, float, float, str]:
    """Applica la correzione ACI ai bound CI. Restituisce (lo80, hi80, lo90, hi90, source).

    source ∈ {"aci", "cqr_static"} — "cqr_static" se uno dei due ACI è in cold start
    (n_updates < ACI_COLD_START_N), segnalato al logger upstream per diagnostica.

    Logica: se ACI è warm, riscala i bound CQR con fattore alpha_target/alpha_t.
    Il fattore è clampato a [MIN_FACTOR, MAX_FACTOR] per evitare correzioni
    patologiche quando alpha_t si avvicina a 0 (drift prolungato). Senza clamp,
    f può arrivare a 10+ e produrre bande inutilmente larghe (es. ±30°C).
    """
    if aci_80.n_updates < ACI_COLD_START_N or aci_90.n_updates < ACI_COLD_START_N:
        return ci80_lo, ci80_hi, ci90_lo, ci90_hi, "cqr_static"

    # Fattore di scala ACI: alpha_target / alpha_t. Clampato a [MIN, MAX] per
    # evitare bande patologiche quando alpha_t si avvicina al clamp eps=0.01.
    f80 = max(ACI_CORRECTION_FACTOR_MIN, min(ACI_CORRECTION_FACTOR_MAX,
                                            aci_80.alpha_target / aci_80.alpha_t))
    f90 = max(ACI_CORRECTION_FACTOR_MIN, min(ACI_CORRECTION_FACTOR_MAX,
                                            aci_90.alpha_target / aci_90.alpha_t))

    w80 = (ci80_hi - ci80_lo) / 2
    w90 = (ci90_hi - ci90_lo) / 2
    c80 = (ci80_hi + ci80_lo) / 2
    c90 = (ci90_hi + ci90_lo) / 2
    return (
        c80 - w80 * f80, c80 + w80 * f80,
        c90 - w90 * f90, c90 + w90 * f90,
        "aci",
    )
