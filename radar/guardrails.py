"""Sistema centralizado de Guardrails de Radar de Titulares.

Implementa y documenta las restricciones epistemológicas, de seguridad, de red,
de anti-alucinación y de costos del sistema.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from radar.net import UnsafeURLError, is_public_ip, validate_url

log = logging.getLogger("radar.guardrails")

_DELIM_RE = re.compile(r"</?\s*nota_[ab]\s*>", re.I)
_INJECTION_SUSPICION_RE = re.compile(
    r"(ignora\s+(todas\s+)?las\s+instrucciones|system\s+prompt|olvida\s+lo\s+anterior|"
    r"responde\s+unicamente\s+diciendo|bypass\s+safety|jailbreak)",
    re.I,
)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = text.replace("“", '"').replace("”", '"').replace("«", '"').replace("»", '"')
    text = text.replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class GuardrailResult:
    passed: bool
    reason: str | None = None
    flags: tuple[str, ...] = ()


class ContentGuardrail:
    """Protección contra inyecciones de prompt y escape de delimitadores."""

    @staticmethod
    def sanitize_untrusted_text(text: str | None) -> str:
        """Elimina etiquetas que intenten cerrar o abrir los bloques <nota_A> / <nota_B>."""
        if not text:
            return ""
        return _DELIM_RE.sub("[etiqueta eliminada]", text).strip()

    @staticmethod
    def detect_suspicious_instructions(text: str | None) -> list[str]:
        """Detecta patrones sospechosos de inyección dentro del texto de artículos."""
        if not text:
            return []
        matches = _INJECTION_SUSPICION_RE.findall(text)
        if matches:
            return ["Texto con posibles instrucciones dirigidas al modelo (ignorado como contenido no confiable)"]
        return []


class NetworkGuardrail:
    """Protección contra SSRF, URLs inválidas e IPs privadas."""

    @staticmethod
    def validate_url(url: str, allowed_domains: list[str] | None = None) -> GuardrailResult:
        domains = allowed_domains or [
            "perfil.com", "eldestapeweb.com", "infobae.com", "lanacion.com.ar", "clarin.com", "pagina12.com.ar"
        ]
        try:
            validate_url(url, allowed_domains=domains)
            return GuardrailResult(passed=True)
        except Exception as exc:
            return GuardrailResult(passed=False, reason=str(exc))


class EvidenceGuardrail:
    """Verificación estricta de citas para evitar alucinaciones en modelos de IA."""

    @staticmethod
    def verify_quote(quote: str, source_text: str, min_chars: int = 3) -> bool:
        """Verifica que un fragmento exista textualmente en el texto fuente normalizado."""
        norm_q = _norm(quote)
        if len(norm_q) < min_chars:
            return False
        return norm_q in _norm(source_text)

    @staticmethod
    def audit_ai_response(data: dict[str, Any], sources: dict[str, str]) -> list[str]:
        """Audita todas las citas devueltas por el modelo. Devuelve lista de violaciones."""
        errors: list[str] = []
        normalized = {k: _norm(v) for k, v in sources.items()}

        # Fragmentos de evidencia general
        frags = data.get("fragmentos_de_evidencia", [])
        if not frags:
            errors.append("Debe incluir al menos un fragmento de evidencia")
        for i, f in enumerate(frags):
            side, txt = f.get("articulo"), f.get("texto", "")
            if not EvidenceGuardrail.verify_quote(txt, normalized.get(side, "")):
                errors.append(f"fragmentos_de_evidencia[{i}]: cita no encontrada en nota {side}")

        # Afirmaciones principales
        for i, c in enumerate(data.get("afirmaciones_principales", [])):
            side, frag = c.get("articulo"), c.get("fragmento", "")
            if not EvidenceGuardrail.verify_quote(frag, normalized.get(side, "")):
                errors.append(f"afirmaciones_principales[{i}]: fragmento no encontrado en nota {side}")

        # Atribuciones de responsabilidad
        for i, r in enumerate(data.get("atribuciones_de_responsabilidad", [])):
            side, frag = r.get("articulo"), r.get("fragmento", "")
            if not EvidenceGuardrail.verify_quote(frag, normalized.get(side, "")):
                errors.append(f"atribuciones_de_responsabilidad[{i}]: fragmento no encontrado en nota {side}")

        return errors


class EpistemicGuardrail:
    """Guardrails de rigor analítico y metodológico."""

    @staticmethod
    def check_temporal_assertion(first_seen_delta_sec: float | None,
                                  published_delta_sec: float | None) -> list[str]:
        """Garantiza que la detección o publicación temporal no se interprete como causalidad."""
        notes = []
        if first_seen_delta_sec is not None and published_delta_sec is None:
            notes.append("Orden de detección no equivale a orden de publicación ni autoría de la primicia.")
        return notes

    @staticmethod
    def check_lexical_assertion(score: float) -> str:
        """Garantiza que un puntaje léxico alto no sea presentado como prueba de coordinación."""
        return f"Puntaje léxico ({score:.2f}): mide superposición de palabras, no coordinación intencional."

    @staticmethod
    def detect_polarity_conflict(text_a: str, text_b: str) -> list[str]:
        """Detecta frases de polaridad opuesta como 'aumentó' vs 'no aumentó' o 'dejó de aumentar'."""
        a, b = _norm(text_a), _norm(text_b)
        negations = ["no ", "dejo de ", "dejó de ", "sin ", "nunca ", "falso "]
        has_neg_a = any(n in a for n in negations)
        has_neg_b = any(n in b for n in negations)
        if has_neg_a != has_neg_b:
            return ["Divergencia de polaridad detectada: una nota incluye negación o cese y la otra no."]
        return []
