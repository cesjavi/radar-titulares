"""Registro de adaptadores: uno independiente por medio."""

from __future__ import annotations

from radar.collector.adapters.base import Endpoint, MediaAdapter
from radar.collector.adapters.eldestape import ElDestapeAdapter
from radar.collector.adapters.infobae import InfobaeAdapter
from radar.collector.adapters.pagina12 import Pagina12Adapter
from radar.collector.adapters.perfil import PerfilAdapter

ADAPTERS: dict[str, MediaAdapter] = {
    a.slug: a for a in (PerfilAdapter(), ElDestapeAdapter(), InfobaeAdapter(), Pagina12Adapter())
}


def get_adapter(slug: str) -> MediaAdapter | None:
    return ADAPTERS.get(slug)


__all__ = ["ADAPTERS", "Endpoint", "MediaAdapter", "get_adapter"]
