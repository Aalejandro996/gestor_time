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
app.config["SQLALCHEMY_DATABASE_URI"] = uri.replace("postgres://", "postgresql://", 1)
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


def login_required(rol=None):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = current()
            if not u:
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
        if not ini:
            flash("Indique la hora de inicio")
            return redirect("/")
        fin = fin or ahora()
        if fin < ini:
            flash("El fin no puede ser anterior al inicio")
            return redirect("/")
        acum = horas(ini, fin)
    else:
        fin = None
    k = Task(detalle=f["detalle"].strip(), actividad=f["actividad"], user_id=uid,
             inicio=ini, fin=fin, estado=est, acumulado=acum, seg_inicio=seg)
    db.session.add(k)
    db.session.flush()  # asigna k.id para la bitácora
    log("TAREA_CREADA", f"tarea={k.id} responsable_id={uid} actividad={k.actividad} "
                        f"estado={est} horas={acum:.2f}")
    db.session.commit()
    return redirect("/")


@app.route("/tarea/<int:tid>/<nuevo>", methods=["POST"])
@login_required()
def accion(tid, nuevo):
    u, t = current(), db.get_or_404(Task, tid)
    if u.rol != "Admin" and t.user_id != u.id:
        abort(403)
    if nuevo not in ("Iniciado", "Suspendido", "Culminado"):
        abort(400)
    n, err = ahora(), None
    if t.estado == "Culminado":
        err = "La tarea ya está culminada"
    elif nuevo == "Iniciado":
        if t.estado != "Iniciado":
            if not laboral(n):
                err = "Solo se puede iniciar entre 8:00 am y 5:00 pm"
            else:
                t.estado, t.seg_inicio, t.inicio, t.fin = "Iniciado", n, t.inicio or n, None
    elif t.estado == "Por Iniciar":
        err = "La tarea aún no ha sido iniciada"
    else:
        if t.estado == "Iniciado" and t.seg_inicio:
            t.acumulado += horas(t.seg_inicio, n)
        t.estado, t.seg_inicio = nuevo, None
        t.fin = n if nuevo == "Culminado" else None
    if err:
        flash(err)
        log("TAREA_RECHAZADA", f"tarea={t.id} motivo={err}")
    else:
        log("TAREA_" + nuevo.upper(), f"tarea={t.id} estado={t.estado} horas_acum={t.acumulado:.2f}")
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
    return render_template("usuarios.html", users=User.query.order_by(User.id).all())


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
