# Control de horas y seguimiento de tareas

Aplicación web (Flask + PostgreSQL) para registrar horas por actividad, con roles y bitácora de auditoría.

- **Admin:** acceso total, registro de usuarios, dashboard de horas y consulta de la bitácora.
- **Auditor:** solo ve el formulario de registro de horas/actividad y sus propias tareas.
- **Actividades:** Oficina, Sucursal, Finca, Casa.
- **Estados:** Por Iniciar → Iniciado → Suspendido / Culminado. Solo se cuentan horas entre 8:00 y 17:00.

## Estructura

```
app.py              Modelos, reglas de negocio y rutas (único módulo Python)
templates/          Vistas HTML (base, login, tareas, usuarios, dashboard, bitacora)
static/style.css    Estilos
docs/DOCUMENTACION.md   Documentación técnica y de controles de auditoría
requirements.txt    Dependencias
render.yaml         Despliegue en Render (servicio web + Postgres)
```

## Ejecución local

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                                          # http://127.0.0.1:5000
```

Sin `DATABASE_URL` usa SQLite local (`control_horas.db`). El primer Admin se crea automáticamente:
usuario `admin`, clave `admin12345` (o los valores de `ADMIN_USER` / `ADMIN_PASSWORD`). **Cámbiela** creando
otro Admin con clave propia en entornos reales.

## Variables de entorno

| Variable | Uso | Por defecto |
|---|---|---|
| `SECRET_KEY` | Firma de sesiones. Obligatoria en producción | valor de desarrollo |
| `DATABASE_URL` | Conexión Postgres (activa también cookies solo-HTTPS) | SQLite local |
| `ADMIN_USER`, `ADMIN_PASSWORD` | Admin inicial (solo si no hay usuarios) | `admin` / `admin12345` |
| `APP_TZ` | Zona horaria para el horario laboral | `America/Caracas` |

## Despliegue en Render

1. Suba esta carpeta a un repositorio de GitHub.
2. En Render: **New → Blueprint** y seleccione el repositorio (usa `render.yaml`).
3. Defina `ADMIN_PASSWORD` cuando lo solicite.
4. Verifique `APP_TZ` con la zona horaria de la organización.

`render.yaml` crea una base Postgres gratuita: Render puede eliminarla pasado un tiempo. Para datos de
auditoría use un plan de pago y respaldos.

## Más información

Ver [`docs/DOCUMENTACION.md`](docs/DOCUMENTACION.md): reglas de negocio, matriz de permisos, controles de
auditoría, modelo de datos y limitaciones conocidas.
