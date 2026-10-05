"""Proveedores con API de chat compatible con OpenAI: Groq y Fireworks (HTTP con httpx).

Endpoints, autenticación y formato de salida según la documentación oficial (2026-10-04):
- Groq:      POST https://api.groq.com/openai/v1/chat/completions
- Fireworks: POST https://api.fireworks.ai/inference/v1/chat/completions
Ambos: `Authorization: Bearer <clave>` y `response_format` de tipo `json_schema`
({"name", "schema"}; Groq además admite "strict" en los modelos que lo soportan).

No se definen modelos por defecto: se eligen con RADAR_GROQ_MODEL / RADAR_FIREWORKS_MODEL
según el catálogo vigente de cada proveedor.
"""

from __future__ import annotations

import os
import time

import httpx

from radar.ai.providers import (
    ProviderError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderRefusal,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
)

SPECS = {
    "groq": {"url": "https://api.groq.com/openai/v1/chat/completions",
             "key_env": "GROQ_API_KEY", "model_env": "RADAR_GROQ_MODEL",
             "strict_env": "RADAR_GROQ_STRICT", "strict_default": True},
    "fireworks": {"url": "https://api.fireworks.ai/inference/v1/chat/completions",
                  "key_env": "FIREWORKS_API_KEY", "model_env": "RADAR_FIREWORKS_MODEL",
                  "strict_env": None, "strict_default": False},
}

_QUOTA_HINTS = ("per day", "tokens per day", "requests per day", "quota", "insufficient",
                "credit", "billing", "balance", "payment")


class ProviderConfigError(ProviderError):
    """Falta configuración (clave o modelo). No se reintenta."""


def provider_model(name: str) -> str | None:
    spec = SPECS[name]
    value = os.getenv(spec["model_env"], "").strip()
    return value or None


def provider_ready(name: str) -> str | None:
    """None si el proveedor está listo; si no, el motivo (sin revelar secretos)."""
    spec = SPECS[name]
    if not os.getenv(spec["key_env"], "").strip():
        return f"{name}: falta {spec['key_env']}"
    if not provider_model(name):
        return f"{name}: falta {spec['model_env']} (elegí un modelo del catálogo del proveedor)"
    return None


class OpenAICompatibleProvider:
    def __init__(self, name: str, timeout_s: float, max_output_tokens: int,
                 transport: httpx.BaseTransport | None = None):
        if name not in SPECS:
            raise ValueError(f"Proveedor no soportado: {name}")
        if transport is None and "PYTEST_CURRENT_TEST" in os.environ and \
                os.getenv("RADAR_AI_ALLOW_TEST_CALLS") != "1":
            raise RuntimeError("Llamadas reales a la API bloqueadas durante las pruebas.")
        self.name = name
        self.spec = SPECS[name]
        self.model = provider_model(name) or ""
        self.max_output_tokens = max_output_tokens
        self._key = os.getenv(self.spec["key_env"], "").strip()
        strict_env = self.spec["strict_env"]
        raw = os.getenv(strict_env, "").strip().lower() if strict_env else ""
        self.strict = (raw in {"1", "true", "si", "sí", "yes"}) if raw else self.spec["strict_default"]
        self.client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0), transport=transport)

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def complete(self, system: str, user: str, schema: dict) -> ProviderResult:
        reason = provider_ready(self.name)
        if reason:
            raise ProviderConfigError(reason)
        json_schema = {"name": "comparacion_de_notas", "schema": schema}
        if self.strict:
            json_schema["strict"] = True
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": json_schema},
            "max_tokens": self.max_output_tokens,
            "temperature": 0,
        }
        t0 = time.monotonic()
        try:
            resp = self.client.post(self.spec["url"], json=body,
                                    headers={"Authorization": f"Bearer {self._key}",
                                             "Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"{self.name}: tiempo de espera agotado") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{self.name}: error de conexión") from exc
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            self._raise_for(resp)
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name}: respuesta con formato inesperado") from exc
        if message.get("refusal"):
            raise ProviderRefusal(f"{self.name}: el modelo declinó el análisis")
        usage = data.get("usage") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        return ProviderResult(
            text=message.get("content") or "",
            model=data.get("model") or self.model,
            stop_reason=choice.get("finish_reason"),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cache_read_tokens=int(cached),
            request_id=resp.headers.get("x-request-id") or data.get("id"),
            latency_ms=latency,
            extra={"provider": self.name},
        )

    def _raise_for(self, resp: httpx.Response) -> None:
        try:
            err = resp.json().get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
        except ValueError:
            msg = resp.text
        msg = self._redact((msg or "")[:300])
        lower = msg.lower()
        status = resp.status_code
        if status == 429:
            if any(h in lower for h in _QUOTA_HINTS):
                raise ProviderQuotaExceeded(f"{self.name}: cuota agotada (429): {msg}")
            retry = resp.headers.get("retry-after")
            try:
                retry_s = float(retry) if retry else None
            except ValueError:
                retry_s = None
            raise ProviderRateLimited(f"{self.name}: límite de solicitudes (429)", retry_s)
        if status == 402 or any(h in lower for h in ("insufficient", "credit", "billing", "balance")):
            raise ProviderQuotaExceeded(f"{self.name}: saldo o cuota agotados ({status})")
        if status in (401, 403):
            raise ProviderConfigError(f"{self.name}: credenciales inválidas o sin permiso ({status})")
        if status >= 500:
            raise ProviderUnavailable(f"{self.name}: no disponible ({status})")
        raise ProviderError(f"{self.name}: solicitud rechazada ({status}): {msg}")


class FailoverProvider:
    """Prueba los proveedores en orden: si uno falla (límite, cuota, caída, rechazo o
    configuración), sigue con el siguiente. Devuelve el resultado del primero que responda."""

    def __init__(self, providers: list):
        self.providers = providers
        self.name = ",".join(p.name for p in providers)
        self.model = ",".join(p.model or "?" for p in providers)
        self.failures: list[tuple[str, str, str]] = []

    def complete(self, system: str, user: str, schema: dict) -> ProviderResult:
        self.failures = []
        last: ProviderError | None = None
        for p in self.providers:
            try:
                result = p.complete(system, user, schema)
                result.extra = dict(result.extra, provider=p.name, fallos_previos=list(self.failures))
                return result
            except ProviderError as exc:
                self.failures.append((p.name, exc.kind, str(exc)))
                last = exc
        assert last is not None
        last.failures = list(self.failures)  # type: ignore[attr-defined]
        raise last
