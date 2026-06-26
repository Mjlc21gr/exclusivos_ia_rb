"""Notificaciones externas del flujo RVE."""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request

from utils.time_utils import format_datetime, now_bogota

logger = logging.getLogger(__name__)

GOOGLE_CHAT_REJECTION_WEBHOOK = os.getenv("GOOGLE_CHAT_WEBHOOK_URL", "")


class RejectionNotificationService:
    """Envia avisos de rechazo sin bloquear la respuesta de la API."""

    def notify_rejection(self, autorizacion: str, razon: str, request_id: str = "-") -> None:
        thread = threading.Thread(
            target=self._send_rejection,
            args=(autorizacion, razon, request_id),
            daemon=True,
        )
        thread.start()

    def notify_critical_feasibility(self, text: str, request_id: str = "-") -> None:
        thread = threading.Thread(
            target=self._send_message,
            args=("critical_feasibility", text, request_id),
            daemon=True,
        )
        thread.start()

    def _send_rejection(self, autorizacion: str, razon: str, request_id: str) -> None:
        payload = {
            "text": (
                "*Servicio RVE rechazado*\n"
                f"Autorizacion: {autorizacion}\n"
                f"Razon: {razon}\n"
                f"Request ID: {request_id}\n"
                f"Timestamp: {format_datetime(now_bogota())}"
            )
        }
        self._post_payload(payload, "rejection", autorizacion, request_id)

    def _send_message(self, alert_type: str, text: str, request_id: str) -> None:
        payload = {"text": text}
        self._post_payload(payload, alert_type, "-", request_id)

    def _post_payload(self, payload: dict, alert_type: str, autorizacion: str, request_id: str) -> None:
        if not GOOGLE_CHAT_REJECTION_WEBHOOK:
            logger.warning(
                "notification.google_chat.%s.skipped reason=GOOGLE_CHAT_WEBHOOK_URL_not_configured",
                alert_type,
            )
            return

        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            GOOGLE_CHAT_REJECTION_WEBHOOK,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                logger.info(
                    "notification.google_chat.%s.sent autorizacion=%s status=%s",
                    alert_type,
                    autorizacion,
                    response.status,
                )
        except (urllib.error.URLError, TimeoutError, OSError):
            logger.exception(
                "notification.google_chat.%s.failed autorizacion=%s request_id=%s",
                alert_type,
                autorizacion,
                request_id,
            )
