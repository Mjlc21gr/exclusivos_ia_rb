"""Limpia el lock de CONFIG para simulaciones locales con reloj fijo."""

from __future__ import annotations

from services.google_sheets import GoogleSheetsRepository
from utils.time_utils import format_datetime, now_bogota


def main() -> None:
    """Resetea el lease del lock en la hoja CONFIG."""

    repository = GoogleSheetsRepository()
    worksheet = repository.worksheet(repository.settings.sheet_config)
    header_map = repository._header_map(worksheet)
    repository._update_row(
        worksheet,
        2,
        header_map,
        {
            "LOCK_OWNER": "",
            "LOCK_EXPIRES_AT": "",
            "LOCK_UPDATED_AT": format_datetime(now_bogota()),
        },
    )


if __name__ == "__main__":
    main()
