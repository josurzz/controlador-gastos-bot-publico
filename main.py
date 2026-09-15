#!/usr/bin/env python3
"""
Controlador de gastos por audio.

Flujo:
  1. El usuario manda un audio (nota de voz) a un bot de Telegram.
  2. El audio se transcribe localmente con faster-whisper (sin costo, sin internet).
  3. El texto transcripto se manda a una IA (Anthropic por default, configurable
     - ver AI_PROVIDER) para extraer: monto, categoria, medio_pago, descripcion y fecha.
  4. Se agrega una fila nueva a una Google Sheet con esos datos.
  5. Se le responde al usuario por Telegram confirmando lo que se registró.

Este archivo es compartido entre todas las personas que usan el bot (ver
people/<nombre>/). Lo que cambia por persona (categorias, medios de pago,
credenciales, Sheet) vive en people/<nombre>/config.json y
people/<nombre>/.env - nunca hardcodees algo especifico de una persona
aca. Cada servicio de systemd corre esto con WorkingDirectory apuntando a
la carpeta de esa persona, asi ".env" y "config.json" se resuelven solos.

Pensado para correr 24/7 en una PC propia (ver systemd/gastos-bot@.service).
"""

import asyncio
import calendar
import json
import logging
import logging.handlers
import os
import re
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from google.oauth2.service_account import Credentials
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Configuracion: variables de entorno (secretos/credenciales, en .env de la
# carpeta de cada persona) + config.json (categorias/medios de pago, NO
# secreto, tambien en la carpeta de cada persona).
# --------------------------------------------------------------------------

# load_dotenv() sin argumentos busca ".env" empezando desde la carpeta de
# ESTE archivo (main.py) hacia arriba - nunca hacia adentro de
# people/<nombre>/, que es donde en realidad vive el ".env" de cada persona.
# Por eso hay que pasarle el directorio de trabajo (que cada servicio de
# systemd fija a people/<nombre>/) de forma explicita.
load_dotenv(os.path.join(os.getcwd(), ".env"))

# Nombre de la persona: se deduce del nombre de su carpeta (people/<nombre>,
# que es el WorkingDirectory del servicio) - se usa solo para el archivo de
# log, para no necesitar un campo de config aparte.
NOMBRE_PERSONA = os.path.basename(os.getcwd().rstrip(os.sep)) or "bot"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Que IA interpreta el texto transcripto: "anthropic" (default), "openai",
# "gemini" o "ollama" (local, sin costo). Cada una lee su propia API key/modelo
# mas abajo (_llamar_ia) - solo hace falta configurar la del proveedor elegido,
# las de los demas pueden faltar en el .env sin problema.
AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic").strip().lower()

GOOGLE_SHEETS_ID = os.environ["GOOGLE_SHEETS_ID"]
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
)
GOOGLE_SHEET_TAB_NAME = os.environ.get("GOOGLE_SHEET_TAB_NAME", "Gastos")

WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")

TIMEZONE = os.environ.get("TIMEZONE", "America/Argentina/Buenos_Aires")

# Si esta seteado, el bot solo responde a este user_id de Telegram (recomendado,
# ya que estos datos son financieros y personales). Dejarlo vacio mientras se
# configura por primera vez para poder obtener el propio user_id con /start.
ALLOWED_TELEGRAM_USER_ID = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").strip()

CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.json")
with open(CONFIG_FILE, encoding="utf-8") as _f:
    _config = json.load(_f)

# Estas dos son solo la SEMILLA inicial: se usan para crear la pestaña
# "Config" de la Sheet la primera vez que el bot corre para esta persona.
# A partir de que esa pestaña existe, es la fuente real (editable desde la
# webapp, sin reiniciar nada) - ver _leer_categorias_y_medios_pago(), que se
# llama en cada mensaje. config.json ya no se vuelve a leer para esto.
CATEGORIAS_SEMILLA = _config["categorias"]
MEDIOS_PAGO_SEMILLA = _config["medios_pago"]
# Texto libre, opcional: notas para la IA sobre que significan categorias
# particulares de esta persona (ej. "Boca" = Boca Juniors). Este si sigue
# viviendo en config.json (no en la Sheet) - es contenido de prompt, no una
# lista de opciones. Se inserta tal cual en el prompt si no esta vacio.
ACLARACIONES_CATEGORIAS = _config.get("aclaraciones_categorias", "").strip()

HEADER_ROW = [
    "Fecha y hora",
    "Monto",
    "Categoria",
    "Medio de pago",
    "Descripcion",
    "Cuota",
    "Mes de pago",
    "Monto total",
    "Texto original",
]

# Tope de cuotas para evitar cargar cientos de filas si la IA se confunde
# interpretando un numero (ej. "cuenta 1234" interpretado como 1234 cuotas).
MAX_CUOTAS = 60

MESES_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

# El cierre real de la tarjeta de credito no es un dia fijo del mes (ej. "el
# ultimo jueves", que puede caer entre fin de un mes y los primeros dias del
# siguiente) - por eso, en vez de asumir un dia de corte, se pregunta cuando
# la fecha de compra cae en una ventana ambigua alrededor del limite del mes.
DIAS_ANTES_FIN_DE_MES_PARA_PREGUNTAR = 5
DIAS_DESPUES_INICIO_DE_MES_PARA_PREGUNTAR = 4


def columna_a1(indice_1_based: int) -> str:
    letras = ""
    while indice_1_based > 0:
        indice_1_based, resto = divmod(indice_1_based - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


MONTO_COL = columna_a1(HEADER_ROW.index("Monto") + 1)
FECHA_COL = columna_a1(HEADER_ROW.index("Fecha y hora") + 1)
CATEGORIA_COL = columna_a1(HEADER_ROW.index("Categoria") + 1)
MEDIO_PAGO_COL = columna_a1(HEADER_ROW.index("Medio de pago") + 1)
DESCRIPCION_COL = columna_a1(HEADER_ROW.index("Descripcion") + 1)
CUOTA_COL = columna_a1(HEADER_ROW.index("Cuota") + 1)
TEXTO_ORIGINAL_COL = columna_a1(HEADER_ROW.index("Texto original") + 1)
MONTO_TOTAL_COL = columna_a1(HEADER_ROW.index("Monto total") + 1)
MES_PAGO_COL = columna_a1(HEADER_ROW.index("Mes de pago") + 1)

# Los logs de TODAS las personas quedan juntos en logs/, un archivo por
# persona (logs/<nombre>.log) - relativo a donde vive este archivo, no al
# WorkingDirectory de cada servicio (que es la carpeta de la persona).
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_formato_log = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

_handler_archivo = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "{}.log".format(NOMBRE_PERSONA)), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_handler_archivo.setFormatter(_formato_log)

_handler_consola = logging.StreamHandler()
_handler_consola.setFormatter(_formato_log)

logging.basicConfig(level=logging.INFO, handlers=[_handler_consola, _handler_archivo])
logger = logging.getLogger("gastos-bot")

# httpx (usado por python-telegram-bot) loguea cada request HTTP a nivel INFO,
# incluido el polling de getUpdates cada pocos segundos - y ademas esas lineas
# incluyen el token del bot en la URL. Se sube a WARNING para no llenar el
# archivo de logs con eso ni escribir el token repetidamente en disco.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# python-telegram-bot tambien loguea a INFO sus propios eventos de ciclo de
# vida (ej. "Application started"/"Application stopped") en cada arranque o
# reinicio, sin actividad real de por medio. Se sube a WARNING para que el
# archivo solo tenga las lineas que el bot mismo escribe a proposito.
logging.getLogger("telegram").setLevel(logging.WARNING)

# --------------------------------------------------------------------------
# Inicializacion de clientes (se hace una sola vez al arrancar)
# --------------------------------------------------------------------------

logger.info("Cargando modelo Whisper local (%s, %s, %s)...", WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE)
whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)

_gspread_scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
]
_google_creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=_gspread_scopes)
gspread_client = gspread.authorize(_google_creds)


def get_worksheet():
    sheet = gspread_client.open_by_key(GOOGLE_SHEETS_ID)
    try:
        worksheet = sheet.worksheet(GOOGLE_SHEET_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=GOOGLE_SHEET_TAB_NAME, rows=1000, cols=len(HEADER_ROW))

    first_row = worksheet.row_values(1)
    if first_row != HEADER_ROW:
        worksheet.update(range_name="A1", values=[HEADER_ROW])
        _formatear_columnas(worksheet)

    return worksheet


def _formatear_columnas(worksheet) -> None:
    """Fija el formato de cada columna ANTES de que se le escriba nada, para
    que Sheets no adivine mal el tipo de dato (con USER_ENTERED, el valor que
    se escribe se interpreta segun el formato/contenido de la celda - una
    columna sin formato puede terminar leyendo "1/3" como una fecha en vez de
    la fraccion de cuota que es, o un monto como texto plano sin $)."""
    formato_fecha = {"numberFormat": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm"}}
    formato_mes_pago = {"numberFormat": {"type": "DATE", "pattern": "mmmm yyyy"}}
    formato_moneda = {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}}
    # TEXT fuerza a Sheets a NUNCA reinterpretar el valor (ni como fecha, ni
    # como numero) - clave en "Cuota" ("1/3" se confunde con una fecha) y
    # defensivo en el resto de las columnas de texto libre.
    formato_texto = {"numberFormat": {"type": "TEXT"}}

    worksheet.format("{0}2:{0}".format(FECHA_COL), formato_fecha)
    worksheet.format("{0}2:{0}".format(MES_PAGO_COL), formato_mes_pago)
    worksheet.format("{0}2:{0}".format(MONTO_COL), formato_moneda)
    worksheet.format("{0}2:{0}".format(MONTO_TOTAL_COL), formato_moneda)
    worksheet.format("{0}2:{0}".format(CUOTA_COL), formato_texto)
    worksheet.format("{0}2:{0}".format(CATEGORIA_COL), formato_texto)
    worksheet.format("{0}2:{0}".format(MEDIO_PAGO_COL), formato_texto)
    worksheet.format("{0}2:{0}".format(DESCRIPCION_COL), formato_texto)
    worksheet.format("{0}2:{0}".format(TEXTO_ORIGINAL_COL), formato_texto)


NOMBRE_HOJA_CONFIG = "Config"
ENCABEZADO_HOJA_CONFIG = ["Categoria", "ColorSlot", "MedioPago"]
# Cuantos colores fijos tiene la paleta de la webapp (--series-1..--series-N
# en style.css) - una categoria nueva agregada desde la webapp solo puede
# elegir un ColorSlot de este rango, y nunca uno ya usado por otra.
MAX_COLOR_SLOTS = 14


def _crear_hoja_config_inicial(sheet):
    """Crea la pestaña 'Config', semillada desde CATEGORIAS_SEMILLA/
    MEDIOS_PAGO_SEMILLA (config.json). Solo se llama la primera vez que esta
    persona corre el bot (si la pestaña ya existe, se la deja tal cual esta:
    a partir de ahi es editable desde la webapp, y esa es la fuente real,
    no config.json)."""
    hoja_config = sheet.add_worksheet(title=NOMBRE_HOJA_CONFIG, rows=200, cols=3)
    largo = max(len(CATEGORIAS_SEMILLA), len(MEDIOS_PAGO_SEMILLA))
    filas = [ENCABEZADO_HOJA_CONFIG]
    for i in range(largo):
        categoria = CATEGORIAS_SEMILLA[i] if i < len(CATEGORIAS_SEMILLA) else ""
        color_slot = str(i + 1) if categoria and i < MAX_COLOR_SLOTS else ""
        medio_pago = MEDIOS_PAGO_SEMILLA[i] if i < len(MEDIOS_PAGO_SEMILLA) else ""
        filas.append([categoria, color_slot, medio_pago])

    hoja_config.update(range_name="A1", values=filas, value_input_option="USER_ENTERED")
    hoja_config.format("A1:C1", {"textFormat": {"bold": True}})
    hoja_config.format("A2:A", {"numberFormat": {"type": "TEXT"}})
    hoja_config.format("C2:C", {"numberFormat": {"type": "TEXT"}})
    return hoja_config


def asegurar_hoja_config() -> None:
    """Se asegura de que la pestaña 'Config' exista - la crea (semillada
    desde config.json) si es la primera vez para esta persona. Se llama una
    sola vez al arrancar el bot; nunca sobreescribe una pestaña que ya
    existe, porque a partir de ahi la puede estar editando alguien desde la
    webapp (agregar categorias, elegir colores, agregar medios de pago)."""
    sheet = gspread_client.open_by_key(GOOGLE_SHEETS_ID)
    try:
        sheet.worksheet(NOMBRE_HOJA_CONFIG)
    except gspread.exceptions.WorksheetNotFound:
        _crear_hoja_config_inicial(sheet)
        logger.info("Pestaña 'Config' creada (primera vez), semillada desde config.json")


def _leer_categorias_y_medios_pago() -> tuple:
    """Lee categorias/medios_pago ACTUALES desde la pestaña 'Config' de la
    Sheet - se llama en cada mensaje (no solo al arrancar), asi una
    categoria o medio de pago agregado desde la webapp esta disponible para
    la IA de inmediato, sin reiniciar el bot. Si por algun motivo la
    pestaña esta vacia o no se puede leer, cae en la semilla de config.json
    como ultimo recurso (nunca deja al bot sin ninguna categoria valida)."""
    try:
        sheet = gspread_client.open_by_key(GOOGLE_SHEETS_ID)
        hoja_config = sheet.worksheet(NOMBRE_HOJA_CONFIG)
        filas = hoja_config.get("A2:C", value_render_option="UNFORMATTED_VALUE")
    except Exception:
        logger.exception("No se pudo leer la pestaña 'Config', se usa la semilla de config.json")
        return list(CATEGORIAS_SEMILLA), list(MEDIOS_PAGO_SEMILLA)

    categorias = [str(fila[0]).strip() for fila in filas if len(fila) > 0 and str(fila[0]).strip()]
    medios_pago = [str(fila[2]).strip() for fila in filas if len(fila) > 2 and str(fila[2]).strip()]

    return (categorias or list(CATEGORIAS_SEMILLA)), (medios_pago or list(MEDIOS_PAGO_SEMILLA))


# --------------------------------------------------------------------------
# Transcripcion (bloqueante -> se corre en un thread aparte)
# --------------------------------------------------------------------------

def transcribir_audio(ruta_audio: str) -> str:
    logger.info("Transcribiendo audio con Whisper local")
    segments, _info = whisper_model.transcribe(ruta_audio, language="es")
    return " ".join(segment.text.strip() for segment in segments).strip()


# --------------------------------------------------------------------------
# Interpretacion del texto con Claude
# --------------------------------------------------------------------------

# Tope de gastos distintos que se aceptan en un mismo mensaje (para evitar
# cargar decenas de filas si la IA se confunde y desglosa de mas).
MAX_GASTOS_POR_MENSAJE = 10


def _validar_gasto(datos: dict, categorias: list, medios_pago: list) -> dict:
    if datos.get("categoria") not in categorias:
        datos["categoria"] = "Otros" if "Otros" in categorias else categorias[-1]
    try:
        datos["monto"] = float(datos.get("monto", 0))
    except (TypeError, ValueError):
        datos["monto"] = 0

    try:
        datos["cuotas"] = int(datos.get("cuotas", 1))
    except (TypeError, ValueError):
        datos["cuotas"] = 1
    if datos["cuotas"] < 1:
        datos["cuotas"] = 1
    elif datos["cuotas"] > MAX_CUOTAS:
        logger.warning("Cuotas fuera de rango (%s), se limita a %s", datos["cuotas"], MAX_CUOTAS)
        datos["cuotas"] = MAX_CUOTAS

    # La unica cuenta que se hace es esta, en Python (no se le confia a la IA):
    # si el monto que dio Claude es el valor de UNA cuota, se multiplica aca por
    # la cantidad de cuotas para obtener el total.
    if datos.get("monto_es_por_cuota") and datos["cuotas"] > 1:
        datos["monto"] = datos["monto"] * datos["cuotas"]

    if datos.get("medio_pago") not in medios_pago:
        # Defaults: en cuotas siempre es credito; si no, es debito. Cubre
        # tambien el caso de que la IA devuelva un medio de pago que esta
        # persona no tiene en su lista (ej: "Transferencia" para alguien que
        # la unifico con "Debito").
        datos["medio_pago"] = "Credito" if datos["cuotas"] > 1 else "Debito"

    if not isinstance(datos.get("fecha"), str) or not datos["fecha"]:
        datos["fecha"] = None  # se completa despues con fecha_hoy si hace falta

    return datos


# --------------------------------------------------------------------------
# Proveedores de IA: cada uno importa su propio SDK recien cuando se necesita
# (asi no hace falta instalar todos para usar uno solo) y lee sus propias
# variables de entorno - solo las del proveedor elegido en AI_PROVIDER tienen
# que estar en el .env, las de los demas pueden faltar sin problema. Todos
# devuelven lo mismo: el texto crudo de la respuesta (el JSON como string,
# _sin_ parsear todavia - eso lo hace interpretar_gastos).
# --------------------------------------------------------------------------

def _llamar_anthropic(prompt_sistema: str, texto_usuario: str) -> str:
    from anthropic import Anthropic

    api_key = os.environ["ANTHROPIC_API_KEY"]
    modelo = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
    client = Anthropic(api_key=api_key)
    respuesta = client.messages.create(
        model=modelo,
        max_tokens=1000,
        system=prompt_sistema,
        messages=[{"role": "user", "content": texto_usuario}],
    )
    return respuesta.content[0].text


def _llamar_openai(prompt_sistema: str, texto_usuario: str) -> str:
    from openai import OpenAI

    api_key = os.environ["OPENAI_API_KEY"]
    modelo = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key)
    respuesta = client.chat.completions.create(
        model=modelo,
        messages=[
            {"role": "system", "content": prompt_sistema},
            {"role": "user", "content": texto_usuario},
        ],
    )
    return respuesta.choices[0].message.content


def _llamar_gemini(prompt_sistema: str, texto_usuario: str) -> str:
    import google.generativeai as genai

    api_key = os.environ["GEMINI_API_KEY"]
    modelo = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(modelo, system_instruction=prompt_sistema)
    respuesta = model.generate_content(texto_usuario)
    return respuesta.text


def _llamar_ollama(prompt_sistema: str, texto_usuario: str) -> str:
    # Corre 100% local (sin costo, sin API key) contra un servidor Ollama
    # (https://ollama.com) ya instalado y con el modelo descargado
    # (ej: "ollama pull llama3.1").
    import requests

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    modelo = os.environ.get("OLLAMA_MODEL", "llama3.1")
    resp = requests.post(
        "{}/api/chat".format(host.rstrip("/")),
        json={
            "model": modelo,
            "messages": [
                {"role": "system", "content": prompt_sistema},
                {"role": "user", "content": texto_usuario},
            ],
            "stream": False,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


_PROVEEDORES_IA = {
    "anthropic": _llamar_anthropic,
    "openai": _llamar_openai,
    "gemini": _llamar_gemini,
    "ollama": _llamar_ollama,
}


def _llamar_ia(prompt_sistema: str, texto_usuario: str) -> str:
    llamar = _PROVEEDORES_IA.get(AI_PROVIDER)
    if llamar is None:
        raise ValueError(
            "AI_PROVIDER={!r} invalido. Opciones validas: {}".format(
                AI_PROVIDER, ", ".join(_PROVEEDORES_IA)
            )
        )
    return llamar(prompt_sistema, texto_usuario)


def interpretar_gastos(texto: str, fecha_hoy: str, categorias: list, medios_pago: list) -> list:
    bloque_aclaraciones = (
        "\nAclaracion sobre algunas categorias personales:\n{}\n".format(ACLARACIONES_CATEGORIAS)
        if ACLARACIONES_CATEGORIAS
        else ""
    )

    prompt_sistema = f"""Sos un asistente que interpreta mensajes hablados sobre gastos personales
en español rioplatense (Argentina) y los convierte en datos estructurados.

Categorias validas (elegi EXACTAMENTE una, tal cual esta escrita): {", ".join(categorias)}
Medios de pago validos (elegi EXACTAMENTE uno, tal cual esta escrito): {", ".join(medios_pago)}
{bloque_aclaraciones}
El texto puede describir UNA sola compra o VARIAS compras distintas en el mismo mensaje (ej: "gaste 500 en
el supermercado y despues 300 en el colectivo"). Devolve un JSON con un ARRAY, un objeto por cada compra
distinta que se menciona. Si el texto describe una sola compra, el array tiene un unico elemento.

Para CADA objeto del array:
Si el texto menciona que esa compra fue en cuotas, el medio de pago es "Credito" (asumilo aunque no lo diga
explicitamente).
Si el texto NO menciona que sea en cuotas y tampoco menciona un medio de pago, asumi "Debito".
Si el texto menciona explicitamente un medio de pago, usa el de tu lista que mas se parezca (ej: "efectivo"
o "en mano" -> "Efectivo" si esta en tu lista; si menciona uno que no esta en tu lista de medios de pago
validos, elegi el mas parecido - por ejemplo "transferencia" cuando no tenes esa categoria separada
generalmente equivale a "Debito").
Si el texto no menciona una fecha especifica, usa la fecha de hoy: {fecha_hoy} (formato YYYY-MM-DD).

Sobre el monto: VOS NO HACES NINGUNA CUENTA. El campo "monto" es exactamente el numero que dice el usuario,
copiado tal cual, sin multiplicarlo ni dividirlo por nada (ni por cantidad de productos, ni de medialunas, ni
de kilos, ni de cuotas, ni por nada). No importa si se compraron 1, 2 o 100 unidades: el numero que se
menciona ES el monto, punto.
Ejemplos (monto = exactamente el numero mencionado, nunca lo recalcules):
- "una docena de medialunas a 10000" -> monto: 10000
- "2 medialunas a 5000" -> monto: 5000
- "compre unas zapatillas en 3 cuotas, me salieron 50000" -> monto: 50000, monto_es_por_cuota: false
- "compre una notebook en 3 cuotas de 20000" -> monto: 20000, monto_es_por_cuota: true (aca 20000 es el
  valor de UNA cuota, no el total; lo marcas con el booleano, vos NO multiplicas)
El campo "monto_es_por_cuota" es true UNICAMENTE si el texto aclara el valor de cada cuota individual (con
la palabra "de" antes del numero, ej: "3 cuotas DE 20000"). En cualquier otro caso es false.
Si el texto menciona que el pago es en cuotas, devolve la cantidad de cuotas en "cuotas".
Si no se menciona que sea en cuotas, "cuotas" tiene que ser 1 y "monto_es_por_cuota" tiene que ser false.
Si el texto no parece describir ningun gasto real, respondé igual con tu mejor interpretacion (un array con
un solo elemento).

Respondé UNICAMENTE con un JSON valido, sin texto adicional, con esta forma exacta (SIEMPRE un array, incluso
si hay una sola compra):
[{{"monto": <numero>, "monto_es_por_cuota": <true o false>, "categoria": "<una de las categorias>", "medio_pago": "<uno de los medios de pago>", "descripcion": "<breve descripcion, unas pocas palabras>", "fecha": "<YYYY-MM-DD>", "cuotas": <numero entero>}}]
"""

    logger.info("Llamando a la IA (%s) para interpretar el mensaje", AI_PROVIDER)
    contenido = _llamar_ia(prompt_sistema, texto).strip()
    # Por si la IA envuelve la respuesta en ```json ... ```
    if contenido.startswith("```"):
        contenido = contenido.strip("`")
        contenido = contenido.split("\n", 1)[1] if "\n" in contenido else contenido
        contenido = contenido.rsplit("```", 1)[0]

    contenido_json = json.loads(contenido)
    lista_datos = contenido_json if isinstance(contenido_json, list) else [contenido_json]

    if len(lista_datos) > MAX_GASTOS_POR_MENSAJE:
        logger.warning(
            "Se detectaron %s gastos en un mensaje, se limita a %s", len(lista_datos), MAX_GASTOS_POR_MENSAJE
        )
        lista_datos = lista_datos[:MAX_GASTOS_POR_MENSAJE]

    gastos = [_validar_gasto(datos, categorias, medios_pago) for datos in lista_datos]
    for gasto in gastos:
        if not gasto["fecha"]:
            gasto["fecha"] = fecha_hoy

    return gastos


# --------------------------------------------------------------------------
# Handlers de Telegram
# --------------------------------------------------------------------------

# Guarda, por chat, el rango de filas del ultimo gasto registrado (para poder
# deshacerlo con /deshacer). Se pierde si se reinicia el bot; es una limitacion
# aceptada para no complicar esto con persistencia en disco.
ultima_operacion = {}


def sumar_meses(fecha, meses: int):
    mes_index = fecha.month - 1 + meses
    anio = fecha.year + mes_index // 12
    mes = mes_index % 12 + 1
    dia = min(fecha.day, calendar.monthrange(anio, mes)[1])
    return fecha.replace(year=anio, month=mes, day=dia)


def _mes_pago_ambiguo(fecha_compra):
    """Si la fecha de compra cae cerca del limite de un mes (donde no se puede
    saber sin preguntar si la tarjeta ya cerro o no), devuelve una tupla
    (mes_si_ya_cerro, mes_si_no_cerro) - cada uno como date del dia 1 de ese
    mes. Si no es ambiguo, devuelve None (se usa el default: mes de compra + 1)."""
    ultimo_dia_del_mes = calendar.monthrange(fecha_compra.year, fecha_compra.month)[1]
    dias_para_fin_de_mes = ultimo_dia_del_mes - fecha_compra.day

    if dias_para_fin_de_mes < DIAS_ANTES_FIN_DE_MES_PARA_PREGUNTAR:
        pivot = fecha_compra.replace(day=1)
    elif fecha_compra.day <= DIAS_DESPUES_INICIO_DE_MES_PARA_PREGUNTAR:
        pivot = sumar_meses(fecha_compra.replace(day=1), -1)
    else:
        return None

    return (sumar_meses(pivot, 1), sumar_meses(pivot, 2))


def _nombre_mes(fecha) -> str:
    return "{} {}".format(MESES_ES[fecha.month - 1], fecha.year)


# Cachea, para el dia de hoy (hora local), si el usuario ya dijo que la
# tarjeta "cerro" o "no cerro" - asi no se le vuelve a preguntar por cada
# gasto en credito que cargue el resto del dia. Se resetea solo (se compara
# la fecha guardada contra la de hoy en cada uso).
_cache_cierre = {"fecha": None, "valor": None}

# chat_id -> asyncio.Future, para poder "esperar" la respuesta de los botones
# de la pregunta de cierre de tarjeta desde dentro de procesar_texto_de_gasto.
_futuros_confirmacion_cierre = {}


async def _preguntar_cierre_tarjeta(mensaje_status, candidatos) -> str:
    mes_si_cerro, mes_si_no_cerro = candidatos
    teclado = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Ya cerró (se paga en {})".format(_nombre_mes(mes_si_cerro)), callback_data="cierre:cerrado"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Todavía no cerró (se paga en {})".format(_nombre_mes(mes_si_no_cerro)),
                    callback_data="cierre:no_cerrado",
                ),
            ],
            [
                InlineKeyboardButton(
                    "No sé (se paga en {})".format(_nombre_mes(mes_si_cerro)), callback_data="cierre:no_se"
                ),
            ],
        ]
    )
    await mensaje_status.edit_text(
        "Este gasto en crédito está cerca del cierre de tu tarjeta y no tengo forma de saber si ya cerró.\n"
        "¿Ya cerró el resumen, o todavía no?",
        reply_markup=teclado,
    )
    futuro = asyncio.get_running_loop().create_future()
    _futuros_confirmacion_cierre[mensaje_status.chat_id] = futuro
    try:
        return await futuro
    finally:
        _futuros_confirmacion_cierre.pop(mensaje_status.chat_id, None)


async def manejar_callback_cierre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    valor_boton = query.data.split(":", 1)[1]  # "cerrado", "no_cerrado" o "no_se"

    if valor_boton == "no_se":
        # "No se" se trata igual que "cerrado" (se asume el mes mas cercano),
        # pero se le avisa al usuario que es una suposicion, no un dato posta.
        await query.answer(
            "Como no sabés, asumo que se paga en el resumen que viene (como si ya hubiera cerrado).",
            show_alert=True,
        )
        valor = "cerrado"
    else:
        await query.answer()
        valor = valor_boton

    futuro = _futuros_confirmacion_cierre.get(update.effective_chat.id)
    if not futuro or futuro.done():
        return  # boton viejo (ya se resolvio esta pregunta o el bot se reinicio)

    _cache_cierre["fecha"] = datetime.now(ZoneInfo(TIMEZONE)).date()
    _cache_cierre["valor"] = valor
    futuro.set_result(valor)


def usuario_autorizado(update: Update) -> bool:
    if not ALLOWED_TELEGRAM_USER_ID:
        return True  # todavia no configurado: se permite a todos (solo para el setup inicial)
    return str(update.effective_user.id) == ALLOWED_TELEGRAM_USER_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        "Hola! Tu Telegram user_id es: {}\n\n"
        "Copialo en la variable ALLOWED_TELEGRAM_USER_ID del archivo .env "
        "para que el bot solo te responda a vos, y reiniciá el servicio.\n\n"
        "Una vez configurado, mandame un audio contando un gasto y lo registro solo.".format(user.id)
    )


def _escribir_gasto(worksheet, datos: dict, texto: str, hora_actual: str) -> dict:
    """Escribe un gasto (y sus cuotas si corresponde) en la planilla.
    Devuelve datos actualizado con fila_inicio/fila_fin/monto_cuota."""
    cuotas = datos["cuotas"]
    monto_total = datos["monto"]
    monto_cuota = round(monto_total / cuotas, 2) if cuotas > 1 else monto_total
    fecha_compra = datetime.strptime(datos["fecha"], "%Y-%m-%d").date()
    # La cuota 1 es del mes de la compra (sin corrimiento); las cuotas siguientes
    # le suman 1 mes cada una a partir de ahi.
    fecha_hora_cuota_1 = fecha_compra.strftime("%Y-%m-%d") + " " + hora_actual
    datos["fecha_hora"] = fecha_hora_cuota_1
    datos["monto_cuota"] = monto_cuota
    # "Mes de pago": en que mes se factura/paga esta cuota - separado de la
    # fecha de compra para no afectar "Actividad mensual" (que sigue siendo
    # por fecha de compra). Solo aplica a Credito; en Efectivo/Debito va vacio.
    mes_pago_cuota_1 = datos.get("mes_pago")
    mes_pago_cuota_1_str = mes_pago_cuota_1.strftime("%Y-%m-%d") if mes_pago_cuota_1 else ""
    datos["mes_pago_legible"] = _nombre_mes(mes_pago_cuota_1) if mes_pago_cuota_1 else ""

    fila_1 = [
        fecha_hora_cuota_1,
        monto_cuota,
        datos["categoria"],
        datos["medio_pago"],
        datos["descripcion"],
        "1/{}".format(cuotas),
        mes_pago_cuota_1_str,
        monto_total,
        texto,
    ]

    respuesta = worksheet.append_rows([fila_1], value_input_option="USER_ENTERED")
    fila_cuota_1 = int(re.search(r"![A-Z]+(\d+)", respuesta["updates"]["updatedRange"]).group(1))

    # El Monto de la cuota 1 se recalcula solo a partir de "Monto total" / cantidad de
    # cuotas: si despues corregis el total (te cobraron distinto a lo que dijiste),
    # alcanza con cambiar esa unica celda.
    formula_monto_cuota_1 = "={col}{fila}/{cuotas}".format(col=MONTO_TOTAL_COL, fila=fila_cuota_1, cuotas=cuotas)
    worksheet.update(
        range_name="{col}{fila}".format(col=MONTO_COL, fila=fila_cuota_1),
        values=[[formula_monto_cuota_1]],
        value_input_option="USER_ENTERED",
    )

    if cuotas > 1:

        def referencia(col: str) -> str:
            return "={}{}".format(col, fila_cuota_1)

        filas_restantes = []
        for i in range(1, cuotas):
            # La fecha depende de la fecha de la cuota 1 (le suma "i" meses, conservando
            # la hora). El resto de las columnas apuntan directo a la celda de la cuota 1,
            # asi que si corregis cualquier dato ahi, se propaga solo a las demas cuotas.
            formula_fecha = "=EDATE({col}{fila},{meses})+MOD({col}{fila},1)".format(
                col=FECHA_COL, fila=fila_cuota_1, meses=i
            )
            # "Mes de pago" de esta cuota: a diferencia del resto de las columnas,
            # se escribe como valor literal (no formula referenciando la cuota 1),
            # para que se pueda corregir una cuota puntual desde la webapp sin
            # que afecte a las demas ni dependa de ellas.
            mes_pago_cuota_i_str = ""
            if mes_pago_cuota_1:
                mes_pago_cuota_i_str = sumar_meses(mes_pago_cuota_1, i).strftime("%Y-%m-%d")
            filas_restantes.append(
                [
                    formula_fecha,
                    referencia(MONTO_COL),
                    referencia(CATEGORIA_COL),
                    referencia(MEDIO_PAGO_COL),
                    referencia(DESCRIPCION_COL),
                    "{}/{}".format(i + 1, cuotas),
                    mes_pago_cuota_i_str,
                    referencia(MONTO_TOTAL_COL),
                    referencia(TEXTO_ORIGINAL_COL),
                ]
            )
        worksheet.append_rows(filas_restantes, value_input_option="USER_ENTERED")

    datos["fila_inicio"] = fila_cuota_1
    datos["fila_fin"] = fila_cuota_1 + cuotas - 1

    logger.info("Gasto registrado en la planilla")
    return datos


def _texto_confirmacion(datos: dict) -> str:
    linea_mes_pago = "\nSe paga en: {}".format(datos["mes_pago_legible"]) if datos.get("mes_pago_legible") else ""
    if datos["cuotas"] > 1:
        return (
            "en {cuotas} cuotas ✅\n"
            "Primera cuota: {fecha_hora}\n"
            "Monto total: {monto}\n"
            "Monto por cuota: {monto_cuota} x {cuotas} (una por mes)\n"
            "Categoria: {categoria}\n"
            "Medio de pago: {medio_pago}\n"
            "Descripcion: {descripcion}"
            + linea_mes_pago
            + "\n"
            "Ya cargué las {cuotas} cuotas futuras en la planilla, una por mes. "
            "Si corregís el Monto de la primera cuota en la planilla, las demás se actualizan solas."
        ).format(**datos)
    return (
        "✅\n"
        "Fecha y hora: {fecha_hora}\n"
        "Monto: {monto}\n"
        "Categoria: {categoria}\n"
        "Medio de pago: {medio_pago}\n"
        "Descripcion: {descripcion}"
        + linea_mes_pago
    ).format(**datos)


async def procesar_texto_de_gasto(update: Update, mensaje_status, texto: str) -> None:
    fecha_hoy = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
    categorias_actuales, medios_pago_actuales = _leer_categorias_y_medios_pago()

    try:
        lista_gastos = interpretar_gastos(texto, fecha_hoy, categorias_actuales, medios_pago_actuales)
    except Exception:
        logger.exception("Error interpretando el gasto con Claude")
        await mensaje_status.edit_text(
            "Transcribí: \"{}\"\npero no pude interpretar el gasto. Probá de nuevo con otras palabras.".format(texto)
        )
        return

    hora_actual = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M")
    hoy = datetime.now(ZoneInfo(TIMEZONE)).date()
    resolucion_hoy = _cache_cierre["valor"] if _cache_cierre["fecha"] == hoy else None

    for datos in lista_gastos:
        if datos["medio_pago"] != "Credito":
            datos["mes_pago"] = None
            continue

        fecha_compra = datetime.strptime(datos["fecha"], "%Y-%m-%d").date()
        candidatos = _mes_pago_ambiguo(fecha_compra)
        if candidatos is None:
            datos["mes_pago"] = sumar_meses(fecha_compra.replace(day=1), 1)
            continue

        if resolucion_hoy is None:
            resolucion_hoy = await _preguntar_cierre_tarjeta(mensaje_status, candidatos)
            _cache_cierre["fecha"] = hoy
            _cache_cierre["valor"] = resolucion_hoy
        datos["mes_pago"] = candidatos[0] if resolucion_hoy == "cerrado" else candidatos[1]

    try:
        worksheet = get_worksheet()
        for datos in lista_gastos:
            _escribir_gasto(worksheet, datos, texto, hora_actual)
    except Exception:
        logger.exception("Error escribiendo en Google Sheets")
        await mensaje_status.edit_text(
            "Interpreté el/los gasto(s) pero no pude guardarlo en la planilla. Revisá los logs del servicio."
        )
        return

    ultima_operacion[update.effective_chat.id] = {
        "fila_inicio": min(datos["fila_inicio"] for datos in lista_gastos),
        "fila_fin": max(datos["fila_fin"] for datos in lista_gastos),
        "resumen": "; ".join(
            "{} (${:.2f})".format(datos["descripcion"], datos["monto"]) for datos in lista_gastos
        ),
    }

    if len(lista_gastos) == 1:
        mensaje_final = "Registrado " + _texto_confirmacion(lista_gastos[0])
        mensaje_final += "\n\nSi algo esta mal, lo podes corregir directamente en la planilla, o mandar /deshacer."
    else:
        partes = [
            "Gasto {} de {}: ".format(i + 1, len(lista_gastos)) + _texto_confirmacion(datos)
            for i, datos in enumerate(lista_gastos)
        ]
        mensaje_final = "Registré {} gastos ✅\n\n".format(len(lista_gastos)) + "\n\n".join(partes)
        mensaje_final += "\n\nSi algo esta mal, lo podes corregir directamente en la planilla, o mandar /deshacer."

    await mensaje_status.edit_text(mensaje_final)


async def manejar_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not usuario_autorizado(update):
        logger.warning("Mensaje de audio de usuario no autorizado: %s", update.effective_user.id)
        return

    mensaje_status = await update.message.reply_text("Escuchando el audio...")

    voice_or_audio = update.message.voice or update.message.audio
    telegram_file = await context.bot.get_file(voice_or_audio.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=True) as tmp_file:
        await telegram_file.download_to_drive(tmp_file.name)

        try:
            loop = asyncio.get_running_loop()
            texto = await loop.run_in_executor(None, transcribir_audio, tmp_file.name)
        except Exception:
            logger.exception("Error transcribiendo audio")
            await mensaje_status.edit_text("No pude transcribir el audio. Probá de nuevo.")
            return

    if not texto:
        await mensaje_status.edit_text("No entendí nada en el audio, probá de nuevo mas cerca del micrófono.")
        return

    await procesar_texto_de_gasto(update, mensaje_status, texto)


async def manejar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not usuario_autorizado(update):
        return

    mensaje_status = await update.message.reply_text("Procesando...")
    texto = update.message.text

    await procesar_texto_de_gasto(update, mensaje_status, texto)


async def cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not usuario_autorizado(update):
        return

    operacion = ultima_operacion.get(update.effective_chat.id)
    if not operacion:
        await update.message.reply_text("No tengo nada reciente para deshacer.")
        return

    try:
        worksheet = get_worksheet()
        worksheet.delete_rows(operacion["fila_inicio"], operacion["fila_fin"])
    except Exception:
        logger.exception("Error borrando filas al deshacer")
        await update.message.reply_text("No pude borrar de la planilla. Borralo a mano si hace falta.")
        return

    logger.info("Gasto deshecho (ultima operacion revertida)")
    del ultima_operacion[update.effective_chat.id]
    await update.message.reply_text("Borrado ✅: {}".format(operacion["resumen"]))


def main() -> None:
    try:
        asegurar_hoja_config()
    except Exception:
        logger.exception("No se pudo sincronizar la pestaña 'Config' (no es fatal, se reintenta al reiniciar)")

    # concurrent_updates=True: sin esto, mientras se espera la respuesta de los
    # botones de "cierre de tarjeta" (un await bloqueado en medio de un
    # handler), el bot no puede procesar el update del click del boton -
    # queda en cola detras del propio handler que lo espera (deadlock).
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("deshacer", cmd_deshacer))
    app.add_handler(CallbackQueryHandler(manejar_callback_cierre, pattern="^cierre:"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, manejar_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto))

    logger.info("Bot arrancado (%s), esperando audios...", NOMBRE_PERSONA)
    app.run_polling()


if __name__ == "__main__":
    main()
