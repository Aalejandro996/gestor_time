"""Control de horas y seguimiento de tareas - Flask (para Render).
Variables de entorno: SECRET_KEY, DATABASE_URL (Postgres), ADMIN_USER, ADMIN_PASSWORD, APP_TZ."""
import os, secrets
from datetime import datetime, time, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, request, session, redirect, render_template, flash, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash

ACT = ["Oficina", "Sucursal", "Finca", "Casa"]
ESTADOS = ["Por Iniciar", "Iniciado", "Culminado", "Suspendido"]
H_INI, H_FIN = time(8, 0), time(17, 0)
TZ = ZoneInfo(os.environ.get("APP_TZ", "America/Caracas"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambiar-en-produccion")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = "DATABASE_URL" in os.environ  # solo HTTPS en producción
uri = os.environ.get("DATABASE_URL", "sqlite:///control_horas.db")
if uri.startswith(("postgres://", "postgresql://")):
    uri = "postgresql+psycopg2://" + uri.split("://", 1)[1]  # driver explícito (psycopg2-binary)
app.config["SQLALCHEMY_DATABASE_URI"] = uri
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # IP real detrás del proxy de Render
db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(80), nullable=False)
    apellido = db.Column(db.String(80), nullable=False)
    username = db.Column(db.String(40), unique=True, nullable=False)
    pw = db.Column(db.String(256), nullable=False)
    rol = db.Column(db.String(10), nullable=False)  # Admin | Auditor

    @property
    def full(self):
        return f"{self.nombre} {self.apellido}"


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    detalle = db.Column(db.Text, nullable=False)
    actividad = db.Column(db.String(20), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    inicio = db.Column(db.DateTime)
    fin = db.Column(db.DateTime)
    estado = db.Column(db.String(20), default="Por Iniciar")
    acumulado = db.Column(db.Float, default=0.0)
    seg_inicio = db.Column(db.DateTime)
    user = db.relationship("User")


class Bitacora(db.Model):
    """Registro de auditoría. Solo se inserta; ninguna ruta lo modifica ni lo elimina."""
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, nullable=False,
                      default=lambda: datetime.now(TZ).replace(tzinfo=None, microsecond=0))
    usuario = db.Column(db.String(40))
    accion = db.Column(db.String(40), nullable=False)
    detalle = db.Column(db.Text)
    ip = db.Column(db.String(45))


class TareaNota(db.Model):
    """Historial de una tarea: observación de cierre, justificación de reinicio y ediciones del detalle."""
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    fecha = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(TZ).replace(tzinfo=None, microsecond=0))
    tipo = db.Column(db.String(20), nullable=False)  # CIERRE | REINICIO | EDICION
    texto = db.Column(db.Text, nullable=False)
    task = db.relationship("Task", backref=db.backref("notas", order_by="TareaNota.id"))
    user = db.relationship("User")


class Bloqueo(db.Model):
    """Bloqueo temporal de un usuario (vacaciones, permisos, etc.) con motivo y justificativo."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    desde = db.Column(db.Date, nullable=False)
    hasta = db.Column(db.Date, nullable=False)
    dias = db.Column(db.Integer, nullable=False)
    motivo = db.Column(db.String(40), nullable=False)
    justificativo = db.Column(db.Text, nullable=False)
    creado_por = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    creado_en = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(TZ).replace(tzinfo=None, microsecond=0))
    anulado = db.Column(db.Boolean, nullable=False, default=False)
    anulado_en = db.Column(db.DateTime)
    user = db.relationship("User", foreign_keys=[user_id])
    autor = db.relationship("User", foreign_keys=[creado_por])


with app.app_context():
    db.create_all()
    if not User.query.first():
        db.session.add(User(nombre="Administrador", apellido="Sistema",
                            username=os.environ.get("ADMIN_USER", "admin"), rol="Admin",
                            pw=generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin12345"))))
        db.session.commit()


# ---------- utilidades ----------
def ahora():
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def laboral(dt):
    return H_INI <= dt.time() < H_FIN


def horas(a, b):
    """Horas entre a y b contando solo 8:00-17:00 de cada día."""
    total, d = 0.0, a.date()
    while d <= b.date():
        s, e = max(a, datetime.combine(d, H_INI)), min(b, datetime.combine(d, H_FIN))
        if e > s:
            total += (e - s).total_seconds() / 3600
        d += timedelta(days=1)
    return total


def parse(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M") if s else None


def current():
    return db.session.get(User, session["uid"]) if "uid" in session else None


def log(accion, detalle="", usuario=None):
    """Agrega un evento a la bitácora dentro de la transacción actual (el commit lo hace el llamador)."""
    u = current()
    db.session.add(Bitacora(usuario=usuario or (u.username if u else "-"), accion=accion,
                            detalle=detalle, ip=request.remote_addr))


def bloqueo_activo(uid):
    """Bloqueo que impide el acceso hoy (no anulado y dentro de su período), o None."""
    h = ahora().date()
    return Bloqueo.query.filter(Bloqueo.user_id == uid, Bloqueo.anulado.is_(False),
                                Bloqueo.desde <= h, Bloqueo.hasta >= h).first()


def estado_bloqueo(b):
    h = ahora().date()
    return "Anulado" if b.anulado else "Programado" if b.desde > h else "Vigente" if b.hasta >= h else "Finalizado"


def login_required(rol=None):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = current()
            if not u:
                return redirect("/login")
            if bloqueo_activo(u.id):
                session.clear()
                flash("Su cuenta está bloqueada. Contacte al administrador.")
                return redirect("/login")
            if rol and u.rol != rol:
                abort(403)
            return f(*a, **k)
        return w
    return deco


@app.before_request
def csrf():
    if request.method == "POST" and request.form.get("_t") != session.get("_t"):
        abort(400)


@app.context_processor
def ctx():
    session.setdefault("_t", secrets.token_hex(16))
    return dict(t=session["_t"], me=current(), ACT=ACT, ESTADOS=ESTADOS)


@app.template_filter("fmt")
def fmt(d):
    return d.strftime("%Y-%m-%d %H:%M") if d else ""


# ---------- acceso ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        nombre = request.form["username"].strip().lower()
        u = User.query.filter_by(username=nombre).first()
        if u and check_password_hash(u.pw, request.form["pw"]):
            b = bloqueo_activo(u.id)
            if b:
                log("LOGIN_BLOQUEADO", f"hasta={b.hasta} motivo={b.motivo}", usuario=nombre)
                db.session.commit()
                flash(f"Su cuenta está bloqueada hasta el {b.hasta:%d/%m/%Y} ({b.motivo}). Contacte al administrador.")
                return render_template("login.html")
            session.clear()
            session["uid"], session["_t"] = u.id, secrets.token_hex(16)
            log("LOGIN_OK", usuario=nombre)
            db.session.commit()
            return redirect("/")
        log("LOGIN_FALLIDO", usuario=nombre)
        db.session.commit()
        flash("Usuario o clave incorrectos")
    return render_template("login.html")


@app.route("/logout")
def logout():
    log("LOGOUT")
    db.session.commit()
    session.clear()
    return redirect("/login")


# ---------- tareas (vista de Auditor y Admin) ----------
@app.route("/")
@login_required()
def tareas():
    u = current()
    q = Task.query.order_by(Task.id.desc())
    if u.rol != "Admin":
        q = q.filter_by(user_id=u.id)
    users = User.query.order_by(User.nombre).all() if u.rol == "Admin" else []
    return render_template("tareas.html", tasks=q.all(), users=users)


@app.route("/tarea", methods=["POST"])
@login_required()
def crear():
    u, f = current(), request.form
    uid = int(f.get("resp") or u.id) if u.rol == "Admin" else u.id
    try:
        ini, fin = parse(f.get("ini")), parse(f.get("fin"))
    except ValueError:
        flash("Formato de fecha inválido")
        return redirect("/")
    est, acum, seg = f["estado"], 0.0, None
    if not f["detalle"].strip() or f["actividad"] not in ACT or est not in ESTADOS:
        flash("Complete correctamente el formulario")
        return redirect("/")
    if est == "Iniciado":
        ini = ini or ahora()
        if not laboral(ini):
            flash("Solo se puede iniciar entre 8:00 am y 5:00 pm")
            return redirect("/")
        fin, seg = None, ini
    elif est == "Culminado":
        obs = f.get("obs", "").strip()
        if not (ini and fin) or not f.get("ok") or not obs:
            flash("Para culminar indique inicio y fin, confirme la fecha y hora y escriba la observación de cierre")
            return redirect("/")
        if fin < ini or fin > ahora():
            flash("El fin no puede ser anterior al inicio ni una fecha futura")
            return redirect("/")
        acum = horas(ini, fin)
    elif est == "Por Iniciar":
        ini = fin = None  # una tarea por iniciar no registra fecha ni hora
    else:
        fin = None
    k = Task(detalle=f["detalle"].strip(), actividad=f["actividad"], user_id=uid,
             inicio=ini, fin=fin, estado=est, acumulado=acum, seg_inicio=seg)
    db.session.add(k)
    db.session.flush()  # asigna k.id para la bitácora
    if est == "Culminado":
        nota(k, "CIERRE", obs)
    log("TAREA_CREADA", f"tarea={k.id} responsable_id={uid} actividad={k.actividad} "
                        f"estado={est} horas={acum:.2f} inicio={ini} fin={fin}")
    db.session.commit()
    return redirect("/")


def propia(tid):
    """Devuelve (usuario, tarea); un Auditor solo puede operar sobre sus propias tareas."""
    u, k = current(), db.get_or_404(Task, tid)
    if u.rol != "Admin" and k.user_id != u.id:
        abort(403)
    return u, k


def nota(k, tipo, texto):
    db.session.add(TareaNota(task_id=k.id, user_id=current().id, tipo=tipo, texto=texto))


def dtl(d):
    return d.strftime("%Y-%m-%dT%H:%M")


@app.route("/tarea/<int:tid>/<nuevo>", methods=["POST"])
@login_required()
def accion(tid, nuevo):
    u, k = propia(tid)
    if nuevo not in ("Iniciado", "Suspendido", "Culminado"):
        abort(400)
    n, err = ahora(), None
    if k.estado == "Culminado":
        err = "La tarea ya está culminada"
    elif nuevo == "Iniciado":
        if k.estado == "Iniciado":
            return redirect("/")
        if not laboral(n):
            err = "Solo se puede iniciar entre 8:00 am y 5:00 pm"
        elif k.estado == "Suspendido" and k.inicio and n.date() != k.inicio.date():
            return redirect(f"/tarea/{k.id}/reiniciar")  # otro día: pide justificación y nueva fecha
        else:
            k.estado, k.seg_inicio, k.inicio, k.fin = (
                "Iniciado", n, k.inicio if k.estado == "Suspendido" else n, None)
    elif k.estado == "Por Iniciar":
        err = "La tarea aún no ha sido iniciada"
    elif nuevo == "Culminado":
        return redirect(f"/tarea/{k.id}/culminar")  # pide confirmar fecha, hora y observación
    else:  # Suspendido
        if k.estado == "Iniciado" and k.seg_inicio:
            k.acumulado += horas(k.seg_inicio, n)
        k.estado, k.seg_inicio, k.fin = "Suspendido", None, None
    if err:
        flash(err)
        log("TAREA_RECHAZADA", f"tarea={k.id} motivo={err}")
    else:
        log("TAREA_" + nuevo.upper(), f"tarea={k.id} estado={k.estado} horas_acum={k.acumulado:.2f}")
    db.session.commit()
    return redirect("/")


@app.route("/tarea/<int:tid>/culminar", methods=["GET", "POST"])
@login_required()
def culminar(tid):
    u, k = propia(tid)
    if k.estado not in ("Iniciado", "Suspendido"):
        flash("Solo se pueden culminar tareas iniciadas o suspendidas")
        return redirect("/")
    n = ahora()
    if request.method == "POST":
        try:
            fin = parse(request.form.get("fin"))
        except ValueError:
            fin = None
        obs = request.form.get("obs", "").strip()
        piso = k.seg_inicio if k.estado == "Iniciado" else k.inicio
        if not fin or fin > n or fin < piso or not obs or not request.form.get("ok"):
            flash("Confirme la fecha y hora de fin (no futura ni anterior al inicio) y escriba la observación")
        else:
            if k.estado == "Iniciado":
                k.acumulado += horas(k.seg_inicio, fin)
            k.estado, k.fin, k.seg_inicio = "Culminado", fin, None
            nota(k, "CIERRE", obs)
            log("TAREA_CULMINADO", f"tarea={k.id} fin={fin} horas_acum={k.acumulado:.2f} hora_sistema={n}")
            db.session.commit()
            return redirect("/")
    return render_template("culminar.html", k=k, defecto=dtl(n))


@app.route("/tarea/<int:tid>/reiniciar", methods=["GET", "POST"])
@login_required()
def reiniciar(tid):
    u, k = propia(tid)
    if k.estado != "Suspendido":
        flash("Solo se puede reiniciar una tarea suspendida")
        return redirect("/")
    n = ahora()
    if request.method == "POST":
        try:
            ini = parse(request.form.get("ini"))
        except ValueError:
            ini = None
        just = request.form.get("just", "").strip()
        if not ini or ini > n or ini < k.inicio or not laboral(ini) or not just:
            flash("Indique una fecha y hora de inicio válida (entre 8:00 y 17:00, no futura ni anterior al "
                  "inicio original) y la justificación")
        else:
            k.estado, k.seg_inicio, k.fin = "Iniciado", ini, None
            nota(k, "REINICIO", f"Nueva fecha de inicio {ini:%Y-%m-%d %H:%M}. Justificación: {just}")
            log("TAREA_REINICIADA", f"tarea={k.id} nuevo_inicio={ini} horas_previas={k.acumulado:.2f}")
            db.session.commit()
            return redirect("/")
    return render_template("reiniciar.html", k=k, defecto=dtl(n))


@app.route("/tarea/<int:tid>/editar", methods=["POST"])
@login_required()
def editar(tid):
    u, k = propia(tid)
    nuevo = request.form.get("detalle", "").strip()
    if not nuevo or len(nuevo) > 2000:
        flash("El detalle no puede estar vacío ni superar 2000 caracteres")
    elif nuevo != k.detalle:
        nota(k, "EDICION", f"Antes: {k.detalle} | Después: {nuevo}")
        log("TAREA_EDITADA", f"tarea={k.id}")
        k.detalle = nuevo
        db.session.commit()
    return redirect("/")


# ---------- usuarios (solo Admin) ----------
@app.route("/usuarios", methods=["GET", "POST"])
@login_required("Admin")
def usuarios():
    if request.method == "POST":
        f = {k: request.form.get(k, "").strip() for k in ("nombre", "apellido", "username", "pw", "rol")}
        f["username"] = f["username"].lower()
        if not all(f.values()) or f["rol"] not in ("Admin", "Auditor") or len(f["pw"]) < 8:
            flash("Complete todos los campos (clave de al menos 8 caracteres)")
        elif User.query.filter_by(username=f["username"]).first():
            flash("Ese usuario ya existe")
        else:
            f["pw"] = generate_password_hash(f["pw"])
            db.session.add(User(**f))
            log("USUARIO_CREADO", f"usuario={f['username']} rol={f['rol']}")
            db.session.commit()
            flash("Usuario creado")
        return redirect("/usuarios")
    hist = [(b, estado_bloqueo(b)) for b in Bloqueo.query.order_by(Bloqueo.id.desc()).all()]
    estado = {}
    for b, e in reversed(hist):
        if e in ("Vigente", "Programado"):
            estado[b.user_id] = (e, b)
    return render_template("usuarios.html", users=User.query.order_by(User.id).all(), hist=hist,
                           estado=estado, hoy=ahora().date().isoformat())


@app.route("/usuarios/bloquear", methods=["POST"])
@login_required("Admin")
def bloquear():
    f = request.form
    try:
        uid, dias = int(f["uid"]), int(f["dias"])
        desde = datetime.strptime(f["desde"], "%Y-%m-%d").date() if f.get("desde") else ahora().date()
    except (ValueError, KeyError):
        flash("Datos de bloqueo inválidos")
        return redirect("/usuarios")
    motivo, just = f.get("motivo", "").strip(), f.get("justificativo", "").strip()
    u = db.session.get(User, uid)
    if not u or u.id == current().id or not 1 <= dias <= 365 or not motivo or not just:
        flash("Indique un usuario distinto del suyo, de 1 a 365 días, el motivo y el justificativo")
        return redirect("/usuarios")
    b = Bloqueo(user_id=u.id, desde=desde, dias=dias, hasta=desde + timedelta(days=dias - 1),
                motivo=motivo, justificativo=just, creado_por=current().id)
    db.session.add(b)
    log("USUARIO_BLOQUEADO", f"usuario={u.username} desde={b.desde} hasta={b.hasta} dias={dias} motivo={motivo}")
    db.session.commit()
    flash(f"Usuario {u.username} bloqueado hasta el {b.hasta:%d/%m/%Y}")
    return redirect("/usuarios")


@app.route("/usuarios/<int:uid>/desbloquear", methods=["POST"])
@login_required("Admin")
def desbloquear(uid):
    h = ahora().date()
    for b in Bloqueo.query.filter(Bloqueo.user_id == uid, Bloqueo.anulado.is_(False), Bloqueo.hasta >= h):
        b.anulado, b.anulado_en = True, ahora()
        log("USUARIO_DESBLOQUEADO", f"usuario_id={uid} bloqueo={b.id}")
    db.session.commit()
    return redirect("/usuarios")


# ---------- dashboard (solo Admin) ----------
@app.route("/dashboard")
@login_required("Admin")
def dashboard():
    rows = db.session.query(User.id, User.nombre, User.apellido, Task.actividad,
                            func.sum(Task.acumulado)).join(Task, Task.user_id == User.id) \
        .group_by(User.id, User.nombre, User.apellido, Task.actividad).all()
    por_act, por_user, matriz = {a: 0.0 for a in ACT}, {}, {}
    for _, n, ap, a, h in rows:
        h = h or 0.0
        nom = f"{n} {ap}"
        por_act[a] = por_act.get(a, 0.0) + h
        por_user[nom] = por_user.get(nom, 0.0) + h
        matriz.setdefault(nom, {})[a] = h
    return render_template("dashboard.html", por_act=por_act, por_user=por_user, matriz=matriz)


# ---------- bitácora (solo lectura, solo Admin) ----------
@app.route("/bitacora")
@login_required("Admin")
def bitacora():
    regs = Bitacora.query.order_by(Bitacora.id.desc()).limit(500).all()
    return render_template("bitacora.html", regs=regs)


if __name__ == "__main__":
    app.run(debug=True)
