"""Lock cooperativo sobre Google Sheets."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

import gspread

from services.google_sheets import GoogleSheetsRepository
from utils.config import get_settings
from utils.constants import TIPO_ENRUTAMIENTO_AUTOMATICO
from utils.time_utils import format_datetime, now_bogota, parse_datetime

logger = logging.getLogger(__name__)


@dataclass
class LockLease:
    """Representa un lock adquirido."""

    owner: str
    expires_at: object | None = None
    acquired_monotonic: float | None = None


class SheetLockService:
    """Implementa un lock simple con token y verificacion posterior."""

    LOCK_HEADERS = ["LOCK_OWNER", "LOCK_EXPIRES_AT", "LOCK_UPDATED_AT"]
    CONFIG_HEADERS = [*LOCK_HEADERS, "TIPO_ENRUTAMIENTO"]

    def __init__(self, repository: GoogleSheetsRepository) -> None:
        self.repository = repository
        self.settings = get_settings()
        self._schema_ready = False
        self._acquired_expiration = None

    @contextmanager
    def locked(self, max_attempts: int | None = None):
        """Context manager del lock distribuido."""

        lease = self.acquire(max_attempts=max_attempts)
        try:
            yield
        finally:
            try:
                self.release(lease)
            except Exception:
                logger.exception("lock.release.error owner=%s", lease.owner)

    def acquire(self, max_attempts: int | None = None) -> LockLease:
        """Intenta tomar el lock hasta agotar los reintentos configurados."""

        owner = str(uuid.uuid4())
        retries = self.settings.lock_max_retries
        if max_attempts is not None:
            retries = max(1, min(retries, max_attempts))
        delay = self.settings.lock_retry_seconds
        logger.warning("lock.acquire.start owner=%s retries=%s", owner, retries)

        for _ in range(retries):
            if self._try_acquire(owner):
                logger.warning("lock.acquire.success owner=%s", owner)
                return LockLease(
                    owner=owner,
                    expires_at=self._acquired_expiration,
                    acquired_monotonic=time.monotonic(),
                )
            time.sleep(delay)

        raise RuntimeError("Sistema ocupado, intente mas tarde")

    def release(self, lease: LockLease) -> None:
        """Libera el lock solo si todavia pertenece al proceso actual."""

        if lease.expires_at and lease.expires_at <= now_bogota():
            logger.warning("lock.release.skip_expired owner=%s", lease.owner)
            return
        worksheet = self._worksheet()
        held_ms = None
        if lease.acquired_monotonic is not None:
            held_ms = int((time.monotonic() - lease.acquired_monotonic) * 1000)
        logger.warning("lock.release owner=%s held_ms=%s", lease.owner, held_ms)
        deadline_token = None
        if hasattr(self.repository, "push_request_deadline"):
            deadline_token = self.repository.push_request_deadline(time.monotonic() + 15.0)
        try:
            self.repository._call_with_retry(
                worksheet.update,
                "A2:C2",
                [["", "", format_datetime(now_bogota())]],
            )
            self.repository._invalidate_sheet_rows_cache(self.settings.sheet_config)
        finally:
            if deadline_token is not None and hasattr(self.repository, "pop_request_deadline"):
                self.repository.pop_request_deadline(deadline_token)

    def _try_acquire(self, owner: str) -> bool:
        started = time.monotonic()
        worksheet = self._worksheet()
        row = self._read_lock_row()
        expires_at = parse_datetime(row.get("LOCK_EXPIRES_AT", ""))
        now_value = now_bogota()

        if row.get("LOCK_OWNER") and expires_at and expires_at > now_value:
            logger.warning(
                "lock.acquire.busy owner=%s current_owner=%s expires_at=%s",
                owner,
                row.get("LOCK_OWNER"),
                row.get("LOCK_EXPIRES_AT"),
            )
            return False

        new_expiration = now_value + timedelta(seconds=self.settings.lock_timeout_seconds)
        self.repository._call_with_retry(
            worksheet.update,
            "A2:C2",
            [[owner, format_datetime(new_expiration), format_datetime(now_value)]],
        )
        self._acquired_expiration = new_expiration
        logger.warning(
            "lock.acquire.write owner=%s duration_ms=%s expires_at=%s",
            owner,
            int((time.monotonic() - started) * 1000),
            format_datetime(new_expiration),
        )
        return True

    def _worksheet(self):
        sheet_name = self.settings.sheet_config
        try:
            worksheet = self.repository.worksheet(sheet_name)
        except gspread.WorksheetNotFound:  # type: ignore[name-defined]
            spreadsheet = self.repository.spreadsheet()
            worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=10, cols=5)
            self._ensure_lock_schema(worksheet, force_reset=True)
        return worksheet

    def _read_lock_row(self) -> dict:
        worksheet = self.repository.worksheet(self.settings.sheet_config)
        values = self.repository._call_with_retry(worksheet.get, "A1:C2")
        if len(values) < 2:
            self._ensure_lock_schema(worksheet, force_reset=True)
            return {"LOCK_OWNER": "", "LOCK_EXPIRES_AT": "", "LOCK_UPDATED_AT": ""}
        row = values[1]
        return {
            "LOCK_OWNER": row[0] if len(row) > 0 else "",
            "LOCK_EXPIRES_AT": row[1] if len(row) > 1 else "",
            "LOCK_UPDATED_AT": row[2] if len(row) > 2 else "",
        }

    def _ensure_lock_schema(self, worksheet, force_reset: bool = False) -> None:
        """Garantiza que CONFIG tenga el esquema esperado por el lock."""

        if self._schema_ready and not force_reset:
            return
        if force_reset:
            logger.warning(
                "lock.schema.reset sheet=%s",
                worksheet.title,
            )
            self.repository._call_with_retry(
                worksheet.update,
                "A1:D2",
                [self.CONFIG_HEADERS, ["", "", "", TIPO_ENRUTAMIENTO_AUTOMATICO]],
            )
            self.repository._header_cache[self.settings.sheet_config] = list(self.CONFIG_HEADERS)
            self.repository._invalidate_sheet_rows_cache(self.settings.sheet_config)
        self._schema_ready = True
