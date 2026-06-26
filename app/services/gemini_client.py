"""Cliente autenticado de Gemini 2.5 Flash via Vertex AI."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import google.auth.transport.requests as google_auth_requests
from google.oauth2 import service_account

from utils.config import get_settings

logger = logging.getLogger(__name__)

_VERTEX_AI_ENDPOINT = (
    "https://{region}-aiplatform.googleapis.com/v1/projects/{project_id}"
    "/locations/{region}/publishers/google/models/{model}:generateContent"
)
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_DEFAULT_REGION = "us-central1"
_DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiClient:
    """Cliente ligero para Gemini via Vertex AI REST sin SDK pesado."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._credentials: Optional[service_account.Credentials] = None
        self._project_id: str = ""
        self._region: str = os.getenv("GEMINI_REGION", _DEFAULT_REGION)
        self._model: str = os.getenv("GEMINI_MODEL", _DEFAULT_MODEL)

    def _get_credentials(self) -> service_account.Credentials:
        """Carga credenciales del service account para Gemini."""

        if self._credentials is not None and self._credentials.valid:
            return self._credentials

        sa_file = self._resolve_service_account_file()
        if not sa_file:
            raise RuntimeError(
                "GEMINI_SERVICE_ACCOUNT_FILE no configurado. "
                "Defina la variable de entorno con la ruta al JSON de la service account."
            )

        self._credentials = service_account.Credentials.from_service_account_file(
            sa_file,
            scopes=_SCOPES,
        )
        with open(sa_file, "r") as f:
            sa_data = json.load(f)
            self._project_id = sa_data.get("project_id", "")

        self._credentials.refresh(google_auth_requests.Request())
        logger.info(
            "gemini.client.authenticated project_id=%s region=%s model=%s",
            self._project_id,
            self._region,
            self._model,
        )
        return self._credentials

    def _resolve_service_account_file(self) -> str:
        """Resuelve la ruta del archivo de service account para Gemini."""

        explicit = os.getenv("GEMINI_SERVICE_ACCOUNT_FILE", "").strip()
        if explicit and os.path.isfile(explicit):
            return explicit

        candidates = [
            "gemini-service-account.json",
            os.path.join(os.path.dirname(__file__), "..", "gemini-service-account.json"),
            "/run/secrets/gemini-service-account.json",
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        return ""

    def generate(self, prompt: str, temperature: float = 0.1, max_tokens: int = 1024) -> str:
        """Envia un prompt a Gemini y retorna la respuesta de texto."""

        import urllib.request
        import urllib.error

        credentials = self._get_credentials()
        if not credentials.valid:
            credentials.refresh(google_auth_requests.Request())

        url = _VERTEX_AI_ENDPOINT.format(
            region=self._region,
            project_id=self._project_id,
            model=self._model,
        )

        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }

        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {credentials.token}",
        }

        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        started = time.monotonic()

        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                body = json.loads(response.read().decode("utf-8"))
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.info("gemini.generate.success elapsed_ms=%s", elapsed_ms)
                return self._extract_text(body)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8") if exc.fp else ""
            logger.error(
                "gemini.generate.http_error status=%s elapsed_ms=%s body=%s",
                exc.code,
                int((time.monotonic() - started) * 1000),
                error_body[:500],
            )
            raise RuntimeError(f"Gemini API error {exc.code}: {error_body[:200]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.error(
                "gemini.generate.network_error elapsed_ms=%s error=%s",
                int((time.monotonic() - started) * 1000),
                str(exc),
            )
            raise RuntimeError(f"Gemini network error: {exc}") from exc

    def _extract_text(self, response_body: dict) -> str:
        """Extrae el texto de la respuesta de Vertex AI."""

        candidates = response_body.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini retorno 0 candidates")
        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            raise RuntimeError("Gemini retorno 0 parts en el candidate")
        return parts[0].get("text", "")
