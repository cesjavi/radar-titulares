"""Configuración por entorno. Los secretos nunca se muestran ni se registran."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Proveedores en orden de uso (cadena con conmutación ante fallos).
DEFAULT_PROVIDERS = "groq,fireworks"
SUPPORTED_PROVIDERS = ("groq", "fireworks", "anthropic")
# Solo Anthropic tiene modelo por defecto (ID oficial vigente). Groq y Fireworks requieren
# RADAR_GROQ_MODEL / RADAR_FIREWORKS_MODEL: no se presupone ningún nombre de modelo.
DEFAULT_MODEL = "claude-opus-5-5"


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "si", "sí", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _float(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


@dataclass(frozen=True)
class AiConfig:
    enabled: bool
    provider: str  # lista separada por comas, en orden
    model: str  # modelo de Anthropic (RADAR_AI_MODEL)
    effort: str | None
    use_fallbacks: bool
    timeout_s: float
    max_retries: int
    max_input_chars: int
    max_output_tokens: int
    max_per_run: int
    daily_max_requests: int
    daily_max_tokens: int
    # Tarifas (USD por millón de tokens) solo si el usuario las configura.
    price_input_mtok: float | None
    price_output_mtok: float | None
    daily_budget_usd: float | None
    breaker_failures: int
    breaker_minutes: int

    @property
    def providers(self) -> list[str]:
        return [p for p in (x.strip() for x in self.provider.split(",")) if p]

    @property
    def models_label(self) -> str:
        from radar.ai.openai_compat import provider_model

        parts = []
        for p in self.providers:
            if p == "anthropic":
                parts.append(f"anthropic:{self.model}")
            elif p in ("groq", "fireworks"):
                parts.append(f"{p}:{provider_model(p) or '(sin modelo)'}")
            else:
                parts.append(f"{p}:(no soportado)")
        return ", ".join(parts)

    @property
    def has_prices(self) -> bool:
        return self.price_input_mtok is not None and self.price_output_mtok is not None


def load_config() -> AiConfig:
    from dotenv import load_dotenv

    from radar.config import BASE_DIR

    if not os.getenv("RADAR_SKIP_DOTENV"):
        load_dotenv(BASE_DIR / ".env", override=False)  # sin pisar variables ya definidas
    effort = os.getenv("RADAR_AI_EFFORT", "medium").strip().lower()
    return AiConfig(
        enabled=_bool("RADAR_AI_ENABLED"),
        provider=os.getenv("RADAR_AI_PROVIDER", DEFAULT_PROVIDERS).strip().lower() or DEFAULT_PROVIDERS,
        model=os.getenv("RADAR_AI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        effort=effort if effort in {"low", "medium", "high", "xhigh", "max"} else None,
        use_fallbacks=_bool("RADAR_AI_FALLBACKS", True),
        timeout_s=float(_int("RADAR_AI_TIMEOUT", 120)),
        max_retries=min(_int("RADAR_AI_MAX_RETRIES", 2), 5),
        max_input_chars=_int("RADAR_AI_MAX_INPUT_CHARS", 12000),
        max_output_tokens=_int("RADAR_AI_MAX_OUTPUT_TOKENS", 8192),
        max_per_run=_int("RADAR_AI_MAX_PER_RUN", 10),
        daily_max_requests=_int("RADAR_AI_DAILY_MAX_REQUESTS", 50),
        daily_max_tokens=_int("RADAR_AI_DAILY_MAX_TOKENS", 300000),
        price_input_mtok=_float("RADAR_AI_PRICE_INPUT_PER_MTOK"),
        price_output_mtok=_float("RADAR_AI_PRICE_OUTPUT_PER_MTOK"),
        daily_budget_usd=_float("RADAR_AI_DAILY_BUDGET_USD"),
        breaker_failures=max(1, _int("RADAR_AI_BREAKER_FAILURES", 3)),
        breaker_minutes=max(1, _int("RADAR_AI_BREAKER_MINUTES", 30)),
    )
