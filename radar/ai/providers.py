"""Interfaz de proveedores y proveedor real de Anthropic (SDK oficial)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Protocol

from radar.ai.config import AiConfig


@dataclass
class ProviderResult:
    text: str
    model: str
    stop_reason: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    request_id: str | None = None
    latency_ms: int = 0
    extra: dict = field(default_factory=dict)


class ProviderError(RuntimeError):
    """Fallo del proveedor. `retryable` indica si tiene sentido reintentar."""

    kind = "error"
    retryable = False


class ProviderTimeout(ProviderError):
    kind = "timeout"
    retryable = True


class ProviderRateLimited(ProviderError):
    kind = "limitado"
    retryable = True

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    kind = "error"
    retryable = True


class ProviderQuotaExceeded(ProviderError):
    """Cuota o saldo agotado: no se reintenta y se desactivan las llamadas por hoy."""

    kind = "cuota"


class ProviderRefusal(ProviderError):
    kind = "rechazo"


class Provider(Protocol):
    name: str
    model: str

    def complete(self, system: str, user: str, schema: dict) -> ProviderResult: ...


class AnthropicProvider:
    """Messages API con salida JSON estructurada (output_config.format) y sin herramientas."""

    name = "anthropic"

    def __init__(self, cfg: AiConfig):
        if "PYTEST_CURRENT_TEST" in os.environ and os.getenv("RADAR_AI_ALLOW_TEST_CALLS") != "1":
            raise RuntimeError("Llamadas reales a la API bloqueadas durante las pruebas.")
        import anthropic

        self._anthropic = anthropic
        self.cfg = cfg
        self.model = cfg.model
        # Reintentos propios (acotados y contados por el circuit breaker): el SDK no reintenta.
        # Credenciales: las que resuelve el SDK (ANTHROPIC_API_KEY, perfil de `ant auth login`...).
        self.client = anthropic.Anthropic(timeout=cfg.timeout_s, max_retries=0)

    def complete(self, system: str, user: str, schema: dict) -> ProviderResult:
        a = self._anthropic
        params = dict(
            model=self.model,
            max_tokens=self.cfg.max_output_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        if self.cfg.effort:
            params["output_config"]["effort"] = self.cfg.effort
        t0 = time.monotonic()
        try:
            if self.cfg.use_fallbacks:
                # Si el modelo rechaza la solicitud, la API la reintenta en el modelo de
                # respaldo recomendado según la categoría del rechazo.
                resp = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
            else:
                resp = self.client.messages.create(**params)
        except a.APITimeoutError as exc:
            raise ProviderTimeout("Tiempo de espera agotado") from exc
        except a.RateLimitError as exc:
            retry = exc.response.headers.get("retry-after") if exc.response is not None else None
            raise ProviderRateLimited("Límite de solicitudes del proveedor (429)",
                                      float(retry) if retry and retry.isdigit() else None) from exc
        except (a.AuthenticationError, a.PermissionDeniedError) as exc:
            raise ProviderError(f"Credenciales inválidas o sin permiso ({exc.status_code})") from exc
        except a.BadRequestError as exc:
            msg = str(getattr(exc, "message", "") or "")
            if "credit" in msg.lower() or "billing" in msg.lower() or "balance" in msg.lower():
                raise ProviderQuotaExceeded("Saldo o cuota del proveedor agotados") from exc
            raise ProviderError(f"Solicitud rechazada por el proveedor (400): {msg[:200]}") from exc
        except a.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ProviderUnavailable(f"Proveedor no disponible ({exc.status_code})") from exc
            raise ProviderError(f"Error del proveedor ({exc.status_code})") from exc
        except a.APIConnectionError as exc:
            raise ProviderUnavailable("Error de conexión con el proveedor") from exc
        latency = int((time.monotonic() - t0) * 1000)
        if resp.stop_reason == "refusal":
            raise ProviderRefusal("El modelo declinó analizar este contenido")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        usage = resp.usage
        return ProviderResult(
            text=text, model=resp.model, stop_reason=resp.stop_reason,
            input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            request_id=getattr(resp, "_request_id", None), latency_ms=latency,
        )


def make_one(name: str, cfg: AiConfig, transport=None) -> Provider:
    from radar.ai.openai_compat import OpenAICompatibleProvider

    if name == "anthropic":
        return AnthropicProvider(cfg)
    if name in ("groq", "fireworks"):
        return OpenAICompatibleProvider(name, cfg.timeout_s, cfg.max_output_tokens, transport)
    raise ValueError(f"Proveedor no soportado: {name}")


def make_provider(cfg: AiConfig, transport=None) -> Provider:
    """Un proveedor o una cadena con conmutación (p. ej. RADAR_AI_PROVIDER=groq,fireworks)."""
    from radar.ai.openai_compat import FailoverProvider

    from radar.ai.openai_compat import SPECS, provider_ready

    names = cfg.providers
    if not names:
        raise ValueError("No hay proveedores configurados (RADAR_AI_PROVIDER).")
    # Los proveedores sin clave o sin modelo no se llaman (no generan intentos ni consumo).
    ready = [n for n in names if n not in SPECS or provider_ready(n) is None]
    names = ready or names
    providers = [make_one(n, cfg, transport) for n in names]
    return providers[0] if len(providers) == 1 else FailoverProvider(providers)


def providers_status(cfg: AiConfig) -> list[str]:
    """Motivos por los que cada proveedor no está listo (vacío si alguno lo está)."""
    from radar.ai.openai_compat import provider_ready

    problems = []
    for name in cfg.providers:
        if name == "anthropic":
            return []  # credenciales resueltas por el SDK
        if name not in ("groq", "fireworks"):
            problems.append(f"{name}: proveedor no soportado")
            continue
        reason = provider_ready(name)
        if reason is None:
            return []
        problems.append(reason)
    return problems or ["No hay proveedores configurados"]
