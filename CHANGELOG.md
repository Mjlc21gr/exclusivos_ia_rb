# Changelog

Todos los cambios notables de este proyecto se documentan en este archivo.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y este proyecto adhiere a [Versionamiento Semántico](https://semver.org/lang/es/).

## [No publicado]

### Agregado
- Se migró el código fuente del microservicio RVE desde el repo cluster/asistencia-sb-ruta-bolivar
- Se creó `.env.example` con todas las variables de entorno documentadas
- Se creó `.gitignore` para excluir secretos, virtualenvs y artefactos de build
- Se creó `cloudbuild.yaml` parametrizado con substitutions para CI/CD en Cloud Build
- Se creó `cloudrun.yaml` declarativo para el servicio exclusivos-ia-rb
- Se creó `.dockerignore` para excluir secretos y tests del build de Docker

### Cambiado
- Se externalizó el webhook de Google Chat (`notification_service.py`) a variable de entorno `GOOGLE_CHAT_WEBHOOK_URL`
- Se externalizó `VISUAL_ACCESS_KEY` (`flask_bridge.py`) a variable de entorno en lugar de texto plano
- Se externalizó `DEV_SPREADSHEET_ID` (`cluster_simulation.py`) a `os.getenv("SPREADSHEET_ID")`
- Se eliminó referencia hardcodeada al service account del proyecto anterior (`fifth-audio-423920-g2`) en `google_sheets.py`
- Se parametrizó la service account de Cloud Run en `cloudbuild.yaml` (antes estaba hardcodeada con nombre de persona)

### Seguridad
- Se eliminaron todas las API keys, tokens y credenciales hardcodeadas del código fuente
- Se configuró Secret Manager como fuente de secretos para el despliegue en GCP
