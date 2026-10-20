# ASISTENTE UDABOL: chat web + Telegram con los mismos comandos
import json, os, threading, time, unicodedata
from datetime import datetime, timedelta, timezone
import requests
from dotenv import load_dotenv
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

load_dotenv()
TOKEN = os.getenv("tokenTelegram")
ADMIN_CHAT = os.getenv("chatIdTelegram")

db = None
if os.getenv("firebaseCredenciales"):
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["firebaseCredenciales"])))
        db = firestore.client()
        print("Datos guardados en Firestore.")
    except Exception as e:
        print("Firestore desactivado, se usan archivos locales:", e)
EN_NUBE = ("agenda.json", "reportes.json")

# ---------- Gemini (Google AI) ----------
GEMINI_KEY = os.getenv("geminiKey")
# Se prueban en orden; si uno está saturado o no existe, pasa al siguiente (se puede cambiar con geminiModelo=a,b,c)
MODELOS = [m.strip() for m in os.getenv("geminiModelo", "gemini-3.8-flash,gemini-2.5-flash,gemini-2.0-flash").split(",") if m.strip()]
try:
    from google import genai
    from google.genai import types
    cliente_ia = genai.Client(api_key=GEMINI_KEY, http_options=types.HttpOptions(timeout=20000)) if GEMINI_KEY else None
except ImportError:
    cliente_ia = None  # recibe avisos de reportes y recordatorios de la web

app = Flask(__name__)
socket = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
URL_ENVIAR = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
URL_RECIBIR = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
BOLIVIA = timezone(timedelta(hours=-4))
DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
candado = threading.Lock()
usuarios = {}  # sid del socket -> nombre

# ---------- datos ----------
def cargar(ruta, defecto):
    if db and ruta in EN_NUBE:
        doc = db.collection("datos").document(ruta[:-5]).get()
        return doc.to_dict().get("items", defecto) if doc.exists else defecto
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto

def guardar(ruta, datos):
    if db and ruta in EN_NUBE:
        db.collection("datos").document(ruta[:-5]).set({"items": datos})
        return
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)

def norm(t):
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")

def a_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

# ---------- comandos (una sola lógica para web y Telegram) ----------
def c_horario(args, quien):
    h = cargar("datos.json", {}).get("horario", [])
    lineas = [f"{f['dia'].capitalize()} {f['hora']} - {f['materia']} (aula {f['aula']})" for f in h]
    return "Tu horario:\n" + "\n".join(lineas), [("horario", h)]

def c_proxima(args, quien):
    h = cargar("datos.json", {}).get("horario", [])
    ahora = datetime.now(BOLIVIA)
    mejor, mejor_t = None, None
    for f in h:
        idx = DIAS.index(norm(f["dia"]))
        t = ((idx - ahora.weekday()) % 7) * 1440 + a_min(f["hora"]) - (ahora.hour * 60 + ahora.minute)
        if t < 0:
            t += 7 * 1440
        if mejor_t is None or t < mejor_t:
            mejor, mejor_t = f, t
    if not mejor:
        return "No hay clases en el horario.", []
    d, r = divmod(mejor_t, 1440)
    falta = (f"{d} d " if d else "") + f"{r // 60} h {r % 60} min"
    return f"Próxima clase: {mejor['materia']}, {mejor['dia']} {mejor['hora']}, aula {mejor['aula']}. Faltan {falta}.", [("proxima", mejor)]

def c_aula(args, quien):
    if not args:
        return "Uso: /aula materia  (ejemplo: /aula programacion)", []
    for f in cargar("datos.json", {}).get("horario", []):
        if norm(args) in norm(f["materia"]):
            return f"{f['materia']}: aula {f['aula']}, {f['dia']} {f['hora']}, {f['docente']}.", [("aula", f)]
    return "No encontré esa materia en tu horario.", []

def c_reporte(args, quien):
    if not args:
        return "Uso: /reporte texto  (ejemplo: /reporte El proyector del aula 12 no funciona)", []
    with candado:
        lista = cargar("reportes.json", [])
        r = {"id": len(lista) + 1, "texto": args, "autor": quien,
             "fecha": datetime.now(BOLIVIA).strftime("%d/%m/%Y %H:%M"), "estado": "Pendiente"}
        lista.append(r)
        guardar("reportes.json", lista)
    enviar_telegram(ADMIN_CHAT, f"Nuevo reporte #{r['id']} de {quien}: {args}")
    return f"Reporte #{r['id']} guardado. Gracias.", [("reportes", lista)]

def c_reportes(args, quien):
    lista = cargar("reportes.json", [])
    if not lista:
        return "Aún no hay reportes.", [("reportes", [])]
    return "Reportes:\n" + "\n".join(f"#{r['id']} [{r['estado']}] {r['texto']}" for r in lista), [("reportes", lista)]

def c_docentes(args, quien):
    d = cargar("datos.json", {}).get("docentes", [])
    lineas = [f"{x['nombre']} - {x['materia']} - {x['correo']}" for x in d]
    return "Docentes:\n" + "\n".join(lineas), [("docentes", d)]

def c_recordar(args, quien, origen="web", sid=None, chat=None):
    partes = args.split(maxsplit=1)
    if len(partes) < 2 or not partes[0].isdigit() or int(partes[0]) < 1:
        return "Uso: /recordar minutos texto  (ejemplo: /recordar 15 entregar informe)", []
    minutos, texto = int(partes[0]), partes[1]

    def avisar():
        if origen == "web":
            socket.emit("accion", {"tipo": "recordatorio", "datos": texto}, to=sid)
            enviar_telegram(ADMIN_CHAT, f"Recordatorio: {texto}")
        else:
            enviar_telegram(chat, f"Recordatorio: {texto}")
            socket.emit("accion", {"tipo": "recordatorio", "datos": texto})
    t = threading.Timer(minutos * 60, avisar)
    t.daemon = True
    t.start()
    return f"Listo, te aviso en {minutos} min: {texto}", []

# ---------- agenda: tareas y exámenes con aviso automático ----------
def minutos_para(vence):
    return (datetime.fromisoformat(vence) - datetime.now(BOLIVIA)).total_seconds() / 60

def falta(vence):
    t = int(minutos_para(vence))
    if t < 0:
        return "vencido"
    d, r = divmod(t, 1440)
    return (f"{d} d " if d else "") + f"{r // 60} h {r % 60} min"

def datos_agenda():
    pend = sorted((x for x in cargar("agenda.json", []) if not x["hecho"]), key=lambda x: x["vence"])
    return [dict(x, fecha=datetime.fromisoformat(x["vence"]).strftime("%d/%m %H:%M"), falta=falta(x["vence"])) for x in pend]

def parsear_fecha(args):
    p = args.split()
    try:
        partes = p[0].split("/")
        d, m = int(partes[0]), int(partes[1])
        ahora = datetime.now(BOLIVIA)
        y = int(partes[2]) if len(partes) > 2 else ahora.year
        resto, hh, mm = p[1:], 8, 0
        if resto and ":" in resto[0]:
            hh, mm = map(int, resto[0].split(":"))
            resto = resto[1:]
        if not resto:
            return None
        f = datetime(y, m, d, hh, mm, tzinfo=BOLIVIA)
        if len(partes) <= 2 and f < ahora:
            f = f.replace(year=y + 1)
        return f, " ".join(resto)
    except (ValueError, IndexError):
        return None

def c_agregar(tipo, args, quien, chat):
    r = parsear_fecha(args)
    if not r:
        return f"Uso: /{tipo} dd/mm [hh:mm] texto  (ejemplo: /{tipo} 25/10 18:00 Informe de Redes)", []
    f, texto = r
    with candado:
        lista = cargar("agenda.json", [])
        item = {"id": max([x["id"] for x in lista], default=0) + 1, "tipo": tipo, "texto": texto, "autor": quien,
                "vence": f.isoformat(), "hecho": False, "chat": chat or ADMIN_CHAT, "avisos": []}
        lista.append(item)
        guardar("agenda.json", lista)
    return (f"Guardado #{item['id']}: {tipo} \"{texto}\" para el {f.strftime('%d/%m/%Y %H:%M')}. "
            "Te aviso 1 día antes y 1 hora antes."), [("agenda", datos_agenda())]

def c_agenda(args, quien):
    d = datos_agenda()
    if not d:
        return "No tienes tareas ni exámenes pendientes.", [("agenda", [])]
    return "Pendientes:\n" + "\n".join(f"#{x['id']} [{x['tipo']}] {x['texto']} - {x['fecha']} (faltan {x['falta']})" for x in d), [("agenda", d)]

def c_cerrar(args, quien, borrar=False):
    if not args.strip().isdigit():
        return f"Uso: /{'borrar' if borrar else 'hecho'} número  (mira el número con /agenda)", []
    n = int(args)
    with candado:
        lista = cargar("agenda.json", [])
        item = next((x for x in lista if x["id"] == n), None)
        if not item:
            return f"No existe el #{n}.", []
        if borrar:
            lista.remove(item)
        else:
            item["hecho"] = True
        guardar("agenda.json", lista)
    return f"#{n} {'borrado' if borrar else 'marcado como hecho'}.", [("agenda", datos_agenda())]

def vigilar_agenda():
    while True:
        avisos = []
        try:
            with candado:
                lista = cargar("agenda.json", [])
                for x in lista:
                    mins = minutos_para(x["vence"])
                    if x["hecho"] or mins <= 0:
                        continue
                    clave = "1h" if mins <= 60 else "1d" if mins <= 1440 else None
                    if clave and clave not in x["avisos"]:
                        x["avisos"] += ["1d", "1h"] if clave == "1h" else ["1d"]
                        cuando = "falta 1 hora o menos" if clave == "1h" else "falta menos de 1 día"
                        avisos.append((x["chat"], f"{x['tipo'].capitalize()}: {x['texto']} ({cuando}, {falta(x['vence'])})"))
                if avisos:
                    guardar("agenda.json", lista)
        except Exception as e:
            print("Error en agenda:", e)
        for chat, msg in avisos:
            enviar_telegram(chat, msg)
            socket.emit("accion", {"tipo": "recordatorio", "datos": msg})
            socket.emit("accion", {"tipo": "agenda", "datos": datos_agenda()})
        time.sleep(30)

def c_preguntar(args, quien):
    if not args:
        return "Uso: /preguntar tu pregunta  (o escribe tu pregunta sin /)", []
    print(f"Preguntando a Gemini ({quien}): {args[:60]}")
    if not cliente_ia:
        return "Gemini no está configurado: falta geminiKey en .env o instalar google-genai.", []
    config = types.GenerateContentConfig(system_instruction=(
        "Eres el asistente de estudiantes de Ingeniería de Sistemas de la UDABOL, sede Oruro. "
        "Responde en español, claro y breve (máximo 120 palabras)."))
    for modelo in MODELOS:
        for intento in range(2):
            try:
                r = cliente_ia.models.generate_content(model=modelo, contents=args, config=config)
                return (r.text or "Gemini no devolvió texto.")[:3500], []
            except Exception as e:
                print(f"Error Gemini ({modelo}, intento {intento + 1}):", e)
                if any(c in str(e) for c in ("503", "429", "UNAVAILABLE")):
                    time.sleep(1.5)  # saturado: reintenta y luego prueba otro modelo
                else:
                    break  # modelo inexistente u otro error: pasa al siguiente modelo
    return "Gemini está saturado o no disponible ahora mismo. Intenta de nuevo en un minuto.", []

AYUDA = ("Comandos: /horario /proxima /aula materia /reporte texto /reportes "
         "/recordar minutos texto /docentes\n"
         "/tarea dd/mm hh:mm texto, /examen dd/mm hh:mm texto, /agenda, /hecho n, /borrar n\n"
         "/preguntar tu pregunta (o escribe sin /, responde Gemini)")

def ejecutar_comando(texto, quien, origen="web", sid=None, chat=None):
    partes = texto.strip().split(maxsplit=1)
    cmd = partes[0].lower().split("@")[0] if partes else ""
    args = partes[1] if len(partes) > 1 else ""
    if cmd == "/preguntar":
        return c_preguntar(args, quien)
    if not texto.strip().startswith("/"):
        return c_preguntar(texto.strip(), quien)
    if cmd == "/recordar":
        return c_recordar(args, quien, origen, sid, chat)
    if cmd in ("/tarea", "/examen"):
        return c_agregar(cmd[1:], args, quien, chat)
    if cmd in ("/hecho", "/borrar"):
        return c_cerrar(args, quien, borrar=(cmd == "/borrar"))
    tabla = {"/agenda": c_agenda, "/horario": c_horario, "/proxima": c_proxima, "/aula": c_aula, "/reporte": c_reporte,
             "/reportes": c_reportes, "/docentes": c_docentes}
    if cmd in tabla:
        return tabla[cmd](args, quien)
    return AYUDA, []

# ---------- Telegram ----------
def enviar_telegram(chat, texto):
    if not TOKEN or not chat:
        return
    try:
        requests.post(URL_ENVIAR, json={"chat_id": chat, "text": texto}, timeout=10)
    except requests.RequestException as e:
        print("Error enviando a Telegram:", e)

def recibir_telegram():
    offset = None
    while True:
        try:
            r = requests.get(URL_RECIBIR, params={"timeout": 25, "offset": offset}, timeout=35).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message", {})
                texto, chat = m.get("text", ""), m.get("chat", {}).get("id")
                if not texto or not chat:
                    continue
                quien = m.get("from", {}).get("first_name", "Telegram")
                resp, acciones = ejecutar_comando(texto, quien, "telegram", chat=chat)
                enviar_telegram(chat, resp)
                for tipo, datos in acciones:  # el panel web también se actualiza
                    socket.emit("accion", {"tipo": tipo, "datos": datos})
        except Exception as e:
            print("Error en Telegram:", e)
            time.sleep(5)

# ---------- web ----------
@app.route("/")
def inicio():
    return render_template("login.html")

@app.route("/salud")
def salud():
    return "ok"

@app.route("/panel")
def panel():
    return render_template("panel.html")

@socket.on("identificar")
def identificar(d):
    # Pendiente (opcional): verificar d["token"] con firebase-admin
    usuarios[request_sid()] = d.get("nombre") or "Estudiante"

@socket.on("mensaje")
def recibir_mensaje(texto):
    sid = request_sid()
    quien = usuarios.get(sid, "Web")
    resp, acciones = ejecutar_comando(texto, quien, "web", sid=sid)
    emit("respuesta", resp)
    for tipo, datos in acciones:
        socket.emit("accion", {"tipo": tipo, "datos": datos}, to=sid if tipo != "reportes" else None)

def request_sid():
    from flask import request
    return request.sid

if __name__ == "__main__":
    threading.Thread(target=vigilar_agenda, daemon=True).start()
    if TOKEN:
        threading.Thread(target=recibir_telegram, daemon=True).start()
    else:
        print("Aviso: falta tokenTelegram en .env, Telegram desactivado.")
    socket.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False, allow_unsafe_werkzeug=True)
