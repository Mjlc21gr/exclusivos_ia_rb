"""Wrapper minimo de metricas para Datadog."""

from __future__ import annotations

import logging

from utils.config import get_settings

logger = logging.getLogger(__name__)


class StubMetrics:
    """Implementacion nula cuando Datadog esta deshabilitado."""

    def increment(self, *args, **kwargs):
        return None

    def histogram(self, *args, **kwargs):
        return None

    def gauge(self, *args, **kwargs):
        return None


def get_metrics():
    """Devuelve cliente Datadog o stub segun configuracion."""

    settings = get_settings()
    if not settings.datadog_enabled:
        return StubMetrics()
    if not settings.datadog_api_key:
        logger.warning("DATADOG_ENABLED=true pero DD_API_KEY no esta configurada")
        return StubMetrics()

    from datadog import initialize, statsd

    initialize(
        api_key=settings.datadog_api_key,
        datadog_site=settings.datadog_site,
        service=settings.datadog_service,
    )
    return statsd
