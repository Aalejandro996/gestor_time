# Documentación técnica — Control de horas

## 1. Arquitectura

Aplicación monolítica Flask (`app.py`) con plantillas Jinja (`templates/`) y una hoja de estilos
(`static/style.css`). Persistencia con SQLAlchemy sobre PostgreSQL (producción) o SQLite (desarrollo).
No usa JavaScript ni recursos externos.

## 2. Roles y permisos

| Ruta | Auditor | Admin |
|---|---|---|
| `/login`, `/logout` | Sí | Sí |
| `/` (formulario + lista de tareas) | Solo sus tareas | Todas |
| `POST /tarea` (crear) | Solo a su nombre | A nombre de cualquier usuario |
| `POST /tarea/<id>/<acción>` | Solo sus tareas | Todas |
| `/usuarios` (crear/listar) | No (403) | Sí |
| `/dashboard` | No (403) | Sí |
| `/bitacora` | No (403) | Sí (solo lectura) |

Los usuarios solo pueden ser creados por un Admin; no existe registro público. El control se aplica en el
servidor (decorador `login_required`), no en la interfaz.

## 3. Modelo de datos

- **User:** `id, nombre, apellido, username (único), pw (hash), rol (Admin|Auditor)`
- **Task:** `id, detalle, actividad, user_id, inicio, fin, estado, acumulado (horas), seg_inicio`
- **Bitacora:** `id, fecha, usuario, accion, detalle, ip`

`seg_inicio` es el inicio del tramo activo; `acumulado` suma los tramos ya cerrados.

## 4. Reglas de negocio

1. Horario laboral: 08:00–17:00 en la zona `APP_TZ`. Solo esas horas se cuentan (`horas()`).
2. **Iniciado** solo puede establecerse dentro del horario laboral; inicia el conteo del tramo.
3. **Suspendido** y **Culminado** cierran el tramo: suman al acumulado las horas del tramo dentro del
   horario. Culminado además fija la hora de fin.
4. Si inicio y fin caen en días distintos, se suma la franja laboral de cada día; lo transcurrido fuera de
   horario no cuenta y el conteo continúa a las 08:00 del día siguiente. La columna "Días" muestra los días
   calendario abarcados.
5. Una tarea Culminada no puede modificarse. Una tarea Por Iniciar no puede suspenderse ni culminarse por
   botón.
6. Al registrar una tarea ya Culminada con inicio y fin manuales, las horas se calculan con la misma
   función. Fines de semana y feriados **no** se excluyen.
7. Las horas de un tramo en curso (Iniciado) se contabilizan al suspender o culminar; el dashboard muestra
   solo horas ya acumuladas.

## 5. Controles de auditoría

- **Bitácora append-only:** ninguna ruta actualiza ni elimina registros de `Bitacora`. Eventos: `LOGIN_OK`,
  `LOGIN_FALLIDO`, `LOGOUT`, `USUARIO_CREADO`, `TAREA_CREADA`, `TAREA_INICIADO`, `TAREA_SUSPENDIDO`,
  `TAREA_CULMINADO`, `TAREA_RECHAZADA`. Cada evento guarda fecha, usuario, detalle e IP.
- El evento se escribe en la **misma transacción** que el cambio (excepto login/logout, que tienen su propio
  commit).
- Las claves se almacenan con hash (Werkzeug) y nunca se registran en bitácora.
- Protección CSRF con token de sesión en todos los POST; cookies `HttpOnly`, `SameSite=Lax` y `Secure` en
  producción; IP real mediante `ProxyFix` tras el proxy de Render.
- Autorización siempre en servidor; un Auditor que fuerce una URL o acción ajena recibe 403.

## 6. Limitaciones conocidas y recomendaciones

- La bitácora es inmutable **por diseño de la aplicación**, no de la base de datos. Para garantía fuerte,
  use un rol de BD con `INSERT/SELECT` sobre `bitacora` (sin `UPDATE/DELETE`) y respaldos periódicos.
- Las ediciones de tareas se registran por acción de estado, no por campo. No hay edición ni borrado de
  tareas o usuarios.
- No hay bloqueo por intentos fallidos de login (los intentos quedan en bitácora); se recomienda añadirlo.
- No hay exclusión de fines de semana ni feriados.
- La zona horaria es única para toda la organización (`APP_TZ`).
- Sin pruebas automatizadas incluidas.

## 7. Mantenimiento

- Dependencias en `requirements.txt`; revise y actualice periódicamente.
- Cambios de esquema: SQLAlchemy `create_all()` solo crea tablas nuevas; para modificar columnas
  existentes use una herramienta de migraciones (p. ej. Flask-Migrate).
- Rotar `SECRET_KEY` cierra todas las sesiones activas.
