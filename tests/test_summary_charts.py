"""Gráficos de la portada: actividad por hora y línea de tiempo de cada historia."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from radar.web.routes.dashboard import TL_PLOT_X0, TL_PLOT_X1, group_timeline, hourly_counts

NOW = datetime(2026, 10, 19, 12, 0)


def test_hourly_counts_buckets_and_ignores_out_of_range():
    times = [NOW - timedelta(minutes=10), NOW - timedelta(minutes=50),  # última hora
             NOW - timedelta(hours=2, minutes=1),                     # hace 2 a 3 h
             NOW - timedelta(hours=49), NOW + timedelta(minutes=5)]   # fuera de la ventana
    counts = hourly_counts(times, NOW, hours=48)
    assert len(counts) == 48 and sum(counts) == 3
    assert counts[47] == 2 and counts[45] == 1


def _member(media, published_at=None, precision="datetime", status="activo"):
    art = NS(media=NS(name=media), published_at=published_at, published_precision=precision,
             is_syndicated=False, title="x")
    return NS(status=status, article=art)


def test_timeline_never_places_notes_without_time():
    g = NS(members=[
        _member("Zeta", NOW),
        _member("Alfa", NOW + timedelta(minutes=30)),
        _member("Beta", None, precision="date"),     # solo fecha: no se le inventa hora
        _member("Gama", NOW, status="excluido"),     # fuera del grupo
    ])
    tl = group_timeline(g)
    assert len(tl["points"]) == 2 and tl["untimed"] == ["Beta"]
    # Filas por nombre, no por quién publicó antes.
    assert [l["name"] for l in tl["lanes"]] == ["Alfa", "Zeta"]
    xs = {p["article"].media.name: p["x"] for p in tl["points"]}
    assert xs == {"Zeta": TL_PLOT_X0, "Alfa": TL_PLOT_X1} and tl["span_min"] == 30


def test_timeline_needs_two_timed_notes():
    assert group_timeline(NS(members=[_member("Alfa", NOW), _member("Beta", None, "date")])) is None
    same = group_timeline(NS(members=[_member("Alfa", NOW), _member("Beta", NOW)]))
    assert all(p["x"] == (TL_PLOT_X0 + TL_PLOT_X1) / 2 for p in same["points"])
