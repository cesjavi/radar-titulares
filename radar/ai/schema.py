"""Esquema de salida (JSON Schema para el proveedor) y validación estricta propia."""

from __future__ import annotations

import json
import re
import unicodedata

TRI = ["si", "no", "incierto"]
ENFOQUE = ["similar", "diferente", "opuesto", "incierto"]
CLAIM_TYPES = ["hecho", "opinion", "atribucion_causal", "generalizacion", "cita_de_tercero"]
SIDES = ["A", "B"]
TONO = ["favorable", "critico", "neutral", "incierto"]
TONO_CON_CITA = ("favorable", "critico")


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": required or list(props), "additionalProperties": False}


_STR = {"type": "string"}
_FRAG = _obj({"articulo": {"type": "string", "enum": SIDES}, "texto": _STR})


def tono_schema(side: dict) -> dict:
    """Tono de cada nota hacia el sujeto central. Lectura automática, sin verificar."""
    return {"type": "array", "items": _obj({
        "articulo": side, "sujeto": _STR,
        "valoracion": {"type": "string", "enum": TONO}, "fragmento": _STR})}


def tone_quotes(data: dict) -> list[tuple[str, str, str]]:
    """Fragmentos del tono a verificar en el texto; los favorables/críticos exigen cita."""
    out = []
    for i, t in enumerate(data.get("tono_por_nota", [])):
        if t["valoracion"] in TONO_CON_CITA or t["fragmento"].strip():
            out.append((f"tono_por_nota[{i}].fragmento", t["articulo"], t["fragmento"]))
    return out


def tone_flags(data: dict) -> list[str]:
    """Marca para revisión cuando notas del mismo conjunto valoran en sentidos opuestos."""
    fav = [t for t in data.get("tono_por_nota", []) if t["valoracion"] == "favorable"]
    cri = [t for t in data.get("tono_por_nota", []) if t["valoracion"] == "critico"]
    if not (fav and cri):
        return []
    f = ", ".join(sorted({t["articulo"] for t in fav}))
    c = ", ".join(sorted({t["articulo"] for t in cri}))
    return [f"Tono distinto según la IA (lectura sin verificar): favorable en {f}, crítico en {c}. "
            "No implica sesgo ni coordinación."]

SCHEMA = _obj({
    "mismo_hecho": {"type": "string", "enum": TRI},
    "tema_compartido": _STR,
    "enfoque": {"type": "string", "enum": ENFOQUE},
    "entidades_compartidas": {"type": "array", "items": _STR},
    "tono_por_nota": tono_schema({"type": "string", "enum": SIDES}),
    "afirmaciones_principales": {"type": "array", "items": _obj({
        "articulo": {"type": "string", "enum": SIDES},
        "texto": _STR,
        "tipo": {"type": "string", "enum": CLAIM_TYPES},
        "fragmento": _STR,
    })},
    "atribuciones_de_responsabilidad": {"type": "array", "items": _obj({
        "articulo": {"type": "string", "enum": SIDES},
        "responsable": _STR,
        "sobre": _STR,
        "fragmento": _STR,
    })},
    "alcance_geografico": _obj({"a": _STR, "b": _STR, "coincide": {"type": "string", "enum": TRI}}),
    "periodo_de_los_datos": _obj({"a": _STR, "b": _STR, "coincide": {"type": "string", "enum": TRI},
                                  "nota": _STR}),
    "diferencias_de_cifras": {"type": "array", "items": _obj({
        "descripcion": _STR, "cifra_a": _STR, "cifra_b": _STR})},
    "relacion_explicita_de_cita": _obj({"existe": {"type": "string", "enum": TRI}, "detalle": _STR}),
    "fragmentos_de_evidencia": {"type": "array", "items": _FRAG},
    "discrepancias_para_revision": {"type": "array", "items": _STR},
    "explicacion_breve": _STR,
    "limitaciones": {"type": "array", "items": _STR},
})

MAX_ITEMS = 20
MAX_TEXT = 1500


class ValidationFailed(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = text.replace("“", '"').replace("”", '"').replace("«", '"').replace("»", '"')
    text = text.replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


def _check_shape(value, schema: dict, path: str, errors: list[str]) -> None:
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            errors.append(f"{path}: se esperaba objeto")
            return
        extra = set(value) - set(schema["properties"])
        missing = set(schema["required"]) - set(value)
        if extra:
            errors.append(f"{path}: campos no permitidos {sorted(extra)}")
        if missing:
            errors.append(f"{path}: faltan campos {sorted(missing)}")
        for k, sub in schema["properties"].items():
            if k in value:
                _check_shape(value[k], sub, f"{path}.{k}", errors)
    elif t == "array":
        if not isinstance(value, list):
            errors.append(f"{path}: se esperaba lista")
            return
        if len(value) > MAX_ITEMS:
            errors.append(f"{path}: demasiados elementos ({len(value)})")
        for i, item in enumerate(value[:MAX_ITEMS]):
            _check_shape(item, schema["items"], f"{path}[{i}]", errors)
    elif t == "string":
        if not isinstance(value, str):
            errors.append(f"{path}: se esperaba texto")
        elif len(value) > MAX_TEXT:
            errors.append(f"{path}: texto demasiado largo")
        elif "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: valor no permitido {value!r}")


def parse_and_validate(raw_text: str, sources: dict[str, str]) -> dict:
    """Parsea y valida la salida. `sources` = {"A": texto de entrada, "B": ...}.

    Rechaza: JSON inválido, forma distinta del esquema, y fragmentos citados que no
    existan literalmente (salvo espacios, comillas tipográficas y mayúsculas) en la nota
    indicada.
    """
    try:
        data = json.loads(raw_text)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed([f"JSON inválido: {exc}"]) from exc
    errors: list[str] = []
    _check_shape(data, SCHEMA, "$", errors)
    if errors:
        raise ValidationFailed(errors)

    normalized = {k: _norm(v) for k, v in sources.items()}
    quoted = [(f"fragmentos_de_evidencia[{i}]", f["articulo"], f["texto"])
              for i, f in enumerate(data["fragmentos_de_evidencia"])]
    quoted += [(f"afirmaciones_principales[{i}].fragmento", c["articulo"], c["fragmento"])
               for i, c in enumerate(data["afirmaciones_principales"])]
    quoted += [(f"atribuciones_de_responsabilidad[{i}].fragmento", c["articulo"], c["fragmento"])
               for i, c in enumerate(data["atribuciones_de_responsabilidad"])]
    quoted += tone_quotes(data)
    for path, side, text in quoted:
        frag = _norm(text)
        if len(frag) < 3:
            errors.append(f"{path}: fragmento vacío o demasiado corto")
        elif frag not in normalized.get(side, ""):
            errors.append(f"{path}: el fragmento no existe en la nota {side}: {text[:80]!r}")
    if not data["fragmentos_de_evidencia"]:
        errors.append("fragmentos_de_evidencia: debe citar al menos un fragmento")
    if errors:
        raise ValidationFailed(errors)
    return data


def derive_flags(data: dict, pub_dates: dict[str, str | None], sources: dict[str, str]) -> list[str]:
    """Discrepancias para revisión humana, derivadas de los campos estructurados.

    No determinan la verdad de ninguna afirmación: solo indican qué mirar.
    """
    flags: list[str] = []
    if data["enfoque"] == "opuesto":
        flags.append("Enfoques opuestos: las notas no transmiten el mismo mensaje.")
    if data["mismo_hecho"] == "si" and data["enfoque"] in ("diferente", "opuesto"):
        flags.append("Mismo hecho con enfoque distinto.")
    if data["alcance_geografico"]["coincide"] == "no":
        a, b = data["alcance_geografico"]["a"], data["alcance_geografico"]["b"]
        flags.append(f"Alcance geográfico distinto: A «{a}» / B «{b}».")
    periodo = data["periodo_de_los_datos"]
    if periodo["coincide"] == "no":
        flags.append(f"Los datos refieren a períodos distintos: A «{periodo['a']}» / B «{periodo['b']}».")
    for side, key in (("A", "a"), ("B", "b")):
        pub = pub_dates.get(side)
        value = (periodo[key] or "").strip()
        if pub and value and (value in pub or pub in value) and value not in sources.get(side, ""):
            flags.append(f"Posible confusión en {side}: el período de los datos coincide con la "
                         "fecha de publicación y no aparece en el texto.")
    if data["diferencias_de_cifras"]:
        flags.append(f"Cifras distintas ({len(data['diferencias_de_cifras'])}).")
    flags.extend(tone_flags(data))
    for item in data["discrepancias_para_revision"]:
        if item and item not in flags:
            flags.append(item)
    return flags[:15]
