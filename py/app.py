#Librerias utilizadas 
import os
import csv
import io
import mysql.connector
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
import matplotlib.pyplot as plt
from reportlab.platypus import Image
import uuid
from datetime import datetime
import re
import json
from google import genai
from google.genai import types
from PIL import Image as PILImage
from pydantic import BaseModel

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, jsonify, Response, session
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)
from werkzeug.security import generate_password_hash, check_password_hash


#Esto nos va a servir para la extracción de datos de la credencial
class DatosINE(BaseModel):
    nombre_completo: str
    curp: str
    domicilio: str
    codigo_postal: str
    seccion: str

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static")
)
 
app.secret_key = "mi_clave_secreta_de_prueba"

@app.before_request
def proteger_paginas():

    paginas_publicas = ["login", "logout", "static"]

    if request.endpoint not in paginas_publicas:
        if "id_usuario" not in session:
            return redirect(url_for("login"))

MYSQL_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "localhost"),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", "root12"),
    "database": os.environ.get("MYSQL_DATABASE", "apoyos"),
}


class Conexion:
    """Envoltorio para que el resto del código siga usando
    conexion.execute(...) con marcadores '?' como antes."""

    def __init__(self, conexion):
        self._conexion = conexion

    def execute(self, sql, params=()):
        cursor = self._conexion.cursor(dictionary=True, buffered=True)
        cursor.execute(sql.replace("?", "%s"), params)
        return cursor

    def commit(self):
        self._conexion.commit()

    def close(self):
        self._conexion.close()


def obtener_conexion():
    return Conexion(
        mysql.connector.connect(**MYSQL_CONFIG, charset="utf8mb4")
    )


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        usuario = request.form.get("usuario", "").strip()
        contrasena = request.form.get("contrasena", "")

        conexion = obtener_conexion()

        cuenta = conexion.execute(
            """
            SELECT *
            FROM usuarios
            WHERE usuario = ?
            """,
            (usuario,)
        ).fetchone()

        conexion.close()

        if cuenta and check_password_hash(
            cuenta["contrasena"], contrasena
        ):

            session.clear()

            session["id_usuario"] = cuenta["id"]
            session["nombre"] = cuenta["nombre"]
            session["rol"] = cuenta["rol"]
            session["departamento"] = cuenta["departamento"]

            return redirect(url_for("registro"))

        flash("Usuario o contraseña incorrectos.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("login"))

def crear_base_datos():
    #Crear la base de datos si no existe
    servidor = mysql.connector.connect(
    host="://aivencloud.com",
    port=19417,
    user="avnadmin",
    password="AVNS_mSRex8W5vrn7YNfNhHl",  # Lee el paso de abajo para ver tu contraseña
    database="defaultdb"
)

    cursor = servidor.cursor()
    cursor.execute(
        f"CREATE DATABASE IF NOT EXISTS `{MYSQL_CONFIG['database']}` "
        "CHARACTER SET utf8mb4"
    )
    cursor.close()
    servidor.close()

    #Crear la tabla si no existe
    conexion = obtener_conexion()
    conexion.execute("""
        CREATE TABLE IF NOT EXISTS apoyos (
            id INT AUTO_INCREMENT PRIMARY KEY,
            nombre VARCHAR(150) NOT NULL,
            curp VARCHAR(18) NOT NULL,
            telefono VARCHAR(20) NOT NULL,
            cp VARCHAR(5) NOT NULL,
            colonia VARCHAR(150) NOT NULL,
            seccion VARCHAR(4) NOT NULL,
            fecha VARCHAR(10) NOT NULL,
            edad VARCHAR(3) NOT NULL,
            sexo VARCHAR(30) NOT NULL,
            domicilio VARCHAR(255) NOT NULL,
            apoyo VARCHAR(150) NOT NULL,
            descripcion TEXT,
            departamento VARCHAR(100) NOT NULL,
            estado ENUM('aprobado', 'en_firma', 'cancelado')
                NOT NULL DEFAULT 'en_firma',
            fecha_creacion VARCHAR(19),
            lote_carga VARCHAR(8),
            INDEX idx_curp (curp),
            INDEX idx_lote (lote_carga)
        ) CHARACTER SET utf8mb4
    """)
    conexion.commit()
    conexion.close()

RUTA_CATALOGO_CP = os.path.join(BASE_DIR, "data", "colonias_cortazar.csv")

def normalizar_fecha(fecha):
    fecha = (fecha or "").strip()

    formatos_aceptados = (
        "%Y-%m-%d",  # 2026-09-25
        "%d/%m/%Y",  # 25/09/2026
        "%d-%m-%Y",  # 25-09-2026
        "%Y/%m/%d",  # 2026/09/25
    )

    for formato in formatos_aceptados:
        try:
            return datetime.strptime(fecha, formato).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None

def cargar_codigos_postales(ruta):
    catalogo = {}

    if not os.path.exists(ruta):
        print(f"AVISO: no se encontró el catálogo de CPs en {ruta}")
        return catalogo

    with open(ruta, newline="", encoding="utf-8-sig") as archivo:
        muestra = archivo.read(4096)
        archivo.seek(0)

        # Detecta si el separador es , o ; (Excel en español suele
        # exportar con ; en vez de ,).
        try:
            dialecto = csv.Sniffer().sniff(muestra, delimiters=",;")
        except csv.Error:
            dialecto = csv.excel  # separador , por defecto

        lector = csv.DictReader(archivo, dialect=dialecto)

        # Normaliza los encabezados: sin espacios y en minúsculas,
        # para que "CP", " cp", "Cp" etc. también funcionen.
        lector.fieldnames = [
            (nombre or "").strip().lower() for nombre in lector.fieldnames
        ]

        columna_colonia = None
        if "colonia" in lector.fieldnames:
            columna_colonia = "colonia"
        elif "colonias" in lector.fieldnames:
            columna_colonia = "colonias"

        if "cp" not in lector.fieldnames or columna_colonia is None:
            print(
                "ERROR: el archivo debe tener las columnas 'cp' y "
                "'colonia' (o 'colonias'). "
                f"Encabezados encontrados: {lector.fieldnames}"
            )
            return catalogo

        for fila in lector:
            cp = (fila.get("cp") or "").strip()
            colonia = (fila.get(columna_colonia) or "").strip()

            if not cp or not colonia:
                continue

            catalogo.setdefault(cp, [])
            if colonia not in catalogo[cp]:
                catalogo[cp].append(colonia)

    for cp in catalogo:
        catalogo[cp].sort()

    return catalogo


COLONIAS_POR_CP = cargar_codigos_postales(RUTA_CATALOGO_CP)

crear_base_datos()

@app.route("/")
def inicio():
    if "id_usuario" not in session:
        return redirect(url_for("login"))

    return redirect(url_for("registro"))

@app.route("/api/colonias/<cp>")
def api_colonias(cp):
    """Devuelve en JSON la lista de colonias para un CP dado."""
    cp = cp.strip()
    colonias = COLONIAS_POR_CP.get(cp, [])
    return jsonify({"cp": cp, "colonias": colonias})
CURP_RE = re.compile(r"[A-Z]{4}\d{6}[HM][A-Z]{2}[B-DF-HJ-NP-TV-Z]{3}[A-Z0-9]\d")


def limpiar_linea(linea):
    linea = re.sub(r"\b(NOMBRE|SEXO|DOMICILIO)\b", " ", linea)
    linea = re.sub(r"[^A-ZÁÉÍÓÚÑ ]", " ", linea)
    linea = re.sub(r"\b[HM]\b", " ", linea)          # "SEXO H"
    return re.sub(r"\s+", " ", linea).strip()


def extraer_datos(texto):
    texto = texto.upper()
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    datos = {
    "nombre": "",
    "curp": "",
    "seccion": "",
    "cp": ""
    }

    # CURP
    m = CURP_RE.search(re.sub(r"\s", "", texto))
    if m:
        datos["curp"] = m.group(0)

    # Sección
    m = re.search(r"SECCI[OÓ]N\s*[:.]?\s*(\d{4})", texto)
    if m:
        datos["seccion"] = m.group(1)

    def indice(patron, desde=0):
        for i in range(desde, len(lineas)):
            if re.search(patron, lineas[i]):
                return i
        return None

    i_nombre = indice(r"\bNOMBRE\b")
    i_dom = indice(r"DOMICILIO")
    i_fin = indice(r"CLAVE DE ELECTOR|CURP", (i_dom or 0) + 1)

    # Nombre
    if i_nombre is not None:
        fin = i_dom if i_dom is not None and i_dom > i_nombre else i_nombre + 5

        partes = [limpiar_linea(l) for l in lineas[i_nombre:fin]]
        partes = [p for p in partes if len(p) >= 2]

        texto_nombre = " ".join(partes)
        palabras = texto_nombre.split()

        if len(palabras) >= 3:
            apellidos = palabras[:2]
            nombres = palabras[2:]

            datos["nombre"] = " ".join(nombres + apellidos)
        else:
            datos["nombre"] = texto_nombre

    # Código postal
    m = re.search(r"\b\d{5}\b", texto)

    if m:
        datos["cp"] = m.group(0)

    return datos



@app.route("/api/escanear-ine", methods=["POST"])
def escanear_ine():
    try:
        archivo = request.files.get("imagen")

        if not archivo:
            return jsonify({"error": "No se recibió la imagen"}), 400

        contenido_bytes = archivo.read()

        try:
            imagen = PILImage.open(
                io.BytesIO(contenido_bytes)
            ).convert("RGB")
        except Exception:
            return jsonify({"error": "La imagen no es válida"}), 400

        # Obtener la clave de Gemini
        api_key = os.environ.get("GEMINI_API_KEY")

        if not api_key:
            return jsonify({
                "error": "No se configuró la clave de Gemini"
            }), 500

        client = genai.Client(api_key=api_key)

        prompt = (
            "Extrae con precisión de esta credencial del INE: "
            "nombre completo, CURP, domicilio, código postal y sección. "
            "Respeta los datos tal como aparecen en la credencial. "
            "Si algún dato no es legible, devuelve una cadena vacía. "
            "No inventes información."
        )

        respuesta = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=[imagen, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=DatosINE,
                temperature=0.1,
            ),
        )

        datos = json.loads(respuesta.text)

        # Cambiar el orden de apellidos y nombres
        nombre_original = datos.get("nombre_completo", "").strip()
        palabras = nombre_original.split()

        if len(palabras) >= 3:
            apellidos = palabras[:2]
            nombres = palabras[2:]

            datos["nombre_completo"] = " ".join(nombres + apellidos)

        # Adaptar los nombres de Gemini a los que usa registro.html
        return jsonify({
            "nombre": datos.get("nombre_completo", ""),
            "curp": datos.get("curp", ""),
            "domicilio": datos.get("domicilio", ""),
            "cp": datos.get("codigo_postal", ""),
            "seccion": datos.get("seccion", "")
        })

    except Exception as e:
        app.logger.exception("Error al escanear INE con Gemini")

        if "503" in str(e):
            return jsonify({
                "error": "Gemini está ocupado en este momento. Intenta nuevamente en unos segundos."
            }), 503

        return jsonify({
            "error": f"{type(e).__name__}: {e}"
        }), 500
    
@app.route("/registro", methods=["GET", "POST"])
def registro():
    errores = {}

    datos = {
        "nombre": "",
        "curp": "",
        "telefono": "",
        "cp": "",
        "colonia": "",
        "seccion": "",
        "fecha": ""
    }

    if request.method == "POST":
        datos["nombre"] = request.form.get("nombre", "").strip()
        datos["curp"] = request.form.get("curp", "").strip().upper()
        datos["telefono"] = request.form.get("telefono", "").strip()
        datos["cp"] = request.form.get("cp", "").strip()
        datos["colonia"] = request.form.get("colonia", "").strip()
        datos["seccion"] = request.form.get("seccion", "").strip()
        datos["fecha"] = request.form.get("fecha", "").strip()

        if not datos["nombre"]:
            errores["nombre"] = "Este campo es obligatorio"

        if not datos["curp"]:
            errores["curp"] = "Este campo es obligatorio"
        elif len(datos["curp"]) != 18:
            errores["curp"] = "La CURP debe tener 18 caracteres"

        if not datos["telefono"]:
            errores["telefono"] = "Este campo es obligatorio"
        elif not datos["telefono"].isdigit() or len(datos["telefono"]) != 10:
            errores["telefono"] = "El teléfono debe tener 10 dígitos"

        if not datos["cp"]:
            errores["cp"] = "Este campo es obligatorio"
        elif not datos["cp"].isdigit() or len(datos["cp"]) != 5:
            errores["cp"] = "El CP debe tener 5 dígitos"
        elif datos["cp"] not in COLONIAS_POR_CP:
            errores["cp"] = "El código postal no existe en el catálogo"

        if not datos["colonia"]:
            errores["colonia"] = "Este campo es obligatorio"
        elif (
            datos["cp"] in COLONIAS_POR_CP
            and datos["colonia"] not in COLONIAS_POR_CP[datos["cp"]]
        ):
            errores["colonia"] = "Selecciona una colonia válida para ese CP"

        if not datos["seccion"].isdigit() or len(datos["seccion"]) > 4:
            errores["seccion"] = "La sección debe tener máximo 4 dígitos"

        if not datos["fecha"]:
            errores["fecha"] = "Este campo es obligatorio"

        if not errores:
            return redirect(
                url_for(
                    "confirmardatos",
                    nombre=datos["nombre"],
                    curp=datos["curp"],
                    telefono=datos["telefono"],
                    cp=datos["cp"],
                    colonia=datos["colonia"],
                    seccion=datos["seccion"],
                    fecha=datos["fecha"]
                )
            )

    return render_template(
        "registro.html",
        errores=errores,
        datos=datos
    )

ESTATUS = {
    "en_firma": "En firma",
    "aprobado": "Aprobado",
    "cancelado": "Cancelado",
}

@app.route("/busqueda")
def busqueda():
    sql = """SELECT id, nombre, curp, cp, telefono,
                    departamento, seccion, apoyo, estado
             FROM apoyos
             WHERE 1=1"""
    params = []

    # Cuando exista el login: cada departamento solo ve lo suyo.
    # Mientras no haya sesión con rol, no se filtra nada. 

    sql += " ORDER BY id DESC"

    conexion = obtener_conexion()
    registros = conexion.execute(sql, params).fetchall()
    conexion.close()

    return render_template("busqueda.html", registros=registros)

@app.route("/informe")
def informe():
    return render_template("informe.html")


@app.route("/api/kpis")
def api_kpis():
    conexion = obtener_conexion()
    total = conexion.execute("SELECT COUNT(*) AS n FROM apoyos").fetchone()["n"]
    aprobados = conexion.execute(
        "SELECT COUNT(*) AS n FROM apoyos WHERE estado = 'aprobado'"
    ).fetchone()["n"]
    en_firma = conexion.execute(
        "SELECT COUNT(*) AS n FROM apoyos WHERE estado = 'en_firma'"
    ).fetchone()["n"]
    cancelados = conexion.execute(
        "SELECT COUNT(*) AS n FROM apoyos WHERE estado = 'cancelado'"
    ).fetchone()["n"]
    conexion.close()

    return jsonify({
        "total": total,
        "aprobados": aprobados,
        "en_firma": en_firma,
        "cancelados": cancelados
    })


@app.route("/api/graficas/departamento")
def api_grafica_departamento():
    conexion = obtener_conexion()
    filas = conexion.execute("""
        SELECT departamento, COUNT(*) AS total
        FROM apoyos
        GROUP BY departamento
        ORDER BY total DESC
    """).fetchall()
    conexion.close()

    return jsonify({
        "labels": [f["departamento"] for f in filas],
        "valores": [f["total"] for f in filas]
    })


@app.route("/api/graficas/mensual")
def api_grafica_mensual():
    conexion = obtener_conexion()
    filas = conexion.execute("""
        SELECT SUBSTR(fecha, 1, 7) AS mes, COUNT(*) AS total
        FROM apoyos
        GROUP BY mes
        ORDER BY mes
    """).fetchall()
    conexion.close()

    return jsonify({
        "labels": [f["mes"] for f in filas],
        "valores": [f["total"] for f in filas]
    })


@app.route("/informe/descargar-datos")
def descargar_datos():
    conexion = obtener_conexion()
    filas = conexion.execute("SELECT * FROM apoyos ORDER BY id").fetchall()
    conexion.close()

    salida = io.StringIO()
    escritor = csv.writer(salida)

    if filas:
        columnas = filas[0].keys()
        escritor.writerow(columnas)
        for fila in filas:
            escritor.writerow([fila[c] for c in columnas])

    respuesta = Response(salida.getvalue(), mimetype="text/csv")
    respuesta.headers["Content-Disposition"] = (
        "attachment; filename=apoyos_datos.csv"
    )
    return respuesta


@app.route("/informe/cargar-datos", methods=["POST"])
def cargar_datos():
    archivo = request.files.get("archivo")

    if not archivo or archivo.filename == "":
        flash("No se seleccionó ningún archivo.", "error")
        return redirect(url_for("informe"))

    try:
        contenido = archivo.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("El archivo debe estar codificado en UTF-8.", "error")
        return redirect(url_for("informe"))

    # Detectar si el archivo usa coma o punto y coma como separador.
    muestra = contenido[:2048]

    try:
        dialecto = csv.Sniffer().sniff(muestra, delimiters=",;")
    except csv.Error:
        dialecto = csv.excel

    lector = csv.DictReader(
        io.StringIO(contenido),
        dialect=dialecto
    )

    # Normalizar encabezados
    lector.fieldnames = [
        (campo or "").strip().lower()
        for campo in (lector.fieldnames or [])
    ]

    columnas_esperadas = {
        "nombre",
        "curp",
        "telefono",
        "cp",
        "colonia",
        "seccion",
        "fecha",
        "edad",
        "sexo",
        "domicilio",
        "apoyo",
        "descripcion",
        "departamento",
        "estado"
    }

    columnas_encontradas = set(lector.fieldnames)

    if not columnas_esperadas.issubset(columnas_encontradas):
        faltantes = columnas_esperadas - columnas_encontradas

        flash(
            f"Faltan estas columnas en tu archivo: "
            f"{', '.join(sorted(faltantes))}. "
            f"Columnas detectadas: "
            f"{', '.join(sorted(columnas_encontradas))}",
            "error"
        )

        return redirect(url_for("informe"))

    conexion = obtener_conexion()

    insertados = 0
    lote_carga = uuid.uuid4().hex[:8]
    momento_carga = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Valores admitidos
    estados_validos = {
        "aprobado",
        "en_firma",
        "cancelado"
    }

    # Valores que se consideran como fecha inexistente.
    fechas_vacias = {
        "",
        "null",
        "none",
        "nan",
        "n/a",
        "na"
    }

    for numero_fila, fila in enumerate(lector, start=2):
        # Normalizar claves por seguridad.
        fila = {
            (clave or "").strip().lower(): valor
            for clave, valor in fila.items()
        }

        fecha_original = (fila.get("fecha") or "").strip()

        if fecha_original.lower() in fechas_vacias:
            # Si no tiene fecha, se guarda la fecha de la importación.
            fecha_normalizada = datetime.now().strftime("%Y-%m-%d")
        else:
            fecha_normalizada = normalizar_fecha(fecha_original)

            # Si el texto tiene un formato inválido, tampoco se detiene
            # la carga: se usa la fecha actual.
            if fecha_normalizada is None:
                fecha_normalizada = datetime.now().strftime("%Y-%m-%d")


        estado_original = (fila.get("estado") or "aprobado").strip()

        estado = (
            estado_original
            .lower()
            .replace(" ", "_")
        )

        if estado not in estados_validos:
            conexion.close()

            flash(
                f"El estado '{estado_original}' de la fila "
                f"{numero_fila} no es válido. "
                "Usa: Aprobado, En firma o Cancelado.",
                "error"
            )

            return redirect(url_for("informe"))

        conexion.execute(
            """
            INSERT INTO apoyos (
                nombre,
                curp,
                telefono,
                cp,
                colonia,
                seccion,
                fecha,
                edad,
                sexo,
                domicilio,
                apoyo,
                descripcion,
                departamento,
                estado,
                fecha_creacion,
                lote_carga
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (fila.get("nombre") or "").strip(),
                (fila.get("curp") or "").strip().upper(),
                (fila.get("telefono") or "").strip(),
                (fila.get("cp") or "").strip(),
                (fila.get("colonia") or "").strip(),
                (fila.get("seccion") or "").strip(),
                fecha_normalizada,
                (fila.get("edad") or "").strip(),
                (fila.get("sexo") or "").strip(),
                (fila.get("domicilio") or "").strip(),
                (fila.get("apoyo") or "").strip(),
                (fila.get("descripcion") or "").strip(),
                (fila.get("departamento") or "").strip(),
                estado,
                momento_carga,
                lote_carga
            )
        )

        insertados += 1

    conexion.commit()
    conexion.close()

    flash(
        f"Se cargaron {insertados} registros correctamente. "
        f"Lote de carga: {lote_carga}. "
        "Puedes deshacerlo desde Historial de cargas de datos.",
        "exito"
    )

    return redirect(url_for("informe"))



@app.route("/detalle/<int:id>/eliminar", methods=["POST"])
def eliminar_registro(id):

    # Verificar que sea administrador
    if session.get("rol") != "administrador":
        flash("No tienes permiso para eliminar registros.", "error")
        return redirect(url_for("busqueda"))

    conexion = obtener_conexion()

    registro = conexion.execute(
        "SELECT id, estado FROM apoyos WHERE id = ?",
        (id,)
    ).fetchone()

    if not registro:
        conexion.close()
        flash("El registro no existe.", "error")
        return redirect(url_for("busqueda"))

    # Solo se pueden eliminar aprobados o cancelados
    if registro["estado"] not in ["aprobado", "cancelado"]:
        conexion.close()
        flash("Solo puedes eliminar registros aprobados o cancelados.", "error")
        return redirect(url_for("busqueda"))

    conexion.execute(
        "DELETE FROM apoyos WHERE id = ?",
        (id,)
    )

    conexion.commit()
    conexion.close()

    flash("Registro eliminado correctamente.", "success")

    return redirect(url_for("busqueda"))


@app.route("/informe/cargas")
def historial_cargas():
    conexion = obtener_conexion()
    lotes = conexion.execute("""
        SELECT
            lote_carga,
            MIN(fecha_creacion) AS fecha_carga,
            COUNT(*) AS total
        FROM apoyos
        WHERE lote_carga IS NOT NULL
        GROUP BY lote_carga
        ORDER BY fecha_carga DESC
    """).fetchall()
    conexion.close()

    return render_template("historial_cargas.html", lotes=lotes)

@app.route("/informe/cargas/<lote_carga>/eliminar", methods=["POST"])
def eliminar_lote(lote_carga):
    conexion = obtener_conexion()

    fila = conexion.execute(
        "SELECT COUNT(*) AS n FROM apoyos WHERE lote_carga = ?",
        (lote_carga,)
    ).fetchone()

    if fila["n"] == 0:
        conexion.close()
        flash("Ese lote no existe o ya fue eliminado.", "error")
        return redirect(url_for("historial_cargas"))

    conexion.execute("DELETE FROM apoyos WHERE lote_carga = ?", (lote_carga,))
    conexion.commit()
    conexion.close()

    flash(f"Se eliminaron {fila['n']} registros del lote {lote_carga}.", "exito")
    return redirect(url_for("historial_cargas"))


@app.route("/informe/descargar-informe")
def descargar_informe():

    conexion = obtener_conexion()

    total = conexion.execute(
        "SELECT COUNT(*) AS n FROM apoyos"
    ).fetchone()["n"]

    aprobados = conexion.execute(
        "SELECT COUNT(*) AS n FROM apoyos WHERE estado = 'aprobado'"
    ).fetchone()["n"]

    en_firma = conexion.execute(
        "SELECT COUNT(*) AS n FROM apoyos WHERE estado = 'en_firma'"
    ).fetchone()["n"]

    cancelados = conexion.execute(
        "SELECT COUNT(*) AS n FROM apoyos WHERE estado = 'cancelado'"
    ).fetchone()["n"]

    por_depto = conexion.execute("""
        SELECT departamento, COUNT(*) AS total
        FROM apoyos
        GROUP BY departamento
        ORDER BY total DESC
    """).fetchall()

    por_mes = conexion.execute("""
    SELECT
        strftime('%Y-%m', fecha) AS mes,
        COUNT(*) AS total
    FROM apoyos
    GROUP BY mes
    ORDER BY mes
""").fetchall()

    conexion.close()

    departamentos = [f["departamento"] for f in por_depto]
    cantidades = [f["total"] for f in por_depto]

    plt.figure(figsize=(8, 4.5))

    plt.bar(
        departamentos,
        cantidades,
        color="#f2a541",
        edgecolor="#d88920"
    )

    plt.title("Apoyos por departamento")
    plt.xlabel("Departamento")
    plt.ylabel("Cantidad de apoyos")

    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()

    grafica_departamento = io.BytesIO()

    plt.savefig(
        grafica_departamento,
        format="png",
        dpi=150,
        bbox_inches="tight"
    )

    plt.close()

    grafica_departamento.seek(0)

    meses = [f["mes"] for f in por_mes]
    cantidades_mensuales = [f["total"] for f in por_mes]

    plt.figure(figsize=(8, 4.5))

    plt.plot(
        meses,
        cantidades_mensuales,
        marker="o",
        linewidth=2
    )

    plt.title("Registros por mes")
    plt.xlabel("Mes")
    plt.ylabel("Cantidad de registros")

    plt.xticks(rotation=30, ha="right")
    plt.grid(axis="y", alpha=0.3)

    plt.tight_layout()

    grafica_mensual = io.BytesIO()

    plt.savefig(
        grafica_mensual,
        format="png",
        dpi=150,
        bbox_inches="tight"
    )

    plt.close()

    grafica_mensual.seek(0)

    buffer = io.BytesIO()

    documento = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40
    )

    estilos = getSampleStyleSheet()
    elementos = []

    elementos.append(
        Paragraph(
            "Informe de Apoyos Sociales · Cortázar",
            estilos["Title"]
        )
    )

    elementos.append(Spacer(1, 15))

    elementos.append(
        Paragraph(
            "<b>Resumen de indicadores</b>",
            estilos["Heading2"]
        )
    )

    elementos.append(Spacer(1, 8))

    datos_kpis = [
        ["Indicador", "Cantidad"],
        ["Total registrados", str(total)],
        ["Aprobados", str(aprobados)],
        ["En firma", str(en_firma)],
        ["Cancelados", str(cancelados)]
    ]

    tabla_kpis = Table(
        datos_kpis,
        colWidths=[300, 120],
        hAlign="LEFT"
    )

    tabla_kpis.setStyle(
        TableStyle([
            (
                "BACKGROUND",
                (0, 0),
                (-1, 0),
                colors.HexColor("#0e7a3d")
            ),
            (
                "TEXTCOLOR",
                (0, 0),
                (-1, 0),
                colors.white
            ),
            (
                "GRID",
                (0, 0),
                (-1, -1),
                0.5,
                colors.grey
            ),
            (
                "ALIGN",
                (1, 1),
                (1, -1),
                "CENTER"
            ),
            (
                "FONTSIZE",
                (0, 0),
                (-1, -1),
                10
            ),
            (
                "BOTTOMPADDING",
                (0, 0),
                (-1, 0),
                8
            ),
            (
                "TOPPADDING",
                (0, 0),
                (-1, 0),
                8
            )
        ])
    )

    elementos.append(tabla_kpis)

    elementos.append(Spacer(1, 20))

    elementos.append(
        Paragraph(
            "Apoyos por departamento",
            estilos["Heading2"]
        )
    )

    elementos.append(Spacer(1, 8))

    elementos.append(
        Image(
            grafica_departamento,
            width=500,
            height=280
        )
    )

    elementos.append(Spacer(1, 20))

    elementos.append(
        Paragraph(
            "Registros por mes",
            estilos["Heading2"]
        )
    )

    elementos.append(Spacer(1, 8))

    elementos.append(
        Image(
            grafica_mensual,
            width=500,
            height=280
        )
    )
    documento.build(elementos)

    buffer.seek(0)

    return Response(
        buffer.getvalue(),
        mimetype="application/pdf",
        headers={
            "Content-Disposition":
            "attachment; filename=informe_apoyos.pdf"
        }
    )


@app.route("/detalle/<int:id>", methods=["GET", "POST"])
def detalle(id):

    conexion = obtener_conexion()

    # Buscar el registro
    fila = conexion.execute(
        """
        SELECT *
        FROM apoyos
        WHERE id = ?
        """,
        (id,)
    ).fetchone()

    # Si no existe
    if fila is None:
        conexion.close()
        return "Registro no encontrado", 404

    # Si se quiere cambiar el estado
    if request.method == "POST":

        estado_actual = fila["estado"]
        nuevo_estado = request.form.get("estado")

        # Si ya está aprobado o cancelado, no se puede cambiar
        if estado_actual in ("aprobado", "cancelado"):

            conexion.close()

            flash(
                "Este registro ya está finalizado y no puede modificarse.",
                "error"
            )

            return redirect(url_for("detalle", id=id))

        # Estados permitidos
        estados_validos = {
            "aprobado",
            "en_firma",
            "cancelado"
        }

        if nuevo_estado not in estados_validos:

            conexion.close()

            flash(
                "El estado seleccionado no es válido.",
                "error"
            )

            return redirect(url_for("detalle", id=id))

        # Guardar el nuevo estado
        conexion.execute(
            """
            UPDATE apoyos
            SET estado = ?
            WHERE id = ?
            """,
            (nuevo_estado, id)
        )

        conexion.commit()
        conexion.close()

        flash(
            "El estado se actualizó correctamente.",
            "exito"
        )

        # Regresar a búsqueda
        return redirect(url_for("busqueda"))

    conexion.close()

    # Información que se manda a detalle.html
    registro = {
        "id": fila["id"],
        "nombre": fila["nombre"],
        "curp": fila["curp"],
        "telefono": fila["telefono"],
        "cp": fila["cp"],
        "colonia": fila["colonia"],
        "seccion": fila["seccion"],
        "fecha": fila["fecha"],
        "edad": fila["edad"],
        "sexo": fila["sexo"],
        "domicilio": fila["domicilio"],
        "apoyo": fila["apoyo"],
        "departamento": fila["departamento"],
        "estado": fila["estado"]
    }

    return render_template(
        "detalle.html",
        registro=registro
    )

@app.route("/confirmardatos", methods=["GET", "POST"])
def confirmardatos():
    datos = {
        "nombre": request.args.get("nombre", ""),
        "curp": request.args.get("curp", ""),
        "telefono": request.args.get("telefono", ""),
        "cp": request.args.get("cp", ""),
        "colonia": request.args.get("colonia", ""),
        "seccion": request.args.get("seccion", ""),
        "fecha": request.args.get("fecha", "")
    }

    if request.method == "POST":
        accion = request.form.get("accion")

        if accion == "si":
            return redirect(
                url_for(
                    "seguimiento",
                    nombre=datos["nombre"],
                    curp=datos["curp"],
                    telefono=datos["telefono"],
                    cp=datos["cp"],
                    colonia=datos["colonia"],
                    seccion=datos["seccion"],
                    fecha=datos["fecha"]
                )
            )

        if accion == "volver":
            return redirect(url_for("registro"))

    return render_template("confirmardatos.html", datos=datos)


@app.route("/seguimiento", methods=["GET", "POST"])
def seguimiento():
    errores = {}

    datos = {
        "nombre": request.args.get("nombre", ""),
        "curp": request.args.get("curp", ""),
        "telefono": request.args.get("telefono", ""),
        "cp": request.args.get("cp", ""),
        "colonia": request.args.get("colonia", ""),
        "seccion": request.args.get("seccion", ""),
        "fecha": request.args.get("fecha", "")
    }

    seguimiento_datos = {
        "edad": "",
        "sexo": "",
        "domicilio": "",
        "apoyo": "",
        "descripcion": "",
        "departamento": ""
    }

    if request.method == "POST":
        seguimiento_datos["edad"] = request.form.get(
            "edad", ""
        ).strip()

        seguimiento_datos["sexo"] = request.form.get(
            "sexo", ""
        ).strip()

        seguimiento_datos["domicilio"] = request.form.get(
            "domicilio", ""
        ).strip()

        seguimiento_datos["apoyo"] = request.form.get(
            "apoyo", ""
        ).strip()

        seguimiento_datos["descripcion"] = request.form.get(
                    "descripcion", ""
                ).strip()

        seguimiento_datos["departamento"] = request.form.get(
            "departamento", ""
        ).strip()

        if not seguimiento_datos["edad"].isdigit():
            errores["edad"] = "La edad debe ser numérica"
        elif len(seguimiento_datos["edad"]) > 3:
            errores["edad"] = "La edad debe tener máximo 3 dígitos"

        if not seguimiento_datos["sexo"]:
            errores["sexo"] = "Este campo es obligatorio"


        if not seguimiento_datos["apoyo"]:
            errores["apoyo"] = "Este campo es obligatorio"

        if not seguimiento_datos["departamento"]:
            errores["departamento"] = "Este campo es obligatorio"

        if not errores:
            conexion = obtener_conexion()

            # Antes si la persona ya estaba registrada en este mismo
            # apoyo, se bloqueaba el registro mostrando "apoyo_existente.html".
            # Peero ahora se avisa con un mensaje de advertencia, pero se deja
            # continuar el registro; la decisión queda bajo responsabilidad
            # de quien está registrando.
            registro_existente = conexion.execute(
                """
                SELECT *
                FROM apoyos
                WHERE curp = ?
                AND apoyo = ?
                AND departamento = ?
                """,
                (
                    datos["curp"],
                    seguimiento_datos["apoyo"],
                    seguimiento_datos["departamento"]
                )
            ).fetchone()

            if registro_existente:
                flash(
                    "Advertencia: esta persona ya está registrada en "
                    "este mismo apoyo. Se registrará de nuevo bajo tu "
                    "responsabilidad.",
                    "advertencia"
                )

            conexion.execute(
                 """
                INSERT INTO apoyos (
                    nombre,
                    curp,
                    telefono,
                    cp,
                    colonia,
                    seccion,
                    fecha,
                    edad,
                    sexo,
                    domicilio,
                    apoyo,
                    descripcion,
                    departamento,
                    estado, 
                    fecha_creacion
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datos["nombre"],
                    datos["curp"],
                    datos["telefono"],
                    datos["cp"],
                    datos["colonia"],
                    datos["seccion"],
                    datos["fecha"],
                    seguimiento_datos["edad"],
                    seguimiento_datos["sexo"],
                    seguimiento_datos["domicilio"],
                    seguimiento_datos["apoyo"],
                    seguimiento_datos["descripcion"],
                    seguimiento_datos["departamento"],
                    "en_firma",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S" )
                )
            )

            conexion.commit()
            conexion.close()

            flash(
                "La persona fue registrada exitosamente.",
                "exito"
            )

            return redirect(
                url_for(
                    "otro_apoyo",
                    nombre=datos["nombre"],
                    curp=datos["curp"],
                    telefono=datos["telefono"],
                    cp=datos["cp"],
                    colonia=datos["colonia"],
                    seccion=datos["seccion"],
                    fecha=datos["fecha"]
                )
            )

    return render_template(
        "seguimiento.html",
        errores=errores,
        datos=datos,
        seguimiento_datos=seguimiento_datos
    )

@app.route("/decidir-otro-apoyo", methods=["POST"])
def decidir_otro_apoyo():
    respuesta = request.form.get("respuesta")

    datos = {
        "nombre": request.form.get("nombre", ""),
        "curp": request.form.get("curp", ""),
        "telefono": request.form.get("telefono", ""),
        "cp": request.form.get("cp", ""),
        "colonia": request.form.get("colonia", ""),
        "seccion": request.form.get("seccion", ""),
        "fecha": request.form.get("fecha", "")
    }

    if respuesta == "si":
        return redirect(
            url_for(
                "seguimiento",
                nombre=datos["nombre"],
                curp=datos["curp"],
                telefono=datos["telefono"],
                cp=datos["cp"],
                colonia=datos["colonia"],
                seccion=datos["seccion"],
                fecha=datos["fecha"]
            )
        )

    return redirect(url_for("registro"))


@app.route("/otro-apoyo")
def otro_apoyo():

    # Recuperar los datos personales
    datos = {
        "nombre": request.args.get("nombre", ""),
        "curp": request.args.get("curp", ""),
        "telefono": request.args.get("telefono", ""),
        "cp": request.args.get("cp", ""),
        "colonia": request.args.get("colonia", ""),
        "seccion": request.args.get("seccion", ""),
        "fecha": request.args.get("fecha", "")
    }

    # Consultar los apoyos de la persona en MySQL
    conexion = obtener_conexion()

    seguimiento_datos = conexion.execute(
        """
        SELECT apoyo, departamento, estado
        FROM apoyos
        WHERE curp = ?
        ORDER BY id DESC
        """,
         (datos["curp"],)
    ).fetchall()

    conexion.close()

    return render_template(
        "apoyo_existente.html",
        datos=datos,
        seguimiento_datos=seguimiento_datos,
        ESTATUS=ESTATUS
    )

@app.route("/modelos-disponibles")
def modelos_disponibles():
    try:
        api_key = os.environ.get("GEMINI_API_KEY")

        if not api_key:
            return jsonify({"error": "Falta la clave API"}), 500

        client = genai.Client(api_key=api_key)

        disponibles = []

        for modelo in client.models.list():
            if "generateContent" in (modelo.supported_actions or []):
                disponibles.append(modelo.name)

        return jsonify({
            "total": len(disponibles),
            "modelos": disponibles
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)