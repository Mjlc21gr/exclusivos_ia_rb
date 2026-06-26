# Exclusivos IA RB

Microservicio de dispatching inteligente para la Red de Vehículos Exclusivos (RVE). Asigna servicios de asistencia vehicular a turnos de técnicos usando OR-Tools o Gemini 2.5 Flash como motor de decisión.

## Stack

- Python 3.12 + FastAPI
- Google Sheets como backend operativo
- OR-Tools 9.14 (optimización de rutas)
- Gemini 2.5 Flash vía Vertex AI (motor alternativo)
- Google Distance Matrix (distancias reales con tráfico)
- Datadog (observabilidad)

## Estructura

```
app/                        # Código fuente (se despliega como imagen Docker)
├── api/                    # Endpoints FastAPI
├── or_engine/              # Motores de decisión (OR-Tools, Gemini, legacy)
├── services/               # Lógica de negocio y adaptadores
├── utils/                  # Configuración, constantes, utilidades
├── tests/                  # Tests unitarios
├── Dockerfile
└── requirements.txt
cloudbuild.yaml             # CI/CD con Cloud Build
cloudrun.yaml               # Spec declarativo de Cloud Run
.env.example                # Variables de entorno requeridas
```

## Setup local

```bash
cd app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copiar y rellenar variables
cp ../.env.example ../.env

# Correr
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

## Endpoints principales

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/rve/servicio` | Evalúa y asigna un nuevo servicio |
| POST | `/rve/cancelacion?autorizacion=X` | Cancela un servicio |
| POST | `/rve/completar?autorizacion=X` | Marca servicio como completado |
| POST | `/rve/validar` | Congela preasignaciones en ventana próxima |
| POST | `/rve/config/enrutamiento` | Cambia modo AUTOMATICO/MANUAL |
| GET | `/health` | Health check |

Todos los endpoints `/rve/*` requieren header `X-Api-Key`.

## Despliegue en GCP

El despliegue usa Cloud Build + Cloud Run. Los secretos se inyectan desde Secret Manager:

```bash
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_BRANCH_NAME=dev,_SERVICE_ACCOUNT=sa@project.iam.gserviceaccount.com,_SPREADSHEET_ID=xxx
```

Ver `cloudbuild.yaml` para la lista completa de substitutions.

## Secretos requeridos en Secret Manager

- `exclusivos-ia-rb-api-key-{env}` — API key para endpoints
- `exclusivos-ia-rb-maps-key-{env}` — Google Maps Distance Matrix key
- `exclusivos-ia-rb-datadog-key-{env}` — Datadog API key
- `exclusivos-ia-rb-chat-webhook-{env}` — Google Chat webhook URL
