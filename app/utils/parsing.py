"""Funciones de parsing y normalizacion."""

from __future__ import annotations


def parse_coordinate(value) -> float:
    """Parsea coordenadas con formato decimal local o estandar.

    Soporta entradas como:
    - ``4,679``
    - ``-74,089``
    - ``1.234,56``
    - ``4.679``
    """

    if value in (None, ""):
        return 0.0

    text = str(value).strip()
    if not text:
        return 0.0

    text = text.replace(" ", "")

    # Formato local: punto para miles y coma para decimales.
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return 0.0


def format_coordinate_for_sheet(value) -> str:
    """Formatea coordenadas para Google Sheets usando coma decimal."""

    number = parse_coordinate(value)
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def normalize_header(value: str) -> str:
    """Normaliza headers de hoja sin traducir su contenido."""

    return str(value).strip().upper()
