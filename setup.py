#!/usr/bin/env python3
"""
Wizard interactivo para crear people/<nombre>/ - te pregunta lo que hace
falta y arma el .env y el config.json solo, en vez de editarlos a mano.

Sirve tanto para la primera instalacion como para agregar una persona
nueva despues: si ya hay gente cargada, te ofrece reusar los datos que
tiene sentido compartir (proveedor de IA y su API key, el service account
de Google, la config del servidor) en vez de volver a pedirlos.

Uso: python3 setup.py
"""

import getpass
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PEOPLE_DIR = ROOT / "people"

PROVEEDORES = {
    "anthropic": {
        "donde": "console.anthropic.com -> API Keys",
        "key_var": "ANTHROPIC_API_KEY",
        "key_ejemplo": "sk-ant-api03-...",
        "model_var": "ANTHROPIC_MODEL",
        "model_default": "claude-haiku-4-5",
    },
    "openai": {
        "donde": "platform.openai.com -> API Keys",
        "key_var": "OPENAI_API_KEY",
        "key_ejemplo": "sk-...",
        "model_var": "OPENAI_MODEL",
        "model_default": "gpt-4o-mini",
    },
    "gemini": {
        "donde": "aistudio.google.com -> Get API Key",
        "key_var": "GEMINI_API_KEY",
        "key_ejemplo": "...",
        "model_var": "GEMINI_MODEL",
        "model_default": "gemini-2.0-flash",
    },
    "ollama": {
        "donde": "local, sin costo - instalar desde https://ollama.com",
        "key_var": None,
        "model_var": "OLLAMA_MODEL",
        "model_default": "llama3.1",
    },
}


def parse_env(path: Path) -> dict:
    valores = {}
    if not path.exists():
        return valores
    for linea in path.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        valores[clave.strip()] = valor.strip()
    return valores


def personas_existentes():
    if not PEOPLE_DIR.exists():
        return []
    return [
        p
        for p in sorted(PEOPLE_DIR.iterdir())
        if p.is_dir() and p.name != "_example" and (p / ".env").exists()
    ]


def pedir_nombre():
    print("\nPersonas ya configuradas:", ", ".join(p.name for p in personas_existentes()) or "(ninguna todavia)")
    while True:
        nombre = input("\nNombre de esta persona (va a ser el nombre de su carpeta - minusculas, sin espacios ni acentos, ej: 'juan'): ").strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]+", nombre or ""):
            print("  Invalido: solo minusculas, numeros, '-' o '_', sin espacios ni acentos.")
            continue
        if nombre == "_example":
            print("  Ese nombre esta reservado, elegi otro.")
            continue
        if (PEOPLE_DIR / nombre).exists():
            print(f"  Ya existe people/{nombre}/ - elegi otro nombre (o borra esa carpeta primero si fue un error).")
            continue
        return nombre


def elegir_o_pedir(etiqueta, opciones, pedir_nuevo):
    """opciones: lista de (nombre_persona, valor). Si hay alguna, deja elegir reusarla."""
    if opciones:
        print(f"\n{etiqueta}")
        for i, (persona, valor) in enumerate(opciones, 1):
            muestra = valor if len(valor) <= 50 else valor[:47] + "..."
            print(f"  {i}) reusar el de '{persona}' ({muestra})")
        print("  0) ingresar uno nuevo")
        while True:
            eleccion = input("Elegi una opcion [0]: ").strip() or "0"
            if eleccion == "0":
                break
            if eleccion.isdigit() and 1 <= int(eleccion) <= len(opciones):
                return opciones[int(eleccion) - 1][1]
            print("  Opcion invalida.")
    return pedir_nuevo()


def pedir_proveedor_ia(existentes):
    opciones = [(p.name, parse_env(p / ".env").get("AI_PROVIDER", "")) for p in existentes]
    opciones = [(n, v) for n, v in opciones if v]

    def nuevo():
        while True:
            val = input(f"Que IA vas a usar ({'/'.join(PROVEEDORES)}) [anthropic]: ").strip().lower() or "anthropic"
            if val in PROVEEDORES:
                return val
            print(f"  Opcion invalida, elegi una de: {', '.join(PROVEEDORES)}")

    return elegir_o_pedir("Proveedor de IA para interpretar los gastos:", opciones, nuevo)


def pedir_clave_ia(proveedor, existentes):
    info = PROVEEDORES[proveedor]
    # Solo tiene sentido reusar la key de personas que usan el MISMO proveedor.
    mismas = [p for p in existentes if parse_env(p / ".env").get("AI_PROVIDER") == proveedor]

    if info["key_var"] is None:
        # ollama: no hay API key, hay un host.
        opciones = [(p.name, parse_env(p / ".env").get("OLLAMA_HOST", "")) for p in mismas]
        opciones = [(n, v) for n, v in opciones if v]
        host = elegir_o_pedir(
            "Host de Ollama:",
            opciones,
            lambda: input("Host de Ollama [http://localhost:11434]: ").strip() or "http://localhost:11434",
        )
        modelo = input(f"Modelo de Ollama [{info['model_default']}]: ").strip() or info["model_default"]
        return {"OLLAMA_HOST": host, "OLLAMA_MODEL": modelo}

    opciones = [(p.name, parse_env(p / ".env").get(info["key_var"], "")) for p in mismas]
    opciones = [(n, v) for n, v in opciones if v]
    print(f"\n(La consegis en: {info['donde']})")
    key = elegir_o_pedir(
        f"API key de {proveedor}:",
        opciones,
        lambda: input(f"API key de {proveedor} (ej: {info['key_ejemplo']}): ").strip(),
    )
    modelo_opciones = [(p.name, parse_env(p / ".env").get(info["model_var"], "")) for p in mismas]
    modelo_opciones = [(n, v) for n, v in modelo_opciones if v]
    modelo = elegir_o_pedir(
        f"Modelo de {proveedor}:",
        modelo_opciones,
        lambda: input(f"Modelo de {proveedor} [{info['model_default']}]: ").strip() or info["model_default"],
    )
    return {info["key_var"]: key, info["model_var"]: modelo}


def pedir_service_account(existentes):
    """Devuelve la ruta de origen a copiar despues (o None si se saltea) - no toca disco todavia."""
    con_archivo = [p for p in existentes if (p / "service_account.json").exists()]
    if con_archivo:
        print("\nArchivo service_account.json de Google (se puede reusar el mismo para varias personas):")
        for i, p in enumerate(con_archivo, 1):
            print(f"  {i}) reusar el de '{p.name}'")
        print("  0) ingresar la ruta a un archivo nuevo")
        print("  s) saltear (lo copio a mano despues)")
        while True:
            eleccion = input("Elegi una opcion [0]: ").strip().lower() or "0"
            if eleccion == "s":
                return None
            if eleccion == "0":
                break
            if eleccion.isdigit() and 1 <= int(eleccion) <= len(con_archivo):
                return con_archivo[int(eleccion) - 1] / "service_account.json"
            print("  Opcion invalida.")

    ruta = input("Ruta al archivo service_account.json que descargaste de Google Cloud (Enter para saltear y copiarlo a mano despues): ").strip()
    if not ruta:
        return None
    origen = Path(ruta).expanduser()
    if not origen.exists():
        print(f"  No encontre {origen} - vas a tener que copiarlo a mano despues.")
        return None
    return origen


def pedir_config_json(existentes):
    con_config = [p for p in existentes if (p / "config.json").exists()]
    if con_config:
        print("\nCategorias y medios de pago:")
        for i, p in enumerate(con_config, 1):
            print(f"  {i}) copiar los de '{p.name}' (los podes editar despues a mano, o desde la webapp si tenes)")
        print("  0) ingresar los propios")
        eleccion = input("Elegi una opcion [0]: ").strip() or "0"
        if eleccion.isdigit() and 1 <= int(eleccion) <= len(con_config):
            return json.loads((con_config[int(eleccion) - 1] / "config.json").read_text(encoding="utf-8"))

    default_cat = ["Supermercado", "Transporte", "Entretenimiento", "Servicios", "Otros"]
    default_medios = ["Efectivo", "Debito", "Credito"]
    cat_in = input(f"Categorias, separadas por coma [{', '.join(default_cat)}]: ").strip()
    medios_in = input(f"Medios de pago, separados por coma [{', '.join(default_medios)}]: ").strip()
    aclaraciones = input("Aclaraciones para la IA sobre categorias raras/apodos (opcional, Enter para saltear): ").strip()
    categorias = [c.strip() for c in cat_in.split(",") if c.strip()] or default_cat
    medios_pago = [m.strip() for m in medios_in.split(",") if m.strip()] or default_medios
    return {"categorias": categorias, "medios_pago": medios_pago, "aclaraciones_categorias": aclaraciones}


def main():
    print("=== Setup de una persona nueva para el bot de gastos ===")

    existentes = personas_existentes()
    nombre = pedir_nombre()
    destino = PEOPLE_DIR / nombre

    print(f"\n--- Datos PROPIOS de '{nombre}' (no se comparten con nadie mas) ---")
    telegram_token = input("Token del bot de Telegram (@BotFather -> /newbot -> te lo da al crearlo): ").strip()
    sheets_id = input("ID de la Google Sheet (la parte de la URL entre /d/ y /edit): ").strip()

    print(f"\n--- Datos que se pueden REUSAR entre personas ---")
    proveedor = pedir_proveedor_ia(existentes)
    claves_ia = pedir_clave_ia(proveedor, existentes)
    tab_opciones = [(p.name, parse_env(p / ".env").get("GOOGLE_SHEET_TAB_NAME", "")) for p in existentes]
    tab_opciones = [(n, v) for n, v in tab_opciones if v]
    tab_name = elegir_o_pedir(
        "Nombre de la pestana de la Sheet donde van los gastos:",
        tab_opciones,
        lambda: input("Nombre de la pestana [Gastos]: ").strip() or "Gastos",
    )
    tz_opciones = [(p.name, parse_env(p / ".env").get("TIMEZONE", "")) for p in existentes]
    tz_opciones = [(n, v) for n, v in tz_opciones if v]
    timezone = elegir_o_pedir(
        "Zona horaria:",
        tz_opciones,
        lambda: input("Zona horaria [America/Argentina/Buenos_Aires]: ").strip() or "America/Argentina/Buenos_Aires",
    )
    whisper_opciones = [
        (p.name, "/".join([
            parse_env(p / ".env").get("WHISPER_MODEL_SIZE", ""),
            parse_env(p / ".env").get("WHISPER_DEVICE", ""),
            parse_env(p / ".env").get("WHISPER_COMPUTE_TYPE", ""),
        ]))
        for p in existentes
    ]
    whisper_opciones = [(n, v) for n, v in whisper_opciones if v.strip("/")]
    whisper = elegir_o_pedir(
        "Configuracion de Whisper (tamano modelo / dispositivo / precision):",
        whisper_opciones,
        lambda: input("Configuracion de Whisper, formato tamano/dispositivo/precision [small/cpu/int8]: ").strip() or "small/cpu/int8",
    )
    whisper_size, whisper_device, whisper_compute = (whisper.split("/") + ["small", "cpu", "int8"])[:3]

    origen_service_account = pedir_service_account(existentes)
    config_json = pedir_config_json(existentes)

    # Recien aca se toca el disco - todo lo anterior fue solo preguntas, asi
    # que cancelar (Ctrl+C) antes de este punto no deja carpetas a medio crear.
    destino.mkdir(parents=True)
    if origen_service_account is not None:
        shutil.copy(origen_service_account, destino / "service_account.json")

    # --- armar .env ---
    lineas = [
        "# Generado por setup.py - se puede editar a mano despues si hace falta.",
        "",
        f"TELEGRAM_BOT_TOKEN={telegram_token}",
        "",
        f"AI_PROVIDER={proveedor}",
        "",
    ]
    for nombre_prov, info in PROVEEDORES.items():
        activo = nombre_prov == proveedor
        prefijo = "" if activo else "# "
        lineas.append(f"# --- {nombre_prov} ({info['donde']}) ---")
        if info["key_var"]:
            valor = claves_ia.get(info["key_var"], "") if activo else "TU_API_KEY_ACA"
            lineas.append(f"{prefijo}{info['key_var']}={valor}")
        if info["model_var"]:
            valor = claves_ia.get(info["model_var"], info["model_default"]) if activo else info["model_default"]
            lineas.append(f"{prefijo}{info['model_var']}={valor}")
        if nombre_prov == "ollama":
            valor = claves_ia.get("OLLAMA_HOST", "http://localhost:11434") if activo else "http://localhost:11434"
            lineas.append(f"{prefijo}OLLAMA_HOST={valor}")
        lineas.append("")
    lineas += [
        f"GOOGLE_SHEETS_ID={sheets_id}",
        "GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json",
        f"GOOGLE_SHEET_TAB_NAME={tab_name}",
        "",
        f"WHISPER_MODEL_SIZE={whisper_size}",
        f"WHISPER_DEVICE={whisper_device}",
        f"WHISPER_COMPUTE_TYPE={whisper_compute}",
        "",
        f"TIMEZONE={timezone}",
        "",
        "# Dejar vacio la primera vez. Despues de mandarle /start al bot, te va a devolver",
        "# tu user_id de Telegram: copialo aca para que el bot solo te responda a vos.",
        "ALLOWED_TELEGRAM_USER_ID=",
        "",
    ]
    (destino / ".env").write_text("\n".join(lineas), encoding="utf-8")
    (destino / "config.json").write_text(json.dumps(config_json, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\n✅ Listo: people/{nombre}/ creada con .env y config.json.")
    if origen_service_account is None:
        print(f"⚠️  Todavia falta copiar service_account.json a people/{nombre}/service_account.json (ver README, seccion 2.3).")

    usuario = getpass.getuser()
    print(f"""
Proximos pasos:

1) Si todavia no instalaste las dependencias:
   python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt

2) Primera prueba a mano:
   cd people/{nombre}
   ../../venv/bin/python ../../main.py
   (mandale /start al bot en Telegram, copia el user_id que te devuelve en
   ALLOWED_TELEGRAM_USER_ID dentro de people/{nombre}/.env, Ctrl+C y volve a correrlo)

3) Para dejarlo corriendo solo (systemd) - reemplaza {usuario} en el archivo la
   primera vez que lo hagas para cualquier persona (una sola vez, no por persona):
   sudo cp systemd/gastos-bot@.service /etc/systemd/system/gastos-bot@.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now gastos-bot@{nombre}.service
""")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelado.")
        sys.exit(1)
