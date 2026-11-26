from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from datetime import datetime, date, timedelta
import os
import sys
import json
import re
import time

# Forzar flush en todos los print() así los logs llegan a Railway de inmediato,
# en vez de quedarse en el buffer de stdout (que Railway muestra con mucho delay).
_PRINT_ORIGINAL = print
def print(*args, **kwargs):  # noqa: A001
    kwargs.setdefault("flush", True)
    _PRINT_ORIGINAL(*args, **kwargs)
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import kapso
import plantillas_wa

app = FastAPI(title="Rinde Agro Server", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

AR_TZ = pytz.timezone("America/Argentina/Buenos_Aires")
scheduler = AsyncIOScheduler(timezone=AR_TZ)

# ── Cache de precios en memoria ──
cache_precios = {
    "cereales": {"soja": 341, "maiz": 181, "trigo": 183, "girasol": 390, "sorgo": 189},
    "bna": 1387,
    "ultima_actualizacion": None,
    "fuente": "referencia",
    "ultimo_precio_alerta": {},   # precio al que se mandó la última alerta
}


# ══════════════════════════════════════════════
# HELPERS — Supabase REST
# ══════════════════════════════════════════════

def _sb_url() -> str:
    return os.environ.get("SUPABASE_URL", "")

def _sb_key() -> str:
    return os.environ.get("SUPABASE_SERVICE_KEY", "")

def _sb_headers(extra: dict = None) -> dict:
    k = _sb_key()
    h = {
        "apikey": k,
        "Authorization": f"Bearer {k}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    if extra:
        h.update(extra)
    return h

async def _sb_get(table: str, params: dict = None) -> list:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{_sb_url()}/rest/v1/{table}", headers=_sb_headers(), params=params)
            if r.status_code == 200:
                return r.json()
            print(f"[SB GET {table}] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[SB GET {table}] excepción: {e}")
    return []

async def _sb_get_todo(table: str, params: dict, pagina: int = 1000) -> list:
    """
    _sb_get paginado. PostgREST corta en 1000 filas por default, así que una
    query "traeme todo" que crezca con los usuarios se trunca sin avisar.
    Requiere que `params` traiga un `order` estable o las páginas se solapan.
    """
    filas, offset = [], 0
    while True:
        p = dict(params)
        p["limit"] = str(pagina)
        p["offset"] = str(offset)
        lote = await _sb_get(table, p) or []
        filas.extend(lote)
        if len(lote) < pagina:
            return filas
        offset += pagina
        if offset >= 100_000:
            print(f"[SB GET TODO {table}] corte de seguridad en {offset} filas")
            return filas


async def _sb_post(table: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{_sb_url()}/rest/v1/{table}", headers=_sb_headers(), json=payload)
            if r.status_code in (200, 201):
                data = r.json()
                return data[0] if isinstance(data, list) and data else (data or {})
            print(f"[SB POST {table}] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[SB POST {table}] excepción: {e}")
    return {}

async def _sb_patch(path: str, payload: dict):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.patch(f"{_sb_url()}/rest/v1/{path}", headers=_sb_headers(), json=payload)
            if r.status_code not in (200, 204):
                print(f"[SB PATCH {path}] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[SB PATCH {path}] excepción: {e}")

async def _sb_delete(path: str) -> bool:
    """DELETE /rest/v1/<path> (ej. "gastos?id=eq.abc"). Devuelve True si fue 2xx."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.delete(f"{_sb_url()}/rest/v1/{path}", headers=_sb_headers())
            if r.status_code in (200, 204):
                return True
            print(f"[SB DELETE {path}] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[SB DELETE {path}] excepción: {e}")
    return False


async def _sb_upsert(table: str, payload: dict, on_conflict: str = "") -> list:
    try:
        h = _sb_headers({"Prefer": "resolution=merge-duplicates,return=representation"})
        params = {"on_conflict": on_conflict} if on_conflict else {}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{_sb_url()}/rest/v1/{table}", headers=h, json=payload, params=params)
            if r.status_code in (200, 201):
                return r.json()
            print(f"[SB UPSERT {table}] HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        print(f"[SB UPSERT {table}] excepción: {e}")
    return []


# ══════════════════════════════════════════════
# WHATSAPP — ESTADO DE CONVERSACIÓN
# ══════════════════════════════════════════════

CONV_TIMEOUT_MIN = 30  # minutos sin actividad para resetear

async def _wa_get_conv(numero: str) -> dict | None:
    """Devuelve la conversación activa del número, o None si no hay o expiró."""
    rows = await _sb_get("wa_conversaciones", {"numero_whatsapp": f"eq.{numero}"})
    print(f"[DEBUG _wa_get_conv] numero={numero} rows={rows}")
    if not rows:
        print(f"[DEBUG _wa_get_conv] → sin registro en wa_conversaciones")
        return None
    conv = rows[0]
    estado = conv.get("estado", "completado")
    print(f"[DEBUG _wa_get_conv] → estado en BD={repr(estado)}")
    if estado == "completado":
        print(f"[DEBUG _wa_get_conv] → estado=completado, retorna None")
        return None
    # Verificar timeout de 30 minutos
    actualizado = conv.get("actualizado_en", "")
    if actualizado:
        try:
            dt = datetime.fromisoformat(actualizado.replace("Z", "+00:00"))
            diff_min = (datetime.now(pytz.utc) - dt).total_seconds() / 60
            print(f"[DEBUG _wa_get_conv] → antigüedad={diff_min:.1f} min")
            if diff_min > CONV_TIMEOUT_MIN:
                print(f"[DEBUG _wa_get_conv] → expiró (>{CONV_TIMEOUT_MIN} min), limpiando")
                await _wa_clear_conv(numero)
                return None
        except Exception as e:
            print(f"[DEBUG _wa_get_conv] → error parseando fecha: {e}")
    conv["numero_whatsapp"] = numero
    print(f"[DEBUG _wa_get_conv] → conversación activa: estado={estado}")
    return conv

async def _wa_set_conv(numero: str, estado: str, datos: dict = None):
    """Crea o actualiza el estado de conversación."""
    print(f"[DEBUG _wa_set_conv] numero={numero} estado={repr(estado)} datos={datos}")
    result = await _sb_upsert(
        "wa_conversaciones",
        {
            "numero_whatsapp": numero,
            "estado": estado,
            "datos_parciales": datos or {},
            "actualizado_en": datetime.utcnow().isoformat(),
        },
        on_conflict="numero_whatsapp",
    )
    print(f"[DEBUG _wa_set_conv] → resultado upsert: {result}")

async def _wa_clear_conv(numero: str):
    """Marca la conversación como completada (finalizada)."""
    await _sb_patch(
        f"wa_conversaciones?numero_whatsapp=eq.{numero}",
        {"estado": "completado", "datos_parciales": {}, "actualizado_en": datetime.utcnow().isoformat()},
    )


# ══════════════════════════════════════════════
# WHATSAPP — MENÚ Y FLUJO CONVERSACIONAL
# ══════════════════════════════════════════════

MENU_TEXT = (
    "🌾 *Rinde Agro* - ¿Qué querés hacer?\n\n"
    "1️⃣ Cargar gasto\n"
    "2️⃣ Registrar lluvia\n"
    "3️⃣ Ver mis tareas pendientes\n"
    "4️⃣ Marcar tarea como completada\n"
    "0️⃣ Cancelar"
)

RUBROS = ["Herbicidas", "Fertilizantes", "Semillas", "Laboreo", "Flete", "Arrendamiento", "Otros"]

STOP_WORDS  = {"stop", "basta", "detener", "para", "parar", "no recibir más", "no recibir mas"}
ACTIVAR_WORDS = {"activar", "reactivar", "activame", "volver"}
MENU_WORDS  = {"menú", "menu", "hola", "hi", "inicio", "start", "empezar", "ayuda", "help"}


def _norm(texto: str) -> str:
    return texto.strip().lower()


def _texto_es_nueva_intencion(texto: str) -> bool:
    """
    Heurística: el mensaje parece una carga completa (ingreso, gasto, lluvia,
    tarea…), no la respuesta corta al flujo guiado en curso. Si es largo y
    menciona verbos de acción o varias palabras clave de dominio, damos por
    hecho que el user está arrancando de cero y descartamos el estado anterior.
    Un "La Escondida" o "sí" cortito nunca entra acá porque son <60 chars.
    """
    n = _norm(texto)
    if len(n) < 60:
        return False
    verbos = ("cargar", "carrer",  # "carrer" es "cargar" mal transcripto por Kapso
              "aplicar", "aplicó", "aplicamos", "aplique",
              "fumigó", "fumigamos", "fumigué", "fumigar",
              "utilizó", "utilizamos", "utilicé", "utilizar", "usó", "usamos", "usé",
              "gasté", "gastamos", "compré", "compramos",
              "llovió", "llovieron", "sembramos", "sembré",
              "vendimos", "vendí", "pagamos", "pagué", "registrar")
    palabras = ("hectárea", "hectarea", "por litro", "dólares", "pesos",
                "por tonelada", "por bolsa", "mm de lluvia", "precio")
    return any(v in n for v in verbos) or sum(1 for p in palabras if p in n) >= 2


# ── Helpers de flujo ────────────────────────

def _buscar_campo(texto: str, campos: list) -> dict | None:
    n = texto.strip().lower()
    for c in campos:
        if n in c["nombre"].lower() or c["nombre"].lower() in n:
            return c
    return None

def _buscar_rubro(texto: str) -> str:
    n = texto.strip().lower()
    for r in RUBROS:
        if r.lower() in n or n in r.lower():
            return r
    return texto.strip().capitalize()

def _extraer_numero(texto: str) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", texto)
    if m:
        try:
            return float(m.group().replace(",", "."))
        except Exception:
            pass
    return None


# ══════════════════════════════════════════════
# WHATSAPP VÍA KAPSO — identidad, flujos y webhook
# ══════════════════════════════════════════════
#
# Único canal de WhatsApp (Twilio se dio de baja).
# Kapso es un proxy de la Cloud API oficial de Meta: soporta botones y listas
# interactivas, y transcribe los audios antes de mandarnos el webhook.
#
# El transporte (enviar, validar firma, parsear) vive en kapso.py.

# ── Decisiones de producto ────────────────────
# Las tres están pensadas para cambiarse sin tocar nada más.
# Los gastos que interpreta la IA siempre se confirman antes de guardar
# (_kapso_confirmar_gasto); los que el bot entiende solo se cargan directo.
PEDIR_EVIDENCIA_TAREA      = False  # False = foto/comentario opcional al completar
BOT_MANDA_INVITACION       = False  # False = el link se lo damos al dueño para que lo reenvíe

APP_URL = os.environ.get("APP_URL", "https://rindeagro.app")

# Anti-duplicados: Kapso reintenta si no contestamos 200 en 10 segundos, y
# puede repetir un evento. Guardamos los ids ya procesados en memoria.
_KAPSO_IDEM_VISTOS: list = []
_KAPSO_IDEM_MAX = 500

# Mismos presets que usa la app (index.html → PERMISOS_PRESETS)
PERMISOS_PRESETS = {
    "carga_basica": {"campos": "ver", "gastos": "editar", "ingresos": None, "lluvias": "editar",
                     "suelo": None, "rentabilidad_campo": None, "rentabilidad_total": None,
                     "mapa": "ver", "tareas": "ver"},
    "acceso_completo": {"campos": "editar", "gastos": "editar", "ingresos": "editar", "lluvias": "editar",
                        "suelo": "editar", "rentabilidad_campo": "ver", "rentabilidad_total": "ver",
                        "mapa": "editar", "tareas": "ver"},
}

# rol de la UI → (rol que acepta el CHECK de la tabla, preset de permisos)
ROLES_INVITACION = {
    "operario":      ("colaborador",   "carga_basica"),
    "administrador": ("administrador", "acceso_completo"),
}

ESTADOS_PENDIENTES = "in.(pendiente,en_progreso,programada)"

# MENU_WORDS se quedaba corto: acá nadie arranca con "hola", arranca con "buenas".
SALUDOS_WA = MENU_WORDS | {
    "buenas", "buen dia", "buen día", "buenos dias", "buenos días",
    "buenas tardes", "buenas noches", "que tal", "qué tal", "como va",
    "cómo va", "che", "holis", "buen finde", "opciones", "volver",
}


# ── Storage ───────────────────────────────────

async def _sb_storage_subir(bucket: str, path: str, contenido: bytes, content_type: str) -> str:
    """Sube un archivo al Storage de Supabase. Devuelve la URL pública o ''."""
    if not contenido:
        return ""
    url = f"{_sb_url()}/storage/v1/object/{bucket}/{path}"
    headers = {
        "apikey": _sb_key(),
        "Authorization": f"Bearer {_sb_key()}",
        "Content-Type": content_type or "application/octet-stream",
        "x-upsert": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, headers=headers, content=contenido)
            if r.status_code in (200, 201):
                return f"{_sb_url()}/storage/v1/object/public/{bucket}/{path}"
            print(f"[STORAGE {bucket}] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[STORAGE {bucket}] excepción: {e}")
    return ""


# ── Identidad ─────────────────────────────────

async def _wa_contexto(user_id, owner_id, nombre, rol, permisos) -> dict:
    campos = await _sb_get("campos", {
        "usuario_id": f"eq.{owner_id}",
        "select": "id,nombre,hectareas,cultivo",
        "order": "nombre.asc",
    })
    return {
        "user_id": user_id,
        "owner_id": owner_id,
        "nombre": nombre or "",
        "rol": rol,
        "es_dueño": rol == "dueño",
        "permisos": permisos or {},
        "campos": campos or [],
    }


# `perfiles.telefono` y `equipo.whatsapp` los tipea el usuario a mano, así que
# están guardados en cualquier formato ('+54 9 2944 56-5308', '02944565308').
# No se puede filtrar por número en la query: hay que normalizar y comparar.
#
# Antes eso se resolvía en cada mensaje entrante trayendo hasta 2000 perfiles y
# 2000 filas de equipo. Con el número de sandbox y cinco usuarios daba igual;
# con un número de producción son dos consultas enormes por mensaje y, peor, el
# que se registra pasada la fila 2000 nunca es reconocido. Ahora se arma un
# índice normalizado en memoria y se refresca cada pocos minutos.

_WA_DIR: dict = {}            # número normalizado → {"tipo", "fila"}
_WA_DIR_TS: float = 0.0       # time.monotonic() del último armado
_WA_DIR_TTL = 300             # segundos de vida del índice
_WA_DIR_PISO_REFRESH = 20     # piso entre refrescos forzados por un miss
_WA_DIR_LOCK = asyncio.Lock()


async def _wa_construir_directorio() -> dict:
    """Índice {número normalizado → dueño o miembro de equipo}."""
    indice = {}

    # El equipo primero y los perfiles después, a propósito: si alguien está en
    # las dos tablas gana el perfil, que es de donde sale el contexto de dueño.
    miembros = await _sb_get_todo("equipo", {
        "select": "id,owner_id,miembro_id,rol,nombre_display,whatsapp,permisos,activo",
        "whatsapp": "not.is.null", "order": "id.asc",
    })
    for e in miembros:
        if e.get("activo") is False:
            continue
        n = kapso.normalizar_numero(e.get("whatsapp") or "")
        if n:
            indice[n] = {"tipo": "equipo", "fila": e}

    perfiles = await _sb_get_todo("perfiles", {
        "select": "id,nombre,telefono", "telefono": "not.is.null", "order": "id.asc",
    })
    for p in perfiles:
        n = kapso.normalizar_numero(p.get("telefono") or "")
        if n:
            indice[n] = {"tipo": "perfil", "fila": p}

    return indice


async def _wa_buscar_en_directorio(n: str) -> dict | None:
    global _WA_DIR, _WA_DIR_TS

    async with _WA_DIR_LOCK:
        if not _WA_DIR or time.monotonic() - _WA_DIR_TS > _WA_DIR_TTL:
            _WA_DIR = await _wa_construir_directorio()
            _WA_DIR_TS = time.monotonic()
            print(f"[WA DIR] índice armado: {len(_WA_DIR)} números")

    hit = _WA_DIR.get(n)
    if hit:
        return hit

    # Puede ser alguien que se registró hace un minuto y todavía no entró al
    # índice. Lo reconstruimos una vez, con un piso de tiempo para que un número
    # desconocido que insiste no nos haga escanear la base a cada mensaje.
    async with _WA_DIR_LOCK:
        if time.monotonic() - _WA_DIR_TS > _WA_DIR_PISO_REFRESH:
            _WA_DIR = await _wa_construir_directorio()
            _WA_DIR_TS = time.monotonic()
            print(f"[WA DIR] índice rearmado por miss: {len(_WA_DIR)} números")
        return _WA_DIR.get(n)


async def _wa_identificar(numero: str) -> dict | None:
    """
    Resuelve quién es el que escribe: dueño de una cuenta o miembro de un equipo.

    Compara con los teléfonos normalizados, no con el texto guardado: los
    perfiles tienen '+5492944565308' y WhatsApp manda '542944565308'.
    """
    n = kapso.normalizar_numero(numero)
    if not n:
        return None

    hit = await _wa_buscar_en_directorio(n)
    if not hit:
        return None

    if hit["tipo"] == "perfil":
        p = hit["fila"]
        # Tener perfil propio no significa ser dueño: un empleado invitado también
        # se crea una cuenta al aceptar la invitación. Si es miembro del equipo de
        # alguien, ese es su contexto — igual que hace la app al armar CONTEXTO.
        membresias = await _sb_get("equipo", {
            "miembro_id": f"eq.{p['id']}", "activo": "eq.true",
            "select": "owner_id,rol,nombre_display,permisos", "limit": "1",
        })
        if membresias:
            e = membresias[0]
            return await _wa_contexto(
                p["id"], e["owner_id"],
                e.get("nombre_display") or p.get("nombre") or "",
                e.get("rol") or "colaborador", e.get("permisos") or {},
            )
        return await _wa_contexto(p["id"], p["id"], p.get("nombre") or "", "dueño", None)

    e = hit["fila"]
    return await _wa_contexto(
        e.get("miembro_id"), e["owner_id"],
        e.get("nombre_display") or "", e.get("rol") or "colaborador",
        e.get("permisos") or {},
    )


def _wa_puede(ident: dict, modulo: str, nivel: str = "ver") -> bool:
    if ident.get("es_dueño") or ident.get("rol") == "administrador":
        return True
    p = (ident.get("permisos") or {}).get(modulo)
    return p in ("ver", "editar") if nivel == "ver" else p == "editar"


def _wa_primer_nombre(ident: dict) -> str:
    return (ident.get("nombre") or "").strip().split(" ")[0]


# ── Menú ──────────────────────────────────────

def _wa_saludo_hora() -> str:
    h = datetime.now(AR_TZ).hour
    if 5 <= h < 13:
        return "Buen día"
    if 13 <= h < 20:
        return "Buenas tardes"
    return "Buenas noches"


async def _kapso_menu(ident: dict, numero: str, encabezado: str = "") -> None:
    nombre = _wa_primer_nombre(ident)
    saludo = encabezado or (f"{_wa_saludo_hora()}, {nombre} 🌾" if nombre
                            else f"{_wa_saludo_hora()} 🌾")

    campo_demo = ((ident.get("campos") or [{}])[0].get("nombre") or "La Loma")

    # 6 opciones numeradas, visibles directo en el texto. Sin botones — el user
    # pidió texto puro para evitar la mezcla "botones + número" que confundía.
    # Orden definido por el user (nov/2026): ingreso, gasto, insumo, pago,
    # tarea, lluvia. Si algún permiso falta, esa opción se saltea y la
    # numeración se corre.
    todas = [
        ("ingreso", "💰 Ingreso (venta)", "ingresos", "editar"),
        ("gasto", "💵 Gasto", "gastos", "editar"),
        ("insumo", "📦 Insumo (stock)", "insumos", "editar"),
        ("pago", "🧾 Pago (cheque/cuota)", "compromisos_pago", "editar"),
        ("tarea_nueva", "📝 Tarea", "tareas", "editar"),
        ("lluvia", "🌧️ Lluvia", "lluvias", "editar"),
    ]
    numeradas = [(a, t) for a, t, mod, niv in todas if _wa_puede(ident, mod, niv)]

    if not numeradas:
        await kapso.enviar_texto(
            numero,
            f"{saludo}\n\nTu usuario todavía no tiene permisos para cargar datos.",
        )
        return

    emoji_num = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
    lineas_num = [f"{emoji_num[i]} {titulo}" for i, (_a, titulo) in enumerate(numeradas)]

    cuerpo = (
        f"{saludo}\n\n"
        "¿Qué querés cargar?\n\n"
        + "\n".join(lineas_num) +
        "\n\nRespondé con el número o escribime directo, por ejemplo "
        f"_llovieron 25 mm en {campo_demo}_, o mandame la *foto de una factura*."
    )

    await kapso.enviar_texto(numero, cuerpo)


# ── Tareas ────────────────────────────────────

async def _wa_tareas_pendientes(ident: dict) -> list:
    params = {
        "select": "id,titulo,descripcion,estado,fecha_vencimiento,campo_id",
        "estado": ESTADOS_PENDIENTES,
        "order": "fecha_vencimiento.asc.nullslast",
        "limit": "10",
    }
    if ident.get("es_dueño") or ident.get("rol") == "administrador":
        params["owner_id"] = f"eq.{ident['owner_id']}"
    else:
        params["asignado_a"] = f"eq.{ident['user_id']}"

    rows = await _sb_get("tareas", params) or []
    nombres = {c["id"]: c.get("nombre", "") for c in ident.get("campos", [])}
    for t in rows:
        t["campo_nombre"] = nombres.get(t.get("campo_id"), "")
    return rows


def _wa_vencimiento_txt(t: dict) -> str:
    f = t.get("fecha_vencimiento")
    if not f:
        return "sin fecha"
    try:
        d = datetime.fromisoformat(str(f)).date()
    except Exception:
        return str(f)
    hoy = datetime.now(AR_TZ).date()
    dias = (d - hoy).days
    if dias < 0:
        return f"venció hace {abs(dias)} día{'s' if abs(dias) != 1 else ''}"
    if dias == 0:
        return "vence hoy"
    if dias == 1:
        return "vence mañana"
    return f"vence en {dias} días"


async def _kapso_tareas(ident: dict, numero: str) -> None:
    tareas = await _wa_tareas_pendientes(ident)
    if not tareas:
        await kapso.enviar_texto(numero, "No tenés tareas pendientes 👌")
        return

    filas = []
    for t in tareas:
        sub = _wa_vencimiento_txt(t)
        if t.get("campo_nombre"):
            sub = f"{t['campo_nombre']} · {sub}"
        filas.append((f"t:{t['id']}", t.get("titulo") or "Sin título", sub))

    plural = "tarea" if len(tareas) == 1 else "tareas"
    await kapso.enviar_lista(
        numero, f"Tenés *{len(tareas)}* {plural} pendiente{'s' if len(tareas) != 1 else ''}.",
        "Ver mis tareas", filas, "Pendientes",
    )


async def _kapso_tarea_detalle(ident: dict, numero: str, tarea_id: str) -> None:
    rows = await _sb_get("tareas", {
        "id": f"eq.{tarea_id}",
        "select": "id,titulo,descripcion,estado,fecha_vencimiento,campo_id,owner_id",
    })
    if not rows:
        await kapso.enviar_texto(numero, "No encontré esa tarea. Puede que ya la hayan borrado.")
        return
    t = rows[0]
    if t.get("owner_id") != ident["owner_id"]:
        await kapso.enviar_texto(numero, "Esa tarea no es de tu equipo.")
        return

    nombres = {c["id"]: c.get("nombre", "") for c in ident.get("campos", [])}
    partes = [f"*{t.get('titulo') or 'Sin título'}*"]
    detalle = []
    if nombres.get(t.get("campo_id")):
        detalle.append(nombres[t["campo_id"]])
    detalle.append(_wa_vencimiento_txt(t))
    partes.append(" · ".join(detalle))
    if t.get("descripcion"):
        partes.append(f"\n{t['descripcion']}")
    if t.get("estado") == "en_progreso":
        partes.append("\n_Ya está empezada._")

    botones = [(f"t_fin:{tarea_id}", "✅ La terminé")]
    if t.get("estado") != "en_progreso":
        botones.append((f"t_ini:{tarea_id}", "▶️ La empecé"))
    botones.append(("tareas", "Volver"))
    await kapso.enviar_botones(numero, "\n".join(partes), botones)
    # Queda en foco: si después escribe "la terminé", sabemos de cuál habla.
    await _wa_set_conv(numero, "k_tarea_activa",
                       {"tarea_id": tarea_id, "titulo": t.get("titulo") or ""})


async def _kapso_tarea_iniciar(ident: dict, numero: str, tarea_id: str) -> None:
    await _sb_patch(f"tareas?id=eq.{tarea_id}", {"estado": "en_progreso"})
    await _wa_set_conv(numero, "k_tarea_activa", {"tarea_id": tarea_id})
    await kapso.enviar_texto(
        numero,
        "Marcada como empezada ▶️\n\nCuando termines escribime *la terminé*. "
        "Si querés dejar una nota o una foto, mandámela cuando quieras.")


async def _kapso_tarea_completar(ident: dict, numero: str, tarea_id: str, nota: str = "") -> None:
    payload = {
        "estado": "completada",
        "completada_at": datetime.utcnow().isoformat(),
        "completado_por": ident.get("user_id"),
    }
    if nota:
        payload["comentario_completado"] = nota[:1000]
    await _sb_patch(f"tareas?id=eq.{tarea_id}", payload)
    await _wa_set_conv(numero, "k_tarea_evidencia", {"tarea_id": tarea_id})

    if nota:
        await kapso.enviar_texto(numero, f"Listo ✅ Anoté: _{nota[:200]}_\n\nSi querés sumar una foto, mandala.")
    elif PEDIR_EVIDENCIA_TAREA:
        await kapso.enviar_texto(numero, "Anotado ✅\n\nMandame una foto o un comentario de cómo quedó.")
    else:
        await kapso.enviar_texto(
            numero,
            "Anotado ✅\n\nSi querés dejar una foto o un comentario, mandámelo ahora. Si no, ya está.",
        )


async def _wa_es_otro_pedido(ident: dict, texto: str, tarea_id: str) -> bool:
    """¿El mensaje es un pedido nuevo y no un comentario de la tarea `tarea_id`?"""
    n = _norm(texto)
    # Lluvia o gasto con monto: claramente es una carga nueva.
    if ("llov" in n or re.search(r"\bmm\b|milimetros", n)) and _extraer_numero(texto) is not None:
        return True
    if _RE_GASTO.search(n) and _parsear_plata(texto):
        return True
    # Nombra otra tarea pendiente (por su título).
    tareas = await _wa_tareas_pendientes(ident)
    for t in tareas:
        if t.get("id") == tarea_id:
            continue
        palabras = [w for w in _norm(t.get("titulo") or "").split() if len(w) >= 5]
        if palabras and any(w in n for w in palabras):
            return True
    return False


async def _kapso_tarea_evidencia(ident: dict, numero: str, m, datos: dict) -> None:
    """Foto o comentario después de completar una tarea."""
    tarea_id = datos.get("tarea_id")
    if not tarea_id:
        await _wa_clear_conv(numero)
        return

    payload = {}
    if m.texto:
        payload["comentario_completado"] = m.texto[:1000]

    if m.tiene_media:
        contenido = await kapso.descargar_media(m.media_url)
        nombre = m.media_nombre or "foto.jpg"
        path = f"{ident['owner_id']}/{int(datetime.utcnow().timestamp())}_{nombre}"
        url = await _sb_storage_subir("tareas-archivos", path, contenido, m.media_tipo)
        if url:
            actuales = await _sb_get("tareas", {"id": f"eq.{tarea_id}", "select": "archivos"})
            archivos = (actuales[0].get("archivos") if actuales else None) or []
            if not isinstance(archivos, list):
                archivos = []
            archivos.append({"url": url, "path": path, "nombre": nombre, "tipo": m.media_tipo})
            payload["archivos"] = archivos

    if payload:
        await _sb_patch(f"tareas?id=eq.{tarea_id}", payload)
        await _wa_clear_conv(numero)
        filas = await _sb_get("tareas", {"id": f"eq.{tarea_id}", "select": "titulo"})
        titulo = (filas[0].get("titulo") if filas else "") or "la tarea"
        que = "la foto" if m.tiene_media else "el comentario"
        await kapso.enviar_texto(numero, f"Guardé {que} en *{titulo}* 📎 Ya lo pueden ver en la app.")
    else:
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(numero, "Listo, quedó completada.")


# ── Lluvia ────────────────────────────────────

TIPOS_LLUVIA = [("pareja", "Pareja"), ("tormenta", "Tormenta"),
                ("garua", "Garúa"), ("granizo", "Granizo")]

ETAPAS_LLUVIA = [("barbecho", "Barbecho"), ("siembra", "Siembra"),
                 ("vegetativo", "Vegetativo"),
                 ("floracion", "Floración (crítica)"),
                 ("llenado", "Llenado de grano"),
                 ("maduracion", "Maduración")]


async def _kapso_guardar_lluvia(ident: dict, numero: str, campo: dict, mm: float,
                                fecha: str = None, tipo: str = None,
                                etapa: str = None, observacion: str = None) -> None:
    """Guarda una lluvia con paridad al modal de la app."""
    payload = {
        "campo_id": campo["id"],
        "usuario_id": ident["owner_id"],
        "mm": float(mm),
        "fecha": fecha or datetime.now(AR_TZ).date().isoformat(),
        "fuente": "whatsapp",
    }
    if tipo:
        payload["tipo"] = tipo
    if etapa:
        payload["etapa"] = etapa
    if observacion:
        payload["observacion"] = observacion[:200]

    await _sb_post("lluvias", payload)
    await _wa_clear_conv(numero)

    # Acumulado del mes, para que el dato sirva en el momento
    desde = datetime.now(AR_TZ).date().replace(day=1).isoformat()
    filas = await _sb_get("lluvias", {
        "campo_id": f"eq.{campo['id']}", "fecha": f"gte.{desde}", "select": "mm",
    })
    total = sum(float(f.get("mm") or 0) for f in filas or [])
    mes = datetime.now(AR_TZ).strftime("%B").lower()
    meses = {"january": "enero", "february": "febrero", "march": "marzo", "april": "abril",
             "may": "mayo", "june": "junio", "july": "julio", "august": "agosto",
             "september": "septiembre", "october": "octubre", "november": "noviembre",
             "december": "diciembre"}
    cuando = "hoy" if payload["fecha"] == datetime.now(AR_TZ).date().isoformat() else _wa_fecha_corta(payload["fecha"])
    extras = []
    if tipo:
        lbl = next((v for k, v in TIPOS_LLUVIA if k == tipo), tipo)
        extras.append(f"🌧️ {lbl}")
    if etapa:
        lbl = next((v for k, v in ETAPAS_LLUVIA if k == etapa), etapa)
        extras.append(f"🌱 {lbl}")
    extra_txt = f"\n{' · '.join(extras)}" if extras else ""
    obs_txt = f"\n📝 _{observacion}_" if observacion else ""
    await kapso.enviar_texto(
        numero,
        f"✅ *{_num(mm)} mm* en {campo['nombre']}, {cuando}.{extra_txt}{obs_txt}\n\n"
        f"_Van {_num(total)} mm en {meses.get(mes, mes)}._",
    )


def _num(v) -> str:
    """Formatea sin decimales cuando es redondo."""
    try:
        f = float(v)
    except Exception:
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:.1f}".replace(".", ",")


async def _kapso_pedir_campo(ident: dict, numero: str, estado: str, datos: dict, texto: str) -> bool:
    """
    Pide elegir campo. Devuelve True si preguntó, False si había uno solo
    (en ese caso deja el campo elegido en datos y sigue el flujo).
    """
    campos = ident.get("campos") or []
    if not campos:
        await kapso.enviar_texto(numero, f"No tenés campos cargados todavía. Creá uno en {APP_URL} y volvé.")
        await _wa_clear_conv(numero)
        return True
    if len(campos) == 1:
        datos["campo_id"] = campos[0]["id"]
        datos["campo_nombre"] = campos[0]["nombre"]
        return False

    await _wa_set_conv(numero, estado, datos)
    filas = []
    for c in campos[:10]:
        sub = []
        if c.get("hectareas"):
            sub.append(f"{_num(c['hectareas'])} ha")
        if c.get("cultivo"):
            sub.append(str(c["cultivo"]))
        filas.append((f"campo:{c['id']}", c["nombre"], " · ".join(sub)))
    await kapso.enviar_lista(numero, texto, "Elegir campo", filas, "Tus campos")
    return True


# ── Gastos ────────────────────────────────────

# Lista de rubros oficial de la app (paridad con index.html). Los primeros 4
# admiten descuento desde stock_insumos; los demás son monto plano.
RUBROS_WA_DEFS = [
    ("herbicidas", "🧪 Herbicidas"),
    ("fungicidas", "🍄 Fungicidas"),
    ("insecticidas", "🐛 Insecticidas"),
    ("fertilizantes", "⚗️ Fertilizantes"),
    ("semillas", "🌱 Semillas"),
    ("laboreo", "🚜 Laboreo"),
    ("flete", "🚛 Flete"),
    ("empleados", "👷 Empleados"),
    ("arrendamiento", "🏠 Arrendamiento"),
    ("riego", "💧 Riego"),
    ("otros", "📦 Otros"),
]
# Para retrocompatibilidad con código viejo que usaba la lista de strings:
RUBROS_WA = [v.split(" ", 1)[1] if " " in v else v for _k, v in RUBROS_WA_DEFS]
RUBROS_WA_INSUMO = {"herbicidas", "fungicidas", "insecticidas", "fertilizantes"}


async def _ocr_factura(contenido: bytes, mime: str) -> list | None:
    """
    Llama a la edge function ocr-factura. Devuelve la lista de renglones,
    o None si no se pudo leer (falta la clave de Anthropic, formato raro, etc).
    """
    import base64
    try:
        b64 = base64.b64encode(contenido).decode()
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{_sb_url()}/functions/v1/ocr-factura",
                headers={"Authorization": f"Bearer {_sb_key()}", "Content-Type": "application/json"},
                json={"image_base64": b64, "mime_type": mime or "image/jpeg", "tipo": "factura"},
            )
            if r.status_code == 200:
                return (r.json() or {}).get("items") or []
            print(f"[OCR factura] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[OCR factura] excepción: {e}")
    return None


async def _ocr_llamar(slug: str, contenido: bytes, mime: str, extra: dict = None) -> dict | None:
    """Helper genérico para OCR de pago/carta-porte/gasto-estructura/activo.
    Devuelve el dict "data" de la respuesta, o None si falló."""
    import base64
    try:
        b64 = base64.b64encode(contenido).decode()
        body = {"image_base64": b64, "mime_type": mime or "image/jpeg"}
        if extra:
            body.update(extra)
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{_sb_url()}/functions/v1/{slug}",
                headers={"Authorization": f"Bearer {_sb_key()}", "Content-Type": "application/json"},
                json=body,
            )
            if r.status_code == 200:
                js = r.json() or {}
                if js.get("ok"):
                    return js.get("data") or {}
                print(f"[OCR {slug}] respondió ok=false: {str(js)[:200]}")
            else:
                print(f"[OCR {slug}] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[OCR {slug}] excepción: {e}")
    return None


def _detectar_tipo_adjunto(texto: str) -> str | None:
    """Detecta qué tipo de documento es por el caption del archivo.
    Devuelve: 'factura', 'cheque', 'credito', 'carta_porte', 'gasto_estr', 'activo'
    o None si no puede decidir."""
    n = _norm(texto or "")
    if not n:
        return None
    if re.search(r"\b(carta\s+de\s+porte|cpe|flete|carta\s+porte)\b", n):
        return "carta_porte"
    if re.search(r"\b(cr(é|e)dito|pr(é|e)stamo|financiaci(ó|o)n)\b", n):
        return "credito"
    if re.search(r"\b(cheque|echeq|ecocheq|transferencia|pago)\b", n):
        return "cheque"
    if re.search(r"\b(sueldo|salario|honorarios|contador|seguro|p(ó|o)liza|alquiler|oficina)\b", n):
        return "gasto_estr"
    if re.search(r"\b(pulverizadora|tractor|cosechadora|sembradora|camioneta|camion|maquinaria|herramienta)\b", n):
        return "activo"
    if re.search(r"\b(factura|remito|comprobante|ticket|insumo)\b", n):
        return "factura"
    return None


RUBROS_FACTURA = {"herbicidas", "fungicidas", "insecticidas", "fertilizantes", "semillas"}


def _rubro_de_factura(tipo) -> str:
    t = _norm(str(tipo or ""))
    return t if t in RUBROS_FACTURA else "otros"


def _subtotal_renglon(it: dict) -> float:
    """El subtotal impreso manda; si no viene, cantidad x precio (en la misma unidad)."""
    if it.get("subtotal_usd") is not None:
        return float(it["subtotal_usd"])
    if it.get("precio_unitario") is not None:
        return float(it.get("cantidad") or 0) * float(it["precio_unitario"])
    return 0.0


async def _kapso_adjunto_router(ident: dict, numero: str, m) -> None:
    """
    Decide qué tipo de documento es el archivo (factura / cheque / crédito /
    carta de porte / gasto estructura / activo) y llama al OCR correcto.
    Si no puede decidir por el caption, guarda el archivo en estado y pide
    al user que elija con botones.
    """
    # 1. Intento detectar por el caption/texto que vino con el archivo.
    tipo_adj = _detectar_tipo_adjunto(m.texto or "")

    # 2. Si no detecto, guardo bytes en memoria del estado y pregunto.
    if not tipo_adj:
        # Para no cargarnos el payload del estado con el binario, lo subimos
        # a una carpeta temporal del bucket y guardamos solo la URL + path.
        contenido = await kapso.descargar_media(m.media_url)
        if not contenido:
            await kapso.enviar_texto(numero, "No pude bajar el archivo. ¿Me lo reenviás?")
            return
        nombre = m.media_nombre or ("adjunto.pdf" if m.es_pdf else "adjunto.jpg")
        path = f"{ident['owner_id']}/tmp/{int(datetime.utcnow().timestamp())}_{nombre}"
        url = await _sb_storage_subir("insumos-facturas", path, contenido, m.media_tipo)
        if not url:
            await kapso.enviar_texto(numero, "No pude subir el archivo. Probá de nuevo.")
            return
        await _wa_set_conv(numero, "k_adjunto_tipo", {
            "path": path, "url": url, "mime": m.media_tipo,
            "nombre": nombre,
        })
        await kapso.enviar_botones(
            numero, "📎 ¿Qué es este archivo?",
            [("adj_factura", "🧾 Factura"),
             ("adj_cheque", "💳 Cheque"),
             ("adj_cp", "🚚 Carta porte")])
        return

    # 3. Si detecté, llamo directo al OCR
    contenido = await kapso.descargar_media(m.media_url)
    if not contenido:
        await kapso.enviar_texto(numero, "No pude bajar el archivo. ¿Me lo reenviás?")
        return
    nombre = m.media_nombre or ("adjunto.pdf" if m.es_pdf else "adjunto.jpg")
    path = f"{ident['owner_id']}/{int(datetime.utcnow().timestamp())}_{nombre}"

    if tipo_adj == "factura":
        await _kapso_gasto_desde_archivo(ident, numero, m)
        return
    if tipo_adj == "carta_porte":
        await _kapso_procesar_ocr_cp(ident, numero, contenido, m.media_tipo, nombre, path)
        return
    if tipo_adj in ("cheque", "credito"):
        await _kapso_procesar_ocr_pago(ident, numero, contenido, m.media_tipo,
                                       nombre, path, tipo_adj)
        return
    if tipo_adj == "gasto_estr":
        await _kapso_procesar_ocr_gasto_estr(ident, numero, contenido, m.media_tipo,
                                              nombre, path)
        return
    if tipo_adj == "activo":
        await _kapso_procesar_ocr_activo(ident, numero, contenido, m.media_tipo,
                                          nombre, path)
        return


async def _kapso_procesar_ocr_cp(ident: dict, numero: str, contenido: bytes,
                                  mime: str, nombre: str, path: str) -> None:
    if not _wa_puede(ident, "gastos", "editar"):
        await kapso.enviar_texto(numero, "Tu usuario no tiene permiso para cargar fletes.")
        return
    await kapso.enviar_texto(numero, "🔍 Leyendo la carta de porte...")
    data = await _ocr_llamar("ocr-carta-porte", contenido, mime)
    if not data:
        await kapso.enviar_texto(numero, "No pude leer la carta de porte. Cargala desde la app.")
        return
    url = await _sb_storage_subir("insumos-facturas", path, contenido, mime)
    datos = {"flete": {
        "campo_id": None,
        "cultivo": (data.get("cultivo") or "").lower() or None,
        "fecha": data.get("fecha") or datetime.now(AR_TZ).date().isoformat(),
        "patente_camion": data.get("patente_camion"),
        "patente_acoplado": data.get("patente_acoplado"),
        "nombre_camionero": data.get("nombre_camionero"),
        "nombre_transporte": data.get("nombre_transporte"),
        "toneladas": data.get("toneladas"),
        "destino": data.get("destino"),
        "empresa_destino": data.get("empresa_destino"),
        "ctg": data.get("ctg"),
        "carta_porte_nro": data.get("carta_porte_nro"),
        "peso_bruto_kg": data.get("peso_bruto_kg"),
        "peso_tara_kg": data.get("peso_tara_kg"),
        "peso_neto_kg": data.get("peso_neto_kg"),
        "humedad_porcentaje": data.get("humedad_porcentaje"),
        "archivo_url": url, "archivo_path": path,
    }}
    await _wa_set_conv(numero, "k_cp_confirmar", datos)
    partes = ["🚚 *Entendí esta carta de porte:*"]
    if data.get("cultivo"):
        partes.append(f"🌾 Cultivo: *{data['cultivo']}*")
    if data.get("toneladas"):
        partes.append(f"⚖️ Toneladas: *{_num(data['toneladas'])}*")
    if data.get("patente_camion"):
        partes.append(f"🚛 Patente: {data['patente_camion']}")
    if data.get("empresa_destino") or data.get("destino"):
        partes.append(f"📍 Destino: {data.get('empresa_destino') or data.get('destino')}")
    if data.get("ctg"):
        partes.append(f"🧾 CTG: {data['ctg']}")
    partes.append("\n¿La cargo así?")
    await kapso.enviar_botones(numero, "\n".join(partes),
        [("cp_ok", "✅ Sí, cargala"), ("cp_cancel", "❌ Cancelar")])


async def _kapso_procesar_ocr_pago(ident: dict, numero: str, contenido: bytes,
                                     mime: str, nombre: str, path: str,
                                     tipo: str) -> None:
    if not _wa_puede(ident, "compromisos_pago", "editar"):
        await kapso.enviar_texto(numero, "No tenés permiso para cargar pagos.")
        return
    tipo_lbl = "cheque" if tipo == "cheque" else "crédito"
    await kapso.enviar_texto(numero, f"🔍 Leyendo el {tipo_lbl}...")
    data = await _ocr_llamar("ocr-pago", contenido, mime, {"tipo": tipo})
    if not data:
        await kapso.enviar_texto(numero, f"No pude leer el {tipo_lbl}. Cargalo desde la app.")
        return
    url = await _sb_storage_subir("insumos-facturas", path, contenido, mime)

    if tipo == "cheque":
        datos = {"pago": {
            "tipo": "cheque",
            "beneficiario": data.get("beneficiario") or "",
            "importe": float(data.get("importe") or 0),
            "moneda": data.get("moneda") or "ARS",
            "fecha": data.get("fecha_cobro") or datetime.now(AR_TZ).date().isoformat(),
            "fecha_emision": data.get("fecha_emision"),
            "banco": data.get("banco"),
            "numero_cheque": data.get("numero_cheque"),
            "archivo_url": url, "archivo_path": path,
        }}
        await _wa_set_conv(numero, "k_pago_confirmar", datos)
        partes = ["💳 *Entendí este cheque:*"]
        partes.append(f"👤 Beneficiario: *{data.get('beneficiario') or '—'}*")
        partes.append(f"💰 Importe: *{data.get('moneda', 'ARS')} {_miles(data.get('importe') or 0)}*")
        if data.get("fecha_cobro"):
            partes.append(f"📅 Cobro: {_wa_fecha_corta(data['fecha_cobro'])}")
        if data.get("banco"):
            partes.append(f"🏦 Banco: {data['banco']}")
        if data.get("numero_cheque"):
            partes.append(f"#️⃣ Nº: {data['numero_cheque']}")
        partes.append("\n¿Lo cargo así?")
        await kapso.enviar_botones(numero, "\n".join(partes),
            [("pago_ok", "✅ Sí, cargalo"), ("pago_cancel", "❌ Cancelar")])
        return

    # credito
    datos = {"pago_credito": {
        "descripcion": data.get("descripcion") or "",
        "beneficiario": data.get("beneficiario") or "",
        "cuota_importe": float(data.get("cuota_importe") or 0),
        "moneda": data.get("moneda") or "ARS",
        "n_cuotas": int(data.get("n_cuotas") or 1),
        "frecuencia": data.get("frecuencia") or "mensual",
        "fecha_primera": data.get("fecha_primera") or datetime.now(AR_TZ).date().isoformat(),
        "archivo_url": url, "archivo_path": path,
    }}
    await _wa_set_conv(numero, "k_pago_confirmar", datos)
    partes = ["💳 *Entendí este crédito:*"]
    if data.get("descripcion"):
        partes.append(f"📝 {data['descripcion']}")
    partes.append(f"💰 {data.get('n_cuotas')} cuotas de "
                  f"*{data.get('moneda', 'ARS')} {_miles(data.get('cuota_importe') or 0)}*")
    partes.append(f"🔁 Frecuencia: {data.get('frecuencia')}")
    partes.append(f"📅 Primera: {_wa_fecha_corta(data.get('fecha_primera'))}")
    partes.append("\n¿Lo cargo así?")
    await kapso.enviar_botones(numero, "\n".join(partes),
        [("pago_ok", "✅ Sí, cargalo"), ("pago_cancel", "❌ Cancelar")])


async def _kapso_procesar_ocr_gasto_estr(ident: dict, numero: str, contenido: bytes,
                                           mime: str, nombre: str, path: str) -> None:
    if not _wa_puede(ident, "gastos", "editar"):
        return
    await kapso.enviar_texto(numero, "🔍 Leyendo el comprobante...")
    data = await _ocr_llamar("ocr-gasto-estr", contenido, mime)
    if not data:
        await kapso.enviar_texto(numero, "No pude leer el comprobante. Cargalo desde la app.")
        return
    datos = {"gasto_estructura_ocr": {
        "descripcion": data.get("descripcion") or "",
        "tipo": (data.get("tipo") or "otros").lower(),
        "importe_usd": float(data.get("importe_usd") or 0),
        "frecuencia": (data.get("frecuencia") or "mensual").lower(),
        "fecha_inicio": data.get("fecha") or datetime.now(AR_TZ).date().isoformat(),
    }}
    await _wa_set_conv(numero, "k_estr_confirmar", datos)
    g = datos["gasto_estructura_ocr"]
    partes = ["🏢 *Entendí este gasto de estructura:*",
              f"📝 {g['descripcion']}",
              f"💰 USD {_num(g['importe_usd'])} ({g['frecuencia']})",
              f"📂 {g['tipo'].capitalize()}",
              "\n¿Lo cargo así?"]
    await kapso.enviar_botones(numero, "\n".join(partes),
        [("estr_ok", "✅ Sí, cargalo"), ("estr_cancel", "❌ Cancelar")])


async def _kapso_procesar_ocr_activo(ident: dict, numero: str, contenido: bytes,
                                       mime: str, nombre: str, path: str) -> None:
    if not _wa_puede(ident, "gastos", "editar"):
        return
    await kapso.enviar_texto(numero, "🔍 Leyendo la factura del activo...")
    data = await _ocr_llamar("ocr-activo", contenido, mime)
    if not data:
        await kapso.enviar_texto(numero, "No pude leer la factura. Cargala desde la app.")
        return
    datos = {"activo_ocr": {
        "descripcion": data.get("descripcion") or "",
        "tipo": (data.get("tipo") or "otros").lower(),
        "valor_compra_usd": float(data.get("valor_compra_usd") or 0),
        "vida_util_anos": int(data.get("vida_util_anos") or 10),
        "fecha_compra": data.get("fecha_compra") or datetime.now(AR_TZ).date().isoformat(),
    }}
    await _wa_set_conv(numero, "k_act_confirmar", datos)
    a = datos["activo_ocr"]
    amort = a["valor_compra_usd"] / a["vida_util_anos"] if a["vida_util_anos"] else 0
    partes = ["🚜 *Entendí este activo:*",
              f"📝 {a['descripcion']}",
              f"💰 USD {_miles(a['valor_compra_usd'])} · vida útil {a['vida_util_anos']} años",
              f"📉 Amortización anual: USD {_miles(round(amort, 2))}",
              "\n¿Lo cargo así?"]
    await kapso.enviar_botones(numero, "\n".join(partes),
        [("act_ok", "✅ Sí, cargalo"), ("act_cancel", "❌ Cancelar")])


async def _kapso_gasto_desde_archivo(ident: dict, numero: str, m) -> None:
    """Foto o PDF de una factura → gasto cargado."""
    if not _wa_puede(ident, "gastos", "editar"):
        await kapso.enviar_texto(numero, "Tu usuario no tiene permiso para cargar gastos.")
        return

    contenido = await kapso.descargar_media(m.media_url)
    if not contenido:
        await kapso.enviar_texto(numero, "No pude bajar el archivo. ¿Me lo reenviás?")
        return

    nombre_arch = m.media_nombre or ("factura.pdf" if m.es_pdf else "factura.jpg")
    path = f"{ident['owner_id']}/{int(datetime.utcnow().timestamp())}_{nombre_arch}"

    items = await _ocr_factura(contenido, m.media_tipo)
    if not items:
        # No se pudo leer (o no tiene renglones de insumos): igual guardamos la
        # foto y la adjuntamos al gasto que carguen a mano, así queda en la web.
        url = await _sb_storage_subir("insumos-facturas", path, contenido, m.media_tipo)
        datos = {"gasto": {"factura_url": url, "factura_path": path, "factura_tipo": "factura"}}
        motivo = ("No pude leer los renglones de la factura 🤔" if items is not None
                  else "Todavía no puedo leer facturas automáticamente 😕")
        if await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos,
                                    f"{motivo}\n\nGuardé la foto 📎 y la cargamos a mano. "
                                    "¿A qué campo va?"):
            return
        await _wa_set_conv(numero, "k_gasto_rubro", datos)
        await _kapso_pedir_rubro(numero)
        return

    url = await _sb_storage_subir("insumos-facturas", path, contenido, m.media_tipo)

    # Un gasto por rubro: una factura con glifosato y urea son dos gastos
    # (herbicidas y fertilizantes), así cada uno cae en su rubro en la web.
    # Lo que no se pueda clasificar va a "otros".
    por_rubro: dict = {}
    for it in items:
        rubro = _rubro_de_factura(it.get("tipo"))
        g = por_rubro.setdefault(rubro, {"rubro": rubro, "nombres": [], "total": 0.0,
                                         "items": []})
        g["nombres"].append(str(it.get("nombre") or "insumo"))
        g["total"] += _subtotal_renglon(it)
        g["items"].append(it)

    gastos = []
    for g in por_rubro.values():
        gasto = {
            "rubro": g["rubro"],
            "descripcion": ", ".join(g["nombres"][:4])[:300],
            "total_de_usd": round(g["total"], 2),
            "factura_url": url,
            "factura_path": path,
            "factura_tipo": "factura",
        }
        if len(g["items"]) == 1:   # con un solo producto, guardamos el detalle
            it = g["items"][0]
            gasto.update({k: it.get(k) for k in ("cantidad", "unidad", "precio_unitario")})
        gastos.append(gasto)
    total = sum(g["total_de_usd"] for g in gastos)
    datos = {"gastos": gastos}

    resumen = ["Leí la factura 👇", ""]
    for it in items[:4]:
        linea = str(it.get("nombre") or "insumo")
        if it.get("cantidad"):
            linea += f" — {_num(it['cantidad'])} {it.get('unidad') or ''}".rstrip()
        resumen.append(linea)
    if len(items) > 4:
        resumen.append(f"_y {len(items) - 4} renglón/es más_")
    if total > 0:
        resumen.append("")
        resumen.append(f"*USD {_num(round(total, 2))}*")
    if len(gastos) > 1:
        resumen.append("_Lo separo en " + ", ".join(
            f"{g['rubro'].capitalize()} USD {_num(g['total_de_usd'])}" for g in gastos) + "._")

    if await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos,
                                "\n".join(resumen) + "\n\n¿A qué campo lo cargo?"):
        return
    await _kapso_guardar_gasto(ident, numero, datos)


async def _kapso_guardar_gasto(ident: dict, numero: str, datos: dict) -> None:
    if datos.get("gastos"):
        await _kapso_guardar_gastos_factura(ident, numero, datos)
        return
    g = dict(datos.get("gasto") or {})
    campo_id = datos.get("campo_id")
    campo_nombre = datos.get("campo_nombre") or ""

    payload = {
        "campo_id": campo_id,
        "usuario_id": ident["owner_id"],
        # La web guarda el rubro en minúscula ("flete", "herbicidas") y agrupa
        # por ese valor exacto: con mayúscula el gasto queda fuera de los
        # gráficos por rubro.
        "rubro": (g.get("rubro") or "otros").strip().lower(),
        "fecha": g.get("fecha") or datetime.now(AR_TZ).date().isoformat(),
        "total_de_usd": g.get("total_de_usd") or 0,
        "descripcion": g.get("descripcion") or "",
    }
    for k in ("cantidad", "unidad", "precio_unitario", "factura_url", "factura_path",
              "factura_tipo", "momento_pulverizacion", "cultivo"):
        if g.get(k) is not None:
            payload[k] = g[k]

    # USD/ha, que es como se mira el costo en la app
    ha = 0.0
    for c in ident.get("campos", []):
        if c["id"] == campo_id:
            ha = float(c.get("hectareas") or 0)
            break
    if ha > 0 and payload["total_de_usd"]:
        payload["usd_ha"] = round(float(payload["total_de_usd"]) / ha, 2)

    await _sb_post("gastos", payload)
    await _wa_clear_conv(numero)

    lineas = [f"✅ Cargado en *{campo_nombre}*", f"{payload['rubro'].capitalize()} · USD {_num(payload['total_de_usd'])}"]
    if payload.get("usd_ha"):
        lineas.append(f"USD {_num(payload['usd_ha'])}/ha")
    if g.get("factura_url"):
        lineas.append("\n_La factura quedó adjunta._")
    await kapso.enviar_botones(numero, "\n".join(lineas), [("menu", "Cargar otra cosa")])


async def _kapso_guardar_gastos_factura(ident: dict, numero: str, datos: dict) -> None:
    """Una factura → un gasto por rubro, todos con la factura adjunta."""
    campo_id = datos.get("campo_id")
    ha = next((float(c.get("hectareas") or 0) for c in ident.get("campos", [])
               if c["id"] == campo_id), 0.0)
    lineas = [f"✅ Cargado en *{datos.get('campo_nombre') or ''}*"]
    for g in datos["gastos"]:
        payload = {
            "campo_id": campo_id,
            "usuario_id": ident["owner_id"],
            "rubro": (g.get("rubro") or "otros").lower(),
            "fecha": datetime.now(AR_TZ).date().isoformat(),
            "total_de_usd": g.get("total_de_usd") or 0,
            "descripcion": g.get("descripcion") or "",
        }
        for k in ("cantidad", "unidad", "precio_unitario", "factura_url", "factura_path", "factura_tipo"):
            if g.get(k) is not None:
                payload[k] = g[k]
        if ha > 0 and payload["total_de_usd"]:
            payload["usd_ha"] = round(float(payload["total_de_usd"]) / ha, 2)
        await _sb_post("gastos", payload)
        lineas.append(f"{payload['rubro'].capitalize()} · USD {_num(payload['total_de_usd'])}")
    await _wa_clear_conv(numero)
    lineas.append("\n_La factura quedó adjunta._")
    await kapso.enviar_botones(numero, "\n".join(lineas), [("menu", "Cargar otra cosa")])


async def _kapso_confirmar_gasto(numero: str, datos: dict) -> None:
    """Muestra lo que se entendió y espera el OK antes de guardar."""
    datos.pop("confirmar", None)
    await _wa_set_conv(numero, "k_gasto_confirmar", datos)
    g = datos["gasto"]
    detalle = f" · {g['descripcion']}" if g.get("descripcion") else ""
    await kapso.enviar_botones(
        numero,
        f"Entendí esto:\n*{g['rubro'].capitalize()}*{detalle}\n"
        f"*USD {_num(g['total_de_usd'])}* · {datos.get('campo_nombre') or ''}\n\n¿Lo cargo así?",
        [("g_ok", "✅ Sí, cargalo"), ("g_fix", "✏️ Corregir")])


async def _kapso_gasto_guiado(ident: dict, numero: str) -> None:
    """Arranca la carga manual de un gasto, paso a paso."""
    datos = {"gasto": {}}
    if await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos, "¿En qué campo fue el gasto?"):
        return
    await _wa_set_conv(numero, "k_gasto_rubro", datos)
    await _kapso_pedir_rubro(numero)


async def _kapso_pedir_rubro(numero: str) -> None:
    filas = [(f"rubro:{k}", v) for k, v in RUBROS_WA_DEFS]
    # WhatsApp list permite hasta 10 filas; tenemos 11 rubros así que recortamos
    # "otros" y dejamos 10 (el user puede escribir "otros" por texto igual).
    await kapso.enviar_lista(numero, "¿Qué rubro?", "Elegir rubro", filas[:10], "Rubros")


async def _wa_insumos_del_user(ident: dict, tipo: str, limite: int = 9) -> list:
    """Lee los insumos del user de un tipo específico, con stock > 0."""
    return await _sb_get("stock_insumos", {
        "owner_id": f"eq.{ident['owner_id']}",
        "tipo": f"eq.{tipo}",
        "stock_actual": "gt.0",
        "select": "id,nombre,unidad,stock_actual,costo_unitario",
        "order": "nombre.asc",
        "limit": str(limite),
    }) or []


async def _kapso_guardar_gasto_especial(ident: dict, numero: str, datos: dict) -> None:
    """
    Guarda un gasto de rubros especiales (flete, riego, empleados, arrendamiento).
    Calcula el usd_total y los campos genericos (cantidad, unidad, precio_unitario)
    según el rubro, igual que guardarGasto() del frontend.
    """
    g = datos["gasto"]
    campo_id = datos.get("campo_id")
    rubro = (g.get("rubro") or "otros").lower()

    # ha del campo para usd/ha
    ha_campo = 0.0
    for c in ident.get("campos", []):
        if c["id"] == campo_id:
            ha_campo = float(c.get("hectareas") or 0)
            break
    ha_apl = float(g.get("ha_aplicadas") or ha_campo or 1)

    usd_total = 0.0
    descripcion = (g.get("descripcion") or "").strip()[:300]
    payload_extra = {}

    if rubro == "flete":
        tn = float(g.get("tn") or 0)
        pr = float(g.get("precio_tn") or 0)
        mon = (g.get("moneda") or "USD").upper()
        tc = float(g.get("tc") or 0)
        if mon == "ARS" and tc > 0:
            usd_total = (tn * pr) / tc
        else:
            usd_total = tn * pr
        payload_extra["cantidad"] = tn
        payload_extra["precio_unitario"] = pr
        payload_extra["unidad"] = "ton_ars" if mon == "ARS" else "toneladas"
        if not descripcion:
            descripcion = "Flete"
    elif rubro == "riego":
        kva = float(g.get("kva") or 0)
        costo_kva = float(g.get("costo_kva") or 0)
        usd_total = kva * costo_kva
        payload_extra["cantidad"] = kva
        payload_extra["precio_unitario"] = costo_kva
        payload_extra["unidad"] = "kva"
        if not descripcion:
            descripcion = f"Riego — {_num(kva)} kVA"
    elif rubro == "empleados":
        monto = float(g.get("monto") or 0)
        mon = (g.get("moneda") or "USD").upper()
        tc = float(g.get("tc") or 0)
        if mon == "ARS" and tc > 0:
            usd_total = monto / tc
        else:
            usd_total = monto
        if not descripcion:
            descripcion = "Gasto de empleados"
    elif rubro == "arrendamiento":
        qq_ha = float(g.get("qq_ha") or 0)
        precio_soja = float(g.get("precio_soja") or 0)  # USD/tn
        usd_total = qq_ha * (precio_soja / 10.0) * (ha_campo or 1)
        payload_extra["cantidad"] = qq_ha
        payload_extra["precio_unitario"] = precio_soja
        payload_extra["unidad"] = "qq_ha"
        if not descripcion:
            descripcion = f"Arrendamiento — {_num(qq_ha)} qq/ha"

    usd_ha = round(usd_total / ha_apl, 2) if ha_apl > 0 else None

    payload = {
        "campo_id": campo_id,
        "usuario_id": ident["owner_id"],
        "rubro": rubro,
        "fecha": g.get("fecha") or datetime.now(AR_TZ).date().isoformat(),
        "total_de_usd": round(usd_total, 2),
        "descripcion": descripcion,
    }
    if usd_ha is not None:
        payload["usd_ha"] = usd_ha
    payload.update(payload_extra)

    await _sb_post("gastos", payload)
    await _wa_clear_conv(numero)

    rubro_label = rubro.capitalize()
    lineas = [f"✅ {rubro_label} cargado en *{datos.get('campo_nombre')}*",
              f"USD *{_num(usd_total)}* total"]
    if usd_ha:
        lineas.append(f"USD {_num(usd_ha)}/ha")
    if descripcion:
        lineas.append(f"📝 _{descripcion}_")
    await kapso.enviar_texto(numero, "\n".join(lineas))


async def _kapso_guardar_gasto_con_stock(ident: dict, numero: str, datos: dict) -> None:
    """
    Guarda un gasto que sale del stock: inserta en gastos (con insumo_id) y
    descuenta cantidad × ha del stock_actual del insumo.
    """
    g = datos["gasto"]
    campo_id = datos.get("campo_id")
    ins_id = g.get("insumo_id")
    cantidad_total = float(g.get("cantidad_total") or 0)
    stock_disp = float(g.get("_stock_disp") or 0)
    costo_unit = float(g.get("precio_unitario") or 0)
    rubro = (g.get("rubro") or "otros").lower()

    usd_total = round(cantidad_total * costo_unit, 2) if cantidad_total and costo_unit else 0
    ha_campo = 0.0
    for c in ident.get("campos", []):
        if c["id"] == campo_id:
            ha_campo = float(c.get("hectareas") or 0)
            break

    payload = {
        "campo_id": campo_id,
        "usuario_id": ident["owner_id"],
        "rubro": rubro,
        "fecha": g.get("fecha") or datetime.now(AR_TZ).date().isoformat(),
        "total_de_usd": usd_total,
        "descripcion": g.get("descripcion", "")[:300],
        "insumo_id": ins_id,
        "cantidad": cantidad_total,  # cantidad TOTAL (no por ha) para que el stock se descuente bien
        "unidad": g.get("unidad"),
        "precio_unitario": costo_unit,
    }
    if ha_campo > 0 and usd_total:
        payload["usd_ha"] = round(usd_total / ha_campo, 2)
    if g.get("momento_pulverizacion"):
        payload["momento_pulverizacion"] = g["momento_pulverizacion"]
    if g.get("cultivo"):
        payload["cultivo"] = g["cultivo"]

    await _sb_post("gastos", payload)

    # Descontar del stock del insumo.
    nuevo_stock = max(0, round(stock_disp - cantidad_total, 4))
    await _sb_patch(f"stock_insumos?id=eq.{ins_id}", {"stock_actual": nuevo_stock})

    await _wa_clear_conv(numero)

    lineas = [
        f"✅ Gasto cargado en *{datos.get('campo_nombre')}*",
        f"{rubro.capitalize()} · *{_num(cantidad_total)} {payload['unidad']}* de {payload['descripcion']}",
        f"USD *{_num(usd_total)}* total",
    ]
    if payload.get("usd_ha"):
        lineas.append(f"USD {_num(payload['usd_ha'])}/ha")
    lineas.append(f"\n📦 Stock descontado — quedan *{_num(nuevo_stock)} {payload['unidad']}*")
    await kapso.enviar_texto(numero, "\n".join(lineas))


# ── Invitar a alguien al equipo ───────────────

async def _kapso_invitar_inicio(ident: dict, numero: str, nombre: str = "") -> None:
    if not (ident.get("es_dueño") or ident.get("rol") == "administrador"):
        await kapso.enviar_texto(numero, "Solo el dueño de la cuenta puede sumar gente al equipo.")
        return
    datos = {"nombre": (nombre or "").strip()}
    await _wa_set_conv(numero, "k_inv_rol", datos)
    quien = datos["nombre"] or "esa persona"
    await kapso.enviar_botones(
        numero, f"¿Qué va a poder hacer {quien}?",
        [("inv_rol:operario", "Operario"), ("inv_rol:administrador", "Administrador")],
    )


async def _kapso_invitar_crear(ident: dict, numero: str, rol_ui: str, datos: dict) -> None:
    import secrets
    rol_db, preset = ROLES_INVITACION.get(rol_ui, ROLES_INVITACION["operario"])
    token = secrets.token_hex(8)

    creada = await _sb_post("invitaciones", {
        "token": token,
        "owner_id": ident["owner_id"],
        "rol": rol_db,
        "permisos": PERMISOS_PRESETS[preset],
    })
    await _wa_clear_conv(numero)

    if not creada:
        await kapso.enviar_texto(numero, "No pude generar la invitación. Probá de nuevo en un minuto.")
        return

    quien = datos.get("nombre") or "la persona"
    link = f"{APP_URL}/?inv={token}"
    descripcion = ("ve y completa las tareas que le asignás, y carga gastos y lluvias. No ve números del negocio."
                   if rol_ui == "operario" else
                   "accede a todo, incluidos los números de rentabilidad.")
    await kapso.enviar_texto(
        numero,
        f"Listo. Este es el link para {quien} 👇\n\n{link}\n\n"
        f"*{rol_ui.capitalize()}:* {descripcion}\n\n"
        "Reenviáselo. Cuando lo abra queda en tu equipo. Vence en 7 días.",
    )


# ── Insumos (stock) ───────────────────────────

TIPOS_INSUMO = [("herbicidas", "Herbicidas"), ("fungicidas", "Fungicidas"),
                ("insecticidas", "Insecticidas"), ("fertilizantes", "Fertilizantes"),
                ("semillas", "Semillas"), ("otros", "Otros")]


async def _kapso_insumo_guiado(ident: dict, numero: str) -> None:
    if not _wa_puede(ident, "insumos", "editar"):
        await kapso.enviar_texto(numero, "Solo el dueño o un administrador pueden cargar insumos al stock.")
        return
    datos = {"insumo": {}}
    await _wa_set_conv(numero, "k_ins_tipo", datos)
    filas = [(f"ins_tipo:{k}", v) for k, v in TIPOS_INSUMO]
    await kapso.enviar_lista(numero, "¿Qué tipo de insumo?", "Elegir tipo", filas, "Tipos")


async def _wa_buscar_stock_existente(ident: dict, nombre: str, ubicacion: str | None) -> dict | None:
    """
    Busca una fila existente en stock_insumos del user, por mismo nombre y
    ubicación (case-insensitive). Si existe, el caller suma al stock en vez
    de crear una nueva fila (pedido del user 2026-11-24: "no me figure
    separado en dos partes, se cargue los 1.200 litros todos juntos").
    """
    if not nombre:
        return None
    params = {
        "owner_id": f"eq.{ident['owner_id']}",
        "nombre": f"ilike.{nombre.strip()}",
        "select": "id,nombre,unidad,stock_actual,costo_unitario,ubicacion",
        "limit": "5",
    }
    if ubicacion:
        params["ubicacion"] = f"ilike.{ubicacion.strip()}"
    else:
        params["ubicacion"] = "is.null"
    filas = await _sb_get("stock_insumos", params) or []
    # Preferimos match exacto por nombre+ubicación normalizados.
    norm_nombre = _norm_cultivo(nombre)
    norm_ubic = _norm_cultivo(ubicacion or "")
    for f in filas:
        if (_norm_cultivo(f.get("nombre") or "") == norm_nombre
                and _norm_cultivo(f.get("ubicacion") or "") == norm_ubic):
            return f
    return filas[0] if filas else None


async def _kapso_guardar_insumo(ident: dict, numero: str, datos: dict) -> None:
    ins = datos.get("insumo", {})
    cantidad = float(ins.get("cantidad") or 0)
    unidad = ins.get("unidad") or "unidades"
    tipo = ins.get("tipo") or "otros"
    nombre = ins.get("nombre") or "Sin nombre"
    ubicacion = ins.get("ubicacion") or None
    costo_u = float(ins["costo_unitario"]) if ins.get("costo_unitario") else None

    existente = await _wa_buscar_stock_existente(ident, nombre, ubicacion)
    if existente:
        # Mergeamos: sumamos al stock actual y promediamos el costo unitario
        # ponderado por stock (para no distorsionar con una compra chica a
        # otro precio).
        stock_prev = float(existente.get("stock_actual") or 0)
        stock_nuevo = stock_prev + cantidad
        patch = {"stock_actual": stock_nuevo}
        costo_prev = existente.get("costo_unitario")
        if costo_u is not None and costo_prev is not None and stock_prev > 0:
            costo_merge = ((float(costo_prev) * stock_prev) + (costo_u * cantidad)) / stock_nuevo
            patch["costo_unitario"] = round(costo_merge, 4)
        elif costo_u is not None and costo_prev is None:
            patch["costo_unitario"] = costo_u
        await _sb_patch(f"stock_insumos?id=eq.{existente['id']}", patch)
        await _wa_clear_conv(numero)
        tipo_label = next((v for k, v in TIPOS_INSUMO if k == tipo), tipo.capitalize())
        partes = [f"✅ Sumé *{_num(cantidad)} {unidad}* al stock existente de *{nombre}*."]
        partes.append(f"📦 Rubro: {tipo_label}")
        partes.append(f"📊 Stock total: *{_num(stock_nuevo)} {unidad}* "
                      f"(antes {_num(stock_prev)})")
        if ubicacion:
            partes.append(f"📍 {ubicacion}")
        if patch.get("costo_unitario"):
            unidad_sing = {"litros": "litro", "kg": "kg", "dosis": "dosis",
                           "bolsas": "bolsa", "unidades": "unidad"}.get(unidad, unidad)
            partes.append(f"💰 USD {_num(patch['costo_unitario'])} por {unidad_sing} (promedio ponderado)")
        await kapso.enviar_texto(numero, "\n".join(partes))
        return

    # No existe → crea nueva fila
    payload = {
        "owner_id": ident["owner_id"],
        "nombre": nombre,
        "tipo": tipo,
        "unidad": unidad,
        "stock_actual": cantidad,
        "ubicacion": ubicacion,
    }
    if costo_u is not None:
        payload["costo_unitario"] = costo_u
    if ins.get("notas"):
        payload["notas"] = ins["notas"][:500]
    await _sb_post("stock_insumos", payload)
    await _wa_clear_conv(numero)
    tipo_label = next((v for k, v in TIPOS_INSUMO if k == tipo), tipo.capitalize())
    partes = [f"✅ Agregaste *{_num(cantidad)} {unidad}* de *{nombre}* al stock.",
              f"📦 Rubro: {tipo_label}"]
    if ubicacion:
        partes.append(f"📍 {ubicacion}")
    if costo_u is not None:
        unidad_sing = {"litros": "litro", "kg": "kg", "dosis": "dosis",
                       "bolsas": "bolsa", "unidades": "unidad"}.get(unidad, unidad)
        partes.append(f"💰 USD {_num(costo_u)} por {unidad_sing}")
    if ins.get("notas"):
        partes.append(f"📝 _{ins['notas']}_")
    await kapso.enviar_texto(numero, "\n".join(partes))


# ── Tareas nuevas (cargar) ────────────────────

async def _kapso_tarea_nueva_guiado(ident: dict, numero: str) -> None:
    if not _wa_puede(ident, "tareas", "editar"):
        await kapso.enviar_texto(numero, "Solo el dueño o un administrador pueden crear tareas.")
        return
    datos = {"tarea_nueva": {}}
    await _wa_set_conv(numero, "k_tar_titulo", datos)
    await kapso.enviar_texto(numero, "¿Qué hay que hacer? (ej: _fumigar La Esperanza con glifosato_)")


PRIORIDADES_TAREA = [("alta", "🔴 Alta"), ("media", "🟡 Media"), ("baja", "🟢 Baja")]


async def _kapso_set_prioridad_tarea(ident: dict, numero: str, datos: dict) -> None:
    """Pide elegir prioridad de la tarea (lista con las 3 opciones)."""
    await _wa_set_conv(numero, "k_tar_prioridad", datos)
    filas = [(f"tar_prio:{k}", v) for k, v in PRIORIDADES_TAREA]
    await kapso.enviar_lista(
        numero, "¿Qué prioridad tiene?", "Elegir prioridad", filas, "Prioridad")


async def _wa_equipo_del_user(owner_id: str, limite: int = 9) -> list:
    """Lee los miembros ACTIVOS del equipo del user. Vacío si no hay."""
    return await _sb_get("equipo", {
        "owner_id": f"eq.{owner_id}",
        "activo": "eq.true",
        "select": "miembro_id,nombre_display,rol",
        "order": "nombre_display.asc",
        "limit": str(limite),
    }) or []


async def _kapso_guardar_tarea_nueva(ident: dict, numero: str, datos: dict) -> None:
    t = datos.get("tarea_nueva", {})
    payload = {
        "owner_id": ident["owner_id"],
        "titulo": t.get("titulo") or "Sin título",
        "estado": "pendiente",
        "fecha_vencimiento": t.get("fecha") or None,
        "campo_id": t.get("campo_id") or None,
    }
    if t.get("descripcion"):
        payload["descripcion"] = t["descripcion"][:500]
    if t.get("prioridad"):
        payload["prioridad"] = t["prioridad"]
    if t.get("asignado_a"):
        payload["asignado_a"] = t["asignado_a"]
    creada = await _sb_post("tareas", payload)
    tarea_id = (creada or {}).get("id") if isinstance(creada, dict) else None
    partes = [f"✅ Tarea creada: *{t.get('titulo')}*"]
    if t.get("descripcion"):
        partes.append(f"📝 _{t['descripcion']}_")
    if t.get("prioridad"):
        lbl = next((v for k, v in PRIORIDADES_TAREA if k == t["prioridad"]), t["prioridad"])
        partes.append(f"⚡ Prioridad: {lbl}")
    if t.get("fecha"):
        partes.append(f"📅 Vence {_wa_fecha_corta(t['fecha'])}")
    if t.get("campo_nombre"):
        partes.append(f"🌱 {t['campo_nombre']}")
    if t.get("asignado_nombre"):
        partes.append(f"👤 Asignada a: *{t['asignado_nombre']}*")
    if tarea_id:
        # Dejamos la conversación esperando un archivo opcional. Si el user
        # manda foto/PDF en los próximos minutos, se adjunta a la tarea.
        await _wa_set_conv(numero, "k_tarea_adjunto", {"tarea_id": tarea_id,
                                                        "titulo": t.get("titulo") or ""})
        partes.append("\n📎 Si querés mandarme una *foto o PDF* ahora, lo "
                      "adjunto a la tarea.")
    else:
        await _wa_clear_conv(numero)
    await kapso.enviar_texto(numero, "\n".join(partes))


async def _kapso_tarea_nueva_adjuntar(ident: dict, numero: str, m, datos: dict) -> None:
    """Adjunta un archivo a una tarea recién creada."""
    tarea_id = datos.get("tarea_id")
    titulo = datos.get("titulo") or "la tarea"
    if not tarea_id or not m.tiene_media:
        await _wa_clear_conv(numero)
        return
    contenido = await kapso.descargar_media(m.media_url)
    nombre = m.media_nombre or "archivo"
    path = f"{ident['owner_id']}/{int(datetime.utcnow().timestamp())}_{nombre}"
    url = await _sb_storage_subir("tareas-archivos", path, contenido, m.media_tipo)
    if not url:
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(numero, "No pude subir el archivo. Probá desde la app.")
        return
    actuales = await _sb_get("tareas", {"id": f"eq.{tarea_id}", "select": "archivos"})
    archivos = (actuales[0].get("archivos") if actuales else None) or []
    if not isinstance(archivos, list):
        archivos = []
    archivos.append({"url": url, "path": path, "nombre": nombre, "tipo": m.media_tipo})
    await _sb_patch(f"tareas?id=eq.{tarea_id}", {"archivos": archivos})
    await _wa_clear_conv(numero)
    await kapso.enviar_texto(numero, f"📎 Adjunté el archivo a *{titulo}*.")


# ── Ingresos (ventas de cultivo) ──────────────

CULTIVOS_WA = [("soja", "Soja"), ("maiz", "Maíz"), ("trigo", "Trigo"),
               ("girasol", "Girasol"), ("sorgo", "Sorgo"), ("otro", "Otro")]


def _norm_cultivo(texto: str) -> str:
    """Normaliza un nombre de cultivo para matchear con CULTIVOS_WA:
    lowercase, sin acentos, strip. Usa unicodedata para quitar diacríticos."""
    import unicodedata
    n = unicodedata.normalize("NFKD", str(texto))
    n = "".join(c for c in n if not unicodedata.combining(c))
    return n.lower().strip()


async def _wa_cultivos_del_campo(campo_id: str) -> list:
    """
    Devuelve los cultivos sembrados en el campo con el NOMBRE ORIGINAL
    (preservando mayúsculas/acentos, como lo guarda la app).
    Si un mismo cultivo aparece con distinto casing en diferentes columnas,
    se dedupea por nombre normalizado y queda el primero encontrado. Se busca en:
      1. campos.cultivo (texto, monocultivo)
      2. campos.hectareas_cultivo (JSON dict cultivo → ha)
      3. campos.ingresos_cultivos (JSON dict cultivo → {...})
    """
    import json
    filas = await _sb_get("campos", {
        "id": f"eq.{campo_id}",
        "select": "cultivo,hectareas_cultivo,ingresos_cultivos",
        "limit": "1",
    })
    if not filas:
        return []
    f = filas[0]
    # Dict normalizado → original: evita duplicados por casing o acentos.
    vistos = {}
    def add(nombre):
        n = _norm_cultivo(nombre)
        if n and n not in vistos:
            vistos[n] = str(nombre).strip()
    if f.get("cultivo"):
        add(f["cultivo"])
    for col in ("hectareas_cultivo", "ingresos_cultivos"):
        try:
            raw = f.get(col)
            d = json.loads(raw) if raw else {}
            if isinstance(d, dict):
                for k in d.keys():
                    add(k)
        except Exception:
            pass
    return sorted(vistos.values(), key=lambda x: _norm_cultivo(x))


async def _kapso_pedir_cultivo_del_campo(numero: str, campo_id: str) -> int:
    """
    Pide elegir cultivo. Muestra solo los sembrados en el campo. Si no hay
    ninguno sembrado, fallback a la lista genérica. Si hay uno solo devuelve
    1 sin preguntar (el caller debe leer datos).
    Devuelve: 2 = preguntó por lista, 1 = autoseleccionado, 0 = error.
    """
    sembrados = await _wa_cultivos_del_campo(campo_id)
    if sembrados and len(sembrados) == 1:
        return 1  # el caller sabe autoseleccionar el único
    if sembrados:
        filas = []
        for c in sembrados[:9]:
            # Match normalizado con CULTIVOS_WA para el label; id preserva el nombre original.
            lbl = next((v for k, v in CULTIVOS_WA if k == _norm_cultivo(c)), str(c))
            filas.append((f"cultivo:{c}", lbl))
        filas.append(("cultivo:otro", "Otro"))
        await kapso.enviar_lista(numero, "¿Qué cultivo vendiste?",
                                 "Elegir cultivo", filas, "Sembrados en el campo")
    else:
        # Campo sin cultivos cargados — mostrar lista genérica
        filas = [(f"cultivo:{k}", v) for k, v in CULTIVOS_WA]
        await kapso.enviar_lista(numero, "¿Qué cultivo vendiste? (el campo no tiene cultivos cargados)",
                                 "Elegir cultivo", filas, "Cultivos")
    return 2


async def _kapso_pedir_cultivo(numero: str) -> None:
    """Versión antigua — mantiene compatibilidad. Prefiere pedir_cultivo_del_campo."""
    filas = [(f"cultivo:{k}", v) for k, v in CULTIVOS_WA]
    await kapso.enviar_lista(numero, "¿Qué cultivo vendiste?", "Elegir cultivo", filas, "Cultivos")


async def _kapso_ingreso_guiado(ident: dict, numero: str) -> None:
    if not _wa_puede(ident, "ingresos", "editar"):
        await kapso.enviar_texto(numero, "Tu usuario no tiene permiso para cargar ingresos.")
        return
    datos = {"ingreso": {}}
    if await _kapso_pedir_campo(ident, numero, "k_ing_campo", datos, "¿En qué campo fue la venta?"):
        return
    # 1 solo campo: seteado por _kapso_pedir_campo → avanzar directo a cultivo
    datos["ingreso"]["campo_id"] = datos["campo_id"]
    datos["ingreso"]["campo_nombre"] = datos["campo_nombre"]
    # Mostrar solo los cultivos sembrados en el campo
    resultado = await _kapso_pedir_cultivo_del_campo(numero, datos["campo_id"])
    if resultado == 1:
        # Autoseleccionar el único cultivo sembrado
        sembrados = await _wa_cultivos_del_campo(datos["campo_id"])
        datos["ingreso"]["cultivo"] = sembrados[0]
        await _wa_set_conv(numero, "k_ing_tn", datos)
        lbl = next((v for k, v in CULTIVOS_WA if k == _norm_cultivo(sembrados[0])), str(sembrados[0]))
        await kapso.enviar_texto(numero, f"¿Cuántas toneladas de *{lbl}* vendiste?")
    else:
        await _wa_set_conv(numero, "k_ing_cultivo", datos)


async def _kapso_confirmar_lluvia(numero: str, campo: dict, mm: float, fecha: str = "") -> None:
    """Muestra el resumen de la lluvia y espera el ✅ antes de escribir."""
    datos = {"mm": float(mm), "campo_id": campo.get("id"),
             "campo_nombre": campo.get("nombre") or ""}
    if fecha:
        datos["fecha"] = fecha
    await _wa_set_conv(numero, "k_lluvia_confirmar", datos)
    fecha_txt = _wa_fecha_corta(fecha) if fecha else "hoy"
    await kapso.enviar_botones(
        numero,
        f"🌧️ Entendí esta lluvia:\n"
        f"💧 *{_num(mm)} mm* en *{campo.get('nombre') or ''}*\n"
        f"📅 {fecha_txt}\n\n¿La cargo así?",
        [("llu_ok", "✅ Sí, cargala"), ("llu_cancel", "❌ Cancelar")],
    )


async def _kapso_confirmar_ingreso(numero: str, datos: dict) -> None:
    """Muestra el resumen del ingreso y espera el ✅ antes de escribir."""
    ing = datos.get("ingreso") or {}
    tn = float(ing.get("tn") or 0)
    precio = float(ing.get("precio") or 0)
    total = tn * precio
    cultivo = ing.get("cultivo") or "sin cultivo"
    campo = ing.get("campo_nombre") or ""
    comprador = ing.get("comprador") or ""

    await _wa_set_conv(numero, "k_ing_confirmar", datos)
    partes = [f"💰 Entendí esta venta en *{campo}*:",
              f"🌾 {_num(tn)} tn de *{cultivo}* a USD {_num(precio)}/tn",
              f"📊 Total: *USD {_miles(total)}*"]
    if comprador:
        partes.append(f"🤝 Comprador: *{comprador}*")
    partes.append("\n¿La cargo así?")
    await kapso.enviar_botones(numero, "\n".join(partes), [
        ("ing_ok", "✅ Sí, cargala"),
        ("ing_cancel", "❌ Cancelar"),
    ])


async def _kapso_guardar_ingreso(ident: dict, numero: str, datos: dict) -> None:
    """
    Los ingresos no tienen tabla propia: viven como JSON en campos.ingresos_cultivos.
    Hay que leer, agregar la venta nueva al array del cultivo, y guardar back.
    """
    import json
    ing = datos.get("ingreso", {})
    campo_id = ing.get("campo_id")
    cultivo = ing.get("cultivo") or "otro"
    tn = float(ing.get("tn") or 0)
    precio = float(ing.get("precio") or 0)
    comprador = (ing.get("comprador") or "").strip()[:150]

    filas = await _sb_get("campos", {
        "id": f"eq.{campo_id}", "select": "ingresos_cultivos", "limit": "1",
    })
    if not filas:
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(numero, "No encontré el campo. Probá de nuevo.")
        return

    actual = filas[0].get("ingresos_cultivos")
    try:
        datos_cultivos = json.loads(actual) if actual else {}
        if not isinstance(datos_cultivos, dict):
            datos_cultivos = {}
    except Exception:
        datos_cultivos = {}

    venta = {
        "tn": tn,
        "precio": precio,
        "tipo": "spot",
        "fecha": datetime.now(AR_TZ).date().isoformat(),
        "fuente": "whatsapp",
    }
    if comprador:
        venta["comprador"] = comprador
    datos_cultivos.setdefault(cultivo, {}).setdefault("ventas", []).append(venta)

    await _sb_patch(f"campos?id=eq.{campo_id}",
                    {"ingresos_cultivos": json.dumps(datos_cultivos)})
    await _wa_clear_conv(numero)
    total = tn * precio
    cultivo_label = next((v for k, v in CULTIVOS_WA if k == _norm_cultivo(cultivo)), str(cultivo))
    comp_txt = f"\n🤝 Comprador: *{comprador}*" if comprador else ""
    await kapso.enviar_texto(
        numero,
        f"✅ Venta guardada: *{_num(tn)} tn* de *{cultivo_label}* a *USD {_num(precio)}/tn* en {ing.get('campo_nombre')}\n"
        f"💰 Total: *USD {_miles(total)}*{comp_txt}"
    )


# ── Pagos (cheques / cuotas) ──────────────────

TIPOS_PAGO = [("cheque", "🧾 Cheque"), ("cuota_credito", "💳 Cuota de crédito"),
              ("otro", "📄 Otro")]

FRECUENCIAS_CREDITO = [("mensual", "Mensual", 1), ("bimestral", "Bimestral", 2),
                       ("trimestral", "Trimestral", 3), ("semestral", "Semestral", 6),
                       ("anual", "Anual", 12)]


async def _kapso_pago_guiado(ident: dict, numero: str) -> None:
    if not _wa_puede(ident, "compromisos_pago", "editar"):
        await kapso.enviar_texto(numero, "Solo el dueño o un administrador pueden cargar pagos.")
        return
    datos = {"pago": {}}
    await _wa_set_conv(numero, "k_pag_tipo", datos)
    filas = [(f"pag_tipo:{k}", v) for k, v in TIPOS_PAGO]
    await kapso.enviar_lista(numero, "¿Qué tipo de pago?", "Elegir tipo", filas, "Tipos")


def _sumar_meses(fecha: date, meses: int) -> date:
    """Suma N meses a una fecha, clipping al último día del mes si hace falta."""
    from calendar import monthrange
    y = fecha.year + (fecha.month + meses - 1) // 12
    m = (fecha.month + meses - 1) % 12 + 1
    d = min(fecha.day, monthrange(y, m)[1])
    return date(y, m, d)


async def _kapso_guardar_pago_cheque(ident: dict, numero: str, datos: dict) -> None:
    """Guarda un cheque con todos los campos (beneficiario, importe, moneda,
    fecha emisión, fecha cobro, banco, número)."""
    p = datos.get("pago", {})
    importe = float(p.get("importe") or 0)
    moneda = p.get("moneda") or "ARS"
    fecha_cobro = p.get("fecha") or datetime.now(AR_TZ).date().isoformat()

    payload = {
        "owner_id": ident["owner_id"],
        "tipo": "cheque",
        "beneficiario": (p.get("beneficiario") or "")[:150],
        "importe": importe,
        "moneda": moneda,
        "fecha_cobro": fecha_cobro,
        "estado": "pendiente",
        "fuente": "whatsapp",
    }
    if p.get("fecha_emision"):
        payload["fecha_emision"] = p["fecha_emision"]
    if p.get("banco"):
        payload["banco"] = p["banco"][:100]
    if p.get("numero_cheque"):
        payload["numero_cheque"] = p["numero_cheque"][:30]
    if p.get("alerta_dias_antes"):
        payload["alerta_dias_antes"] = int(p["alerta_dias_antes"])

    await _sb_post("compromisos_pago", payload)
    await _wa_clear_conv(numero)

    lineas = [f"✅ Cheque de *{moneda} {_miles(importe)}* a *{payload['beneficiario']}*",
              f"📅 Cobro: {_wa_fecha_corta(fecha_cobro)}"]
    if p.get("banco"):
        lineas.append(f"🏦 {p['banco']}")
    if p.get("numero_cheque"):
        lineas.append(f"#️⃣ Nº {p['numero_cheque']}")
    if p.get("alerta_dias_antes"):
        lineas.append(f"🔔 Aviso {p['alerta_dias_antes']} días antes")
    await kapso.enviar_texto(numero, "\n".join(lineas))


async def _kapso_guardar_pago_credito(ident: dict, numero: str, datos: dict) -> None:
    """Genera N cuotas de un crédito, todas con el mismo credito_id, fechas
    calculadas según frecuencia."""
    import secrets
    p = datos.get("pago", {})
    importe_cuota = float(p.get("importe_cuota") or 0)
    moneda = p.get("moneda") or "ARS"
    cantidad = int(p.get("cantidad_cuotas") or 1)
    frecuencia = p.get("frecuencia") or "mensual"
    beneficiario = (p.get("beneficiario") or "")[:150]
    descripcion = (p.get("descripcion") or "")[:300]
    try:
        fecha_primera = datetime.strptime(p["fecha_primera"], "%Y-%m-%d").date()
    except Exception:
        fecha_primera = datetime.now(AR_TZ).date()

    inc_meses = next((m for k, _, m in FRECUENCIAS_CREDITO if k == frecuencia), 1)
    credito_id = secrets.token_hex(8)

    alerta_dias = int(p.get("alerta_dias_antes") or 0) or None
    for i in range(cantidad):
        fecha_cuota = _sumar_meses(fecha_primera, inc_meses * i)
        payload = {
            "owner_id": ident["owner_id"],
            "tipo": "cuota_credito",
            "beneficiario": beneficiario,
            "descripcion": descripcion,
            "importe": importe_cuota,
            "moneda": moneda,
            "fecha_cobro": fecha_cuota.isoformat(),
            "credito_id": credito_id,
            "numero_cuota": i + 1,
            "total_cuotas": cantidad,
            "estado": "pendiente",
            "fuente": "whatsapp",
        }
        if alerta_dias:
            payload["alerta_dias_antes"] = alerta_dias
        await _sb_post("compromisos_pago", payload)

    await _wa_clear_conv(numero)
    total = importe_cuota * cantidad
    freq_label = next((lbl for k, lbl, _ in FRECUENCIAS_CREDITO if k == frecuencia), frecuencia)
    alerta_txt = f"\n🔔 Aviso {alerta_dias} días antes de cada cuota" if alerta_dias else ""
    await kapso.enviar_texto(
        numero,
        f"✅ Crédito cargado: *{cantidad} cuotas* de *{moneda} {_miles(importe_cuota)}* ({freq_label})\n"
        f"🏦 {beneficiario}\n"
        f"📅 1ra: {_wa_fecha_corta(p['fecha_primera'])}\n"
        f"💰 Total: *{moneda} {_miles(total)}*{alerta_txt}"
    )


async def _kapso_guardar_pago_otro(ident: dict, numero: str, datos: dict) -> None:
    """Pago 'otro' — campo generico (importe + beneficiario + fecha)."""
    p = datos.get("pago", {})
    importe = float(p.get("importe") or 0)
    moneda = p.get("moneda") or "ARS"
    fecha = p.get("fecha") or datetime.now(AR_TZ).date().isoformat()
    payload = {
        "owner_id": ident["owner_id"],
        "tipo": "otro",
        "beneficiario": (p.get("beneficiario") or "")[:150],
        "importe": importe,
        "moneda": moneda,
        "fecha_cobro": fecha,
        "estado": "pendiente",
        "fuente": "whatsapp",
    }
    if p.get("descripcion"):
        payload["descripcion"] = p["descripcion"][:300]
    await _sb_post("compromisos_pago", payload)
    await _wa_clear_conv(numero)
    await kapso.enviar_texto(
        numero,
        f"✅ Pago de *{moneda} {_miles(importe)}* a *{payload['beneficiario']}*\n"
        f"📅 Vence {_wa_fecha_corta(fecha)}"
    )


# Compat: función antigua redirige al guardador correcto
async def _kapso_guardar_pago(ident: dict, numero: str, datos: dict) -> None:
    tipo = (datos.get("pago", {}).get("tipo") or "cheque")
    if tipo == "cheque":
        await _kapso_guardar_pago_cheque(ident, numero, datos)
    elif tipo == "cuota_credito":
        await _kapso_guardar_pago_credito(ident, numero, datos)
    else:
        await _kapso_guardar_pago_otro(ident, numero, datos)


# ── Texto libre ───────────────────────────────

# ── Productos parecidos y alias aprendidos ─────────────────
# Los audios se transcriben mal a veces ("orea" por urea). Si una palabra se
# parece a un producto conocido pero no es igual, repreguntamos en vez de
# adivinar; y si la persona confirma, lo guardamos como alias en mensajes_wa
# (tipo='alias') para reconocerlo solo la próxima vez.

import difflib

def _productos_simples() -> list:
    # Función y no constante: _RUBRO_POR_PRODUCTO se define más abajo.
    return sorted({p for ps in _RUBRO_POR_PRODUCTO.values() for p in ps
                   if " " not in p and len(p) >= 4})
_PALABRAS_COMUNES = {
    "gaste", "gasto", "pague", "pago", "pagamos", "compre", "compramos", "dolar", "dolares",
    "pesos", "lucas", "verdes", "campo", "lote", "para", "porque", "esta", "este", "todo",
    "cargar", "quiero", "anota", "anotar", "ayer", "hoy", "semana", "mañana", "manana",
    "litros", "kilos", "bolsas", "bidon", "bidones", "contratista", "fletero", "prueba",
    "hola", "buenas", "gracias", "quedo", "bien", "termine", "llovio", "llovieron",
}
_ALIAS = {"datos": {}, "ts": 0.0}


async def _wa_alias() -> dict:
    """{alias normalizado → producto}, confirmados por usuarios. Cache 10 min."""
    if time.monotonic() - _ALIAS["ts"] < 600:
        return _ALIAS["datos"]
    try:
        filas = await _sb_get("mensajes_wa", {"tipo": "eq.alias", "confirmado": "eq.true",
                                              "select": "contenido,interpretacion", "limit": "1000"})
        _ALIAS["datos"] = {_norm(f["contenido"]): (f.get("interpretacion") or {}).get("producto")
                           for f in filas or [] if f.get("contenido")}
    except Exception as e:
        print(f"[ALIAS] error: {e}")
    _ALIAS["ts"] = time.monotonic()
    return _ALIAS["datos"]


def _reemplazar_palabra(texto: str, vieja: str, nueva: str) -> str:
    return re.sub(r"(?i)(?<![\wáéíóúñ])" + re.escape(vieja) + r"(?![\wáéíóúñ])", nueva, texto)


def _producto_parecido(texto: str, campos: list) -> tuple | None:
    """("orea", "urea") si una palabra se parece a un producto sin serlo."""
    nombres_campo = {w for c in campos for w in _norm(c.get("nombre") or "").split()}
    catalogo = _productos_simples()
    for token in re.findall(r"[a-záéíóúñ0-9\-]+", _norm(texto)):
        if (len(token) < 4 or token in _PALABRAS_COMUNES or token in nombres_campo
                or token in catalogo or token.isdigit()):
            continue
        cerca = difflib.get_close_matches(token, catalogo, n=1, cutoff=0.75)
        if cerca:
            return token, cerca[0]
    return None


async def _wa_guardar_alias(ident: dict, alias: str, producto: str) -> None:
    try:
        await _sb_post("mensajes_wa", {
            "usuario_id": ident.get("owner_id"), "tipo": "alias", "contenido": alias,
            "interpretacion": {"producto": producto}, "confirmado": True,
        })
        _ALIAS["datos"][_norm(alias)] = producto
    except Exception as e:
        print(f"[ALIAS] no se pudo guardar: {e}")


async def _wa_log_mensaje(ident: dict | None, numero: str, m) -> None:
    """Deja registro de cada mensaje (y la transcripción de los audios)."""
    try:
        await _sb_post("mensajes_wa", {
            "usuario_id": (ident or {}).get("owner_id"), "telefono": numero,
            "tipo": m.tipo or "text", "contenido": (m.texto or "")[:2000],
        })
    except Exception as e:
        print(f"[LOG WA] {e}")


def _intento_cargar(texto: str) -> str:
    """
    Reconoce frases tipo "quiero cargar un gasto", "cargá una lluvia",
    "nuevo insumo", etc. Devuelve el tipo detectado o "" si no hay intención.
    Para los casos con dato concreto (monto, mm), devuelve "" para que el
    parseador específico siga haciendo su trabajo.
    """
    n = _norm(texto)
    if not re.search(
            r"\b(quiero|voy a|voy|necesito|puedo|debo|querr[íi]a|quisiera"
            r"|cargar|cargá|carga|registrar|registra|crear|crea|anotar|anota"
            r"|sumar|suma|hacer|haz|dar de alta|nuevo|nueva|nuevos|nuevas"
            r"|agregar|agrega|poner|pone|meter|mete|añadir)\b", n):
        return ""
    if re.search(r"\b(invitar|sumar a|agregar a)\b", n):
        return ""  # dejar que el bloque invitar lo tome
    if re.search(r"\b(gasto|gastos)\b", n):
        return "gasto"
    if re.search(r"\b(lluvia|lluvias|precipitaci[oó]n)\b", n):
        return "lluvia"
    if re.search(r"\b(insumo|insumos|stock|dep[oó]sito)\b", n):
        return "insumo"
    if re.search(r"\b(tarea|tareas|pendiente|pendientes)\b", n):
        return "tarea"
    if re.search(r"\b(ingreso|ingresos|venta|ventas|vend[ií])\b", n):
        return "ingreso"
    if re.search(r"\b(pago|pagos|cheque|cheques|cr[eé]dito|cuota|cuotas)\b", n):
        return "pago"
    return ""


async def _wa_actualizar_ultimo_cheque(ident: dict, numero: str, texto: str) -> bool:
    """
    Si el texto trae info adicional para un cheque recién cargado (número y/o
    banco, sin otro contexto de carga), buscamos el último cheque del user de
    los últimos 10 minutos y lo actualizamos. Devuelve True si manejó el
    mensaje, False si no era un caso de actualización.
    """
    n = _norm(texto)
    # Debe mencionar cheque + número y/o banco, sin plata/fecha nueva.
    tiene_numero = bool(re.search(r"(?:n(ú|u)mero|nro|n°|#)\s*(?:de\s*)?(?:cheque)?\s*[:\.]?\s*(\d{3,})", n))
    tiene_banco = bool(re.search(r"\bbanco\b", n))
    es_sobre_cheque = ("cheque" in n or "echeq" in n or tiene_numero or tiene_banco)
    tiene_plata_nueva = bool(re.search(r"\$|pesos|d(ó|o)lares|usd|ars", n))
    if not es_sobre_cheque or tiene_plata_nueva:
        return False
    if not (tiene_numero or tiene_banco):
        return False

    # Buscamos el último cheque pendiente del user, cargado hace menos de 10 min.
    filas = await _sb_get("compromisos_pago", {
        "owner_id": f"eq.{ident['owner_id']}",
        "tipo": "eq.cheque",
        "select": "id,beneficiario,banco,numero_cheque,created_at,importe,moneda,fecha_cobro",
        "order": "created_at.desc",
        "limit": "1",
    }) or []
    if not filas:
        return False
    cheque = filas[0]
    try:
        hace_min = (datetime.now(AR_TZ) - datetime.fromisoformat(
            cheque["created_at"].replace("Z", "+00:00"))).total_seconds() / 60
    except Exception:
        hace_min = 999
    if hace_min > 10:
        return False  # muy viejo, no asumimos que es ESE cheque

    patch = {}
    # Número: extraer dígitos después de "número/n°/#".
    m_num = re.search(r"(?:n(ú|u)mero|nro|n°|#)\s*(?:de\s*)?(?:cheque)?\s*[:\.]?\s*(\d{3,})", n)
    if m_num and not cheque.get("numero_cheque"):
        patch["numero_cheque"] = m_num.group(2)[:30]
    # Banco: desde "banco X" hasta el final o hasta otro keyword.
    m_bco = re.search(r"\bbanco\s+([a-záéíóúñ0-9\.\- ]{3,80}?)(?:\s*(?:n(ú|u)mero|nro|cheque)|$)", n)
    if m_bco and not cheque.get("banco"):
        patch["banco"] = m_bco.group(1).strip().title()[:100]
    if not patch:
        return False

    await _sb_patch(f"compromisos_pago?id=eq.{cheque['id']}", patch)
    lineas = ["📝 Actualicé el último cheque:"]
    if patch.get("banco"):
        lineas.append(f"🏦 Banco: {patch['banco']}")
    if patch.get("numero_cheque"):
        lineas.append(f"#️⃣ Nº: {patch['numero_cheque']}")
    await kapso.enviar_texto(numero, "\n".join(lineas))
    return True


async def _kapso_texto_libre(ident: dict, numero: str, texto: str,
                             tarea_activa: dict = None, saltar_parecidos: bool = False) -> None:
    """
    Intenta resolver el mensaje sin menú: primero lluvia (que es un patrón
    clarísimo), después la IA si está configurada, y si no, el menú.
    """
    n = _norm(texto)

    # Caso frecuente: después de cargar un cheque, el user manda número y/o
    # banco por separado. Lo asociamos al último cheque suyo sin re-cargar.
    if await _wa_actualizar_ultimo_cheque(ident, numero, texto):
        return

    # Intento explícito de carga ("quiero cargar un gasto", "cargá una tarea",
    # "nuevo cheque"): si no viene con dato concreto, arranca el flujo guiado.
    intento = _intento_cargar(texto)
    tiene_dato = bool(_parsear_plata(texto) or re.search(r"\b\d+\s*mm\b", n)
                      or re.search(r"\b\d+\s*(tn|toneladas?|kg|litros?|bolsas?)\b", n))
    if intento and not tiene_dato:
        if intento == "gasto" and _wa_puede(ident, "gastos", "editar"):
            await _kapso_gasto_guiado(ident, numero)
            return
        if intento == "lluvia" and _wa_puede(ident, "lluvias", "editar"):
            await _wa_set_conv(numero, "k_lluvia_mm", {"guiado": True})
            await kapso.enviar_texto(numero, "¿Cuántos mm?")
            return
        if intento == "insumo" and _wa_puede(ident, "insumos", "editar"):
            await _kapso_insumo_guiado(ident, numero)
            return
        if intento == "tarea" and _wa_puede(ident, "tareas", "editar"):
            await _kapso_tarea_nueva_guiado(ident, numero)
            return
        if intento == "ingreso" and _wa_puede(ident, "ingresos", "editar"):
            await _kapso_ingreso_guiado(ident, numero)
            return
        if intento == "pago" and _wa_puede(ident, "compromisos_pago", "editar"):
            await _kapso_pago_guiado(ident, numero)
            return

    # Invitar: usamos word-boundary para NO matchear "agregar al stock" o
    # "agregar al campo" (caso real 2026-11-24: "agregar al stock de insumos"
    # caía como invitación). Sólo matcheamos las frases exactas.
    if re.search(r"\b(invitar|sumar\s+a|agregar\s+a(?!l\b)|dar\s+de\s+alta)\b", n):
        # Excluimos aún así los casos típicos donde "agregar a" es parte de
        # "agregar al stock/los insumos/la planilla" etc.
        if not re.search(r"\bagregar\s+al?\s+(stock|lista|listado|planilla|campo|archivo|insumo|lluvia|gasto|tarea|ingreso|pago)\b", n):
            posible = re.sub(r".*(invitar|sumar a|agregar a|dar de alta)\s*", "", n).strip()
            posible = re.sub(r"^(a|al)\s+", "", posible)
            await _kapso_invitar_inicio(ident, numero, posible.title())
            return

    # Alias aprendidos: "orea" → "urea" si alguien ya lo confirmó antes.
    for alias, producto in (await _wa_alias()).items():
        if producto and re.search(r"(?<![\wáéíóúñ])" + re.escape(alias) + r"(?![\wáéíóúñ])", n):
            texto = _reemplazar_palabra(texto, alias, producto)
            n = _norm(texto)

    # Un gasto con un producto que se parece a uno conocido pero no es igual:
    # preguntamos antes de cargarlo mal.
    if (not saltar_parecidos and _wa_puede(ident, "gastos", "editar")
            and (_RE_GASTO.search(n) or _parsear_plata(texto))
            and not _rubro_por_texto(texto)[1]):
        parecido = _producto_parecido(texto, ident.get("campos") or [])
        if parecido:
            alias, producto = parecido
            await _wa_set_conv(numero, "k_confirmar_producto",
                               {"texto": texto, "alias": alias, "producto": producto})
            await kapso.enviar_botones(
                numero, f"Entendí *{alias}*. ¿Quisiste decir *{producto}*?",
                [("prod_si", f"Sí, {producto}"[:20]), ("prod_no", "No, otra cosa")])
            return

    # "cargá esta factura", "te paso un remito": todavía no mandó el archivo
    if re.search(r"\b(factura|facturas|remito|remitos|ticket|comprobante)\b", n) \
            and not _parsear_plata(texto):
        await kapso.enviar_texto(
            numero,
            "Dale, mandame la *foto* o el *PDF* de la factura y la cargo 📎",
        )
        return

    # Lluvia: "llovieron 25 en la loma", "25mm en el ombú"
    if _wa_puede(ident, "lluvias", "editar") and ("llov" in n or "mm" in n):
        mm = _extraer_numero(texto)
        campo = _buscar_campo(texto, ident.get("campos") or [])
        if mm is not None:
            if campo:
                await _kapso_guardar_lluvia(ident, numero, campo, mm)
                return
            datos = {"mm": mm}
            if not await _kapso_pedir_campo(ident, numero, "k_lluvia_campo", datos, f"¿Dónde llovieron {_num(mm)} mm?"):
                campo_unico = {"id": datos["campo_id"], "nombre": datos["campo_nombre"]}
                await _kapso_guardar_lluvia(ident, numero, campo_unico, mm)
            return

    # Ver tareas: sólo si el texto parece una CONSULTA. Si tiene un verbo de
    # acción (borrar / modificar / crear / completar / asignar / iniciar), cae
    # a Claude para que decida el accion real (caso real 2026-11-26: "borra la
    # última tarea" terminaba en ver_tareas porque el regex matchea "tarea" sola).
    if _wa_puede(ident, "tareas") and _RE_VER_TAREAS.search(n) and len(n) < 60:
        verbo_accion = re.search(
            r"\b(borr|elimin|cancel|anul|quit|deshac|modif|cambi|corrig|edit|"
            r"asign|complet|termin|hice|cerr|cre[oa]r|crea|nuev|inici|arranc|"
            r"empec|empez)",
            n)
        if not verbo_accion:
            await _kapso_tareas(ident, numero)
            return

    # Gasto dicho en criollo, SIN IA, SOLO si Claude no está disponible o el
    # texto es muy corto y simple (p. ej. "gasté 100 de urea"). Si Claude está
    # configurado y el texto tiene pinta de ser multi-producto o una venta,
    # queremos que lo decida Claude.
    #
    # Caso real 2026-11-24: "Cargar gasto en la escondida. 3 litros de
    # glifosato por hectárea, precio del glifosato 2 dólares por litro, cargar
    # 0,5 litros de atrazina por hectárea precio del litro de atrazina 7,5 usd
    # y 20 litros de aceite precio del aceite 12 usd" se parseaba como un
    # gasto único de Herbicidas USD 2 porque _parsear_gasto encontró "gasto"+
    # "glifosato"+"2 dólares" y guardaba SIN pasar por Claude.
    # Con Claude disponible, el parser legacy sólo se usa para los casos ultra
    # simples y ambiguos donde Claude es exceso: "gasté 100 de urea". Para
    # cualquier texto con pinta de ser stock, venta o multi-producto, lo manda
    # directo a Claude.
    #
    # Caso real 2026-11-24: "Agregar 700 litros de glifosato 48% en cerealista
    # moldes" era interpretado como un gasto de USD 700 porque el parser legacy
    # vio "agregar" + "700" + "glifosato" y guardó. Pero ese texto es un stock
    # de insumo. Claude lo clasifica bien si no lo intercepta el parser.
    saltear_parser = False
    if os.environ.get("ANTHROPIC_API_KEY"):
        n_lower = _norm(texto)
        es_simple_criollo = bool(re.match(
            r"^(gast(é|amos|aron|o)|pag(ué|amos|aron|o)|compr(é|amos|aron|o))\b",
            n_lower,
        ))
        # Si el texto empieza con "agregar", "cargar", "sumar", "añadir" sin
        # ser un verbo de gasto, va a Claude (puede ser stock/insumo/ingreso).
        verbo_no_gasto = bool(re.match(r"^(agregar|cargar|sumar|añadir|anadir|anotar)\b", n_lower))
        # "eliminar/borrar/cancelar/anular" también va a Claude (accion=eliminar).
        verbo_eliminar = bool(re.search(r"\b(eliminar|borrar|cancelar|anular|quitar|deshacer)\b", n_lower))
        # Preguntas/consultas → Claude (accion=consultar). Marcadas por "?",
        # "cuánto", "qué" seguido de pregunta, "stock de", "cheques pendientes".
        verbo_consulta = (
            "?" in texto
            or bool(re.search(r"\b(cu(á|a)nto|cu(á|a)ntos|cu(á|a)ntas)\b", n_lower))
            or bool(re.search(r"\bstock\s+(de|tengo)\b", n_lower))
            or bool(re.search(r"\bcheques?\s+(pendientes?|por\s+venir|por\s+cobrar)\b", n_lower))
            or bool(re.search(r"\b(resumen|resumí|resume|mostrame|most(rá|ra)|dame)\b", n_lower))
        )
        # Correcciones del tipo "el X no era Y era Z" → modificar_ultimo.
        verbo_modificar = bool(re.search(r"\b(cambi(á|a)|corregí|corrige|modific(á|a)|actualiz(á|a))\b", n_lower)) \
                          or bool(re.search(r"\bno\s+era\b.*\bera\b", n_lower))
        # Texto largo, multi-insumo, multi-precio o "venta/vendí" → Claude.
        complejo = (
            len(texto) > 80
            or _contar_insumos_mencionados(texto) >= 2
            or len(re.findall(r"por\s+(litro|kilo|kg|bolsa|tonelada|hect)", n_lower)) >= 2
            or bool(re.search(r"\b(venta|vendí|vendimos|vender)\b", n_lower))
            or bool(re.search(r"\b(stock|insumo|insumos|bodega|galp(ó|o)n|dep(ó|o)sito)\b", n_lower))
        )
        if (verbo_no_gasto or verbo_eliminar or verbo_consulta or verbo_modificar
                or complejo or not es_simple_criollo):
            saltear_parser = True

    parseado = _parsear_gasto(texto, ident.get("campos") or []) if (
        _wa_puede(ident, "gastos", "editar") and not saltear_parser) else None
    # Si no reconocimos el producto ("orea" en vez de urea, por la
    # transcripción del audio) y hay IA, que decida Claude el rubro en vez
    # de mandarlo derecho a "otros".
    # Lo mismo con los pesos: Claude entiende "300, eh, 300 mil" o "300 lucas".
    if parseado and os.environ.get("ANTHROPIC_API_KEY") and (
            parseado.get("ars") or not _rubro_por_texto(texto)[1]):
        parseado = None
    if parseado and parseado.get("ars"):
        ars = parseado["gasto"]["total_de_usd"]
        tc = await _dolar_venta()
        parseado["gasto"]["total_de_usd"] = round(ars / tc, 2)
        parseado["gasto"]["descripcion"] = (
            f"{parseado['gasto']['descripcion']} (ARS {_miles(ars)} al BNA ${_miles(tc)})")
    if parseado:
        datos = {"gasto": parseado["gasto"]}
        campo = parseado.get("campo")
        if campo:
            datos["campo_id"] = campo["id"]
            datos["campo_nombre"] = campo["nombre"]
            await _kapso_guardar_gasto(ident, numero, datos)
        elif not await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos,
                                          "¿A qué campo lo cargo?"):
            await _kapso_guardar_gasto(ident, numero, datos)
        return

    # Claude entiende el resto: "la terminé, anotá que sobró medio bidón"
    intencion_claude = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        tareas = await _wa_tareas_pendientes(ident)
        intencion_claude = await _interpretar_con_claude(texto, ident, tareas, tarea_activa)
        if intencion_claude and await _kapso_aplicar_intencion(ident, numero, intencion_claude, texto):
            return

    # Si Claude entendió el tipo de carga pero no pudo cerrarlo (p. ej. faltó un
    # campo o un total), NO caemos al motor legacy de OpenAI: esa IA vieja no
    # sabe de precio_unit y pisa el audio con un total mal (caso 2026-11:
    # "10 L de glifosato a $5/L" → legacy guardaba USD 3). Mejor menú guiado.
    if intencion_claude and intencion_claude.get("accion") in (
            "gasto", "lluvia", "ingreso", "pago", "insumo", "tarea_nueva", "siembra",
            "flete", "rendimiento_real", "gasto_estructura", "activo",
            "analisis_suelo", "cerrar_campana"):
        print(f"[WA FALLBACK] Claude entendió accion={intencion_claude.get('accion')} "
              f"confianza={intencion_claude.get('confianza')} pero no se pudo aplicar — voy a menú")
        await _kapso_menu(ident, numero, "Entendí lo que me mandaste pero me falta info 🤔\n"
                                           "Elegí abajo qué querés cargar y lo hacemos paso a paso:")
        return

    # IA para gastos dichos en criollo ("gasté 1200 de urea en La Esperanza")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if api_key and len(texto) > 8 and _wa_puede(ident, "gastos", "editar"):
        usuario_ia = {"id": ident["owner_id"], "campos": ident.get("campos") or []}
        resultado = await interpretar_con_ia(texto, usuario_ia, api_key)
        if resultado and resultado.get("confianza") in ("alta", "media"):
            manejado = await _kapso_desde_ia(ident, numero, resultado)
            if manejado:
                return

    # Entendimos un monto pero no el resto (audio mal transcripto, etc.):
    # arrancamos la carga guiada con el monto ya puesto en vez de "no entendí".
    monto = _parsear_plata(texto)
    if monto and _wa_puede(ident, "gastos", "editar") and (
            _RE_PLATA_EXPLICITA.search(n) or _RE_GASTO.search(n)):
        datos = {"gasto": {"total_de_usd": monto}, "confirmar": True}
        if _RE_PESOS.search(n):
            tc = await _dolar_venta()
            datos["gasto"]["total_de_usd"] = round(monto / tc, 2)
            datos["gasto"]["descripcion_extra"] = f"(ARS {_miles(monto)} al BNA ${_miles(tc)})"
            monto_txt = f"ARS {_miles(monto)}"
        else:
            monto_txt = f"USD {_num(monto)}"
        if await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos,
                                    f"Entendí un gasto de *{monto_txt}*, pero no qué fue 🤔\n\n"
                                    "¿En qué campo?"):
            return
        await _wa_set_conv(numero, "k_gasto_rubro", datos)
        await _kapso_pedir_rubro(numero)
        return

    await _kapso_menu(ident, numero, "No te entendí del todo 🤔")


async def _kapso_desde_ia(ident: dict, numero: str, resultado: dict) -> bool:
    """Toma la salida de interpretar_con_ia y la convierte en un alta. True si la manejó."""
    tipo = (resultado.get("tipo") or "").lower()
    d = resultado.get("datos") or {}
    campo = None
    if resultado.get("campo_nombre"):
        campo = _buscar_campo(resultado["campo_nombre"], ident.get("campos") or [])

    if tipo == "lluvia" and d.get("mm"):
        if not campo:
            datos = {"mm": float(d["mm"])}
            if not await _kapso_pedir_campo(ident, numero, "k_lluvia_campo", datos,
                                            f"¿Dónde llovieron {_num(d['mm'])} mm?"):
                campo = {"id": datos["campo_id"], "nombre": datos["campo_nombre"]}
            else:
                return True
        await _kapso_guardar_lluvia(ident, numero, campo, float(d["mm"]))
        return True

    if tipo == "gasto" and d.get("total_usd"):
        datos = {"gasto": {
            "rubro": (d.get("rubro") or "Otros").capitalize(),
            "descripcion": d.get("descripcion") or "",
            "total_de_usd": float(d["total_usd"]),
        }}
        if campo:
            datos["campo_id"] = campo["id"]
            datos["campo_nombre"] = campo["nombre"]
        else:
            datos["confirmar"] = True
            if await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos, "¿A qué campo lo cargo?"):
                return True

        # Lo interpretó la IA: mostramos lo que entendimos antes de guardar.
        await _kapso_confirmar_gasto(numero, datos)
        return True

    return False


# ── Conversaciones en curso ───────────────────

async def _kapso_flujo(ident: dict, numero: str, m, conv: dict) -> None:
    estado = conv.get("estado") or ""
    datos = conv.get("datos_parciales") or {}
    accion = m.accion or ""
    texto = m.texto or ""

    if estado == "k_tarea_adjunto":
        # Espera un archivo opcional para adjuntar a la tarea recién creada.
        # Si viene archivo → adjuntar. Si viene texto largo (nueva intención) o
        # botón del menú, descartar y seguir normal. Texto corto tipo "no" o
        # "listo" también cierra sin adjuntar.
        if m.tiene_media:
            await _kapso_tarea_nueva_adjuntar(ident, numero, m, datos)
            return
        n = _norm(texto or "")
        if n in ("no", "listo", "no gracias", "dejalo", "nada", "sin archivo"):
            await _wa_clear_conv(numero)
            return
        # Cualquier otra cosa: descartamos el estado y reintentamos normal.
        await _wa_clear_conv(numero)
        await _kapso_texto_libre(ident, numero, texto)
        return

    if estado == "k_tarea_activa":
        tid = datos.get("tarea_id")
        if not tid:
            await _wa_clear_conv(numero)
            await _kapso_menu(ident, numero)
            return
        if m.tiene_media:
            await _kapso_tarea_evidencia(ident, numero, m, datos)
            return
        n = _norm(texto)
        if _RE_TERMINE.search(n):
            await _kapso_tarea_completar(ident, numero, tid, _extraer_nota(texto))
            return
        if _RE_ANOTAR.search(n):
            await _kapso_tarea_nota(ident, numero, tid, _extraer_nota(texto))
            return
        if _RE_EMPECE.search(n):
            await _kapso_tarea_iniciar(ident, numero, tid)
            return
        await _kapso_texto_libre(ident, numero, texto,
                                 {"id": tid, "titulo": datos.get("titulo") or ""})
        return

    if estado == "k_confirmar_producto":
        original = datos.get("texto") or ""
        await _wa_clear_conv(numero)
        if accion == "prod_si" or _norm(texto) in ("si", "sí", "dale", "ok", "correcto"):
            await _wa_guardar_alias(ident, datos.get("alias") or "", datos.get("producto") or "")
            corregido = _reemplazar_palabra(original, datos.get("alias") or "", datos.get("producto") or "")
            await _kapso_texto_libre(ident, numero, corregido, saltar_parecidos=True)
        elif accion == "prod_no":
            await _kapso_texto_libre(ident, numero, original, saltar_parecidos=True)
        else:
            # Escribió el producto correcto ("no, era atrazina"): lo usamos.
            nuevo = re.sub(r"^(no,?\s*)?(era\s+)?", "", _norm(texto)).strip()
            if nuevo and len(nuevo) < 40:
                await _wa_guardar_alias(ident, datos.get("alias") or "", nuevo)
                original = _reemplazar_palabra(original, datos.get("alias") or "", nuevo)
            await _kapso_texto_libre(ident, numero, original, saltar_parecidos=True)
        return

    if estado == "k_tarea_evidencia":
        # Esperábamos una foto o un comentario de la tarea recién completada,
        # pero la persona puede estar pidiendo otra cosa ("ya terminé de
        # fumigar", "llovieron 20 mm"). Eso no se guarda como comentario.
        if not m.tiene_media and texto and await _wa_es_otro_pedido(ident, texto, datos.get("tarea_id")):
            await _wa_clear_conv(numero)
            await _kapso_texto_libre(ident, numero, texto)
            return
        await _kapso_tarea_evidencia(ident, numero, m, datos)
        return

    if estado == "k_lluvia_mm":
        mm = _extraer_numero(texto)
        if mm is None:
            await kapso.enviar_texto(numero, "No entendí. Escribí solo el número, por ejemplo *25*.")
            return
        datos["mm"] = mm
        if await _kapso_pedir_campo(ident, numero, "k_lluvia_campo", datos, f"¿Dónde llovieron {_num(mm)} mm?"):
            return
        # 1 solo campo + guiado → ir a fecha. Si no guiado → guardar directo.
        if datos.get("guiado"):
            await _wa_set_conv(numero, "k_lluvia_fecha", datos)
            await kapso.enviar_texto(
                numero,
                "¿Cuándo llovió? (ej: _hoy_, _ayer_, _15/12_)")
            return
        await _kapso_guardar_lluvia(
            ident, numero, {"id": datos["campo_id"], "nombre": datos["campo_nombre"]}, mm)
        return

    if estado == "k_lluvia_fecha":
        n = _norm(texto or "")
        fecha = None
        if n and n not in ("hoy", "ahora", "-"):
            fecha = _parsear_fecha_natural(texto)
            if not fecha:
                await kapso.enviar_texto(
                    numero, "No entendí la fecha. Escribí _hoy_, _ayer_, _15/12_, o _hace 3 días_.")
                return
        datos["fecha"] = fecha or datetime.now(AR_TZ).date().isoformat()
        await _wa_set_conv(numero, "k_lluvia_tipo", datos)
        filas = [(f"ll_tipo:{k}", v) for k, v in TIPOS_LLUVIA]
        await kapso.enviar_lista(
            numero, "¿Qué tipo de lluvia fue?", "Elegir tipo", filas, "Tipo")
        return

    if estado == "k_lluvia_tipo":
        tipo = None
        if accion.startswith("ll_tipo:"):
            tipo = accion.split(":", 1)[1]
        elif texto:
            n = _norm(texto)
            for k, _v in TIPOS_LLUVIA:
                if k in n:
                    tipo = k
                    break
            if "sin" in n or n in ("no", "-", "skip"):
                tipo = None
        if tipo:
            datos["tipo"] = tipo
        await _wa_set_conv(numero, "k_lluvia_etapa", datos)
        filas = [(f"ll_etapa:{k}", v) for k, v in ETAPAS_LLUVIA]
        filas.append(("ll_etapa:skip", "Sin especificar"))
        await kapso.enviar_lista(
            numero, "¿Etapa del cultivo? (opcional)", "Elegir etapa", filas[:9], "Etapa")
        return

    if estado == "k_lluvia_etapa":
        etapa = None
        if accion.startswith("ll_etapa:"):
            v = accion.split(":", 1)[1]
            if v != "skip":
                etapa = v
        elif texto:
            n = _norm(texto)
            for k, _v in ETAPAS_LLUVIA:
                if k in n:
                    etapa = k
                    break
        if etapa:
            datos["etapa"] = etapa
        await _wa_set_conv(numero, "k_lluvia_obs", datos)
        await kapso.enviar_texto(
            numero,
            "¿Alguna observación? (opcional — escribí _sin obs_ para guardar)")
        return

    if estado == "k_lluvia_obs":
        n = _norm(texto or "")
        obs = None
        if n and n not in ("sin obs", "sin observacion", "sin observación", "no", "-"):
            obs = texto.strip()[:200]
        campo = {"id": datos["campo_id"], "nombre": datos["campo_nombre"]}
        await _kapso_guardar_lluvia(
            ident, numero, campo, float(datos.get("mm") or 0),
            fecha=datos.get("fecha"),
            tipo=datos.get("tipo"),
            etapa=datos.get("etapa"),
            observacion=obs,
        )
        return

    if estado == "k_gasto_multi_edit_texto":
        # El user mandó un texto/audio con la corrección de un item del multi.
        # Lo mandamos a Claude para re-interpretar y pisamos el item en la lista,
        # después volvemos al resumen.
        idx = datos.get("_editing_idx")
        multi = datos.get("multi_gasto") or {}
        items_tmp = multi.get("items") or []
        if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(items_tmp):
            await _wa_clear_conv(numero)
            await kapso.enviar_texto(numero, "No supe qué item estás editando. Volvé a arrancar.")
            return
        # Pre-contexto: le decimos a Claude que SOLO devuelva el item nuevo.
        intencion_edit = await _interpretar_con_claude(texto, ident, [], None)
        if not intencion_edit or intencion_edit.get("accion") != "gasto":
            await kapso.enviar_texto(numero, "No entendí la corrección. Probá de nuevo con "
                                              "nombre + cantidad + unidad + precio.")
            return
        # Extraer un item "nivel arriba" o el primero del items[] de la corrección.
        if intencion_edit.get("items"):
            it_nuevo = intencion_edit["items"][0]
        else:
            it_nuevo = {
                "rubro": intencion_edit.get("rubro"),
                "descripcion": intencion_edit.get("descripcion"),
                "cantidad": intencion_edit.get("cantidad"),
                "unidad": intencion_edit.get("unidad"),
                "precio_unit_usd": intencion_edit.get("precio_unit_usd"),
                "precio_unit_ars": intencion_edit.get("precio_unit_ars"),
                "monto_usd": intencion_edit.get("monto_usd"),
                "monto_ars": intencion_edit.get("monto_ars"),
                "por_hectarea": intencion_edit.get("por_hectarea"),
            }
        campo_actual = {"id": multi.get("campo_id"), "nombre": multi.get("campo_nombre"),
                        "hectareas": multi.get("hectareas")}
        tc = await _dolar_venta()
        nuevo_g = await _wa_armar_gasto(ident, campo_actual, it_nuevo, tc)
        if not nuevo_g:
            await kapso.enviar_texto(numero, "La corrección no tiene datos suficientes "
                                              "(me falta cantidad o precio). Probá de nuevo.")
            return
        items_tmp[idx] = nuevo_g
        multi["items"] = items_tmp
        datos["multi_gasto"] = multi
        datos.pop("_editing_idx", None)
        await _kapso_confirmar_multi(ident, numero,
                                     {"id": multi.get("campo_id"),
                                      "nombre": multi.get("campo_nombre"),
                                      "hectareas": multi.get("hectareas")},
                                     items_tmp)
        return

    if estado == "k_gasto_multi_campo":
        # El user volvió con el campo elegido; retomamos el multi-producto.
        campo = None
        if accion.startswith("campo:"):
            cid = accion.split(":", 1)[1]
            campo = next((c for c in ident.get("campos", []) if c["id"] == cid), None)
        if not campo:
            campo = _buscar_campo(texto, ident.get("campos") or [])
        if not campo:
            await kapso.enviar_texto(numero, "No reconocí ese campo. Tocá uno de la lista de arriba.")
            return
        intencion = datos.get("intencion_pendiente") or {}
        intencion["campo_id"] = campo["id"]
        await _wa_clear_conv(numero)
        await _kapso_aplicar_intencion(ident, numero, intencion,
                                       intencion.get("_texto_crudo") or "")
        return

    if estado == "k_flete_campo":
        campo = None
        if accion.startswith("campo:"):
            cid = accion.split(":", 1)[1]
            campo = next((c for c in ident.get("campos", []) if c["id"] == cid), None)
        if not campo:
            campo = _buscar_campo(texto, ident.get("campos") or [])
        if not campo:
            await kapso.enviar_texto(numero, "No reconocí ese campo. Tocá uno de la lista de arriba.")
            return
        datos.setdefault("flete", {})["campo_id"] = campo["id"]
        datos["flete"]["campo_nombre"] = campo["nombre"]
        await _kapso_guardar_flete(ident, numero, datos)
        return

    if estado in ("k_gasto_campo", "k_lluvia_campo"):
        campo = None
        if accion.startswith("campo:"):
            cid = accion.split(":", 1)[1]
            campo = next((c for c in ident.get("campos", []) if c["id"] == cid), None)
        if not campo:
            campo = _buscar_campo(texto, ident.get("campos") or [])
        if not campo:
            await kapso.enviar_texto(numero, "No reconocí ese campo. Tocá uno de la lista de arriba.")
            return
        datos["campo_id"] = campo["id"]
        datos["campo_nombre"] = campo["nombre"]

        if estado == "k_lluvia_campo":
            if datos.get("guiado"):
                await _wa_set_conv(numero, "k_lluvia_fecha", datos)
                await kapso.enviar_texto(
                    numero,
                    "¿Cuándo llovió? (ej: _hoy_, _ayer_, _15/12_, o solo escribí _hoy_ para seguir)")
                return
            await _kapso_guardar_lluvia(ident, numero, campo, float(datos.get("mm") or 0))
            return
        if datos.get("gasto", {}).get("total_de_usd") and not datos["gasto"].get("rubro"):
            await _wa_set_conv(numero, "k_gasto_rubro", datos)
            await _kapso_pedir_rubro(numero)
        elif datos.get("confirmar") and datos.get("gasto", {}).get("total_de_usd"):
            await _kapso_confirmar_gasto(numero, datos)
        elif datos.get("gastos") or datos.get("gasto", {}).get("total_de_usd"):
            await _kapso_guardar_gasto(ident, numero, datos)
        else:
            await _wa_set_conv(numero, "k_gasto_rubro", datos)
            await _kapso_pedir_rubro(numero)
        return

    if estado == "k_gasto_rubro":
        rubro = accion.split(":", 1)[1].capitalize() if accion.startswith("rubro:") else _buscar_rubro(texto)
        datos.setdefault("gasto", {})["rubro"] = rubro
        rubro_lower = (rubro or "").lower()

        # Si es rubro de insumo y venimos del flujo guiado (sin monto todavía),
        # ofrecer descontar del stock.
        if rubro_lower in RUBROS_WA_INSUMO and not datos["gasto"].get("total_de_usd"):
            insumos_stock = await _wa_insumos_del_user(ident, rubro_lower, limite=9)
            if insumos_stock:
                filas = []
                for ins in insumos_stock:
                    lbl = str(ins.get("nombre", ""))[:24]
                    sub_bits = [f"{_num(ins['stock_actual'])} {ins['unidad']} disp."]
                    if ins.get("costo_unitario"):
                        sub_bits.append(f"USD {_num(ins['costo_unitario'])}/u")
                    filas.append((f"g_ins:{ins['id']}", lbl, " · ".join(sub_bits)[:72]))
                filas.append(("g_ins:nuevo", "➕ Cargar nuevo (sin stock)", "Compro y uso ahora"))
                await _wa_set_conv(numero, "k_gasto_stock", datos)
                await kapso.enviar_lista(
                    numero, f"¿Lo sacás de tu stock de {rubro_lower}?",
                    "Elegir del stock", filas, "Stock disponible")
                return

        # ── Rubros especiales con flujo propio ─────
        if rubro_lower == "flete" and not datos["gasto"].get("total_de_usd"):
            await _wa_set_conv(numero, "k_gasto_fl_tons", datos)
            await kapso.enviar_texto(numero, "🚛 ¿Cuántas toneladas transportaste?")
            return
        if rubro_lower == "riego" and not datos["gasto"].get("total_de_usd"):
            await _wa_set_conv(numero, "k_gasto_rg_kva", datos)
            await kapso.enviar_texto(numero, "💧 ¿Cuánto consumo en kVA?")
            return
        if rubro_lower == "empleados" and not datos["gasto"].get("total_de_usd"):
            await _wa_set_conv(numero, "k_gasto_em_desc", datos)
            await kapso.enviar_texto(
                numero, "👷 ¿Qué gasto de personal? (ej: _jornalero_, _maquinista_, _salario mensual_)")
            return
        if rubro_lower == "arrendamiento" and not datos["gasto"].get("total_de_usd"):
            await _wa_set_conv(numero, "k_gasto_arr_qq", datos)
            await kapso.enviar_texto(
                numero, "🏠 ¿Cuántos quintales de soja por hectárea? (ej: *9*)")
            return

        if datos["gasto"].get("total_de_usd"):
            await _wa_set_conv(numero, "k_gasto_desc", datos)
            await kapso.enviar_texto(numero, "¿Qué producto era? (ej: glifosato, urea, semilla de soja)")
            return
        await _wa_set_conv(numero, "k_gasto_monto", datos)
        await kapso.enviar_texto(numero, "¿Cuánto fue en dólares? Escribí solo el número.")
        return

    if estado == "k_gasto_stock":
        if accion.startswith("g_ins:"):
            ins_id = accion.split(":", 1)[1]
            if ins_id == "nuevo":
                # Sin stock → flujo normal (pedir monto + descripción)
                await _wa_set_conv(numero, "k_gasto_monto", datos)
                await kapso.enviar_texto(
                    numero, "Dale. ¿Cuánto fue en dólares? Escribí solo el número.")
                return
            # Buscar insumo
            filas = await _sb_get("stock_insumos", {
                "id": f"eq.{ins_id}",
                "select": "id,nombre,unidad,stock_actual,costo_unitario",
                "limit": "1",
            })
            if not filas:
                await kapso.enviar_texto(numero, "No encontré ese insumo. Volvé a probar.")
                return
            ins = filas[0]
            g = datos.setdefault("gasto", {})
            g["insumo_id"] = ins["id"]
            g["descripcion"] = ins["nombre"]
            g["unidad"] = ins["unidad"]
            g["_stock_disp"] = float(ins.get("stock_actual") or 0)
            if ins.get("costo_unitario"):
                g["precio_unitario"] = float(ins["costo_unitario"])
            await _wa_set_conv(numero, "k_gasto_cant_total", datos)
            await kapso.enviar_texto(
                numero,
                f"¿Cuánto *{ins['nombre']}* usaste en total?\n"
                f"(disponibles: *{_num(ins['stock_actual'])} {ins['unidad']}*)")
            return
        # Texto libre — tratar como si eligió "nuevo"
        await _wa_set_conv(numero, "k_gasto_monto", datos)
        await kapso.enviar_texto(numero, "¿Cuánto fue en dólares? Escribí solo el número.")
        return

    if estado == "k_gasto_cant_total":
        cantidad = _extraer_numero(texto)
        if cantidad is None or cantidad <= 0:
            await kapso.enviar_texto(numero, "Escribí solo el número, por ejemplo *30*.")
            return
        g = datos.setdefault("gasto", {})
        stock_disp = float(g.get("_stock_disp") or 0)
        if cantidad > stock_disp + 0.001:
            await kapso.enviar_texto(
                numero,
                f"⛔ No alcanza. Tenés *{_num(stock_disp)} {g.get('unidad', '')}*, pedís *{_num(cantidad)}*.\n"
                "Cargá más stock primero o escribí _cancelar_.")
            return
        g["cantidad_total"] = cantidad
        if g.get("precio_unitario"):
            rubro = (g.get("rubro") or "").lower()
            if rubro in ("herbicidas", "fungicidas", "insecticidas"):
                await _wa_set_conv(numero, "k_gasto_momento_stock", datos)
                filas = [("g_mom:barbecho", "Barbecho"), ("g_mom:presiembra", "Presiembra"),
                         ("g_mom:postemergente", "Postemergente"), ("g_mom:aplicacion", "Aplicación"),
                         ("g_mom:skip", "Sin especificar")]
                await kapso.enviar_lista(
                    numero, "¿Momento de la pulverización?",
                    "Elegir", filas, "Momento")
                return
            await _kapso_guardar_gasto_con_stock(ident, numero, datos)
            return
        await _wa_set_conv(numero, "k_gasto_precio_unit", datos)
        await kapso.enviar_texto(
            numero,
            f"El insumo no tenía precio cargado. ¿Cuánto costó por {g.get('unidad', 'unidad')} en USD?")
        return

    if estado == "k_gasto_precio_unit":
        precio = _extraer_numero(texto)
        if precio is None or precio <= 0:
            await kapso.enviar_texto(numero, "Escribí solo el precio en USD, por ejemplo *3.5*.")
            return
        datos.setdefault("gasto", {})["precio_unitario"] = precio
        rubro = (datos["gasto"].get("rubro") or "").lower()
        if rubro in ("herbicidas", "fungicidas", "insecticidas"):
            await _wa_set_conv(numero, "k_gasto_momento_stock", datos)
            filas = [("g_mom:barbecho", "Barbecho"), ("g_mom:presiembra", "Presiembra"),
                     ("g_mom:postemergente", "Postemergente"), ("g_mom:aplicacion", "Aplicación"),
                     ("g_mom:skip", "Sin especificar")]
            await kapso.enviar_lista(
                numero, "¿Momento de la pulverización?",
                "Elegir", filas, "Momento")
            return
        await _kapso_guardar_gasto_con_stock(ident, numero, datos)
        return

    if estado == "k_gasto_momento_stock":
        momento = None
        if accion.startswith("g_mom:"):
            v = accion.split(":", 1)[1]
            if v != "skip":
                momento = v
        elif texto:
            n = _norm(texto)
            for m in ("barbecho", "presiembra", "postemergente", "aplicacion"):
                if m in n:
                    momento = m
                    break
        if momento:
            datos.setdefault("gasto", {})["momento_pulverizacion"] = momento
        await _kapso_guardar_gasto_con_stock(ident, numero, datos)
        return

    # ── Flete ─────────────────────────────────
    if estado == "k_gasto_fl_tons":
        tn = _extraer_numero(texto)
        if tn is None or tn <= 0:
            await kapso.enviar_texto(numero, "Escribí solo el número de toneladas, por ejemplo *30*.")
            return
        datos.setdefault("gasto", {})["tn"] = tn
        await _wa_set_conv(numero, "k_gasto_fl_precio", datos)
        await kapso.enviar_texto(numero, "¿Precio por tonelada? (ej: _USD 25_, _3 mil pesos_)")
        return

    if estado == "k_gasto_fl_precio":
        precio = _parsear_plata(texto)
        if precio is None or precio <= 0:
            precio = _extraer_numero(texto)
        if precio is None or precio <= 0:
            await kapso.enviar_texto(numero, "No entendí el precio. Escribí _USD 25_ o _3 mil pesos_.")
            return
        n = _norm(texto)
        es_ars = _RE_PESOS.search(n) or ("pesos" in n or "ars" in n)
        datos.setdefault("gasto", {})["precio_tn"] = precio
        datos["gasto"]["moneda"] = "ARS" if es_ars else "USD"
        if es_ars:
            # Si es ARS, usar TC del BNA
            tc = await _dolar_venta()
            datos["gasto"]["tc"] = tc
        await _kapso_guardar_gasto_especial(ident, numero, datos)
        return

    # ── Riego ─────────────────────────────────
    if estado == "k_gasto_rg_kva":
        kva = _extraer_numero(texto)
        if kva is None or kva <= 0:
            await kapso.enviar_texto(numero, "Escribí solo los kVA, por ejemplo *450*.")
            return
        datos.setdefault("gasto", {})["kva"] = kva
        await _wa_set_conv(numero, "k_gasto_rg_costo", datos)
        await kapso.enviar_texto(numero, "¿Costo por kVA en USD? (ej: *0.08*)")
        return

    if estado == "k_gasto_rg_costo":
        costo = _extraer_numero(texto)
        if costo is None or costo <= 0:
            await kapso.enviar_texto(numero, "Escribí solo el costo en USD por kVA, por ejemplo *0.08*.")
            return
        datos.setdefault("gasto", {})["costo_kva"] = costo
        await _kapso_guardar_gasto_especial(ident, numero, datos)
        return

    # ── Empleados ─────────────────────────────
    if estado == "k_gasto_em_desc":
        desc = (texto or "").strip()[:200]
        if not desc:
            await kapso.enviar_texto(numero, "Escribí una descripción.")
            return
        datos.setdefault("gasto", {})["descripcion"] = desc
        await _wa_set_conv(numero, "k_gasto_em_monto", datos)
        await kapso.enviar_texto(numero, "¿Cuánto fue? (ej: _USD 500_, _200 mil pesos_)")
        return

    if estado == "k_gasto_em_monto":
        monto = _parsear_plata(texto)
        if monto is None or monto <= 0:
            monto = _extraer_numero(texto)
        if monto is None or monto <= 0:
            await kapso.enviar_texto(numero, "No entendí el monto. Escribí _USD 500_ o _200 mil pesos_.")
            return
        n = _norm(texto)
        es_ars = _RE_PESOS.search(n) or ("pesos" in n or "ars" in n)
        datos.setdefault("gasto", {})["monto"] = monto
        datos["gasto"]["moneda"] = "ARS" if es_ars else "USD"
        if es_ars:
            tc = await _dolar_venta()
            datos["gasto"]["tc"] = tc
        await _kapso_guardar_gasto_especial(ident, numero, datos)
        return

    # ── Arrendamiento ─────────────────────────
    if estado == "k_gasto_arr_qq":
        qq = _extraer_numero(texto)
        if qq is None or qq <= 0:
            await kapso.enviar_texto(numero, "Escribí solo los qq/ha, por ejemplo *9*.")
            return
        datos.setdefault("gasto", {})["qq_ha"] = qq
        await _wa_set_conv(numero, "k_gasto_arr_precio_soja", datos)
        await kapso.enviar_texto(
            numero, "¿Precio de la soja en USD por tonelada? (ej: *320*)")
        return

    if estado == "k_gasto_arr_precio_soja":
        precio = _extraer_numero(texto)
        if precio is None or precio <= 0:
            await kapso.enviar_texto(numero, "Escribí solo el precio en USD/tn, por ejemplo *320*.")
            return
        datos.setdefault("gasto", {})["precio_soja"] = precio
        await _kapso_guardar_gasto_especial(ident, numero, datos)
        return

    if estado == "k_gasto_monto":
        monto = _extraer_numero(texto)
        if monto is None:
            await kapso.enviar_texto(numero, "No entendí el monto. Escribí solo el número, por ejemplo *1200*.")
            return
        datos.setdefault("gasto", {})["total_de_usd"] = monto
        await _wa_set_conv(numero, "k_gasto_desc", datos)
        await kapso.enviar_texto(numero, "¿Qué producto era? (ej: glifosato, urea, semilla de soja)")
        return

    if estado == "k_gasto_desc":
        g = datos.setdefault("gasto", {})
        g["descripcion"] = f"{texto.strip()} {g.pop('descripcion_extra', '')}".strip()[:300]
        if datos.get("confirmar"):
            await _kapso_confirmar_gasto(numero, datos)
            return
        rubro = (g.get("rubro") or "").lower()
        if rubro in ("herbicidas", "fungicidas", "insecticidas"):
            await _wa_set_conv(numero, "k_gasto_momento_simple", datos)
            filas = [("g_mom:barbecho", "Barbecho"), ("g_mom:presiembra", "Presiembra"),
                     ("g_mom:postemergente", "Postemergente"), ("g_mom:aplicacion", "Aplicación"),
                     ("g_mom:skip", "Sin especificar")]
            await kapso.enviar_lista(
                numero, "¿Momento de la pulverización?",
                "Elegir", filas, "Momento")
            return
        await _kapso_guardar_gasto(ident, numero, datos)
        return

    if estado == "k_gasto_momento_simple":
        momento = None
        if accion.startswith("g_mom:"):
            v = accion.split(":", 1)[1]
            if v != "skip":
                momento = v
        elif texto:
            n = _norm(texto)
            for m in ("barbecho", "presiembra", "postemergente", "aplicacion"):
                if m in n:
                    momento = m
                    break
        if momento:
            datos.setdefault("gasto", {})["momento_pulverizacion"] = momento
        await _kapso_guardar_gasto(ident, numero, datos)
        return

    if estado == "k_gasto_confirmar":
        if accion == "g_ok" or _norm(texto) in ("si", "sí", "dale", "ok", "cargalo"):
            await _kapso_guardar_gasto(ident, numero, datos)
        else:
            await _wa_clear_conv(numero)
            await kapso.enviar_texto(
                numero, "Dale, no lo cargué. Decímelo de nuevo, por ejemplo: "
                        "_gasté 500 dólares de urea en La Esperanza_.")
        return

    if estado == "k_inv_rol":
        rol_ui = accion.split(":", 1)[1] if accion.startswith("inv_rol:") else _norm(texto)
        if rol_ui not in ROLES_INVITACION:
            await kapso.enviar_texto(numero, "Tocá una de las dos opciones de arriba.")
            return
        await _kapso_invitar_crear(ident, numero, rol_ui, datos)
        return

    # ── Insumo (stock) ─────────────────────────
    if estado == "k_ins_tipo":
        tipo = None
        if accion.startswith("ins_tipo:"):
            tipo = accion.split(":", 1)[1]
        elif texto:
            n = _norm(texto)
            for k, _v in TIPOS_INSUMO:
                if k[:-1] in n or k in n:
                    tipo = k
                    break
            if not tipo:
                tipo = "otros"
        if not tipo:
            await kapso.enviar_texto(numero, "Tocá un tipo de la lista.")
            return
        datos.setdefault("insumo", {})["tipo"] = tipo
        await _wa_set_conv(numero, "k_ins_nombre", datos)
        await kapso.enviar_texto(numero, "¿Qué producto? (ej: _Glifosato 48%_, _Urea granulada_)")
        return

    if estado == "k_ins_nombre":
        nombre = (texto or "").strip()[:150]
        if not nombre:
            await kapso.enviar_texto(numero, "Escribí el nombre del producto.")
            return
        datos.setdefault("insumo", {})["nombre"] = nombre
        await _wa_set_conv(numero, "k_ins_cantidad", datos)
        await kapso.enviar_texto(numero, "¿Cuánta cantidad? (ej: _300 litros_, _50 kg_, _10 bolsas_)")
        return

    if estado == "k_ins_cantidad":
        cantidad, unidad = _parsear_cantidad_unidad(texto)
        if cantidad <= 0:
            await kapso.enviar_texto(numero, "No entendí la cantidad. Escribí _300 litros_ o _50 kg_.")
            return
        datos.setdefault("insumo", {})["cantidad"] = cantidad
        datos["insumo"]["unidad"] = unidad
        await _wa_set_conv(numero, "k_ins_ubicacion", datos)
        await kapso.enviar_texto(
            numero, "¿Dónde lo guardás? (ej: _Galpón 1_, o escribí _sin especificar_)")
        return

    if estado == "k_ins_ubicacion":
        n = _norm(texto or "")
        if n and n not in ("sin especificar", "no", "ninguna", "ninguno", "-"):
            datos.setdefault("insumo", {})["ubicacion"] = texto.strip()[:100]
        await _wa_set_conv(numero, "k_ins_costo", datos)
        unidad = datos.get("insumo", {}).get("unidad", "unidad")
        unidad_singular = {"litros": "litro", "kg": "kg", "dosis": "dosis",
                           "bolsas": "bolsa", "unidades": "unidad"}.get(unidad, unidad)
        await kapso.enviar_texto(
            numero,
            f"¿Cuánto cuesta por {unidad_singular}? (ej: _USD 3.5_ por litro, o escribí _sin costo_)")
        return

    if estado == "k_ins_precio":
        # Respuesta al pedido de precio cuando se cargó un insumo por texto
        # libre sin precio. El user puede responder con un número o "saltar".
        n = _norm(texto or "")
        ins = datos.get("insumo") or {}
        if n not in ("saltar", "skip", "no", "-", "0", ""):
            costo = _extraer_numero(texto)
            if costo is not None and costo > 0:
                ins["costo_unitario"] = float(costo)
            else:
                await kapso.enviar_texto(
                    numero,
                    "No entendí el precio. Respondé con un número (ej: _4_ o _4.50_), "
                    "o _saltar_ para no guardarlo.")
                return
        await _kapso_guardar_insumo(ident, numero, {"insumo": ins})
        return

    if estado == "k_ins_costo":
        n = _norm(texto or "")
        if n and n not in ("sin costo", "no", "ninguno", "-", "0"):
            costo = _extraer_numero(texto)
            if costo is not None and costo > 0:
                datos.setdefault("insumo", {})["costo_unitario"] = costo
        await _wa_set_conv(numero, "k_ins_notas", datos)
        await kapso.enviar_texto(
            numero,
            "¿Alguna nota? (marca, vencimiento, lote... o escribí _sin notas_)")
        return

    if estado == "k_ins_notas":
        n = _norm(texto or "")
        if n and n not in ("sin notas", "sin nota", "no", "ninguna", "-"):
            datos.setdefault("insumo", {})["notas"] = texto.strip()[:500]
        await _kapso_guardar_insumo(ident, numero, datos)
        return

    # ── Tarea nueva (carga desde WhatsApp) ─────
    if estado == "k_tar_titulo":
        titulo = (texto or "").strip()[:200]
        if not titulo:
            await kapso.enviar_texto(numero, "¿Qué hay que hacer? Escribí el título.")
            return
        datos.setdefault("tarea_nueva", {})["titulo"] = titulo
        await _wa_set_conv(numero, "k_tar_desc", datos)
        await kapso.enviar_texto(
            numero, "¿Agregás algún detalle? (descripción opcional, o escribí _sin detalle_)")
        return

    if estado == "k_tar_desc":
        n = _norm(texto or "")
        if n and n not in ("sin detalle", "sin descripcion", "sin descripción", "no", "ninguno", "-"):
            datos.setdefault("tarea_nueva", {})["descripcion"] = texto.strip()[:500]
        await _wa_set_conv(numero, "k_tar_fecha", datos)
        await kapso.enviar_texto(
            numero, "¿Para cuándo? (ej: _mañana_, _15/12_, _próxima semana_, o _sin fecha_)")
        return

    if estado == "k_tar_fecha":
        n = _norm(texto or "")
        if n not in ("sin fecha", "no", "ninguna", "-", ""):
            fecha = _parsear_fecha_natural(texto)
            if not fecha:
                await kapso.enviar_texto(
                    numero, "No entendí la fecha. Escribí _mañana_, _15/12_, _próxima semana_, o _sin fecha_.")
                return
            datos.setdefault("tarea_nueva", {})["fecha"] = fecha
        # Pedir campo (opcional) → después prioridad y asignado SIEMPRE.
        campos = ident.get("campos") or []
        if not campos:
            # Sin campos → salta a prioridad directo
            await _kapso_set_prioridad_tarea(ident, numero, datos)
            return
        if len(campos) == 1:
            datos["tarea_nueva"]["campo_id"] = campos[0]["id"]
            datos["tarea_nueva"]["campo_nombre"] = campos[0]["nombre"]
            await _kapso_set_prioridad_tarea(ident, numero, datos)
            return
        await _wa_set_conv(numero, "k_tar_campo", datos)
        filas = []
        for c in campos[:9]:
            sub = f"{_num(c['hectareas'])} ha" if c.get("hectareas") else ""
            filas.append((f"tar_campo:{c['id']}", c["nombre"], sub))
        filas.append(("tar_campo:none", "Sin campo específico", ""))
        await kapso.enviar_lista(numero, "¿En qué campo?", "Elegir campo", filas, "Tus campos")
        return

    if estado == "k_tar_campo":
        if accion.startswith("tar_campo:"):
            cid = accion.split(":", 1)[1]
            if cid != "none":
                campo = next((c for c in ident.get("campos", []) if c["id"] == cid), None)
                if campo:
                    datos["tarea_nueva"]["campo_id"] = campo["id"]
                    datos["tarea_nueva"]["campo_nombre"] = campo["nombre"]
        elif texto:
            n = _norm(texto)
            if n not in ("sin campo", "ninguno", "no", "-"):
                campo = _buscar_campo(texto, ident.get("campos") or [])
                if campo:
                    datos["tarea_nueva"]["campo_id"] = campo["id"]
                    datos["tarea_nueva"]["campo_nombre"] = campo["nombre"]
        await _kapso_set_prioridad_tarea(ident, numero, datos)
        return

    if estado == "k_tar_prioridad":
        prio = None
        if accion.startswith("tar_prio:"):
            prio = accion.split(":", 1)[1]
        elif texto:
            n = _norm(texto)
            if "alta" in n or "urgente" in n:
                prio = "alta"
            elif "baja" in n:
                prio = "baja"
            else:
                prio = "media"
        datos.setdefault("tarea_nueva", {})["prioridad"] = prio or "media"
        # Pedir asignado si el user tiene equipo
        equipo = await _wa_equipo_del_user(ident["owner_id"])
        if not equipo:
            # Sin equipo → guardar directo
            await _kapso_guardar_tarea_nueva(ident, numero, datos)
            return
        await _wa_set_conv(numero, "k_tar_asignado", datos)
        filas = []
        for m in equipo[:9]:
            sub = str(m.get("rol") or "").capitalize()
            filas.append((f"tar_asig:{m['miembro_id']}",
                          str(m.get("nombre_display") or "Sin nombre")[:24], sub))
        filas.append(("tar_asig:none", "Sin asignar", "Para vos o cualquiera"))
        await kapso.enviar_lista(
            numero, "¿A quién le asignás la tarea?",
            "Elegir", filas[:10], "Equipo")
        return

    if estado == "k_tar_asignado":
        if accion.startswith("tar_asig:"):
            mid = accion.split(":", 1)[1]
            if mid != "none":
                # Buscar el nombre en el equipo
                equipo = await _wa_equipo_del_user(ident["owner_id"])
                m = next((m for m in equipo if m["miembro_id"] == mid), None)
                if m:
                    datos["tarea_nueva"]["asignado_a"] = mid
                    datos["tarea_nueva"]["asignado_nombre"] = m.get("nombre_display")
        elif texto:
            n = _norm(texto)
            if n not in ("sin asignar", "no", "ninguno", "-", "para mi"):
                equipo = await _wa_equipo_del_user(ident["owner_id"])
                for m in equipo:
                    nombre_norm = _norm(str(m.get("nombre_display") or ""))
                    if nombre_norm and nombre_norm in n:
                        datos["tarea_nueva"]["asignado_a"] = m["miembro_id"]
                        datos["tarea_nueva"]["asignado_nombre"] = m.get("nombre_display")
                        break
        await _kapso_guardar_tarea_nueva(ident, numero, datos)
        return

    # ── Ingreso (venta de cultivo) ─────────────
    if estado == "k_ing_campo":
        campo = None
        if accion.startswith("campo:"):
            cid = accion.split(":", 1)[1]
            campo = next((c for c in ident.get("campos", []) if c["id"] == cid), None)
        if not campo:
            campo = _buscar_campo(texto, ident.get("campos") or [])
        if not campo:
            await kapso.enviar_texto(numero, "No reconocí ese campo. Tocá uno de la lista.")
            return
        datos.setdefault("ingreso", {})["campo_id"] = campo["id"]
        datos["ingreso"]["campo_nombre"] = campo["nombre"]
        # Mostrar solo los cultivos sembrados del campo (no la lista genérica).
        # Si hay uno solo, autoseleccionar y saltar al siguiente estado.
        resultado = await _kapso_pedir_cultivo_del_campo(numero, campo["id"])
        if resultado == 1:
            sembrados = await _wa_cultivos_del_campo(campo["id"])
            datos["ingreso"]["cultivo"] = sembrados[0]
            await _wa_set_conv(numero, "k_ing_tn", datos)
            lbl = next((v for k, v in CULTIVOS_WA if k == _norm_cultivo(sembrados[0])), str(sembrados[0]))
            await kapso.enviar_texto(numero, f"¿Cuántas toneladas de *{lbl}* vendiste?")
        else:
            await _wa_set_conv(numero, "k_ing_cultivo", datos)
        return

    if estado == "k_ing_cultivo":
        cultivo = None
        if accion.startswith("cultivo:"):
            # El id del botón preserva el nombre original (ej "Maíz") para que
            # la venta quede en la misma key que ya tiene el campo.
            cultivo = accion.split(":", 1)[1]
        elif texto:
            # Si el user escribe el cultivo, intentamos matchear primero con los
            # sembrados del campo para preservar el casing original.
            campo_id = datos.get("ingreso", {}).get("campo_id")
            if campo_id:
                sembrados = await _wa_cultivos_del_campo(campo_id)
                n = _norm_cultivo(texto)
                for s in sembrados:
                    if _norm_cultivo(s) == n or n in _norm_cultivo(s):
                        cultivo = s
                        break
            if not cultivo:
                cultivo = texto.strip()[:50]
        if not cultivo:
            await kapso.enviar_texto(numero, "Tocá un cultivo de la lista.")
            return
        datos["ingreso"]["cultivo"] = cultivo
        await _wa_set_conv(numero, "k_ing_tn", datos)
        lbl = next((v for k, v in CULTIVOS_WA if k == _norm_cultivo(cultivo)), str(cultivo))
        await kapso.enviar_texto(numero, f"¿Cuántas toneladas de *{lbl}* vendiste?")
        return

    if estado == "k_ing_tn":
        tn = _extraer_numero(texto)
        if tn is None or tn <= 0:
            await kapso.enviar_texto(numero, "Escribí solo el número de toneladas, por ejemplo *50*.")
            return
        datos["ingreso"]["tn"] = tn
        await _wa_set_conv(numero, "k_ing_precio", datos)
        cultivo_actual = datos["ingreso"].get("cultivo") or ""
        lbl = next((v for k, v in CULTIVOS_WA if k == _norm_cultivo(cultivo_actual)), str(cultivo_actual) or "cultivo")
        await kapso.enviar_texto(
            numero, f"¿A qué precio USD por tonelada vendiste la *{lbl}*?")
        return

    if estado == "k_ing_precio":
        precio = _extraer_numero(texto)
        if precio is None or precio <= 0:
            await kapso.enviar_texto(numero, "Escribí solo el precio en USD por tonelada, por ejemplo *320*.")
            return
        datos["ingreso"]["precio"] = precio
        await _wa_set_conv(numero, "k_ing_comprador", datos)
        await kapso.enviar_texto(
            numero,
            "¿A quién le vendiste? (ej: _Acopio La Hortensia_, o escribí _sin comprador_ si no querés registrarlo)")
        return

    if estado == "k_ing_comprador":
        n = _norm(texto or "")
        if n and n not in ("sin comprador", "no", "ninguno", "-", ""):
            datos["ingreso"]["comprador"] = texto.strip()[:150]
        await _kapso_guardar_ingreso(ident, numero, datos)
        return

    # ── Pago: bifurca según tipo (cheque / cuota_credito / otro) ───────
    if estado == "k_pag_tipo":
        tipo = None
        if accion.startswith("pag_tipo:"):
            tipo = accion.split(":", 1)[1]
        elif texto:
            n = _norm(texto)
            if "cheque" in n:
                tipo = "cheque"
            elif "cuota" in n or "credito" in n or "crédito" in n:
                tipo = "cuota_credito"
            else:
                tipo = "otro"
        if not tipo:
            await kapso.enviar_texto(numero, "Tocá un tipo de la lista.")
            return
        datos.setdefault("pago", {})["tipo"] = tipo
        if tipo == "cuota_credito":
            await _wa_set_conv(numero, "k_pag_cr_desc", datos)
            await kapso.enviar_texto(
                numero, "¿Cómo le ponemos al crédito? (ej: _Préstamo Galicia para sembradora_)")
        else:
            await _wa_set_conv(numero, "k_pag_beneficiario", datos)
            quien = "¿A quién le pagás?" if tipo != "cheque" else "¿A nombre de quién sale el cheque?"
            await kapso.enviar_texto(numero, f"{quien} (ej: _Acopio La Hortensia_)")
        return

    if estado == "k_pag_beneficiario":
        bene = (texto or "").strip()[:150]
        if not bene:
            await kapso.enviar_texto(numero, "Escribí el nombre del beneficiario.")
            return
        datos.setdefault("pago", {})["beneficiario"] = bene
        await _wa_set_conv(numero, "k_pag_importe", datos)
        await kapso.enviar_texto(numero, "¿Cuánto? (ej: _500 mil pesos_, _USD 2000_)")
        return

    if estado == "k_pag_importe":
        importe = _parsear_plata(texto)
        if importe is None or importe <= 0:
            await kapso.enviar_texto(numero, "No entendí el monto. Escribí _500 mil pesos_ o _USD 2000_.")
            return
        n = _norm(texto)
        es_usd = ("usd" in n or "dolar" in n or "dólar" in n or "u$s" in n or "u$d" in n)
        datos.setdefault("pago", {})["importe"] = importe
        datos["pago"]["moneda"] = "USD" if es_usd else "ARS"
        await _wa_set_conv(numero, "k_pag_fecha", datos)
        tipo_pago = datos["pago"].get("tipo", "")
        prompt = "¿Cuándo se cobra?" if tipo_pago == "cheque" else "¿Cuándo vence?"
        await kapso.enviar_texto(
            numero, f"{prompt} (ej: _15/12_, _próxima semana_, _en 30 días_)")
        return

    if estado == "k_pag_fecha":
        fecha = _parsear_fecha_natural(texto)
        if not fecha:
            await kapso.enviar_texto(
                numero, "No entendí la fecha. Escribí _15/12_, _próxima semana_, o _en 30 días_.")
            return
        datos.setdefault("pago", {})["fecha"] = fecha
        if datos["pago"].get("tipo") == "cheque":
            # Para cheque pedimos banco + número opcionales antes de guardar
            await _wa_set_conv(numero, "k_pag_banco", datos)
            await kapso.enviar_texto(
                numero, "¿De qué banco? (opcional, escribí _sin banco_ para saltear)")
            return
        await _kapso_guardar_pago(ident, numero, datos)
        return

    # ── Cheque: campos extra opcionales ─────────
    if estado == "k_pag_banco":
        n = _norm(texto or "")
        if n and n not in ("sin banco", "no", "ninguno", "-"):
            datos.setdefault("pago", {})["banco"] = texto.strip()[:100]
        await _wa_set_conv(numero, "k_pag_numero", datos)
        await kapso.enviar_texto(
            numero, "¿Número de cheque? (opcional, escribí _sin número_ para saltear)")
        return

    if estado == "k_pag_numero":
        n = _norm(texto or "")
        if n and n not in ("sin numero", "sin número", "no", "ninguno", "-"):
            datos.setdefault("pago", {})["numero_cheque"] = texto.strip()[:30]
        await _wa_set_conv(numero, "k_pag_ch_alerta", datos)
        await kapso.enviar_texto(
            numero,
            "¿Querés que te avise antes del vencimiento? (ej: _7 días antes_, _15 días_, o _sin alerta_)")
        return

    if estado == "k_pag_ch_alerta":
        n = _norm(texto or "")
        if n and n not in ("sin alerta", "no", "ninguno", "-", "sin aviso"):
            dias = _extraer_numero(texto)
            if dias and dias > 0 and dias <= 365:
                datos.setdefault("pago", {})["alerta_dias_antes"] = int(dias)
        await _kapso_guardar_pago(ident, numero, datos)
        return

    # ── Crédito: flujo propio con cuotas ─────────
    if estado == "k_pag_cr_desc":
        desc = (texto or "").strip()[:300]
        if not desc:
            await kapso.enviar_texto(numero, "Escribí una descripción del crédito.")
            return
        datos.setdefault("pago", {})["descripcion"] = desc
        await _wa_set_conv(numero, "k_pag_cr_beneficiario", datos)
        await kapso.enviar_texto(numero, "¿Qué banco o entidad? (ej: _Banco Galicia_)")
        return

    if estado == "k_pag_cr_beneficiario":
        bene = (texto or "").strip()[:150]
        if not bene:
            await kapso.enviar_texto(numero, "Escribí el banco o entidad.")
            return
        datos.setdefault("pago", {})["beneficiario"] = bene
        await _wa_set_conv(numero, "k_pag_cr_importe_cuota", datos)
        await kapso.enviar_texto(numero, "¿Importe de cada cuota? (ej: _500 mil pesos_, _USD 400_)")
        return

    if estado == "k_pag_cr_importe_cuota":
        importe = _parsear_plata(texto)
        if importe is None or importe <= 0:
            await kapso.enviar_texto(numero, "No entendí el importe. Escribí _500 mil pesos_ o _USD 400_.")
            return
        n = _norm(texto)
        es_usd = ("usd" in n or "dolar" in n or "dólar" in n or "u$s" in n or "u$d" in n)
        datos.setdefault("pago", {})["importe_cuota"] = importe
        datos["pago"]["moneda"] = "USD" if es_usd else "ARS"
        await _wa_set_conv(numero, "k_pag_cr_cantidad", datos)
        await kapso.enviar_texto(numero, "¿Cuántas cuotas en total? (ej: _12_)")
        return

    if estado == "k_pag_cr_cantidad":
        cantidad = _extraer_numero(texto)
        if cantidad is None or cantidad <= 0 or cantidad > 120:
            await kapso.enviar_texto(numero, "Escribí la cantidad de cuotas (1 a 120).")
            return
        datos.setdefault("pago", {})["cantidad_cuotas"] = int(cantidad)
        await _wa_set_conv(numero, "k_pag_cr_frecuencia", datos)
        filas = [(f"pag_cr_freq:{k}", lbl) for k, lbl, _ in FRECUENCIAS_CREDITO]
        await kapso.enviar_lista(
            numero, "¿Con qué frecuencia?", "Elegir", filas, "Frecuencia")
        return

    if estado == "k_pag_cr_frecuencia":
        freq = None
        if accion.startswith("pag_cr_freq:"):
            freq = accion.split(":", 1)[1]
        elif texto:
            n = _norm(texto)
            for k, _lbl, _m in FRECUENCIAS_CREDITO:
                if k in n:
                    freq = k
                    break
        if not freq:
            await kapso.enviar_texto(numero, "Tocá una frecuencia de la lista.")
            return
        datos.setdefault("pago", {})["frecuencia"] = freq
        await _wa_set_conv(numero, "k_pag_cr_fecha", datos)
        await kapso.enviar_texto(
            numero, "¿Cuándo vence la primera cuota? (ej: _15/12_, _próximo mes_)")
        return

    if estado == "k_pag_cr_fecha":
        fecha = _parsear_fecha_natural(texto)
        if not fecha:
            await kapso.enviar_texto(numero, "No entendí la fecha. Escribí _15/12_ o _próximo mes_.")
            return
        datos.setdefault("pago", {})["fecha_primera"] = fecha
        await _wa_set_conv(numero, "k_pag_cr_alerta", datos)
        await kapso.enviar_texto(
            numero,
            "¿Querés que te avise antes de cada cuota? (ej: _7 días antes_, _15 días_, o _sin alerta_)")
        return

    if estado == "k_pag_cr_alerta":
        n = _norm(texto or "")
        if n and n not in ("sin alerta", "no", "ninguno", "-", "sin aviso"):
            dias = _extraer_numero(texto)
            if dias and dias > 0 and dias <= 365:
                datos.setdefault("pago", {})["alerta_dias_antes"] = int(dias)
        await _kapso_guardar_pago_credito(ident, numero, datos)
        return

    # Estado desconocido → limpiar y volver al menú
    await _wa_clear_conv(numero)
    await _kapso_menu(ident, numero)


# ── Entender sin IA ───────────────────────────
#
# La mayoría de los mensajes de campo siguen patrones muy estables. Resolverlos
# acá evita depender de una clave de API y no cuesta nada por mensaje. Lo que
# no cae en estos patrones recién ahí va a Claude.

# Producto → rubro. Nombres como los dice la gente, no como figuran en la factura.
_RUBRO_POR_PRODUCTO = {
    "herbicidas": ["glifosato", "glifo", "2,4-d", "24d", "dicamba", "atrazina", "acetoclor",
                   "metolaclor", "s-metolaclor", "sulfentrazone", "flumioxazin", "imazetapir",
                   "cletodim", "haloxifop", "quizalofop", "bentazon", "saflufenacil", "picloram",
                   "metsulfuron", "glufosinato", "paraquat", "tembotrione", "herbicida"],
    "fungicidas": ["mancozeb", "carbendazim", "clorotalonil", "tebuconazole", "epoxiconazole",
                   "cyproconazole", "azoxistrobina", "piraclostrobina", "trifloxistrobina",
                   "estrobilurina", "triazol", "carboxamida", "curasemilla", "fungicida"],
    "insecticidas": ["cipermetrina", "lambdacialotrina", "bifentrin", "deltametrina", "permetrina",
                     "imidacloprid", "tiametoxam", "acetamiprid", "clotianidina", "clorantraniliprole",
                     "coragen", "flubendiamide", "belt", "spinosad", "spinetoram", "metoxifenocide",
                     "lufenuron", "clorpirifos", "dimetoato", "insecticida"],
    "fertilizantes": ["urea", "uan", "solmix", "sulfato de amonio", "nitrato de amonio", "map", "dap",
                      "fosfato", "superfosfato", "sfs", "sft", "kcl", "cloruro de potasio", "sulpomag",
                      "nps", "npk", "starter", "fertilizante", "fertilizante liquido", "nitrogeno",
                      "azufre", "boro", "zinc"],
    "semillas": ["semilla", "semillas", "bolsa de soja", "bolsas de soja", "bolsa de maiz",
                 "bolsas de maiz", "plantin", "simiente"],
    "laboreo": ["laboreo", "labranza", "siembra", "sembrada", "pulverizacion", "pulverizada",
                "cosecha", "cosechada", "arada", "rastra", "disco", "contratista"],
    "arrendamiento": ["arrendamiento", "arriendo", "alquiler del campo", "alquiler de campo"],
    "flete": ["flete", "fletes", "camion", "camiones", "transporte", "acarreo", "chasis"],
}

# Verbos con los que se anuncia un gasto.
_RE_GASTO = re.compile(
    r"\b(gast[eé]|gast[oó]|compr[eé]|compr[oó]|pagu[eé]|pag[oó]|puse|met[ií]|"
    r"me sali[oó]|sali[oó]|cost[oó]|invert[ií])\b")

# Plata: 1200 · 1.200 · 1,200 · $1200 · usd 1200 · 1200 dolares · 3 mil
# Miles con punto, coma o espacio ("100 000" es como transcribe los audios).
_NUM = r"\d{1,3}(?:[., ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_NUM_SUELTO = r"\d{1,3}(?:[., ]\d{3})+(?:[.,]\d{1,2})?|\d{3,}(?:[.,]\d{1,2})?"

_RE_PLATA = re.compile(
    r"(?:us\$|usd|u\$s|\$)\s*(" + _NUM + r")"
    r"|(" + _NUM + r")\s*(?:mil\b|k\b|usd|u\$s|d[oó]lar(?:es)?\b|verdes|palos)"
    r"|(" + _NUM_SUELTO + r")")

_RE_PLATA_EXPLICITA = re.compile(r"\b(usd|u\$s|d[oó]lar(es)?|verdes|pesos|lucas)\b|\$")
_RE_PESOS = re.compile(r"\bpesos\b|\bar\$|\$ar\b|\bars\b|\blucas\b")

_RE_VER_TAREAS = re.compile(
    r"(qu[eé] (tengo|hay|me toca|tenemos)|mis tareas|tareas pendientes|"
    r"pendientes|qu[eé] hacer|para hacer hoy|tarea)")


def _parsear_plata(texto: str) -> float | None:
    """Saca el monto de un mensaje. None si no hay uno claro."""
    m = _RE_PLATA.search(texto)
    if not m:
        return None
    crudo = m.group(1) or m.group(2) or m.group(3)
    if not crudo:
        return None

    # El último separador es decimal solo si le siguen 1 o 2 dígitos:
    # "1.200" es mil doscientos, "1200.50" son mil doscientos con centavos,
    # y "1.200,50" es las dos cosas a la vez.
    dec = re.search(r"[.,](\d{1,2})$", crudo)
    if dec:
        entero = re.sub(r"[., ]", "", crudo[:dec.start()])
        valor = float(f"{entero}.{dec.group(1)}")
    else:
        valor = float(re.sub(r"[., ]", "", crudo))

    # "3 mil" / "3k" → 3000
    cola = texto[m.end():m.end() + 6].lower()
    if m.group(2) and (cola.startswith(" mil") or cola.startswith("mil") or
                       cola.startswith("k") or cola.startswith(" k")):
        valor *= 1000
    elif re.search(r"\b" + re.escape(crudo) + r"\s*(mil|k)\b", texto.lower()):
        valor *= 1000
    return valor


def _parsear_fecha_natural(texto: str) -> str | None:
    """
    Convierte 'mañana', '15/12', 'próxima semana', 'en 30 días' a ISO 'YYYY-MM-DD'.
    None si no se reconoce.
    """
    n = _norm(texto.strip())
    hoy = datetime.now(AR_TZ).date()

    if n in ("hoy", "ahora"):
        return hoy.isoformat()
    if n in ("mañana", "manana"):
        return (hoy + timedelta(days=1)).isoformat()
    if n in ("pasado", "pasado mañana", "pasado manana"):
        return (hoy + timedelta(days=2)).isoformat()
    if ("proxima semana" in n or "próxima semana" in n or
            "semana que viene" in n or "en 1 semana" in n or "en una semana" in n):
        return (hoy + timedelta(days=7)).isoformat()
    if "proximo mes" in n or "próximo mes" in n or "mes que viene" in n:
        return (hoy + timedelta(days=30)).isoformat()

    # Formato DD/MM, DD/MM/YYYY, DD-MM-YYYY
    m = re.search(r"(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?", n)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        anio = int(y) if y else hoy.year
        if anio < 100:
            anio += 2000
        try:
            fecha = datetime(anio, mo, d).date()
            # Si fue DD/MM sin año y ya pasó, asumir año que viene
            if not y and fecha < hoy:
                fecha = datetime(anio + 1, mo, d).date()
            return fecha.isoformat()
        except Exception:
            pass

    # "en N días"
    m = re.search(r"en\s+(\d+)\s+d[ií]a", n)
    if m:
        try:
            return (hoy + timedelta(days=int(m.group(1)))).isoformat()
        except Exception:
            pass

    # Meses escritos: "15 de diciembre", "15 diciembre"
    meses = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
             "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
             "noviembre": 11, "diciembre": 12}
    m = re.search(r"(\d{1,2})\s*(?:de\s+)?(" + "|".join(meses.keys()) + r")", n)
    if m:
        d = int(m.group(1))
        mo = meses[m.group(2)]
        try:
            fecha = datetime(hoy.year, mo, d).date()
            if fecha < hoy:
                fecha = datetime(hoy.year + 1, mo, d).date()
            return fecha.isoformat()
        except Exception:
            pass

    return None


def _wa_fecha_corta(fecha_iso: str) -> str:
    """ '2026-12-15' → '15/12/2026'. Si no parsea, devuelve la entrada tal cual. """
    if not fecha_iso:
        return ""
    try:
        return datetime.strptime(fecha_iso[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return str(fecha_iso)


def _parsear_cantidad_unidad(texto: str) -> tuple:
    """
    '300 litros' → (300.0, 'litros'). Sin unidad reconocible → (n, 'unidades').
    Devuelve (0.0, 'unidades') si no hay número.
    """
    n = _norm(texto.strip())
    m = re.search(
        r"([\d.,]+)\s*(l|lt|lts|litros?|kg|kilos?|bolsas?|dosis|u|unidades?|unidad)\b",
        n)
    if m:
        try:
            cantidad = float(m.group(1).replace(".", "").replace(",", "."))
            raw = m.group(2)
            unidad = ("litros" if raw.startswith("l") and raw != "unidades" else
                      "kg" if raw.startswith("k") else
                      "bolsas" if raw.startswith("bolsa") else
                      "dosis" if raw == "dosis" else
                      "unidades")
            return cantidad, unidad
        except Exception:
            pass
    solo = _extraer_numero(texto)
    return (float(solo) if solo else 0.0), "unidades"


def _rubro_por_texto(texto: str) -> tuple:
    """Devuelve (rubro, producto) mirando qué producto se nombró."""
    n = _norm(texto)
    # Palabra entera, no substring: "uan" (el fertilizante) está adentro de
    # "Don Juan", y "map" adentro de "mapa".
    for rubro, palabras in _RUBRO_POR_PRODUCTO.items():
        for p in palabras:
            if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", n):
                return rubro.capitalize(), p
    # Sin rubro propio en la web, pero que la descripción diga qué fue.
    m = re.search(r"\b(gasoil|gas oil|combustible|nafta)\b", n)
    if m:
        return "Otros", m.group(1)
    return "Otros", ""


def _parsear_gasto(texto: str, campos: list) -> dict | None:
    """
    "gasté 1200 de urea en La Esperanza" → gasto listo para guardar.
    None si no parece un gasto o falta el monto.
    """
    n = _norm(texto)
    rubro, producto = _rubro_por_texto(texto)
    anuncia_gasto = bool(_RE_GASTO.search(n))

    # Sin verbo de gasto y sin producto reconocido, no arriesgamos.
    if not anuncia_gasto and not producto:
        return None

    en_pesos = bool(_RE_PESOS.search(n))

    monto = _parsear_plata(texto)
    if monto is None or monto <= 0:
        return None

    # La descripción es lo que va después de "de"/"en" y antes del campo.
    desc = producto or ""
    m = re.search(r"\b(?:de|en|para)\s+([a-záéíóúñ0-9%\s\-\.]{3,40})", n)
    if m and not desc:
        desc = m.group(1).strip()
    campo = _buscar_campo(texto, campos)

    return {
        "gasto": {"rubro": rubro, "descripcion": (desc or producto or "sin detalle")[:300],
                  "total_de_usd": monto},
        "campo": campo,
        "ars": en_pesos,   # si es True, total_de_usd todavía está en pesos
    }


# ── Interpretación de lenguaje natural con Claude ──

MODELO_CLAUDE = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")

# Lista de nombres de insumos comunes en pulverizaciones. Si el audio los
# menciona explícitamente, cada uno DEBE entrar como un item separado. Si
# Claude devuelve menos items que insumos nombrados, re-preguntamos.
INSUMOS_CONOCIDOS = (
    "glifosato", "atrazina", "2,4-d", "2,4 d", "dicamba", "paraquat",
    "metsulfuron", "clorimurón", "clorimuron", "imazapic", "imazetapir",
    "s-metolaclor", "metolaclor", "clorpirifos", "cipermetrina",
    "bifentrin", "endosulfan", "lambda", "cihalotrina",
    "azoxistrobin", "mancozeb", "triazoles", "carbendazim",
    "urea", "fosfato", "dap", "map", "sulfato", "potasio", "nitrógeno",
    "aceite", "coadyuvante", "tensioactivo", "silicona", "aceite agrícola",
    "soja", "maíz", "maiz", "trigo", "girasol", "sorgo", "cebada",
    "fertilizante", "herbicida", "fungicida", "insecticida",
)


def _contar_insumos_mencionados(texto: str) -> int:
    """Cuenta nombres de insumos distintos en el texto para validar items[]."""
    if not texto:
        return 0
    n = _norm_cultivo(texto)
    vistos = set()
    for nombre in INSUMOS_CONOCIDOS:
        if nombre in n:
            # Agrupamos sinónimos obvios (ej. "maíz"/"maiz") en una sola cuenta.
            clave = nombre.replace("maiz", "maíz").replace("2,4 d", "2,4-d")
            vistos.add(clave)
    return len(vistos)

# Todo lo que el bot sabe hacer. Claude elige una acción y completa los datos.
#
# Importante: la API de Claude permite máximo 16 parámetros con union types
# (arrays o anyOf) en un schema. Si lo pasamos, el request devuelve 400 y Claude
# NUNCA responde (caso real 2026-11-23: cada audio caía al motor legacy de
# OpenAI). Por eso NO usamos `["string", "null"]` para los opcionales: en su
# lugar, strings opcionales van como string con default "", números como number
# con default 0, booleans con default false. En el servidor tratamos esos
# valores como "no mencionado" (ver _normalizar_intencion).
ESQUEMA_INTENCION = {
    "type": "object",
    "properties": {
        "accion": {
            "type": "string",
            "enum": ["completar_tarea", "iniciar_tarea", "nota_tarea", "ver_tareas",
                     "gasto", "lluvia", "invitar", "menu", "nada",
                     "ingreso", "pago", "insumo", "tarea_nueva", "siembra",
                     "eliminar", "modificar_ultimo", "consultar",
                     "flete", "rendimiento_real", "gasto_estructura", "activo",
                     "analisis_suelo", "cerrar_campana"],
        },
        "tarea_id":        {"type": "string"},
        "nota":            {"type": "string"},
        "campo_id":        {"type": "string"},
        "rubro":           {"type": "string"},
        "monto_usd":       {"type": "number"},
        "monto_ars":       {"type": "number"},
        "descripcion":     {"type": "string"},
        "mm":              {"type": "number"},
        "nombre_invitado": {"type": "string"},
        # Ingreso (venta de cereal)
        "cultivo":         {"type": "string"},
        "toneladas":       {"type": "number"},
        "precio_tn_usd":   {"type": "number"},
        "comprador":       {"type": "string"},
        # Pago
        "tipo_pago":       {"type": "string"},
        "beneficiario":    {"type": "string"},
        "fecha_venc":      {"type": "string"},
        # Cheque: banco emisor + número impreso en el talón.
        "banco":           {"type": "string"},
        "numero_cheque":   {"type": "string"},
        # Insumo / gasto detallado
        "tipo_insumo":     {"type": "string"},
        "cantidad":        {"type": "number"},
        "unidad":          {"type": "string"},
        # Precio unitario: cantidad × precio_unit se calcula en el servidor.
        "precio_unit_usd": {"type": "number"},
        "precio_unit_ars": {"type": "number"},
        # Dosis por hectárea: multiplicamos por has del campo en el servidor.
        "por_hectarea":    {"type": "boolean"},
        # Varios productos en un solo mensaje (pulverización): cada item en
        # items[], y los campos de arriba quedan vacíos.
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rubro":           {"type": "string"},
                    "descripcion":     {"type": "string"},
                    "cantidad":        {"type": "number"},
                    "unidad":          {"type": "string"},
                    "precio_unit_usd": {"type": "number"},
                    "precio_unit_ars": {"type": "number"},
                    "monto_usd":       {"type": "number"},
                    "monto_ars":       {"type": "number"},
                    "por_hectarea":    {"type": "boolean"},
                    # Ubicación física del stock: "Fraga", "galpón sur",
                    # "cerealista moldes". Se usa solo en accion=insumo.
                    "ubicacion":       {"type": "string"},
                },
                "required": ["rubro", "descripcion", "cantidad", "unidad",
                             "precio_unit_usd", "precio_unit_ars",
                             "monto_usd", "monto_ars", "por_hectarea",
                             "ubicacion"],
                "additionalProperties": False,
            },
        },
        # Tarea nueva
        "titulo_tarea":    {"type": "string"},
        "prioridad":       {"type": "string"},
        # Tarea nueva: nombre de la persona a quien se le asigna.
        "asignado_nombre": {"type": "string"},
        # Siembra
        "fecha_siembra":   {"type": "string"},
        # Eliminar / modificar: qué tipo de registro. Valores: "gasto",
        # "ingreso", "lluvia", "insumo", "pago", "tarea".
        "tipo_eliminar":   {"type": "string"},
        # Consultas: tipo de pregunta. Valores: "gastos_mes", "stock_producto",
        # "ventas_cultivo", "cheques_pendientes", "resumen".
        "tipo_consulta":   {"type": "string"},
        # Modificar: campo a cambiar ("descripcion", "total", "campo",
        # "precio", "cantidad", "fecha") y valor nuevo ya extraído.
        "campo_a_cambiar": {"type": "string"},
        "valor_nuevo":     {"type": "string"},
        # Flete: datos del camión y la carga.
        "patente_camion":  {"type": "string"},
        "destino":         {"type": "string"},
        "ctg":             {"type": "string"},
        # Rendimiento: toneladas por hectárea cosechadas.
        "rendimiento_tha": {"type": "number"},
        # Gasto de estructura / activo: con qué periodicidad se repite.
        "frecuencia_pago": {"type": "string"},
        # Activo amortizable: vida útil en años.
        "vida_util_anos":  {"type": "number"},
        # Pulverización: momento fenológico de la aplicación.
        "momento_pulverizacion": {"type": "string"},
        # Siembra: variedad específica (ej "DK 7220", "Don Mario 60i62").
        "variedad":        {"type": "string"},
        # Invitar: rol propuesto + teléfono.
        "rol_invitado":    {"type": "string"},
        "telefono_invitado": {"type": "string"},
        # Análisis de suelo: valores numéricos extraídos del texto.
        "n_total":         {"type": "number"},
        "p_disponible":    {"type": "number"},
        "k_suelo":         {"type": "number"},
        "ph":              {"type": "number"},
        "materia_organica": {"type": "number"},
        "laboratorio":     {"type": "string"},
        "confianza":       {"type": "string", "enum": ["alta", "media", "baja"]},
    },
    "required": ["accion", "tarea_id", "nota", "campo_id", "rubro", "monto_usd", "monto_ars",
                 "descripcion", "mm", "nombre_invitado",
                 "cultivo", "toneladas", "precio_tn_usd", "comprador",
                 "tipo_pago", "beneficiario", "fecha_venc",
                 "banco", "numero_cheque",
                 "tipo_insumo", "cantidad", "unidad",
                 "precio_unit_usd", "precio_unit_ars", "por_hectarea", "items",
                 "titulo_tarea", "prioridad", "asignado_nombre",
                 "fecha_siembra", "tipo_eliminar",
                 "tipo_consulta", "campo_a_cambiar", "valor_nuevo",
                 "patente_camion", "destino", "ctg", "rendimiento_tha",
                 "frecuencia_pago", "vida_util_anos",
                 "momento_pulverizacion", "variedad", "rol_invitado",
                 "telefono_invitado", "n_total", "p_disponible", "k_suelo",
                 "ph", "materia_organica", "laboratorio",
                 "confianza"],
    "additionalProperties": False,
}


# Keys numéricas: 0 quiere decir "no mencionado" para las que mi handler ya
# protege con `is None`. Las volvemos a None para no cambiar la lógica de abajo.
_KEYS_NUM_OPCIONALES = (
    "monto_usd", "monto_ars", "mm", "toneladas", "precio_tn_usd",
    "cantidad", "precio_unit_usd", "precio_unit_ars",
    "rendimiento_tha", "vida_util_anos",
    "n_total", "p_disponible", "k_suelo", "ph", "materia_organica",
)
_KEYS_STR_OPCIONALES = (
    "tarea_id", "nota", "campo_id", "rubro", "descripcion", "nombre_invitado",
    "cultivo", "comprador", "tipo_pago", "beneficiario", "fecha_venc",
    "banco", "numero_cheque",
    "tipo_insumo", "unidad", "titulo_tarea", "prioridad", "asignado_nombre",
    "fecha_siembra",
    "tipo_eliminar", "tipo_consulta", "campo_a_cambiar", "valor_nuevo",
    "patente_camion", "destino", "ctg", "frecuencia_pago",
    "momento_pulverizacion", "variedad", "rol_invitado", "telefono_invitado",
    "laboratorio",
)


def _normalizar_intencion(intencion: dict) -> dict:
    """Convierte 0 y "" a None en los campos opcionales, para que el resto del
    código que chequea `is None` siga funcionando sin cambios. Los items[] se
    normalizan también, item por item."""
    if not isinstance(intencion, dict):
        return intencion
    for k in _KEYS_NUM_OPCIONALES:
        v = intencion.get(k)
        if v == 0 or v is False:
            intencion[k] = None
    for k in _KEYS_STR_OPCIONALES:
        if intencion.get(k) == "":
            intencion[k] = None
    if intencion.get("por_hectarea") in (False, None):
        intencion["por_hectarea"] = False  # default coherente
    items = intencion.get("items") or []
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in ("cantidad", "precio_unit_usd", "precio_unit_ars",
                  "monto_usd", "monto_ars"):
            if it.get(k) == 0:
                it[k] = None
        for k in ("rubro", "descripcion", "unidad", "ubicacion"):
            if it.get(k) == "":
                it[k] = None
    return intencion

INSTRUCCIONES_CLAUDE = """Sos el asistente de Rinde Agro, una app de gestión agropecuaria argentina, \
atendiendo por WhatsApp a productores y a sus empleados de campo.

Tu trabajo es leer un mensaje escrito como habla la gente en el campo y decidir qué acción corresponde.

Acciones:
- completar_tarea: dice que terminó/hizo algo. Poné el tarea_id de la lista. Si además comenta algo \
("la terminé, sobró medio bidón"), ese comentario va en nota.
- iniciar_tarea: dice que arrancó o está haciendo algo.
- nota_tarea: quiere dejar un comentario sobre una tarea sin darla por terminada.
- ver_tareas: pregunta qué tiene pendiente.
- gasto: compró o pagó algo del campo. rubro entre herbicidas, fungicidas, \
insecticidas, fertilizantes, semillas, laboreo, flete, empleados, arrendamiento, riego, otros. \
descripcion es el NOMBRE del producto específico, nunca el rubro genérico: si dice "glifosato" la \
descripcion es "Glifosato", no "Herbicida". Si menciona cantidad y unidad ("30 litros") ponelas \
en cantidad y unidad (unidad entre litros/kg/bolsas/dosis/unidades). Precios: si da un total \
("gasté 150 dólares"), ponelo en monto_usd; si da precio por unidad ("a $5 el litro", "$200 \
la bolsa") ponelo en precio_unit_usd y dejá monto_usd en null — el sistema calcula el total \
con cantidad × precio_unit. Mismo criterio con pesos: monto_ars para el total, precio_unit_ars \
para el unitario.

Dosis por hectárea: si dice "3 litros por hectárea" o "3 L/ha", poné cantidad=3, unidad="litros" \
Y por_hectarea=true. El servidor multiplica por las hectáreas del campo para obtener la cantidad \
total aplicada y el total en plata. "en total" o "en todo el campo" significan lo contrario: la \
cantidad es absoluta, por_hectarea=false (o null).

REGLA CRÍTICA — Varios productos en un solo mensaje: si el mensaje enumera DOS O MÁS insumos \
(típico de una pulverización que lista glifosato, atrazina, aceite, 2,4-D, dicamba, graminicida, \
coadyuvante, fungicida, etc.) tenés que devolver TODOS en el array items, sin excepción. Es \
error grave devolver sólo el primero. Cada item lleva su propio rubro, descripcion, cantidad, \
unidad, precio y por_hectarea. En ese caso, al NIVEL DE ARRIBA dejá rubro, descripcion, \
cantidad, unidad, precio_unit_usd, monto_usd TODOS en null — sólo campo_id y fecha arriba se \
aplican a todos los items.

Precios ambiguos: en una pulverización el precio SIEMPRE es por litro (o por kg, o por bolsa — \
la unidad de ese producto), nunca un monto total. Si el audio dice "glifosato, 2 dólares" sin \
decir explícitamente "por litro", igual tenés que interpretarlo como precio_unit_usd=2, NO como \
monto_usd=2. Un "2 dólares" sueltos junto a un insumo es siempre el precio unitario de ese \
insumo, porque nadie paga un gasto TOTAL de 2 dólares por una pulverización.

Ejemplo clave 1 (un solo producto): "cargar un gasto de 30 litros de glifosato en Arlicher a $5 \
por litro" → rubro=herbicidas, descripcion="Glifosato", cantidad=30, unidad="litros", \
precio_unit_usd=5, monto_usd=null, campo_id=<id de Arlicher>, items=null.

Ejemplo clave 2 (por hectárea): "se aplicaron 3 litros de glifosato por hectárea a 3 dólares \
el litro en La Escondida" → rubro=herbicidas, descripcion="Glifosato", cantidad=3, \
unidad="litros", precio_unit_usd=3, por_hectarea=true, monto_usd=null, campo_id=<id La \
Escondida>. (El sistema calcula: 3 L/ha × hectáreas del campo × 3 USD/L.)

Ejemplo clave 3 (varios productos): "cargar gasto en escondida. Se aplicó 3 L de glifosato por \
hectárea a 3 dólares el litro, 2 L de atrazina por hectárea a 1 dólar el litro, 5 L de aceite \
en todo el campo a 2 dólares el litro" → accion=gasto, campo_id=<id La Escondida>, rubro/desc/ \
cantidad/unidad/precio/monto TODOS NULL ARRIBA, items=[ \
  {rubro:"herbicidas", descripcion:"Glifosato", cantidad:3, unidad:"litros", \
   precio_unit_usd:3, por_hectarea:true}, \
  {rubro:"herbicidas", descripcion:"Atrazina", cantidad:2, unidad:"litros", \
   precio_unit_usd:1, por_hectarea:true}, \
  {rubro:"otros", descripcion:"Aceite", cantidad:5, unidad:"litros", \
   precio_unit_usd:2, por_hectarea:false}].

Ojo con "en total": si dice "se aplicaron 30 litros en total" el 30 son LITROS (cantidad=30, \
unidad=litros), NO son 30 dólares. "En total" ahí describe la cantidad aplicada, no el precio. \
El total en plata siempre se identifica por la palabra "dólares", "pesos", "USD" o el símbolo \
"\\$", nunca por "en total" a secas.

Nombre del campo: el user dice el nombre del campo de forma suelta ("en escondida", "la loma", \
"lo del silo"). Matchealo contra la lista de campos de abajo aunque no coincida palabra por \
palabra: "escondida" = "La Escondida", "loma" = "La Loma Alta". Siempre devolvé campo_id si \
hay un campo del user que parezca razonable; sólo dejalo null si no mencionó ningún campo.
- lluvia: llovió. mm es la cantidad. Si menciona fecha ("ayer", "15/12"), ponela en fecha_venc.
- ingreso: vendió cereal. cultivo (soja/maiz/trigo/girasol/sorgo), toneladas, precio_tn_usd, \
comprador (si menciona), campo_id si lo indica.
- pago: cheque, crédito u otro compromiso. tipo_pago entre cheque, cuota_credito, otro. \
beneficiario, monto_usd o monto_ars, fecha_venc (vencimiento o cobro). Para cheques, si el \
mensaje menciona el banco emisor ("Banco Macro", "del Nación", "Galicia") ponelo en banco, y si \
dice el número del cheque ("número 876543", "Nº 12345", "cheque 4521") ponelo en numero_cheque. \
Si no los menciona dejalos vacíos "".
- insumo: cargó al stock un producto. tipo_insumo entre herbicidas, fungicidas, insecticidas, \
fertilizantes, semillas, otros. descripcion = nombre del producto (incluí la concentración si \
la menciona, ej. "Glifosato 48%"). cantidad + unidad (litros/kg/bolsas/dosis). Si menciona \
dónde está guardado ("ubicado en Fraga", "en el galpón sur", "en cerealista moldes"), ponelo \
en items[].ubicacion. Múltiples insumos en un solo mensaje = usar items[] con un item por \
producto. Al nivel de arriba todo en null/"".
Ejemplo multi-insumo: "agregar al stock 300 L de glifosato al 48% ubicado en Fraga, 500 L del \
mismo glifosato en cerealista moldes, 280 L de 2,4-D al 60% en Fraga" → accion=insumo, items=[\
{rubro:"herbicidas", descripcion:"Glifosato 48%", cantidad:300, unidad:"litros", \
ubicacion:"Fraga"}, {rubro:"herbicidas", descripcion:"Glifosato 48%", cantidad:500, \
unidad:"litros", ubicacion:"cerealista moldes"}, {rubro:"herbicidas", descripcion:"2,4-D 60%", \
cantidad:280, unidad:"litros", ubicacion:"Fraga"}].
- tarea_nueva: pide crear una tarea nueva. titulo_tarea con el título. descripcion si hay detalle. \
prioridad entre alta, media, baja. fecha_venc si menciona cuándo. Si menciona a quién se le \
asigna ("asignada a Agustina", "para Juan", "que lo haga Pedro"), poné el nombre en \
asignado_nombre — el servidor lo matchea contra el equipo del user. Si no menciona a nadie, \
dejalo "".
- siembra: dice que se sembró un campo ("sembré La Escondida con soja el 10/12"). cultivo, campo_id, \
fecha_siembra (YYYY-MM-DD). Se guarda como gasto de semillas con la fecha.
- flete: cargar un flete / carta de porte ("mandé 30 tn de soja a Rosario, patente ABC123, CTG \
456789", "flete desde Erlicher, 28 tn de soja a Cargill con patente DEF456"). cultivo, \
toneladas, destino, patente_camion (del camión), ctg (número CTG), campo_id (el campo DESDE \
donde salió el cereal, si lo menciona: "desde Erlicher", "salió de Fraga", "del lote La \
Escondida"), fecha_venc (fecha del flete), precio_unit_usd (si da precio por tonelada), \
monto_usd (si da total). El servidor va a pedir el campo si no lo decís.
- rendimiento_real: cosechó un campo y quiere registrar el rendimiento real ("rindió 3.2 \
toneladas por hectárea de maíz en Erlicher", "el rendimiento fue de 4 t/ha"). cultivo, campo_id, \
rendimiento_tha (número en toneladas por hectárea).
- gasto_estructura: carga un gasto fijo o estructural que no es de un campo específico (sueldos, \
honorarios, seguros, oficina, contadores). "pagué 1500 dólares de sueldo mensual al encargado", \
"300 dólares por mes del contador". descripcion (concepto), monto_usd o monto_ars (importe), \
frecuencia_pago ("mensual", "anual", "unico"), rubro ("sueldo", "honorarios", "seguro", \
"oficina", "otros").
- activo: carga un activo amortizable (vehículo, maquinaria, herramienta, construcción). "compré \
una pulverizadora por 45000 dólares con vida útil de 10 años". descripcion (qué compró), \
monto_usd (valor de compra), rubro ("vehiculo", "maquinaria", "herramienta", "construccion"), \
vida_util_anos (años de vida útil, default 10 si no lo dice), fecha_venc (fecha de compra).
- analisis_suelo: cargar un análisis de suelo. "análisis de suelo del Erlicher, N 0.15%, P 25 \
ppm, K 0.8, MO 3.2, pH 6.8, hecho en Agrolab". campo_id, laboratorio, n_total, p_disponible, \
k_suelo, ph, materia_organica (poné los valores que menciones, el resto en 0).
- cerrar_campana: el user terminó de cosechar y quiere marcar la campaña como cerrada / cosechada \
("cerrar la campaña de maíz de Erlicher", "terminé de cosechar La Escondida"). campo_id, cultivo, \
fecha_venc (fecha de cosecha, default hoy si no dice).

Para acciones de GASTO con pulverización: si el user menciona el momento fenológico ("pulvericé \
en barbecho", "aplicación de postemergente", "para la siembra", "en presiembra") poné \
momento_pulverizacion con uno de: "barbecho", "presiembra", "postemergente", "aplicacion", \
"preemergente". Solo cuando sea una pulverización, no para otros gastos.

Para SIEMBRA: si menciona la variedad específica del cultivo ("sembré con DK 7220", "la \
variedad fue Don Mario 60i62") poné variedad con ese texto exacto.

Para INVITAR: si el user menciona el rol ("de administrador", "como operario", "admin sin \
finanzas") poné rol_invitado con uno de: "administrador", "operario", "admin_sin_finanzas". Si \
menciona el número de teléfono ("el +54 9 11 1234-5678") poné telefono_invitado con los dígitos.
- eliminar: quiere borrar el último registro recién cargado ("eliminar el último gasto", "borrar \
la venta que cargué", "cancelar la última lluvia", "borrar ese cheque"). Poné tipo_eliminar con \
el tipo: "gasto", "ingreso", "lluvia", "insumo", "pago" o "tarea". Si dice "el último" sin \
decir de qué tipo, poné tipo_eliminar="ultimo" (el servidor busca el más reciente de cualquier \
tipo). El servidor sólo borra lo cargado en los últimos 30 minutos y siempre pide confirmación.
- modificar_ultimo: quiere corregir algo del último registro ("cambiar el último gasto a 180 \
dólares", "el gasto no era glifosato era urea", "la lluvia era en Fraga no en Erlicher", "el \
precio del cheque eran 10 millones", "asignale la tarea a Juan", "cambiá la tarea a prioridad \
baja"). Poné tipo_eliminar con el tipo de registro (gasto, lluvia, insumo, pago, tarea, \
ingreso), campo_a_cambiar con QUÉ cambiar ("total", "descripcion", "campo", "precio", \
"cantidad", "fecha", "banco", "numero", "beneficiario", "comprador", "titulo", "asignado", \
"prioridad") y valor_nuevo con el valor como string ("180" para montos, "Urea" para \
descripción, "Fraga" para campo, "Agustina" para asignado, "alta/media/baja" para prioridad). \
El servidor sólo toca registros de los últimos 30 minutos y pide confirmación.

Para eliminar y modificar_ultimo, si el user dice "la primera" / "la primer" / "la vieja" / \
"la anterior" / "la de antes", el servidor va a buscar el más antiguo de la ventana en vez del \
más reciente. No pongas nada especial por eso, el servidor detecta esas palabras solo.
- consultar: hace una pregunta. Poné tipo_consulta con uno de los valores de abajo. Si pregunta \
por un campo, poné campo_id. Si pregunta por un cultivo, poné cultivo. Si pregunta por un rubro, \
poné rubro. Si pregunta por un producto/insumo específico, poné descripcion.

Valores de tipo_consulta:
  • "gastos_mes" — total gastado este mes (opcional filtro por rubro). "cuánto gasté", "cuánto va en herbicidas".
  • "gastos_campo" — gastos totales de un campo (requiere campo_id). "cuánto llevo gastado en Erlicher".
  • "gastos_producto" — gastos de un producto específico (poné el nombre en descripcion). "cuánto gasté en glifosato".
  • "stock_producto" — stock de un insumo. "cuánto stock tengo de glifosato".
  • "ventas_cultivo" — total tn/USD vendidos de un cultivo. "cuánto vendí de maíz".
  • "ventas_forward_spot" — desglose forward vs spot. "qué tengo vendido forward".
  • "cheques_pendientes" — pagos a futuro. "qué cheques tengo pendientes".
  • "cuentas_por_vencer" — todos los pagos (cheques + cuotas) por vencer en 30 días. "qué pagos me vienen".
  • "lluvia_campo" — lluvia acumulada del mes/temporada en un campo (requiere campo_id). "cuánto llovió en Fraga".
  • "lluvia_por_etapa" — lluvia desglosada por etapa fenológica del campo (barbecho, siembra, vegetativo, etc.). "cuánto llovió en la etapa floración".
  • "rentabilidad_campo" — P&L estimado de un campo (requiere campo_id). "cómo va la rentabilidad de Erlicher".
  • "rentabilidad_total" — P&L global sumando todos los campos. "cómo voy en total este año".
  • "ranking_campos" — ranking de campos por rentabilidad USD/ha. "qué campo me va mejor".
  • "rendimiento_campo" — rendimiento t/ha real del campo (requiere campo_id). "cuánto rindió La Escondida".
  • "precios_dia" — precios pizarra de hoy (soja/maíz/trigo/girasol/sorgo) + dólar BNA. "a cuánto está la soja".
  • "historial_insumo" — dónde se aplicó un insumo (requiere descripcion). "dónde usé el glifosato".
  • "analisis_suelo" — último análisis de suelo de un campo (requiere campo_id). "cómo está el suelo de Fraga".
  • "fletes_recientes" — últimos fletes cargados. "qué fletes cargué este mes".
  • "timeline_campo" — últimas actividades de un campo (requiere campo_id). "qué pasó en Erlicher últimamente".
  • "resumen" (default) — gastos + lluvia + pagos del mes. "cómo vino el mes".
- invitar: quiere sumar a alguien al equipo. nombre_invitado es de quién habla.
- menu: saluda o pide ver las opciones.
- nada: no se entiende o no tiene que ver con la app.

Reglas:
- campo_id y tarea_id tienen que ser IDs exactos de las listas de abajo, o null. Nunca inventes uno.
- Si menciona un campo o una tarea de forma aproximada ("la loma", "lo del silo"), resolvé al ID que \
corresponda.
- Si hay una tarea en foco y el mensaje habla de "la tarea", "esto" o "la", se refiere a esa.
- Los montos son en dólares salvo que diga pesos. Si dice pesos, poné el número en monto_ars y dejá \
monto_usd en null: la conversión la hace el sistema. "300 lucas" o "300 mil pesos" son 300000.
- Si la persona se corrige ("300, eh, 300 mil"), tomá el último número que dice.
- Fechas: resolvé a formato YYYY-MM-DD. "ayer", "hoy", "15/12" → fecha concreta. Si no mencionan año, \
asumí el año actual.
- confianza: alta si está TODO claro (acción + datos requeridos); media si falta algún opcional \
(ej. campo o fecha) pero los datos núcleo están; baja SOLO si estás adivinando lo esencial \
(acción incierta, montos inventados, nombres que no cuadran). Si el mensaje dice "30 litros de \
glifosato a $5 el litro" la confianza es alta, aunque falte el campo.
- Para los campos opcionales que el mensaje NO menciona: strings devolvelos como cadena vacía "" \
(no inventes un string), números devolvelos como 0, por_hectarea como false. items: si no es \
una pulverización con varios productos, devolvelo como array vacío []. El servidor los trata \
como "no mencionado" y no los usa. No inventes datos.

Ejemplos reales de mensajes que recibimos en producción (basados en casos del user, 2026-11):

Ejemplo A — venta con comprador: "Agregar otra venta a Erlicher de 70 toneladas a 178 usd por \
tonelada en cerealista moldes" → accion=ingreso, campo_id=<Erlicher>, toneladas=70, \
precio_tn_usd=178, comprador="Cerealista Moldes", cultivo="". El servidor pone el cultivo del \
campo si tiene uno solo sembrado.

Ejemplo B — cheque: "Quiero cargar un pago. Entrega de echeq fecha de pago el 26/12/2026 \
importe 8.000.000 de pesos. Entregado a cerealista moldes para pagar insumos" → accion=pago, \
tipo_pago="cheque", beneficiario="Cerealista Moldes", monto_ars=8000000, monto_usd=0, \
fecha_venc="2026-12-26", descripcion="pagar insumos". "Echeq" (electronic check) o "ecocheq" o \
"ecocheque" también son cheque.

Ejemplo B2 — cheque con banco y número: "Cargar un cheque para pagar insumos de 5.400.000 \
pesos a Agropecuaria del Sur, se cobra el 15 de enero, Banco Macro número 876543" → \
accion=pago, tipo_pago="cheque", beneficiario="Agropecuaria del Sur", monto_ars=5400000, \
fecha_venc="2027-01-15", banco="Banco Macro", numero_cheque="876543", descripcion="insumos". \
Si el mismo mensaje trae banco Y número, extraelos a los dos campos — el servidor no vuelve a \
preguntar lo que ya está.

Ejemplo C — insumo con ubicación: "Agregar 700 litros de glifosato 48% en cerealista moldes" → \
accion=insumo, items=[{rubro:"herbicidas", descripcion:"Glifosato 48%", cantidad:700, \
unidad:"litros", ubicacion:"cerealista moldes"}]. "en" + un nombre que no es un campo del user \
suele ser una UBICACIÓN (galpón, sucursal, cliente), no el campo donde se aplicó. Si dudás, \
tratalo como ubicacion del insumo, no como campo_id del gasto.

Ejemplo D — lluvia sin fecha: "llovieron 25 mm en Fraga" → accion=lluvia, mm=25, campo_id=<Fraga>, \
fecha_venc="". El servidor asume hoy si no dijo fecha; si dijo ayer o una fecha, respetala.

Ejemplo E — texto corto ambiguo: "484828 banco nacional" después de un cheque recién cargado → \
accion=nada, confianza=baja. El servidor tiene un handler aparte para info adicional de cheque."""


def _miles(v: float) -> str:
    """100000 → '100.000' (como se escriben los pesos en Argentina)."""
    return f"{round(float(v)):,}".replace(",", ".")


_DOLAR_CACHE = {"valor": None, "ts": 0.0}


async def _dolar_venta() -> float:
    """Dólar BNA divisa vendedor (el que usa la web para egresos). Cachea 30 min."""
    if _DOLAR_CACHE["valor"] and time.monotonic() - _DOLAR_CACHE["ts"] < 1800:
        return _DOLAR_CACHE["valor"]
    try:
        rows = await _sb_get("v_precio_dolar_actual", {"select": "divisa_venta", "limit": "1"})
        v = float((rows or [{}])[0].get("divisa_venta") or 0)
        if v > 100:
            _DOLAR_CACHE.update(valor=v, ts=time.monotonic())
            return v
    except Exception as e:
        print(f"[DOLAR] error leyendo v_precio_dolar_actual: {e}")
    return float(cache_precios.get("bna") or 1400)


async def _wa_terceros_conocidos(ident: dict) -> list[str]:
    """Lista única de compradores + beneficiarios que el user ya usó, para que
    Claude resuelva nombres parciales ("cerealista" → "Cerealista Moldes")."""
    owner_id = ident.get("owner_id")
    if not owner_id:
        return []
    nombres = set()
    # Beneficiarios de pagos
    filas = await _sb_get("compromisos_pago", {
        "owner_id": f"eq.{owner_id}",
        "select": "beneficiario",
        "order": "created_at.desc",
        "limit": "50",
    }) or []
    for f in filas:
        v = (f.get("beneficiario") or "").strip()
        if v:
            nombres.add(v)
    # Compradores de ingresos (viven dentro de campos.ingresos_cultivos)
    for c in ident.get("campos") or []:
        try:
            data = json.loads(c.get("ingresos_cultivos") or "{}")
        except Exception:
            data = {}
        for info in (data or {}).values():
            for v in (info.get("ventas") or []):
                comp = (v.get("comprador") or "").strip()
                if comp:
                    nombres.add(comp)
    return sorted(nombres, key=lambda x: x.lower())


async def _contexto_para_claude(ident: dict, tareas: list, tarea_activa: dict = None) -> str:
    partes = []
    campos = ident.get("campos") or []
    if campos:
        partes.append("Campos del productor:")
        for c in campos:
            partes.append(f"- id={c['id']} · {c['nombre']}")
    else:
        partes.append("El productor no tiene campos cargados.")

    # Terceros ya conocidos (compradores, beneficiarios). Si el user los
    # menciona parcial ("cerealista"), Claude usa el nombre completo.
    try:
        terceros = await _wa_terceros_conocidos(ident)
    except Exception:
        terceros = []
    if terceros:
        partes.append("\nCompradores y proveedores ya conocidos (reutilizalos tal cual "
                      "si el user los menciona parciales):")
        for t in terceros[:30]:
            partes.append(f"- {t}")

    if tareas:
        partes.append("\nTareas pendientes:")
        for t in tareas:
            partes.append(f"- id={t['id']} · {t.get('titulo') or 'sin título'}")
    else:
        partes.append("\nNo hay tareas pendientes.")

    if tarea_activa:
        partes.append(f"\nTarea en foco ahora mismo: id={tarea_activa.get('id')} · "
                      f"{tarea_activa.get('titulo') or ''}")

    quien = "el dueño de la cuenta" if ident.get("es_dueño") else f"un {ident.get('rol')} del equipo"
    partes.append(f"\nQuien escribe es {quien}, se llama {ident.get('nombre') or 'sin nombre'}.")
    return "\n".join(partes)


async def _llamar_claude(cliente, messages: list, effort: str = "high") -> dict | None:
    """Una llamada a Claude con el schema de intención. Devuelve el JSON parseado.
    Si el modelo preferido falla, cae a claude-opus-5 como respaldo."""
    modelos_a_probar = [MODELO_CLAUDE]
    if MODELO_CLAUDE != "claude-opus-5":
        modelos_a_probar.append("claude-opus-5")

    for modelo in modelos_a_probar:
        try:
            print(f"[CLAUDE] llamando modelo={modelo} effort={effort}")
            r = await cliente.messages.create(
                model=modelo,
                max_tokens=4000,
                system=INSTRUCCIONES_CLAUDE,
                messages=messages,
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": ESQUEMA_INTENCION},
                },
            )
            crudo = next((b.text for b in r.content if b.type == "text"), "")
            if not crudo:
                print(f"[CLAUDE] modelo={modelo} devolvió vacío, próximo")
                continue
            try:
                parsed = json.loads(crudo)
            except json.JSONDecodeError:
                print(f"[CLAUDE] modelo={modelo} JSON inválido: {crudo[:300]!r}")
                continue
            return _normalizar_intencion(parsed)
        except Exception as e:
            print(f"[CLAUDE] modelo={modelo} falló: {type(e).__name__}: {e}")
            continue
    return None


async def _interpretar_con_claude(texto: str, ident: dict, tareas: list,
                                  tarea_activa: dict = None) -> dict | None:
    """Traduce un mensaje libre a una acción concreta. None si no hay clave o falla.

    Si el texto menciona N insumos distintos pero Claude devuelve menos items en el
    array (falla típica en audios largos multi-producto), re-llamamos a Claude
    con mensaje de corrección automático para que vuelva a leer.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        print("[CLAUDE] falta el paquete anthropic")
        return None

    try:
        cliente = anthropic.AsyncAnthropic()
        contexto = await _contexto_para_claude(ident, tareas, tarea_activa)
        prompt_user = f"{contexto}\n\nMensaje recibido:\n{texto}"
        print(f"[CLAUDE] IN >> {texto[:400]!r}")

        parsed = await _llamar_claude(cliente, [{"role": "user", "content": prompt_user}],
                                      effort="high")
        if parsed is None:
            print(f"[CLAUDE] respuesta vacía intento 1")
            return None
        print(f"[CLAUDE] OUT 1 >> {json.dumps(parsed, ensure_ascii=False)}")

        # Retry defensivo: si el texto nombra >=2 insumos y Claude devolvió
        # menos items en el array, le decimos qué faltó y le pedimos de
        # nuevo el JSON completo. Es el fallo real que vimos con el audio
        # "glifosato + atrazina + aceite".
        n_insumos = _contar_insumos_mencionados(texto)
        n_items = len(parsed.get("items") or [])
        if parsed.get("accion") == "gasto" and n_insumos >= 2 and n_items < n_insumos:
            print(f"[CLAUDE] retry: texto nombra {n_insumos} insumos pero items={n_items}")
            correccion = (
                f"La respuesta anterior sólo tiene {n_items} items en el array, "
                f"pero el mensaje original nombra {n_insumos} productos distintos. "
                f"Volvé a leer el mensaje entero y devolvé el JSON completo con un "
                f"item por cada producto nombrado. No omitas ninguno. "
                f"Usá items[] siempre que haya 2 o más insumos.\n\n"
                f"Mensaje original:\n{texto}"
            )
            parsed2 = await _llamar_claude(
                cliente,
                [{"role": "user", "content": prompt_user},
                 {"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)},
                 {"role": "user", "content": correccion}],
                effort="high",
            )
            if parsed2 and len(parsed2.get("items") or []) >= n_items:
                parsed = parsed2
                print(f"[CLAUDE] OUT 2 >> {json.dumps(parsed, ensure_ascii=False)}")

        return parsed
    except Exception as e:
        print(f"[CLAUDE] error interpretando: {e}")
        return None


def _wa_resolver_campo_fuzzy(ident: dict, texto: str, campo_id_hint: str | None) -> dict | None:
    """
    Resolver el campo de un mensaje: 1) Claude ya devolvió campo_id válido,
    2) fuzzy match del texto contra los nombres de los campos del user
    ("escondida" → "La Escondida"), 3) si el user tiene UN SOLO campo, ese.
    None sólo cuando el user tiene varios campos y ninguno aparece en el texto.
    """
    campos = ident.get("campos") or []
    if campo_id_hint:
        for c in campos:
            if c["id"] == campo_id_hint:
                return c
    norm = _norm_cultivo(texto)
    # Match por token: cada palabra del nombre del campo (ignorando artículos)
    # tiene que estar en el texto para considerarlo match. Esto deja "escondida"
    # matchear "La Escondida" pero no un tag suelto que solo diga "la".
    stop = {"la", "el", "los", "las", "lo", "de", "del"}
    mejor = None
    mejor_score = 0
    for c in campos:
        nombre = c.get("nombre") or ""
        palabras = [p for p in _norm_cultivo(nombre).split() if p and p not in stop]
        if not palabras:
            continue
        score = sum(1 for p in palabras if p in norm)
        if score == len(palabras) and score > mejor_score:
            mejor, mejor_score = c, score
    if mejor:
        return mejor
    # Fallback: si el user tiene un solo campo, lo usamos sin preguntar.
    if len(campos) == 1:
        return campos[0]
    return None


async def _wa_armar_gasto(ident: dict, campo: dict | None, item: dict, tc_dolar: float) -> dict | None:
    """
    Aplica la lógica de cálculo de un gasto a partir de un dict con los campos
    estándar (rubro, descripcion, cantidad, unidad, precio_unit_usd,
    precio_unit_ars, monto_usd, monto_ars, por_hectarea). Devuelve el gasto
    listo para `_kapso_guardar_gasto` (bajo "gasto") o None si no alcanza.
    """
    descripcion = (item.get("descripcion") or "").strip()
    cantidad_raw = item.get("cantidad")
    unidad = (item.get("unidad") or "").strip()
    monto_usd = item.get("monto_usd")
    monto_ars = item.get("monto_ars")
    p_unit_usd = item.get("precio_unit_usd")
    p_unit_ars = item.get("precio_unit_ars")
    por_ha = bool(item.get("por_hectarea"))

    cantidad = None if cantidad_raw is None else float(cantidad_raw)
    hectareas = float((campo or {}).get("hectareas") or 0)

    # Dosis por hectárea: multiplicamos por las has del campo. Si el campo no
    # tiene hectáreas cargadas no podemos; devolvemos None para pedir el total.
    cantidad_total = cantidad
    if por_ha:
        if not cantidad or hectareas <= 0:
            return None
        cantidad_total = cantidad * hectareas

    usd = None
    descripcion_extra = ""
    if monto_usd is not None:
        usd = float(monto_usd)
    elif monto_ars is not None:
        usd = round(float(monto_ars) / tc_dolar, 2)
        descripcion_extra = f" (ARS {_miles(float(monto_ars))} al BNA ${_miles(tc_dolar)})"
    elif p_unit_usd is not None and cantidad_total is not None:
        usd = round(float(p_unit_usd) * cantidad_total, 2)
    elif p_unit_ars is not None and cantidad_total is not None:
        ars = round(float(p_unit_ars) * cantidad_total, 2)
        usd = round(ars / tc_dolar, 2)
        descripcion_extra = f" (ARS {_miles(ars)} al BNA ${_miles(tc_dolar)})"

    if usd is None:
        return None

    # Descripción: "Glifosato (90 litros)" cuando sabemos la cantidad total, o
    # "Glifosato (3 litros/ha × 30 ha)" cuando aplicamos por hectárea, así el
    # resumen deja claro en qué queda el número.
    desc_final = descripcion
    if cantidad_total is not None and unidad:
        if por_ha and hectareas > 0 and cantidad is not None:
            desc_final = (f"{desc_final or 'Insumo'} ({_num(cantidad)} {unidad}/ha"
                          f" × {_num(hectareas)} ha = {_num(cantidad_total)} {unidad})")
        else:
            desc_final = f"{desc_final or 'Insumo'} ({_num(cantidad_total)} {unidad})"
    desc_final = (desc_final + descripcion_extra).strip()

    gasto = {
        "rubro": (item.get("rubro") or "Otros").strip().capitalize() or "Otros",
        "descripcion": desc_final or "sin detalle",
        "total_de_usd": usd,
    }
    if cantidad_total is not None:
        gasto["cantidad"] = cantidad_total
    if unidad:
        gasto["unidad"] = unidad
    if p_unit_usd is not None:
        gasto["precio_unitario"] = float(p_unit_usd)
    elif p_unit_ars is not None:
        gasto["precio_unitario"] = round(float(p_unit_ars) / tc_dolar, 4)
    # Momento fenológico (si lo mencionó): barbecho/presiembra/postemergente/etc.
    mp = (item.get("momento_pulverizacion") or "").strip().lower()
    if mp in ("barbecho", "presiembra", "preemergente", "postemergente", "aplicacion"):
        gasto["momento_pulverizacion"] = mp
    return gasto


def _formato_resumen_multi(campo: dict, items_gasto: list[dict]) -> str:
    """Texto del resumen de un multi-producto para mostrar al user antes de guardar."""
    hectareas = float(campo.get("hectareas") or 0)
    total_general = sum(float(g.get("total_de_usd") or 0) for g in items_gasto)
    lineas = []
    for i, g in enumerate(items_gasto, 1):
        total_g = float(g.get("total_de_usd") or 0)
        lineas.append(f"{i}️⃣ {g.get('descripcion') or 'sin detalle'} · "
                      f"*USD {_num(total_g)}*")
    encabezado = (f"🎤 Entendí tu pulverización en *{campo.get('nombre') or ''}*"
                  + (f" ({_num(hectareas)} ha)" if hectareas > 0 else "")
                  + f":\n\n" + "\n".join(lineas)
                  + f"\n\n💰 *Total: USD {_num(round(total_general, 2))}*")
    if hectareas > 0 and total_general:
        encabezado += f" ({_num(round(total_general / hectareas, 2))} USD/ha)"
    return encabezado


async def _kapso_confirmar_multi(ident: dict, numero: str, campo: dict,
                                  items_gasto: list[dict]) -> None:
    """
    Muestra el resumen de N gastos y espera la confirmación del user ANTES
    de guardar. Nunca escribe a la base sin que el user toque ✅.
    """
    datos = {
        "multi_gasto": {
            "campo_id": campo["id"],
            "campo_nombre": campo.get("nombre") or "",
            "hectareas": float(campo.get("hectareas") or 0),
            "items": items_gasto,
        },
    }
    await _wa_set_conv(numero, "k_gasto_multi_confirmar", datos)
    texto = (_formato_resumen_multi(campo, items_gasto)
             + "\n\n¿Lo cargo así?")
    await kapso.enviar_botones(numero, texto, [
        ("g_multi_ok", "✅ Cargar todo"),
        ("g_multi_fix", "✏️ Corregir uno"),
        ("g_multi_cancel", "❌ Cancelar"),
    ])


async def _kapso_guardar_multi_definitivo(ident: dict, numero: str, datos: dict) -> None:
    """Cuando el user tocó ✅: escribe los N gastos a la base y manda resumen."""
    multi = datos.get("multi_gasto") or {}
    campo_id = multi.get("campo_id")
    campo_nombre = multi.get("campo_nombre") or ""
    hectareas = float(multi.get("hectareas") or 0)
    items_gasto = multi.get("items") or []
    hoy = datetime.now(AR_TZ).date().isoformat()

    total_general = 0.0
    lineas = []
    for g in items_gasto:
        payload = {
            "campo_id": campo_id,
            "usuario_id": ident["owner_id"],
            "rubro": (g.get("rubro") or "otros").strip().lower(),
            "fecha": hoy,
            "total_de_usd": g.get("total_de_usd") or 0,
            "descripcion": g.get("descripcion") or "",
        }
        for k in ("cantidad", "unidad", "precio_unitario"):
            if g.get(k) is not None:
                payload[k] = g[k]
        if hectareas > 0 and payload["total_de_usd"]:
            payload["usd_ha"] = round(float(payload["total_de_usd"]) / hectareas, 2)
        await _sb_post("gastos", payload)
        total_general += float(payload["total_de_usd"])
        lineas.append(f"• *{payload['rubro'].capitalize()}* — {payload['descripcion']} · "
                      f"USD {_num(payload['total_de_usd'])}")

    await _wa_clear_conv(numero)
    encabezado = (f"✅ Cargué {len(items_gasto)} gastos en *{campo_nombre}*\n"
                  f"Total: USD {_num(round(total_general, 2))}\n")
    if hectareas > 0:
        encabezado += f"USD {_num(round(total_general / hectareas, 2))}/ha\n"
    await kapso.enviar_botones(
        numero,
        encabezado + "\n" + "\n".join(lineas),
        [("menu", "Cargar otra cosa")],
    )


def _formato_resumen_multi_insumo(items_insumo: list[dict]) -> str:
    """Texto del resumen de un multi-insumo para mostrar al user antes de guardar."""
    lineas = []
    for i, it in enumerate(items_insumo, 1):
        bits = [f"{_num(it.get('cantidad') or 0)} {it.get('unidad') or 'u'}",
                it.get('nombre') or 'sin nombre']
        if it.get("ubicacion"):
            bits.append(f"📍 {it['ubicacion']}")
        if it.get("costo_unitario"):
            bits.append(f"USD {_num(it['costo_unitario'])}/u")
        lineas.append(f"{i}️⃣ " + " · ".join(bits))
    return "📦 Entendí estos insumos para el stock:\n\n" + "\n".join(lineas)


async def _kapso_confirmar_multi_insumo(ident: dict, numero: str,
                                         items_insumo: list[dict]) -> None:
    """
    Muestra el resumen de N insumos y espera confirmación del user ANTES de
    guardar en stock_insumos. Nunca escribe sin que toque ✅.
    """
    datos = {"multi_insumo": {"items": items_insumo}}
    await _wa_set_conv(numero, "k_ins_multi_confirmar", datos)
    texto = _formato_resumen_multi_insumo(items_insumo) + "\n\n¿Los agrego al stock?"
    await kapso.enviar_botones(numero, texto, [
        ("i_multi_ok", "✅ Agregar todos"),
        ("i_multi_fix", "✏️ Corregir uno"),
        ("i_multi_cancel", "❌ Cancelar"),
    ])


async def _kapso_guardar_multi_insumo(ident: dict, numero: str, datos: dict) -> None:
    """Cuando el user tocó ✅: escribe los N insumos al stock y manda resumen."""
    items_insumo = (datos.get("multi_insumo") or {}).get("items") or []
    lineas = []
    for it in items_insumo:
        payload = {
            "owner_id": ident["owner_id"],
            "nombre": it.get("nombre") or "Sin nombre",
            "tipo": it.get("tipo") or "otros",
            "unidad": it.get("unidad") or "unidades",
            "stock_actual": float(it.get("cantidad") or 0),
        }
        if it.get("ubicacion"):
            payload["ubicacion"] = it["ubicacion"]
        if it.get("costo_unitario"):
            payload["costo_unitario"] = float(it["costo_unitario"])
        await _sb_post("stock_insumos", payload)
        bits = [f"{_num(payload['stock_actual'])} {payload['unidad']}", payload["nombre"]]
        if payload.get("ubicacion"):
            bits.append(f"📍 {payload['ubicacion']}")
        lineas.append("• " + " · ".join(bits))
    await _wa_clear_conv(numero)
    await kapso.enviar_botones(
        numero,
        f"✅ Agregué {len(items_insumo)} insumos al stock:\n\n" + "\n".join(lineas),
        [("menu", "Cargar otra cosa")],
    )


_ELIMINAR_MAX_MIN = 30  # ventana por defecto (gastos, pagos, lluvia, insumos, ingresos)
# Las tareas no mueven plata ni stock, así que una ventana más amplia tiene
# poco riesgo y cubre el caso real "cargué la tarea hace media hora y la
# quiero borrar" que con 30 min quedaba raspado (caso 2026-11-26: 31.6 min).
_ELIMINAR_MAX_MIN_TAREA = 60


def _ventana_minutos(tipo: str) -> int:
    """Minutos de la ventana de edición según el tipo de registro."""
    return _ELIMINAR_MAX_MIN_TAREA if tipo == "tarea" else _ELIMINAR_MAX_MIN


async def _wa_edad_ultimo_registro(ident: dict, tipo: str) -> int | None:
    """
    Minutos desde el último registro del user de ese tipo, ignorando la ventana
    de edición. Sirve para decirle al user "la última fue hace X min" cuando no
    cae dentro de la ventana. Devuelve None si no tiene ninguno.
    """
    owner_id = ident["owner_id"]
    tabla_col = {
        "gasto":   ("gastos",           "creado_en",   "usuario_id"),
        "lluvia":  ("lluvias",          "creado_en",   "usuario_id"),
        "insumo":  ("stock_insumos",    "created_at",  "owner_id"),
        "pago":    ("compromisos_pago", "created_at",  "owner_id"),
        "tarea":   ("tareas",           "created_at",  "owner_id"),
    }
    mejor = None
    pares = tabla_col.values() if tipo == "ultimo" else [tabla_col.get(tipo)]
    for par in pares:
        if not par:
            continue
        tabla, col, col_owner = par
        rows = await _sb_get(tabla, {
            f"{col_owner}": f"eq.{owner_id}",
            "select": col,
            "order": f"{col}.desc",
            "limit": "1",
        }) or []
        if not rows:
            continue
        v = rows[0].get(col)
        if not v:
            continue
        if mejor is None or v > mejor:
            mejor = v
    if not mejor:
        return None
    try:
        # created_at viene con tz offset (+00). Lo parseamos con fromisoformat.
        dt = datetime.fromisoformat(mejor.replace("Z", "+00:00"))
    except Exception:
        return None
    ahora = datetime.now(AR_TZ)
    delta = (ahora - dt).total_seconds() / 60
    return max(0, int(delta))


def _detectar_cual_registro(texto: str) -> str:
    """
    Si el user habla de "la PRIMERA", "la PRIMER", "la VIEJA", "la de antes",
    "la que cargué antes", "la anterior", "el PRIMERO" → quiere el más antiguo
    dentro de la ventana de edición. Default: "ultimo" (el más reciente), que
    es lo que el user quiere la mayoría de las veces ("el último gasto que cargué").
    Caso real 2026-11-26: "eliminar la primer tarea que te pase de revisar
    pulverizadora a agustina" y el bot borraba la última (la bien cargada).
    """
    n = (texto or "").strip().lower()
    patrones_primero = (
        "primer tarea", "primera tarea", "primer gasto", "primer ingreso",
        "primer pago", "primer cheque", "primer insumo", "primera lluvia",
        "primer ", "primera ", "primero ",
        "la vieja", "el viejo", "la de antes", "el de antes",
        "la anterior", "el anterior", "la previa", "el previo",
        "la que cargue antes", "la que cargué antes",
    )
    for p in patrones_primero:
        if p in n:
            return "primero"
    return "ultimo"


async def _wa_buscar_ultimo_registro(ident: dict, tipo: str,
                                     cual: str = "ultimo") -> dict | None:
    """
    Busca un registro del user del tipo pedido, cargado en los últimos
    _ELIMINAR_MAX_MIN minutos. tipo puede ser: gasto, ingreso, lluvia, insumo,
    pago, tarea, o "ultimo" (el más reciente de cualquier tipo).
    cual: "ultimo" (default, el más reciente) o "primero" (el más antiguo de la
    ventana, cuando el user dijo "la primera" / "la vieja" / "la anterior").
    Devuelve {kind, id, label, extra} o None.
    """
    owner_id = ident["owner_id"]
    # Orden por created_at: ascendente cuando el user pidió "la primera".
    orden = "asc" if cual == "primero" else "desc"
    # La ventana depende del tipo (tareas = 60 min, resto = 30 min). En el
    # branch "ultimo" cada kind usa su propia ventana dentro de _buscar().
    ahora = datetime.now(AR_TZ)
    def _corte(kind: str) -> str:
        return (ahora - timedelta(minutes=_ventana_minutos(kind))).isoformat()
    corte = _corte(tipo if tipo != "ultimo" else "gasto")

    async def _buscar(kind: str) -> dict | None:
        if kind == "gasto":
            rows = await _sb_get("gastos", {
                "usuario_id": f"eq.{owner_id}",
                "creado_en": f"gt.{corte}",
                "select": "id,rubro,descripcion,total_de_usd,creado_en,campo_id",
                "order": f"creado_en.{orden}",
                "limit": "1",
            }) or []
            if not rows:
                return None
            r = rows[0]
            nombre_campo = next((c["nombre"] for c in ident.get("campos", [])
                                 if c["id"] == r.get("campo_id")), "")
            return {"kind": "gasto", "id": r["id"], "table": "gastos",
                    "label": f"Gasto · {(r.get('rubro') or '').capitalize()} · "
                             f"{r.get('descripcion') or ''} · USD {_num(r.get('total_de_usd') or 0)}"
                             + (f" en {nombre_campo}" if nombre_campo else ""),
                    "creado_en": r["creado_en"]}
        if kind == "ingreso":
            # Los ingresos viven como JSON en campos.ingresos_cultivos. El
            # "último" lo detectamos por la fecha del elemento. No tenemos una
            # fila a borrar, sino un JSON a reescribir: tratamos esto aparte en
            # el delete.
            campos = ident.get("campos") or []
            if not campos:
                return None
            mejor = None
            mejor_fecha = None
            for c in campos:
                filas = await _sb_get("campos", {
                    "id": f"eq.{c['id']}", "select": "ingresos_cultivos", "limit": "1",
                }) or []
                if not filas:
                    continue
                try:
                    data = json.loads(filas[0].get("ingresos_cultivos") or "{}")
                except Exception:
                    data = {}
                for cultivo, info in (data or {}).items():
                    for v in (info.get("ventas") or []):
                        f = v.get("fecha") or ""
                        if not f:
                            continue
                        es_mejor = (mejor_fecha is None
                                    or (f < mejor_fecha if cual == "primero"
                                        else f > mejor_fecha))
                        if es_mejor:
                            mejor_fecha = f
                            mejor = {"campo_id": c["id"], "campo_nombre": c["nombre"],
                                     "cultivo": cultivo, "venta": v}
            if not mejor:
                return None
            # Sólo si la venta es de hoy (la fecha de carga no la guardamos por
            # venta, pero la fecha del JSON es suficiente para el día).
            hoy = datetime.now(AR_TZ).date().isoformat()
            if mejor_fecha != hoy:
                return None
            v = mejor["venta"]
            return {"kind": "ingreso", "id": None, "table": "campos",
                    "campo_id": mejor["campo_id"], "cultivo": mejor["cultivo"],
                    "venta_ref": v,
                    "label": f"Venta · {_num(v.get('tn') or 0)} tn de {mejor['cultivo']} "
                             f"a USD {_num(v.get('precio') or 0)}/tn en {mejor['campo_nombre']}"}
        if kind == "lluvia":
            rows = await _sb_get("lluvias", {
                "usuario_id": f"eq.{owner_id}",
                "creado_en": f"gt.{corte}",
                "select": "id,mm,fecha,creado_en,campo_id",
                "order": f"creado_en.{orden}",
                "limit": "1",
            }) or []
            if not rows:
                return None
            r = rows[0]
            nombre_campo = next((c["nombre"] for c in ident.get("campos", [])
                                 if c["id"] == r.get("campo_id")), "")
            return {"kind": "lluvia", "id": r["id"], "table": "lluvias",
                    "label": f"Lluvia · {_num(r.get('mm') or 0)} mm"
                             + (f" en {nombre_campo}" if nombre_campo else ""),
                    "creado_en": r["creado_en"]}
        if kind == "insumo":
            rows = await _sb_get("stock_insumos", {
                "owner_id": f"eq.{owner_id}",
                "created_at": f"gt.{corte}",
                "select": "id,nombre,tipo,stock_actual,unidad,created_at",
                "order": f"created_at.{orden}",
                "limit": "1",
            }) or []
            if not rows:
                return None
            r = rows[0]
            return {"kind": "insumo", "id": r["id"], "table": "stock_insumos",
                    "label": f"Insumo · {_num(r.get('stock_actual') or 0)} "
                             f"{r.get('unidad') or ''} de {r.get('nombre') or ''}",
                    "creado_en": r["created_at"]}
        if kind == "pago":
            rows = await _sb_get("compromisos_pago", {
                "owner_id": f"eq.{owner_id}",
                "created_at": f"gt.{corte}",
                "select": "id,tipo,beneficiario,importe,moneda,fecha_cobro,created_at",
                "order": f"created_at.{orden}",
                "limit": "1",
            }) or []
            if not rows:
                return None
            r = rows[0]
            return {"kind": "pago", "id": r["id"], "table": "compromisos_pago",
                    "label": f"Pago · {r.get('tipo', '').capitalize()} · "
                             f"{r.get('moneda')} {_miles(r.get('importe') or 0)} a "
                             f"{r.get('beneficiario') or ''}",
                    "creado_en": r["created_at"]}
        if kind == "tarea":
            # Las tareas usan su propia ventana (60 min vs 30 min del resto),
            # para cubrir el caso frecuente de "borrá la tarea de hace un rato"
            # que con 30 min quedaba raspado (2026-11-26: raspó por 1.6 min).
            rows = await _sb_get("tareas", {
                "owner_id": f"eq.{owner_id}",
                "created_at": f"gt.{_corte('tarea')}",
                "select": "id,titulo,created_at",
                "order": f"created_at.{orden}",
                "limit": "1",
            }) or []
            if not rows:
                return None
            r = rows[0]
            return {"kind": "tarea", "id": r["id"], "table": "tareas",
                    "label": f"Tarea · {r.get('titulo') or 'sin título'}",
                    "creado_en": r["created_at"]}
        return None

    if tipo == "ultimo":
        # Probamos todos y nos quedamos con el más reciente (o el más antiguo
        # si el user pidió "la primera") por creado_en.
        candidatos = []
        for k in ("gasto", "lluvia", "insumo", "pago", "tarea"):
            r = await _buscar(k)
            if r:
                candidatos.append(r)
        if not candidatos:
            return None
        picker = min if cual == "primero" else max
        return picker(candidatos, key=lambda x: x.get("creado_en") or "")
    return await _buscar(tipo)


async def _kapso_confirmar_eliminar(numero: str, registro: dict) -> None:
    """Muestra el registro candidato a borrar y espera el ✅."""
    await _wa_set_conv(numero, "k_elim_confirmar", {"registro": registro})
    await kapso.enviar_botones(
        numero,
        f"🗑️ ¿Borro este registro?\n\n{registro.get('label') or 'sin detalle'}",
        [("elim_ok", "✅ Sí, borralo"), ("elim_cancel", "❌ Cancelar")],
    )


async def _kapso_ejecutar_eliminar(ident: dict, numero: str, registro: dict) -> None:
    """Hace el DELETE real. Para ingresos reescribe el JSON del campo."""
    kind = registro.get("kind")
    if kind == "ingreso":
        campo_id = registro.get("campo_id")
        cultivo = registro.get("cultivo")
        venta_ref = registro.get("venta_ref") or {}
        filas = await _sb_get("campos", {
            "id": f"eq.{campo_id}", "select": "ingresos_cultivos", "limit": "1"}) or []
        try:
            data = json.loads(filas[0].get("ingresos_cultivos") or "{}") if filas else {}
        except Exception:
            data = {}
        ventas = (data.get(cultivo) or {}).get("ventas") or []
        # Filtramos la venta exacta (por tn + precio + fecha).
        nuevas = [v for v in ventas if not (
            v.get("tn") == venta_ref.get("tn")
            and v.get("precio") == venta_ref.get("precio")
            and v.get("fecha") == venta_ref.get("fecha"))]
        if len(nuevas) == len(ventas):
            await kapso.enviar_texto(numero, "No encontré esa venta para borrar. "
                                              "Puede que ya la hayas quitado.")
            await _wa_clear_conv(numero)
            return
        if nuevas:
            data[cultivo]["ventas"] = nuevas
        else:
            # Cultivo sin ventas: lo dejamos vacío pero conservamos el cultivo.
            data[cultivo]["ventas"] = []
        await _sb_patch(f"campos?id=eq.{campo_id}",
                        {"ingresos_cultivos": json.dumps(data)})
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(numero, f"✅ Borré la venta.")
        return

    table = registro.get("table")
    rid = registro.get("id")
    if not table or not rid:
        await kapso.enviar_texto(numero, "No pude identificar el registro para borrar.")
        await _wa_clear_conv(numero)
        return
    ok = await _sb_delete(f"{table}?id=eq.{rid}")
    await _wa_clear_conv(numero)
    if ok:
        await kapso.enviar_texto(numero, f"✅ Borré: {registro.get('label') or ''}")
    else:
        await kapso.enviar_texto(numero, "No pude borrarlo. Probá desde la app.")


async def _kapso_guardar_flete(ident: dict, numero: str, datos: dict) -> None:
    """Inserta un flete en la tabla fletes."""
    f = datos.get("flete", {})
    payload = {"owner_id": ident["owner_id"], "fuente": "whatsapp"}
    for k in ("campo_id", "cultivo", "fecha", "patente_camion", "nombre_transporte",
              "toneladas", "destino", "empresa_destino", "ctg", "carta_porte_nro",
              "peso_bruto_kg", "peso_tara_kg", "peso_neto_kg", "humedad_porcentaje",
              "precio_flete_usd", "km_recorridos", "notas"):
        if f.get(k) is not None:
            payload[k] = f[k]
    await _sb_post("fletes", payload)
    await _wa_clear_conv(numero)
    partes = [f"🚚 Flete cargado."]
    if f.get("toneladas"):
        partes.append(f"⚖️ {_num(f['toneladas'])} tn" + (f" de {f['cultivo']}" if f.get("cultivo") else ""))
    if f.get("campo_nombre"):
        partes.append(f"🌱 Desde: {f['campo_nombre']}")
    if f.get("destino"):
        partes.append(f"📍 Destino: {f['destino']}")
    if f.get("patente_camion"):
        partes.append(f"🚛 Patente: {f['patente_camion']}")
    if f.get("ctg"):
        partes.append(f"🧾 CTG: {f['ctg']}")
    if f.get("precio_flete_usd"):
        partes.append(f"💰 Precio: USD {_num(f['precio_flete_usd'])}")
    await kapso.enviar_texto(numero, "\n".join(partes))


async def _kapso_guardar_rendimiento(ident: dict, numero: str, datos: dict) -> None:
    """Guarda rendimiento en campanas (si hay año + cultivo) o en campos.rendimiento."""
    r = datos.get("rendimiento", {})
    campo_id = r.get("campo_id")
    cultivo = (r.get("cultivo") or "").strip()
    rend = float(r.get("rendimiento_tha") or 0)
    anio = datetime.now(AR_TZ).year
    campanas_filas = await _sb_get("campanas", {
        "campo_id": f"eq.{campo_id}",
        "anio": f"eq.{anio}",
        "select": "id",
        "limit": "1"}) or []
    if campanas_filas:
        await _sb_patch(f"campanas?id=eq.{campanas_filas[0]['id']}",
                        {"rendimiento": rend, "cultivo": cultivo or None})
    elif cultivo:
        await _sb_post("campanas", {
            "campo_id": campo_id, "usuario_id": ident["owner_id"],
            "anio": anio, "cultivo": cultivo, "rendimiento": rend})
    else:
        # Fallback: actualizar campos.rendimiento (no es por campaña, pero
        # al menos queda registrado).
        await _sb_patch(f"campos?id=eq.{campo_id}", {"rendimiento": rend})
    await _wa_clear_conv(numero)
    campo_nombre = r.get("campo_nombre") or ""
    ha = float(r.get("hectareas") or 0)
    tn_total = rend * ha if ha else 0
    partes = [f"🌾 Rendimiento cargado: *{_num(rend)} t/ha*"
              + (f" de {cultivo}" if cultivo else "")
              + (f" en {campo_nombre}" if campo_nombre else "")]
    if tn_total:
        partes.append(f"📊 Total: {_num(tn_total)} tn ({_num(ha)} ha)")
    await kapso.enviar_texto(numero, "\n".join(partes))


async def _kapso_guardar_gasto_estructura(ident: dict, numero: str, datos: dict) -> None:
    g = datos.get("gasto_estructura", {})
    payload = {
        "owner_id": ident["owner_id"],
        "descripcion": (g.get("descripcion") or "sin detalle")[:200],
        "tipo": (g.get("tipo") or "otros").lower(),
        "importe_usd": float(g.get("importe_usd") or 0),
        "frecuencia": (g.get("frecuencia") or "mensual").lower(),
    }
    if g.get("fecha_inicio"):
        payload["fecha_inicio"] = g["fecha_inicio"]
    await _sb_post("gastos_estructura", payload)
    await _wa_clear_conv(numero)
    await kapso.enviar_texto(
        numero,
        f"🏢 Gasto de estructura cargado.\n"
        f"📝 {payload['descripcion']}\n"
        f"💰 USD {_num(payload['importe_usd'])} ({payload['frecuencia']})\n"
        f"📂 Rubro: {payload['tipo']}")


async def _kapso_guardar_analisis_suelo(ident: dict, numero: str, datos: dict) -> None:
    a = datos.get("analisis_suelo", {})
    payload = {
        "usuario_id": ident["owner_id"],
        "campo_id": a.get("campo_id"),
        "fecha": a.get("fecha") or datetime.now(AR_TZ).date().isoformat(),
    }
    for k in ("laboratorio", "n_total", "p_disponible", "k", "s",
              "materia_org", "ph", "zinc", "boro", "notas"):
        if a.get(k) is not None:
            payload[k] = a[k]
    await _sb_post("analisis_suelo", payload)
    await _wa_clear_conv(numero)
    partes = [f"🧪 Análisis de suelo cargado en *{a.get('campo_nombre') or ''}*"]
    if a.get("laboratorio"):
        partes.append(f"🏢 {a['laboratorio']}")
    valores = []
    for k, label in [("n_total", "N"), ("p_disponible", "P"), ("k", "K"),
                     ("materia_org", "MO"), ("ph", "pH")]:
        if a.get(k) is not None:
            valores.append(f"{label} {a[k]}")
    if valores:
        partes.append("📊 " + " · ".join(valores))
    await kapso.enviar_texto(numero, "\n".join(partes))


async def _kapso_cerrar_campana(ident: dict, numero: str, datos: dict) -> None:
    c = datos.get("campana", {})
    campo_id = c.get("campo_id")
    cultivo = (c.get("cultivo") or "").strip()
    fecha_cosecha = c.get("fecha_cosecha") or datetime.now(AR_TZ).date().isoformat()
    anio = datetime.now(AR_TZ).year
    filas = await _sb_get("campanas", {
        "campo_id": f"eq.{campo_id}", "anio": f"eq.{anio}",
        "select": "id,cultivo", "limit": "1"}) or []
    if filas:
        await _sb_patch(f"campanas?id=eq.{filas[0]['id']}",
                        {"fecha_cosecha": fecha_cosecha,
                         "cultivo": cultivo or filas[0].get("cultivo")})
    else:
        await _sb_post("campanas", {
            "campo_id": campo_id, "usuario_id": ident["owner_id"],
            "anio": anio, "cultivo": cultivo or None,
            "fecha_cosecha": fecha_cosecha})
    await _wa_clear_conv(numero)
    await kapso.enviar_texto(
        numero,
        f"🏁 Campaña {anio}" + (f" de *{cultivo}*" if cultivo else "") +
        f" cerrada en *{c.get('campo_nombre') or ''}*\n"
        f"📅 Fecha de cosecha: {_wa_fecha_corta(fecha_cosecha)}")


async def _kapso_guardar_activo(ident: dict, numero: str, datos: dict) -> None:
    a = datos.get("activo", {})
    payload = {
        "owner_id": ident["owner_id"],
        "descripcion": (a.get("descripcion") or "sin detalle")[:200],
        "tipo": (a.get("tipo") or "otros").lower(),
        "valor_compra_usd": float(a.get("valor_compra_usd") or 0),
        "vida_util_anos": int(a.get("vida_util_anos") or 10),
    }
    if a.get("fecha_compra"):
        payload["fecha_compra"] = a["fecha_compra"]
    await _sb_post("activos_amortizables", payload)
    await _wa_clear_conv(numero)
    amort = payload["valor_compra_usd"] / payload["vida_util_anos"] if payload["vida_util_anos"] else 0
    await kapso.enviar_texto(
        numero,
        f"🚜 Activo cargado.\n"
        f"📝 {payload['descripcion']}\n"
        f"💰 USD {_miles(payload['valor_compra_usd'])} · vida útil {payload['vida_util_anos']} años\n"
        f"📉 Amortización anual: USD {_miles(round(amort, 2))}")


def _wa_resolver_campo_consulta(ident: dict, intencion: dict) -> dict | None:
    """Helper para consultas: Claude campo_id → fuzzy → único del user."""
    return _wa_resolver_campo_fuzzy(ident, intencion.get("_texto_crudo") or "",
                                   intencion.get("campo_id"))


async def _wa_consultar(ident: dict, numero: str, intencion: dict) -> None:
    """Responde consultas rápidas."""
    tipo = (intencion.get("tipo_consulta") or "resumen").strip().lower()
    owner_id = ident["owner_id"]

    # --- precios del día: pizarra + dólar
    if tipo == "precios_dia":
        pizarras = await _sb_get("precios_pizarra", {
            "select": "fecha,soja,maiz,trigo,girasol,sorgo,bna",
            "order": "fecha.desc", "limit": "1"}) or []
        dolar = await _sb_get("precios_dolar", {
            "select": "fecha,billete_compra,billete_venta,divisa_venta",
            "order": "fecha.desc", "limit": "1"}) or []
        if not pizarras and not dolar:
            await kapso.enviar_texto(numero, "No tengo precios cargados.")
            return
        lineas = []
        if pizarras:
            p = pizarras[0]
            lineas.append(f"📊 *Pizarra ({_wa_fecha_corta(p.get('fecha'))})*")
            for cultivo, label in [("soja", "Soja"), ("maiz", "Maíz"),
                                     ("trigo", "Trigo"), ("girasol", "Girasol"),
                                     ("sorgo", "Sorgo")]:
                v = p.get(cultivo)
                if v:
                    lineas.append(f"  🌾 {label}: *USD {_num(v)}/tn*")
        if dolar:
            d = dolar[0]
            lineas.append(f"\n💵 *Dólar BNA*")
            if d.get("billete_compra"):
                lineas.append(f"  Compra: ARS {_num(d.get('billete_compra'))}")
            if d.get("billete_venta"):
                lineas.append(f"  Venta:  ARS {_num(d.get('billete_venta'))}")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    # --- gastos del mes (ya existía, lo refactorizo con filtro por producto)
    if tipo in ("gastos_mes", "gastos_producto"):
        hoy = datetime.now(AR_TZ).date()
        desde = hoy.replace(day=1).isoformat()
        rubro_filtro = (intencion.get("rubro") or "").strip().lower()
        producto_filtro = (intencion.get("descripcion") or "").strip()
        params = {
            "usuario_id": f"eq.{owner_id}",
            "fecha": f"gte.{desde}",
            "select": "rubro,descripcion,total_de_usd,campo_id",
            "limit": "500",
        }
        if rubro_filtro:
            params["rubro"] = f"eq.{rubro_filtro}"
        if producto_filtro or tipo == "gastos_producto":
            # Si pidió un producto específico, ampliamos la ventana a año en curso.
            params["fecha"] = f"gte.{hoy.replace(month=1, day=1).isoformat()}"
            if producto_filtro:
                params["descripcion"] = f"ilike.%{producto_filtro}%"
        filas = await _sb_get("gastos", params) or []
        if not filas:
            await kapso.enviar_texto(
                numero, f"No encontré gastos" +
                        (f" de *{producto_filtro}*" if producto_filtro else "") +
                        (f" en *{rubro_filtro}*" if rubro_filtro else "") +
                        ".")
            return
        total = sum(float(f.get("total_de_usd") or 0) for f in filas)
        encabezado = (f"💰 Gastos de *{producto_filtro}*" if producto_filtro
                      else "💰 Gastos de este mes")
        lineas = [f"{encabezado}: *USD {_num(round(total, 2))}* ({len(filas)} cargas)"]
        if not producto_filtro:
            por_rubro = {}
            for f in filas:
                r = (f.get("rubro") or "otros").capitalize()
                por_rubro[r] = por_rubro.get(r, 0) + float(f.get("total_de_usd") or 0)
            for r, v in sorted(por_rubro.items(), key=lambda kv: -kv[1]):
                lineas.append(f"• {r} · USD {_num(round(v, 2))}")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    # --- gastos por campo
    if tipo == "gastos_campo":
        campo = _wa_resolver_campo_consulta(ident, intencion)
        if not campo:
            await kapso.enviar_texto(numero, "¿De qué campo?")
            return
        hoy = datetime.now(AR_TZ).date()
        desde = hoy.replace(month=1, day=1).isoformat()
        filas = await _sb_get("gastos", {
            "usuario_id": f"eq.{owner_id}",
            "campo_id": f"eq.{campo['id']}",
            "fecha": f"gte.{desde}",
            "select": "rubro,total_de_usd",
            "limit": "1000",
        }) or []
        if not filas:
            await kapso.enviar_texto(numero, f"No hay gastos cargados en *{campo['nombre']}* este año.")
            return
        total = sum(float(f.get("total_de_usd") or 0) for f in filas)
        ha = float(campo.get("hectareas") or 0)
        por_rubro = {}
        for f in filas:
            r = (f.get("rubro") or "otros").capitalize()
            por_rubro[r] = por_rubro.get(r, 0) + float(f.get("total_de_usd") or 0)
        lineas = [f"💰 Gastos en *{campo['nombre']}* (año en curso): "
                  f"*USD {_num(round(total, 2))}*"]
        if ha > 0:
            lineas.append(f"  📐 USD {_num(round(total/ha, 2))}/ha ({_num(ha)} ha)")
        for r, v in sorted(por_rubro.items(), key=lambda kv: -kv[1]):
            lineas.append(f"• {r} · USD {_num(round(v, 2))}")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    if tipo == "stock_producto":
        nombre = (intencion.get("descripcion") or "").strip()
        params = {
            "owner_id": f"eq.{owner_id}",
            "select": "nombre,stock_actual,unidad,ubicacion,costo_unitario",
            "order": "stock_actual.desc",
            "limit": "20",
        }
        if nombre:
            params["nombre"] = f"ilike.%{nombre}%"
        filas = await _sb_get("stock_insumos", params) or []
        if not filas:
            await kapso.enviar_texto(
                numero, f"No tengo stock" +
                        (f" de *{nombre}*" if nombre else "") + " cargado.")
            return
        total = sum(float(f.get("stock_actual") or 0) for f in filas)
        unidad = filas[0].get("unidad") or ""
        lineas = []
        if nombre:
            lineas.append(f"📦 Stock de *{nombre}*: *{_num(total)} {unidad}*")
        else:
            lineas.append(f"📦 Stock total ({len(filas)} productos):")
        for f in filas[:10]:
            ubi = f" · 📍 {f['ubicacion']}" if f.get("ubicacion") else ""
            lineas.append(f"• {f.get('nombre')} · {_num(f.get('stock_actual') or 0)} "
                          f"{f.get('unidad') or ''}{ubi}")
        if len(filas) > 10:
            lineas.append(f"...y {len(filas) - 10} más.")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    if tipo == "ventas_cultivo":
        cultivo = (intencion.get("cultivo") or "").strip()
        campos = ident.get("campos") or []
        total_tn = 0.0
        total_usd = 0.0
        por_campo = {}
        for c in campos:
            try:
                data = json.loads(c.get("ingresos_cultivos") or "{}")
            except Exception:
                data = {}
            for k, info in (data or {}).items():
                if cultivo and _norm_cultivo(k) != _norm_cultivo(cultivo):
                    continue
                for v in (info.get("ventas") or []):
                    tn = float(v.get("tn") or 0)
                    pr = float(v.get("precio") or 0)
                    total_tn += tn
                    total_usd += tn * pr
                    por_campo[c["nombre"]] = por_campo.get(c["nombre"], 0) + tn
        if total_tn == 0:
            cultivo_lbl = f" de *{cultivo}*" if cultivo else ""
            await kapso.enviar_texto(numero, f"No tengo ventas{cultivo_lbl} registradas.")
            return
        lineas = [f"🌾 Ventas{' de ' + cultivo if cultivo else ''}: "
                  f"*{_num(total_tn)} tn · USD {_miles(round(total_usd, 2))}*"]
        for nombre, tn in sorted(por_campo.items(), key=lambda kv: -kv[1]):
            lineas.append(f"• {nombre}: {_num(tn)} tn")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    if tipo in ("cheques_pendientes", "cuentas_por_vencer"):
        hoy = datetime.now(AR_TZ).date()
        hasta = (hoy + timedelta(days=30)).isoformat() if tipo == "cuentas_por_vencer" else None
        params = {
            "owner_id": f"eq.{owner_id}",
            "estado": "eq.pendiente",
            "fecha_cobro": f"gte.{hoy.isoformat()}",
            "select": "tipo,beneficiario,importe,moneda,fecha_cobro,banco,numero_cheque,descripcion",
            "order": "fecha_cobro.asc",
            "limit": "40",
        }
        filas = await _sb_get("compromisos_pago", params) or []
        if hasta:
            filas = [f for f in filas if (f.get("fecha_cobro") or "") <= hasta]
        if not filas:
            titulo = "pagos en los próximos 30 días" if tipo == "cuentas_por_vencer" else "pagos pendientes a futuro"
            await kapso.enviar_texto(numero, f"No tenés {titulo}. 🎉")
            return
        titulo = "pagos en los próximos 30 días" if tipo == "cuentas_por_vencer" else "pagos por venir"
        lineas = [f"🧾 Tenés *{len(filas)} {titulo}*:"]
        total_ars = sum(float(f.get("importe") or 0) for f in filas if f.get("moneda") == "ARS")
        total_usd = sum(float(f.get("importe") or 0) for f in filas if f.get("moneda") == "USD")
        for f in filas[:12]:
            tipo_lbl = f.get("tipo", "pago").capitalize()
            num = f" #{f['numero_cheque']}" if f.get("numero_cheque") else ""
            lineas.append(
                f"• {_wa_fecha_corta(f.get('fecha_cobro'))} · {tipo_lbl}{num} · "
                f"{f.get('moneda', 'ARS')} {_miles(f.get('importe') or 0)} "
                f"a {f.get('beneficiario') or '—'}")
        if len(filas) > 12:
            lineas.append(f"...y {len(filas) - 12} más.")
        if total_ars or total_usd:
            lineas.append("")
            if total_ars:
                lineas.append(f"💰 Total ARS: *{_miles(total_ars)}*")
            if total_usd:
                lineas.append(f"💰 Total USD: *{_miles(total_usd)}*")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    # --- Ventas forward vs spot
    if tipo == "ventas_forward_spot":
        campos = ident.get("campos") or []
        forward_tn = spot_tn = 0.0
        forward_usd = spot_usd = 0.0
        for c in campos:
            try:
                data = json.loads(c.get("ingresos_cultivos") or "{}")
            except Exception:
                data = {}
            for info in (data or {}).values():
                for v in (info.get("ventas") or []):
                    tn = float(v.get("tn") or 0)
                    pr = float(v.get("precio") or 0)
                    if (v.get("tipo") or "spot").lower() == "forward":
                        forward_tn += tn
                        forward_usd += tn * pr
                    else:
                        spot_tn += tn
                        spot_usd += tn * pr
        if forward_tn == 0 and spot_tn == 0:
            await kapso.enviar_texto(numero, "No tenés ventas cargadas.")
            return
        lineas = ["🌾 *Ventas forward vs spot*"]
        if forward_tn:
            lineas.append(f"📅 Forward: *{_num(forward_tn)} tn · USD {_miles(round(forward_usd, 2))}*")
        if spot_tn:
            lineas.append(f"💵 Spot:    *{_num(spot_tn)} tn · USD {_miles(round(spot_usd, 2))}*")
        lineas.append(f"\nTotal: *{_num(forward_tn + spot_tn)} tn · USD {_miles(round(forward_usd + spot_usd, 2))}*")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    # --- Lluvia por campo
    if tipo in ("lluvia_campo", "lluvia_por_etapa"):
        campo = _wa_resolver_campo_consulta(ident, intencion)
        if not campo:
            await kapso.enviar_texto(numero, "¿De qué campo?")
            return
        hoy = datetime.now(AR_TZ).date()
        desde = hoy.replace(month=1, day=1).isoformat()
        filas = await _sb_get("lluvias", {
            "usuario_id": f"eq.{owner_id}",
            "campo_id": f"eq.{campo['id']}",
            "fecha": f"gte.{desde}",
            "select": "fecha,mm,etapa",
            "order": "fecha.desc",
            "limit": "500",
        }) or []
        if not filas:
            await kapso.enviar_texto(numero,
                f"No tengo lluvias registradas en *{campo['nombre']}* este año.")
            return
        total = sum(float(f.get("mm") or 0) for f in filas)
        del_mes = [f for f in filas if (f.get("fecha") or "")[:7] == hoy.isoformat()[:7]]
        mm_mes = sum(float(f.get("mm") or 0) for f in del_mes)
        if tipo == "lluvia_por_etapa":
            por_etapa = {}
            for f in filas:
                e = (f.get("etapa") or "sin etapa").lower()
                por_etapa[e] = por_etapa.get(e, 0) + float(f.get("mm") or 0)
            lineas = [f"🌧️ Lluvias en *{campo['nombre']}* por etapa (año en curso): "
                      f"*{_num(total)} mm*"]
            for e, mm in sorted(por_etapa.items(), key=lambda kv: -kv[1]):
                lineas.append(f"• {e.capitalize()}: {_num(mm)} mm")
        else:
            lineas = [f"🌧️ Lluvias en *{campo['nombre']}*",
                      f"📅 Este mes: *{_num(mm_mes)} mm* ({len(del_mes)} eventos)",
                      f"📊 Año: *{_num(total)} mm* ({len(filas)} eventos)"]
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    # --- Rentabilidad por campo (texto) — calculamos P&L con lo que hay en DB
    async def _calc_renta_campo(campo: dict) -> dict:
        year_desde = datetime.now(AR_TZ).date().replace(month=1, day=1).isoformat()
        gastos = await _sb_get("gastos", {
            "usuario_id": f"eq.{owner_id}",
            "campo_id": f"eq.{campo['id']}",
            "fecha": f"gte.{year_desde}",
            "select": "total_de_usd",
            "limit": "1000"}) or []
        total_gasto = sum(float(g.get("total_de_usd") or 0) for g in gastos)
        total_venta = 0.0
        tn_total = 0.0
        try:
            data = json.loads(campo.get("ingresos_cultivos") or "{}")
        except Exception:
            data = {}
        for info in (data or {}).values():
            for v in (info.get("ventas") or []):
                tn = float(v.get("tn") or 0)
                pr = float(v.get("precio") or 0)
                tn_total += tn
                total_venta += tn * pr
        ha = float(campo.get("hectareas") or 0)
        margen = total_venta - total_gasto
        return {"gasto": total_gasto, "venta": total_venta, "margen": margen,
                "ha": ha, "tn": tn_total}

    if tipo == "rentabilidad_campo":
        campo = _wa_resolver_campo_consulta(ident, intencion)
        if not campo:
            await kapso.enviar_texto(numero, "¿De qué campo?")
            return
        r = await _calc_renta_campo(campo)
        lineas = [f"📈 *Rentabilidad de {campo['nombre']}* (año en curso)",
                  f"💰 Ingresos: USD {_miles(round(r['venta'], 2))}",
                  f"💵 Gastos:   USD {_miles(round(r['gasto'], 2))}",
                  f"📊 Margen:   *USD {_miles(round(r['margen'], 2))}*"]
        if r["ha"] > 0:
            lineas.append(f"📐 USD {_num(round(r['margen']/r['ha'], 2))}/ha ({_num(r['ha'])} ha)")
        if r["tn"] > 0:
            lineas.append(f"🌾 {_num(r['tn'])} tn vendidas")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    if tipo in ("rentabilidad_total", "ranking_campos"):
        campos = ident.get("campos") or []
        if not campos:
            await kapso.enviar_texto(numero, "No tenés campos cargados.")
            return
        resultados = []
        for c in campos:
            r = await _calc_renta_campo(c)
            resultados.append({**r, "nombre": c["nombre"]})
        total_gasto = sum(r["gasto"] for r in resultados)
        total_venta = sum(r["venta"] for r in resultados)
        margen_total = total_venta - total_gasto
        if tipo == "rentabilidad_total":
            lineas = [f"📈 *Rentabilidad total* (año en curso, {len(resultados)} campos)",
                      f"💰 Ingresos: USD {_miles(round(total_venta, 2))}",
                      f"💵 Gastos:   USD {_miles(round(total_gasto, 2))}",
                      f"📊 Margen:   *USD {_miles(round(margen_total, 2))}*"]
            await kapso.enviar_texto(numero, "\n".join(lineas))
            return
        # ranking_campos
        resultados.sort(key=lambda r: (r["margen"] / r["ha"]) if r["ha"] > 0 else r["margen"], reverse=True)
        lineas = [f"🏆 *Ranking de campos por USD/ha* (margen anual):"]
        for r in resultados[:10]:
            if r["ha"] > 0:
                lineas.append(f"• {r['nombre']}: *USD {_num(round(r['margen']/r['ha'], 2))}/ha* "
                              f"(margen {_miles(round(r['margen'], 2))})")
            else:
                lineas.append(f"• {r['nombre']}: margen USD {_miles(round(r['margen'], 2))}")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    # --- Rendimiento del campo (desde campanas o campos.rendimiento)
    if tipo == "rendimiento_campo":
        campo = _wa_resolver_campo_consulta(ident, intencion)
        if not campo:
            await kapso.enviar_texto(numero, "¿De qué campo?")
            return
        camps = await _sb_get("campanas", {
            "campo_id": f"eq.{campo['id']}",
            "select": "anio,cultivo,rendimiento,precio_venta",
            "order": "anio.desc",
            "limit": "5"}) or []
        if not camps and campo.get("rendimiento"):
            await kapso.enviar_texto(
                numero,
                f"🌾 *{campo['nombre']}*: {_num(campo['rendimiento'])} t/ha "
                f"({_num(float(campo['rendimiento']) * float(campo.get('hectareas') or 0))} tn totales)")
            return
        if not camps:
            await kapso.enviar_texto(numero, f"No tengo rendimiento cargado en *{campo['nombre']}*.")
            return
        ha = float(campo.get("hectareas") or 0)
        lineas = [f"🌾 *Rendimiento histórico de {campo['nombre']}*"]
        for c in camps:
            rend = float(c.get("rendimiento") or 0)
            tn = rend * ha if ha else 0
            cultivo = c.get("cultivo") or ""
            lineas.append(f"• {c.get('anio')} ({cultivo}): *{_num(rend)} t/ha*"
                          + (f" · {_num(tn)} tn" if tn else ""))
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    # --- Historial de un insumo (dónde se aplicó)
    if tipo == "historial_insumo":
        nombre = (intencion.get("descripcion") or "").strip()
        if not nombre:
            await kapso.enviar_texto(numero, "¿De qué insumo?")
            return
        filas = await _sb_get("gastos", {
            "usuario_id": f"eq.{owner_id}",
            "descripcion": f"ilike.%{nombre}%",
            "select": "fecha,descripcion,cantidad,unidad,total_de_usd,campo_id",
            "order": "fecha.desc",
            "limit": "15",
        }) or []
        if not filas:
            await kapso.enviar_texto(numero, f"No encontré aplicaciones de *{nombre}*.")
            return
        nombres_campo = {c["id"]: c["nombre"] for c in ident.get("campos", [])}
        lineas = [f"📜 Últimos usos de *{nombre}* ({len(filas)}):"]
        for f in filas[:10]:
            cnt = _num(f.get("cantidad") or 0) + " " + (f.get("unidad") or "") if f.get("cantidad") else ""
            campo_nm = nombres_campo.get(f.get("campo_id"), "")
            lineas.append(
                f"• {_wa_fecha_corta(f.get('fecha'))} · {cnt} · USD {_num(f.get('total_de_usd') or 0)}"
                + (f" en {campo_nm}" if campo_nm else ""))
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    # --- Análisis de suelo del campo
    if tipo == "analisis_suelo":
        campo = _wa_resolver_campo_consulta(ident, intencion)
        if not campo:
            await kapso.enviar_texto(numero, "¿De qué campo?")
            return
        filas = await _sb_get("analisis_suelo", {
            "usuario_id": f"eq.{owner_id}",
            "campo_id": f"eq.{campo['id']}",
            "select": "fecha,laboratorio,n_total,p_disponible,k,s,materia_org,ph,zinc,boro",
            "order": "fecha.desc",
            "limit": "1",
        }) or []
        if not filas:
            await kapso.enviar_texto(numero, f"No tengo análisis de suelo en *{campo['nombre']}*.")
            return
        a = filas[0]
        lineas = [f"🧪 *Suelo de {campo['nombre']}* ({_wa_fecha_corta(a.get('fecha'))})"]
        if a.get("laboratorio"):
            lineas.append(f"🏢 {a['laboratorio']}")
        for key, label, unit in [
            ("n_total", "N total", "%"), ("p_disponible", "P disponible", "ppm"),
            ("k", "K", "me/100g"), ("s", "S", "ppm"),
            ("materia_org", "Mat. orgánica", "%"), ("ph", "pH", ""),
            ("zinc", "Zn", "ppm"), ("boro", "B", "ppm"),
        ]:
            v = a.get(key)
            if v is not None:
                lineas.append(f"• {label}: *{v}{unit}*")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    # --- Fletes recientes
    if tipo == "fletes_recientes":
        hoy = datetime.now(AR_TZ).date()
        desde = hoy.replace(day=1).isoformat()
        filas = await _sb_get("fletes", {
            "owner_id": f"eq.{owner_id}",
            "fecha": f"gte.{desde}",
            "select": "fecha,cultivo,toneladas,destino,empresa_destino,patente_camion,ctg,carta_porte_nro",
            "order": "fecha.desc",
            "limit": "20",
        }) or []
        if not filas:
            await kapso.enviar_texto(numero, "No cargaste fletes este mes.")
            return
        total_tn = sum(float(f.get("toneladas") or 0) for f in filas)
        lineas = [f"🚚 Fletes de este mes: *{len(filas)} viajes · {_num(total_tn)} tn*"]
        for f in filas[:10]:
            dest = f.get("empresa_destino") or f.get("destino") or ""
            ctg = f" CTG {f['ctg']}" if f.get("ctg") else ""
            lineas.append(
                f"• {_wa_fecha_corta(f.get('fecha'))} · {_num(f.get('toneladas') or 0)} tn de "
                f"{f.get('cultivo') or ''} → {dest}{ctg}")
        if len(filas) > 10:
            lineas.append(f"...y {len(filas) - 10} más.")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    # --- Timeline del campo (últimas N actividades)
    if tipo == "timeline_campo":
        campo = _wa_resolver_campo_consulta(ident, intencion)
        if not campo:
            await kapso.enviar_texto(numero, "¿De qué campo?")
            return
        desde = (datetime.now(AR_TZ).date() - timedelta(days=60)).isoformat()
        acts = []
        # Gastos
        gastos = await _sb_get("gastos", {
            "usuario_id": f"eq.{owner_id}", "campo_id": f"eq.{campo['id']}",
            "fecha": f"gte.{desde}", "select": "fecha,rubro,descripcion,total_de_usd",
            "order": "fecha.desc", "limit": "15"}) or []
        for g in gastos:
            acts.append((g.get("fecha") or "", "💵",
                         f"{(g.get('rubro') or '').capitalize()} · {g.get('descripcion') or ''} · "
                         f"USD {_num(g.get('total_de_usd') or 0)}"))
        # Lluvias
        lluvias = await _sb_get("lluvias", {
            "usuario_id": f"eq.{owner_id}", "campo_id": f"eq.{campo['id']}",
            "fecha": f"gte.{desde}", "select": "fecha,mm,etapa",
            "order": "fecha.desc", "limit": "10"}) or []
        for l in lluvias:
            etapa = f" ({l['etapa']})" if l.get("etapa") else ""
            acts.append((l.get("fecha") or "", "🌧️", f"{_num(l.get('mm') or 0)} mm{etapa}"))
        # Eventos
        eventos = await _sb_get("eventos", {
            "usuario_id": f"eq.{owner_id}", "campo_id": f"eq.{campo['id']}",
            "fecha": f"gte.{desde}", "select": "fecha,tipo,descripcion",
            "order": "fecha.desc", "limit": "10"}) or []
        for e in eventos:
            acts.append((e.get("fecha") or "", "📌",
                         f"{(e.get('tipo') or '').capitalize()}: {e.get('descripcion') or ''}"))
        if not acts:
            await kapso.enviar_texto(numero, f"No hay actividad en *{campo['nombre']}* los últimos 60 días.")
            return
        acts.sort(key=lambda x: x[0], reverse=True)
        lineas = [f"📜 *Timeline de {campo['nombre']}* (60 días)"]
        for fecha, emoji, texto in acts[:15]:
            lineas.append(f"{emoji} {_wa_fecha_corta(fecha)} · {texto}")
        await kapso.enviar_texto(numero, "\n".join(lineas))
        return

    # resumen genérico del mes (gastos + ingresos + lluvias + cheques)
    hoy = datetime.now(AR_TZ).date()
    desde = hoy.replace(day=1).isoformat()
    gastos = await _sb_get("gastos", {
        "usuario_id": f"eq.{owner_id}", "fecha": f"gte.{desde}",
        "select": "total_de_usd", "limit": "500"}) or []
    total_gasto = sum(float(g.get("total_de_usd") or 0) for g in gastos)
    lluvias = await _sb_get("lluvias", {
        "usuario_id": f"eq.{owner_id}", "fecha": f"gte.{desde}",
        "select": "mm", "limit": "500"}) or []
    total_mm = sum(float(l.get("mm") or 0) for l in lluvias)
    cheques = await _sb_get("compromisos_pago", {
        "owner_id": f"eq.{owner_id}", "estado": "eq.pendiente",
        "fecha_cobro": f"gte.{hoy.isoformat()}",
        "select": "importe", "limit": "100"}) or []
    lineas = [f"📊 Resumen de este mes:",
              f"💰 Gastos: *USD {_num(round(total_gasto, 2))}* ({len(gastos)} cargas)",
              f"🌧️ Lluvia: *{_num(total_mm)} mm* ({len(lluvias)} registros)",
              f"🧾 Pagos pendientes: *{len(cheques)}*"]
    await kapso.enviar_texto(numero, "\n".join(lineas))


async def _wa_modificar_ultimo(ident: dict, numero: str, intencion: dict) -> None:
    """Modifica el último registro del tipo indicado. Pide confirmación."""
    tipo = (intencion.get("tipo_eliminar") or "ultimo").strip().lower()
    if tipo not in ("gasto", "ingreso", "lluvia", "insumo", "pago", "tarea", "ultimo"):
        tipo = "ultimo"
    campo_cambiar = (intencion.get("campo_a_cambiar") or "").strip().lower()
    valor_nuevo = (intencion.get("valor_nuevo") or "").strip()

    if not campo_cambiar or not valor_nuevo:
        await kapso.enviar_texto(
            numero, "No entendí qué querés cambiar. Probá así:\n"
                     "_cambiá el último gasto a USD 180_\n"
                     "_el gasto no era glifosato era urea_")
        return

    cual = _detectar_cual_registro(intencion.get("_texto_crudo") or "")
    registro = await _wa_buscar_ultimo_registro(ident, tipo, cual)
    if not registro:
        tipo_lbl = "nada reciente" if tipo == "ultimo" else f"ningún {tipo}"
        ventana = _ventana_minutos(tipo if tipo != "ultimo" else "gasto")
        edad = await _wa_edad_ultimo_registro(ident, tipo)
        if edad is not None and edad > ventana:
            extra = (f" El más reciente que tenés es de hace *{edad} min*, "
                     f"pasó la ventana de {ventana} min. Para editarlo entrá "
                     f"desde la app.")
        else:
            extra = (f" Para editar algo más viejo que {ventana} min, entrá "
                     f"desde la app.")
        await kapso.enviar_texto(
            numero,
            f"No encontré {tipo_lbl} tuyo cargado en los últimos {ventana} minutos.{extra}")
        return

    # Armo el patch según el campo que quiere cambiar
    patch = {}
    kind = registro.get("kind")
    cm = campo_cambiar
    try:
        if kind == "gasto":
            if cm in ("total", "importe", "monto"):
                patch["total_de_usd"] = float(valor_nuevo.replace(",", "."))
            elif cm in ("descripcion", "producto", "nombre"):
                patch["descripcion"] = valor_nuevo[:200]
            elif cm == "rubro":
                patch["rubro"] = valor_nuevo.strip().lower()
            elif cm in ("precio", "precio_unitario"):
                patch["precio_unitario"] = float(valor_nuevo.replace(",", "."))
            elif cm == "cantidad":
                patch["cantidad"] = float(valor_nuevo.replace(",", "."))
            elif cm == "fecha":
                f = _parsear_fecha_natural(valor_nuevo)
                if f:
                    patch["fecha"] = f
        elif kind == "lluvia":
            if cm in ("mm", "cantidad"):
                patch["mm"] = float(valor_nuevo.replace(",", "."))
            elif cm == "fecha":
                f = _parsear_fecha_natural(valor_nuevo)
                if f:
                    patch["fecha"] = f
        elif kind == "insumo":
            if cm in ("cantidad", "stock"):
                patch["stock_actual"] = float(valor_nuevo.replace(",", "."))
            elif cm in ("precio", "precio_unitario", "costo"):
                patch["costo_unitario"] = float(valor_nuevo.replace(",", "."))
            elif cm in ("ubicacion", "ubicación", "lugar"):
                patch["ubicacion"] = valor_nuevo[:150]
            elif cm in ("nombre", "descripcion"):
                patch["nombre"] = valor_nuevo[:150]
        elif kind == "pago":
            if cm in ("importe", "monto", "total"):
                patch["importe"] = float(valor_nuevo.replace(",", "."))
            elif cm in ("beneficiario", "a quien", "a quién"):
                patch["beneficiario"] = valor_nuevo[:150]
            elif cm == "banco":
                patch["banco"] = valor_nuevo[:100]
            elif cm in ("numero", "número"):
                patch["numero_cheque"] = valor_nuevo[:30]
            elif cm == "fecha":
                f = _parsear_fecha_natural(valor_nuevo)
                if f:
                    patch["fecha_cobro"] = f
        elif kind == "tarea":
            # Editar tarea: titulo, descripcion, prioridad, fecha,
            # asignado (resuelto contra el equipo del user).
            if cm in ("titulo", "título", "nombre"):
                patch["titulo"] = valor_nuevo[:200]
            elif cm in ("descripcion", "descripción", "detalle"):
                patch["descripcion"] = valor_nuevo[:500]
            elif cm == "prioridad":
                prio = valor_nuevo.strip().lower()
                if prio in ("alta", "media", "baja"):
                    patch["prioridad"] = prio
            elif cm == "fecha":
                f = _parsear_fecha_natural(valor_nuevo)
                if f:
                    patch["fecha_vencimiento"] = f
            elif cm in ("asignado", "asignada", "persona", "a quien", "a quién"):
                # Match contra el equipo del user, igual criterio que k_tar_asignado
                # (match por nombre normalizado). Si no matchea nadie, no se aplica.
                equipo = await _wa_equipo_del_user(ident["owner_id"])
                n_pedido = _norm(valor_nuevo)
                for m in equipo:
                    nombre_norm = _norm(str(m.get("nombre_display") or ""))
                    if not nombre_norm:
                        continue
                    if nombre_norm == n_pedido or n_pedido in nombre_norm or nombre_norm in n_pedido:
                        patch["asignado_a"] = m["miembro_id"]
                        break
    except (ValueError, TypeError):
        pass

    if not patch:
        await kapso.enviar_texto(
            numero,
            f"No entendí cómo cambiar *{campo_cambiar}* en un {kind}. Probá otra forma.")
        return

    # Confirmación: estado + botones
    await _wa_set_conv(numero, "k_mod_confirmar",
                       {"registro": registro, "patch": patch})
    cambios = ", ".join(f"*{k}* → *{v}*" for k, v in patch.items())
    await kapso.enviar_botones(
        numero,
        f"✏️ Cambios para:\n{registro.get('label') or ''}\n\n{cambios}\n\n¿Lo aplico?",
        [("mod_ok", "✅ Sí, cambialo"), ("mod_cancel", "❌ Cancelar")],
    )


async def _kapso_ejecutar_modificar(ident: dict, numero: str, datos: dict) -> None:
    registro = datos.get("registro") or {}
    patch = datos.get("patch") or {}
    kind = registro.get("kind")

    if kind == "ingreso":
        # Ingresos no tienen tabla, requieren reescribir el JSON.
        # Para simplificar, soportamos cambiar precio o tn: buscamos la venta
        # exacta del JSON y reemplazamos los campos del patch.
        campo_id = registro.get("campo_id")
        cultivo = registro.get("cultivo")
        venta_ref = registro.get("venta_ref") or {}
        filas = await _sb_get("campos", {
            "id": f"eq.{campo_id}", "select": "ingresos_cultivos", "limit": "1"}) or []
        try:
            data = json.loads(filas[0].get("ingresos_cultivos") or "{}") if filas else {}
        except Exception:
            data = {}
        for v in (data.get(cultivo) or {}).get("ventas") or []:
            if (v.get("tn") == venta_ref.get("tn")
                    and v.get("precio") == venta_ref.get("precio")
                    and v.get("fecha") == venta_ref.get("fecha")):
                if "mm" in patch or "precio" in patch:
                    pass  # precio ya está arriba; no toco
                # Aplicar campos del patch (mapeo: 'total_de_usd' no aplica aquí)
                for k, val in patch.items():
                    if k == "precio_unitario":
                        v["precio"] = val
                break
        await _sb_patch(f"campos?id=eq.{campo_id}",
                        {"ingresos_cultivos": json.dumps(data)})
    else:
        table = registro.get("table")
        rid = registro.get("id")
        if not table or not rid:
            await kapso.enviar_texto(numero, "No pude identificar el registro.")
            await _wa_clear_conv(numero)
            return
        await _sb_patch(f"{table}?id=eq.{rid}", patch)

    await _wa_clear_conv(numero)
    await kapso.enviar_texto(numero, f"✅ Listo, actualicé el registro.")


async def _kapso_aplicar_intencion(ident: dict, numero: str, intencion: dict,
                                   texto_crudo: str = "") -> bool:
    """Ejecuta lo que Claude entendió. Devuelve False si no se pudo hacer nada."""
    # Lo guardamos en la intención para que helpers (p. ej. fuzzy match del
    # campo) puedan consultarlo sin cambiarles la firma.
    intencion["_texto_crudo"] = texto_crudo
    accion = intencion.get("accion") or "nada"
    if accion == "nada":
        return False
    # confianza=baja no se descarta ciegamente: si los datos núcleo están, es
    # mejor arrancar el flujo con prefill que caer al motor legacy (OpenAI) que
    # no sabe de precio_unit y pisa con un total equivocado (caso real 2026-11:
    # audio "10 L de glifosato a $5 el litro" → legacy guardaba USD 3 suelto).
    # Sólo descartamos si falta la señal mínima para arrancar el flujo correcto.
    if intencion.get("confianza") == "baja":
        tiene_dato_util = (
            intencion.get("monto_usd") is not None
            or intencion.get("monto_ars") is not None
            or intencion.get("precio_unit_usd") is not None
            or intencion.get("precio_unit_ars") is not None
            or intencion.get("mm") is not None
            or intencion.get("cantidad") is not None
            or intencion.get("toneladas") is not None
            or intencion.get("tarea_id")
            or intencion.get("titulo_tarea")
        )
        if not tiene_dato_util:
            return False

    ids_campos = {c["id"] for c in ident.get("campos", [])}

    if accion == "menu":
        await _kapso_menu(ident, numero)
        return True

    if accion == "ver_tareas":
        await _kapso_tareas(ident, numero)
        return True

    if accion == "eliminar":
        tipo = (intencion.get("tipo_eliminar") or "ultimo").strip().lower()
        if tipo not in ("gasto", "ingreso", "lluvia", "insumo", "pago", "tarea", "ultimo"):
            tipo = "ultimo"
        cual = _detectar_cual_registro(intencion.get("_texto_crudo") or "")
        registro = await _wa_buscar_ultimo_registro(ident, tipo, cual)
        if not registro:
            tipo_lbl = "nada reciente" if tipo == "ultimo" else f"ningún {tipo}"
            ventana = _ventana_minutos(tipo if tipo != "ultimo" else "gasto")
            edad = await _wa_edad_ultimo_registro(ident, tipo)
            if edad is not None and edad > ventana:
                extra = (f" El más reciente que tenés es de hace *{edad} min*, "
                         f"pasó la ventana de {ventana} min. Para borrarlo "
                         f"entrá desde la app.")
            else:
                extra = (f" Para borrar algo más viejo que {ventana} min, "
                         f"entrá desde la app.")
            await kapso.enviar_texto(
                numero,
                f"No encontré {tipo_lbl} tuyo cargado en los últimos {ventana} minutos.{extra}")
            return True
        await _kapso_confirmar_eliminar(numero, registro)
        return True

    if accion == "modificar_ultimo":
        await _wa_modificar_ultimo(ident, numero, intencion)
        return True

    if accion == "consultar":
        await _wa_consultar(ident, numero, intencion)
        return True

    # ── Flete ────────────────────────────────────────
    if accion == "flete":
        if not _wa_puede(ident, "gastos", "editar"):
            return False
        campo = _wa_resolver_campo_fuzzy(ident, intencion.get("_texto_crudo") or "",
                                        intencion.get("campo_id"))
        datos = {"flete": {
            "campo_id": campo["id"] if campo else None,
            "campo_nombre": campo["nombre"] if campo else None,
            "cultivo": intencion.get("cultivo") or None,
            "fecha": intencion.get("fecha_venc") or datetime.now(AR_TZ).date().isoformat(),
            "patente_camion": intencion.get("patente_camion") or None,
            "toneladas": float(intencion["toneladas"]) if intencion.get("toneladas") else None,
            "destino": intencion.get("destino") or None,
            "ctg": intencion.get("ctg") or None,
            "precio_flete_usd": float(intencion["monto_usd"]) if intencion.get("monto_usd") else None,
        }}
        # Faltan datos clave → mandar a menú guiado
        if not datos["flete"].get("toneladas") and not datos["flete"].get("patente_camion"):
            await kapso.enviar_texto(
                numero,
                "Necesito más datos del flete: cuántas toneladas, de qué cultivo, "
                "patente del camión, destino. Probá mandándolos juntos.")
            return True
        # Sin campo de origen → preguntar (así el flete queda atribuido al
        # campo correspondiente para la rentabilidad). Si el user tiene un
        # solo campo, _kapso_pedir_campo lo elige solo y sigue de largo.
        if not datos["flete"].get("campo_id"):
            tn = datos["flete"].get("toneladas")
            cultivo = datos["flete"].get("cultivo")
            pregunta = "¿De qué campo salió el cereal?"
            if tn and cultivo:
                pregunta = f"¿De qué campo salieron las {_num(tn)} tn de {cultivo}?"
            elif tn:
                pregunta = f"¿De qué campo salieron las {_num(tn)} tn?"
            if await _kapso_pedir_campo(ident, numero, "k_flete_campo", datos, pregunta):
                return True
            # Un solo campo: _kapso_pedir_campo seteó datos["campo_id"], lo
            # pasamos al dict del flete.
            datos["flete"]["campo_id"] = datos.get("campo_id")
            datos["flete"]["campo_nombre"] = datos.get("campo_nombre")
        await _kapso_guardar_flete(ident, numero, datos)
        return True

    # ── Rendimiento real ─────────────────────────────
    if accion == "rendimiento_real" and intencion.get("rendimiento_tha"):
        if not _wa_puede(ident, "gastos", "editar"):
            return False
        campo = _wa_resolver_campo_fuzzy(ident, intencion.get("_texto_crudo") or "",
                                        intencion.get("campo_id"))
        if not campo:
            await kapso.enviar_texto(numero, "¿De qué campo es el rendimiento?")
            return True
        datos = {"rendimiento": {
            "campo_id": campo["id"],
            "campo_nombre": campo["nombre"],
            "hectareas": campo.get("hectareas") or 0,
            "cultivo": intencion.get("cultivo") or "",
            "rendimiento_tha": float(intencion["rendimiento_tha"]),
        }}
        await _kapso_guardar_rendimiento(ident, numero, datos)
        return True

    # ── Gasto de estructura ──────────────────────────
    if accion == "gasto_estructura" and (
            intencion.get("monto_usd") is not None or intencion.get("monto_ars") is not None):
        if not _wa_puede(ident, "gastos", "editar"):
            return False
        if intencion.get("monto_usd") is not None:
            importe_usd = float(intencion["monto_usd"])
        else:
            ars = float(intencion["monto_ars"])
            tc = await _dolar_venta()
            importe_usd = round(ars / tc, 2)
        tipo = (intencion.get("rubro") or "otros").strip().lower()
        if tipo not in ("sueldo", "honorarios", "seguro", "oficina", "combustible", "otros"):
            tipo = "otros"
        frec = (intencion.get("frecuencia_pago") or "mensual").strip().lower()
        if frec not in ("mensual", "anual", "unico", "trimestral", "semestral"):
            frec = "mensual"
        datos = {"gasto_estructura": {
            "descripcion": intencion.get("descripcion") or "sin detalle",
            "tipo": tipo,
            "importe_usd": importe_usd,
            "frecuencia": frec,
            "fecha_inicio": intencion.get("fecha_venc") or datetime.now(AR_TZ).date().isoformat(),
        }}
        await _kapso_guardar_gasto_estructura(ident, numero, datos)
        return True

    # ── Análisis de suelo ────────────────────────────
    if accion == "analisis_suelo":
        if not _wa_puede(ident, "gastos", "editar"):
            return False
        campo = _wa_resolver_campo_fuzzy(ident, intencion.get("_texto_crudo") or "",
                                        intencion.get("campo_id"))
        if not campo:
            await kapso.enviar_texto(numero, "¿De qué campo es el análisis?")
            return True
        tiene_algun_valor = any(intencion.get(k) is not None for k in
                                ("n_total", "p_disponible", "k_suelo", "ph", "materia_organica"))
        if not tiene_algun_valor:
            await kapso.enviar_texto(
                numero,
                "Necesito al menos un valor del análisis. Probá: _N 0.15%, P 25, K 0.8, "
                "MO 3.2, pH 6.8_.")
            return True
        datos = {"analisis_suelo": {
            "campo_id": campo["id"],
            "campo_nombre": campo["nombre"],
            "fecha": intencion.get("fecha_venc"),
            "laboratorio": intencion.get("laboratorio"),
            "n_total": intencion.get("n_total"),
            "p_disponible": intencion.get("p_disponible"),
            "k": intencion.get("k_suelo"),  # mapeo: schema usa k_suelo, DB usa k
            "ph": intencion.get("ph"),
            "materia_org": intencion.get("materia_organica"),  # DB usa materia_org
        }}
        await _kapso_guardar_analisis_suelo(ident, numero, datos)
        return True

    # ── Cerrar campaña ───────────────────────────────
    if accion == "cerrar_campana":
        if not _wa_puede(ident, "gastos", "editar"):
            return False
        campo = _wa_resolver_campo_fuzzy(ident, intencion.get("_texto_crudo") or "",
                                        intencion.get("campo_id"))
        if not campo:
            await kapso.enviar_texto(numero, "¿Qué campaña cerrar? Decime el campo.")
            return True
        datos = {"campana": {
            "campo_id": campo["id"],
            "campo_nombre": campo["nombre"],
            "cultivo": intencion.get("cultivo") or "",
            "fecha_cosecha": intencion.get("fecha_venc") or datetime.now(AR_TZ).date().isoformat(),
        }}
        await _kapso_cerrar_campana(ident, numero, datos)
        return True

    # ── Activo amortizable ───────────────────────────
    if accion == "activo" and intencion.get("monto_usd"):
        if not _wa_puede(ident, "gastos", "editar"):
            return False
        tipo = (intencion.get("rubro") or "otros").strip().lower()
        if tipo not in ("vehiculo", "maquinaria", "herramienta", "construccion", "otros"):
            tipo = "otros"
        datos = {"activo": {
            "descripcion": intencion.get("descripcion") or "sin detalle",
            "tipo": tipo,
            "valor_compra_usd": float(intencion["monto_usd"]),
            "vida_util_anos": int(intencion.get("vida_util_anos") or 10),
            "fecha_compra": intencion.get("fecha_venc") or datetime.now(AR_TZ).date().isoformat(),
        }}
        await _kapso_guardar_activo(ident, numero, datos)
        return True

    if accion in ("completar_tarea", "iniciar_tarea", "nota_tarea"):
        tid = intencion.get("tarea_id")
        if not tid:
            await _kapso_tareas(ident, numero)
            return True
        if accion == "iniciar_tarea":
            await _kapso_tarea_iniciar(ident, numero, tid)
        elif accion == "nota_tarea":
            await _kapso_tarea_nota(ident, numero, tid, intencion.get("nota") or "")
        else:
            await _kapso_tarea_completar(ident, numero, tid, intencion.get("nota") or "")
        return True

    if accion == "lluvia" and intencion.get("mm") is not None:
        if not _wa_puede(ident, "lluvias", "editar"):
            return False
        # Resolver campo: Claude → fuzzy del texto → único del user.
        campo = _wa_resolver_campo_fuzzy(ident, intencion.get("_texto_crudo") or "",
                                        intencion.get("campo_id"))
        if campo is not None:
            await _kapso_confirmar_lluvia(numero, campo, float(intencion["mm"]),
                                           intencion.get("fecha_venc") or "")
            return True
        # No se pudo inferir: preguntar campo primero, después confirmar.
        datos = {"mm": float(intencion["mm"])}
        if intencion.get("fecha_venc"):
            datos["fecha"] = intencion["fecha_venc"]
        if await _kapso_pedir_campo(ident, numero, "k_lluvia_campo", datos,
                                    f"¿Dónde llovieron {_num(intencion['mm'])} mm?"):
            return True
        await _kapso_confirmar_lluvia(
            numero, {"id": datos["campo_id"], "nombre": datos["campo_nombre"]},
            float(intencion["mm"]), datos.get("fecha") or "")
        return True

    # Gasto desde texto libre. Dos formas:
    #   1. Un solo producto: los datos vienen al nivel de arriba (rubro,
    #      descripcion, cantidad, unidad, precio_unit, monto).
    #   2. Varios productos (típico de un audio con una pulverización):
    #      Claude los pone en items[]. El nivel de arriba queda null y acá
    #      iteramos por cada item.
    items = intencion.get("items") or []
    tiene_item_arriba = (intencion.get("monto_usd") is not None
                         or intencion.get("monto_ars") is not None
                         or intencion.get("precio_unit_usd") is not None
                         or intencion.get("precio_unit_ars") is not None)
    insumos_nombrados = _contar_insumos_mencionados(intencion.get("_texto_crudo") or "")
    print(f"[WA GASTO DBG] accion={accion} items_len={len(items)} "
          f"tiene_item_arriba={tiene_item_arriba} insumos_en_texto={insumos_nombrados} "
          f"campo_id_claude={intencion.get('campo_id')!r}")

    # FAIL-SAFE: si el texto nombra 2+ insumos distintos (típico pulverización
    # con varios químicos) pero Claude devolvió menos items en el array, NO
    # guardamos nada. Preferimos un mensaje claro al user "no pude leer todos"
    # antes que un gasto solo con el primer producto (caso real del 2026-11-23).
    if (accion == "gasto" and insumos_nombrados >= 2
            and len(items) < insumos_nombrados):
        print(f"[WA GASTO FAILSAFE] texto menciona {insumos_nombrados} insumos "
              f"pero Claude devolvió {len(items)} items. Aviso al user y no guardo.")
        await _wa_clear_conv(numero)
        texto_err = (
            f"🎤 Entendí que es una *pulverización* pero no pude leer bien los "
            f"{insumos_nombrados} productos del audio.\n\n"
            "Probá de una de estas formas:\n\n"
            "🅰️ Mandame un audio *más corto por producto*. Ejemplo:\n"
            "_Glifosato, 3 litros por hectárea, 2 dólares el litro._\n\n"
            "🅱️ O cargá cada uno por texto, uno por uno, con este formato:\n"
            "_cargar gasto de 50 litros de atrazina en La Escondida a 7 dólares el litro_\n\n"
            "Ya podés arrancar con el primero cuando quieras. 👍"
        )
        await kapso.enviar_texto(numero, texto_err)
        return True

    if accion == "gasto" and (items or tiene_item_arriba):
        if not _wa_puede(ident, "gastos", "editar"):
            return False

        campo = _wa_resolver_campo_fuzzy(ident, intencion.get("_texto_crudo") or "",
                                        intencion.get("campo_id"))
        print(f"[WA GASTO DBG] fuzzy_campo={(campo or {}).get('nombre')!r} "
              f"hectareas={(campo or {}).get('hectareas')}")
        tc_dolar = await _dolar_venta()

        # Lista final de items a procesar. Si no hay items[], tratamos el nivel
        # de arriba como un item único.
        lista_items = items if items else [{
            "rubro": intencion.get("rubro"),
            "descripcion": intencion.get("descripcion"),
            "cantidad": intencion.get("cantidad"),
            "unidad": intencion.get("unidad"),
            "precio_unit_usd": intencion.get("precio_unit_usd"),
            "precio_unit_ars": intencion.get("precio_unit_ars"),
            "monto_usd": intencion.get("monto_usd"),
            "monto_ars": intencion.get("monto_ars"),
            "por_hectarea": intencion.get("por_hectarea"),
            "momento_pulverizacion": intencion.get("momento_pulverizacion"),
        }]
        # Si viene momento_pulverizacion arriba, lo propagamos a todos los items
        # que no lo tengan ya (típico de una pulverización multi-producto donde
        # el usuario dice el momento una sola vez).
        mp_global = (intencion.get("momento_pulverizacion") or "").strip().lower()
        if items and mp_global:
            for it in lista_items:
                if not (it.get("momento_pulverizacion") or "").strip():
                    it["momento_pulverizacion"] = mp_global

        # Si algún item es "por hectárea" necesitamos saber el campo antes de
        # calcular. Si Claude no dio el campo y el fuzzy no lo encontró,
        # arrancamos el guiado pidiéndolo.
        necesita_campo = any(bool(it.get("por_hectarea")) for it in lista_items)
        if campo is None and necesita_campo:
            # Guardamos la intencion en el estado para seguir después
            datos = {"intencion_pendiente": intencion}
            if await _kapso_pedir_campo(ident, numero, "k_gasto_multi_campo", datos,
                                        "¿En qué campo fue la aplicación?"):
                return True
            # _kapso_pedir_campo devolvió False → no había campos cargados,
            # abortamos silencioso.
            return False

        gastos_calculados = []
        for it in lista_items:
            g = await _wa_armar_gasto(ident, campo, it, tc_dolar)
            if g is not None:
                gastos_calculados.append(g)

        if not gastos_calculados:
            # Ni el nivel de arriba ni ningún item pudo calcular total. Caemos
            # al menú guiado; no tiramos al legacy OpenAI.
            print(f"[WA GASTO IA] no pude armar ningún gasto de la intencion")
            return False

        if campo is None:
            # Sin campo todavía (y ningún item era por hectárea): guardamos
            # el primer gasto y pedimos el campo con el flujo single-item
            # existente, así no perdemos retrocompatibilidad.
            datos = {"gasto": gastos_calculados[0]}
            if len(gastos_calculados) > 1:
                datos["gastos_extra"] = gastos_calculados[1:]
            datos["confirmar"] = True
            if await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos,
                                        "¿A qué campo lo cargo?"):
                return True

        print(f"[WA GASTO IA] campo={campo.get('nombre')!r} items={len(gastos_calculados)} "
              f"totales={[g['total_de_usd'] for g in gastos_calculados]}")

        if len(gastos_calculados) == 1:
            datos = {"gasto": gastos_calculados[0],
                     "campo_id": campo["id"], "campo_nombre": campo.get("nombre")}
            await _kapso_confirmar_gasto(numero, datos)
        else:
            # NO guardar sin confirmación. El user ve los N items y decide.
            await _kapso_confirmar_multi(ident, numero, campo, gastos_calculados)
        return True

    if accion == "invitar":
        nombre = intencion.get("nombre_invitado") or ""
        rol = (intencion.get("rol_invitado") or "").strip().lower()
        # Si ya nombró el rol, lo saltamos directo a crear la invitación.
        if rol in ("administrador", "admin", "operario"):
            if not (ident.get("es_dueño") or ident.get("rol") == "administrador"):
                await kapso.enviar_texto(numero, "Solo el dueño puede sumar gente al equipo.")
                return True
            rol_ui = "administrador" if rol.startswith("admin") else "operario"
            await _kapso_invitar_crear(ident, numero, rol_ui, {"nombre": nombre})
            return True
        await _kapso_invitar_inicio(ident, numero, nombre)
        return True

    # ── Ingreso (venta de cereal) ────────────────────
    if accion == "ingreso" and intencion.get("toneladas") and intencion.get("precio_tn_usd"):
        if not _wa_puede(ident, "ingresos", "editar"):
            return False
        cultivo = (intencion.get("cultivo") or "").strip()
        datos = {"ingreso": {
            "tn": float(intencion["toneladas"]),
            "precio": float(intencion["precio_tn_usd"]),
        }}
        if intencion.get("comprador"):
            datos["ingreso"]["comprador"] = intencion["comprador"]

        # Resolver campo: Claude → fuzzy del texto → único del user → pedir.
        campo = _wa_resolver_campo_fuzzy(ident, intencion.get("_texto_crudo") or "",
                                        intencion.get("campo_id"))
        if campo is None:
            campos_user = ident.get("campos") or []
            if len(campos_user) == 1:
                campo = campos_user[0]

        if campo is None:
            # Preguntar campo al user (igual que antes)
            if await _kapso_pedir_campo(ident, numero, "k_ing_campo", datos,
                                        f"¿De qué campo sale la venta de *{_num(intencion['toneladas'])} tn*?"):
                return True
            campo = {"id": datos["campo_id"], "nombre": datos["campo_nombre"]}

        datos["ingreso"]["campo_id"] = campo["id"]
        datos["ingreso"]["campo_nombre"] = campo["nombre"]

        # Resolver cultivo: Claude → único del campo → pedir.
        # NUNCA guardamos bajo "otro": la app filtra por los cultivos reales del
        # campo y una venta bajo "otro" queda invisible (caso real 2026-11-24).
        # Además matcheamos contra cultivos ya existentes en el campo
        # (por nombre normalizado, ignorando acentos y mayúsculas) para no
        # crear keys duplicadas tipo "Maíz" vs "maiz" (caso real 2026-11-26).
        sembrados = await _wa_cultivos_del_campo(campo["id"])
        if cultivo:
            cultivo_norm = _norm_cultivo(cultivo)
            for s in sembrados:
                if _norm_cultivo(s) == cultivo_norm:
                    if s != cultivo:
                        print(f"[WA ING IA] normalizo cultivo {cultivo!r} → {s!r} "
                              f"(ya existe en {campo['nombre']!r})")
                    cultivo = s
                    break
        if not cultivo:
            if len(sembrados) == 1:
                cultivo = sembrados[0]
                print(f"[WA ING IA] auto-cultivo={cultivo!r} (único sembrado en {campo['nombre']!r})")
            elif len(sembrados) > 1:
                # Varios cultivos sembrados: el user tiene que elegir.
                await _wa_set_conv(numero, "k_ing_cultivo", datos)
                await _kapso_pedir_cultivo_del_campo(numero, campo["id"])
                return True
            else:
                # El campo no tiene cultivos cargados: pedir con lista genérica.
                await _wa_set_conv(numero, "k_ing_cultivo", datos)
                await _kapso_pedir_cultivo(numero)
                return True

        datos["ingreso"]["cultivo"] = cultivo
        print(f"[WA ING IA] campo={campo['nombre']!r} cultivo={cultivo!r} "
              f"tn={intencion['toneladas']} precio={intencion['precio_tn_usd']}")
        # Confirmación obligatoria antes de guardar (pedido user 2026-11-25
        # alineado con cómo FieldData maneja audios: siempre confirmar).
        await _kapso_confirmar_ingreso(numero, datos)
        return True

    # ── Pago (cheque/credito/otro) ───────────────────
    if accion == "pago" and (intencion.get("monto_usd") is not None
                             or intencion.get("monto_ars") is not None):
        if not _wa_puede(ident, "compromisos_pago", "editar"):
            return False
        tipo = (intencion.get("tipo_pago") or "otro").lower()
        if tipo not in ("cheque", "cuota_credito", "otro"):
            tipo = "otro"
        if intencion.get("monto_usd") is not None:
            importe = float(intencion["monto_usd"])
            moneda = "USD"
        else:
            importe = float(intencion["monto_ars"])
            moneda = "ARS"
        datos = {"pago": {
            "tipo": tipo,
            "beneficiario": intencion.get("beneficiario") or "",
            "importe": importe,
            "moneda": moneda,
        }}
        # Fecha: SI viene de Claude, la usamos; si NO, se la pedimos al user.
        # Nunca poner la fecha de hoy silenciosamente (caso real 2026-11-24:
        # cargó un cheque sin fecha y el bot le puso 24/11, el usuario no se
        # dio cuenta y quedó un cheque con cobro HOY cuando era otro día).
        if intencion.get("fecha_venc"):
            datos["pago"]["fecha"] = intencion["fecha_venc"]
        if intencion.get("descripcion"):
            datos["pago"]["descripcion"] = intencion["descripcion"]
        # Banco + número del cheque: si vinieron en el mismo mensaje, los
        # tomamos de una. Así el bot no vuelve a pedir lo que el user ya dijo
        # (caso real 2026-11-26: "cheque 5.400.000 a Agropecuaria del Sur,
        # Banco Macro número 876543" y el bot preguntaba banco y número igual).
        if tipo == "cheque":
            if intencion.get("banco"):
                datos["pago"]["banco"] = intencion["banco"][:100]
            if intencion.get("numero_cheque"):
                datos["pago"]["numero_cheque"] = intencion["numero_cheque"][:30]

        # Si es cheque pedimos lo que falta en el mismo orden del flujo guiado:
        # fecha → banco → número → alerta. El flujo k_pag_* ya encadena entre
        # estados hasta guardar (pedido del user 2026-11-24: "revisá todas las
        # secuencias que tienen mal la conversación").
        if tipo == "cheque":
            if not datos["pago"].get("fecha"):
                await _wa_set_conv(numero, "k_pag_fecha", datos)
                await kapso.enviar_texto(
                    numero,
                    "¿Cuándo se cobra el cheque? (ej: _15/12_, _próxima semana_, o _en 30 días_)")
                return True
            if not datos["pago"].get("banco"):
                await _wa_set_conv(numero, "k_pag_banco", datos)
                await kapso.enviar_texto(numero, "¿De qué banco es el cheque? "
                                                 "(opcional, escribí _sin banco_ para saltear)")
                return True
            if not datos["pago"].get("numero_cheque"):
                await _wa_set_conv(numero, "k_pag_numero", datos)
                await kapso.enviar_texto(numero, "¿Cuál es el número del cheque? "
                                                 "(opcional, escribí _sin número_ para saltear)")
                return True
            # Fecha + banco + número ya están: cerramos pidiendo sólo la alerta.
            # El paso de alerta es útil (recordatorio del vencimiento) y es el
            # último en el flujo guiado, así cerramos igual que k_pag_numero.
            await _wa_set_conv(numero, "k_pag_ch_alerta", datos)
            await kapso.enviar_texto(
                numero,
                "¿Querés que te avise antes del vencimiento? (ej: _7 días antes_, _15 días_, o _sin alerta_)")
            return True

        # Para pagos tipo "otro" o cuota_credito single: también pedir la
        # fecha si no viene, en vez de poner hoy silenciosamente.
        if tipo in ("otro", "cuota_credito") and not datos["pago"].get("fecha"):
            await _wa_set_conv(numero, "k_pag_fecha", datos)
            prompt = ("¿Cuándo vence?" if tipo == "cuota_credito" else "¿Cuándo es el pago?")
            await kapso.enviar_texto(numero, f"{prompt} (ej: _15/12_, _próxima semana_, _en 30 días_)")
            return True

        await _kapso_guardar_pago(ident, numero, datos)
        return True

    # ── Insumo al stock ─────────────────────────────
    # Insumo al stock — single o multi (varios productos en un solo mensaje).
    if accion == "insumo":
        if not _wa_puede(ident, "insumos", "editar"):
            return False
        items_insumo = intencion.get("items") or []

        def _norm_item_insumo(it: dict) -> dict | None:
            desc = (it.get("descripcion") or "").strip()
            cant_raw = it.get("cantidad")
            try:
                cant = float(cant_raw) if cant_raw is not None else 0.0
            except (TypeError, ValueError):
                cant = 0.0
            if not desc or cant <= 0:
                return None
            tipo = (it.get("rubro") or it.get("tipo_insumo") or "").strip().lower() or "otros"
            if tipo not in ("herbicidas", "fungicidas", "insecticidas",
                            "fertilizantes", "semillas", "otros"):
                tipo = "otros"
            unidad = (it.get("unidad") or "").strip().lower() or "unidades"
            if unidad not in ("litros", "kg", "dosis", "bolsas", "unidades"):
                unidad = "unidades"
            r = {"tipo": tipo, "nombre": desc[:150], "cantidad": cant, "unidad": unidad}
            if it.get("ubicacion"):
                r["ubicacion"] = str(it["ubicacion"])[:150]
            if it.get("precio_unit_usd"):
                r["costo_unitario"] = float(it["precio_unit_usd"])
            elif it.get("monto_usd"):
                r["costo_unitario"] = float(it["monto_usd"])
            return r

        normalizados: list[dict] = []
        for it in items_insumo:
            n = _norm_item_insumo(it)
            if n is not None:
                normalizados.append(n)

        if not normalizados:
            # Sin items[] válidos: pruebo con el nivel de arriba (modo single).
            if intencion.get("descripcion") and intencion.get("cantidad"):
                up = _norm_item_insumo({
                    "rubro": intencion.get("tipo_insumo"),
                    "descripcion": intencion.get("descripcion"),
                    "cantidad": intencion.get("cantidad"),
                    "unidad": intencion.get("unidad"),
                    "monto_usd": intencion.get("monto_usd"),
                })
                if up:
                    normalizados.append(up)

        if not normalizados:
            return False

        if len(normalizados) == 1:
            ins_uno = normalizados[0]
            # Si el user no dijo precio, pedírselo antes de guardar.
            # (pedido user 2026-11-24: "me lo tienen que preguntar el precio")
            if not ins_uno.get("costo_unitario"):
                await _wa_set_conv(numero, "k_ins_precio", {"insumo": ins_uno})
                unidad_sing = {"litros": "litro", "kg": "kilo", "dosis": "dosis",
                               "bolsas": "bolsa", "unidades": "unidad"}.get(
                                   ins_uno.get("unidad") or "unidades", "unidad")
                await kapso.enviar_texto(
                    numero,
                    f"¿A qué precio comprás el {unidad_sing} de *{ins_uno['nombre']}*?\n\n"
                    "Respondé con el número en USD (ej: _4_ o _4.50_).\n"
                    "Si no querés guardar el precio, escribí _saltar_ o _0_.")
                return True
            await _kapso_guardar_insumo(ident, numero, {"insumo": ins_uno})
            return True

        # Varios insumos: pedir confirmación antes de insertar N stock rows.
        print(f"[WA INS IA] items={len(normalizados)} tipos={[n['tipo'] for n in normalizados]}")
        await _kapso_confirmar_multi_insumo(ident, numero, normalizados)
        return True

    # ── Tarea nueva ─────────────────────────────────
    if accion == "tarea_nueva" and intencion.get("titulo_tarea"):
        if not _wa_puede(ident, "tareas", "editar"):
            return False
        datos = {"tarea_nueva": {
            "titulo": intencion["titulo_tarea"][:200],
        }}
        if intencion.get("descripcion"):
            datos["tarea_nueva"]["descripcion"] = intencion["descripcion"][:500]
        if intencion.get("prioridad") in ("alta", "media", "baja"):
            datos["tarea_nueva"]["prioridad"] = intencion["prioridad"]
        if intencion.get("fecha_venc"):
            datos["tarea_nueva"]["fecha"] = intencion["fecha_venc"]
        # Campo: Claude → fuzzy → único del user.
        campo = _wa_resolver_campo_fuzzy(ident, intencion.get("_texto_crudo") or "",
                                        intencion.get("campo_id"))
        if campo is not None:
            datos["tarea_nueva"]["campo_id"] = campo["id"]
            datos["tarea_nueva"]["campo_nombre"] = campo["nombre"]
        # Asignado: si Claude extrajo un nombre, lo matcheamos contra el
        # equipo del user. Mismo criterio que k_tar_asignado: match por
        # nombre normalizado, el primero que contenga o sea contenido por
        # el nombre pedido (caso real 2026-11-26: "asignada a Agustina"
        # creaba la tarea Sin asignar porque el fast path no miraba el
        # campo de asignado).
        nombre_asig = (intencion.get("asignado_nombre") or "").strip()
        if nombre_asig:
            equipo = await _wa_equipo_del_user(ident["owner_id"])
            n_pedido = _norm(nombre_asig)
            for m in equipo:
                nombre_norm = _norm(str(m.get("nombre_display") or ""))
                if not nombre_norm:
                    continue
                if nombre_norm == n_pedido or n_pedido in nombre_norm or nombre_norm in n_pedido:
                    datos["tarea_nueva"]["asignado_a"] = m["miembro_id"]
                    datos["tarea_nueva"]["asignado_nombre"] = m.get("nombre_display")
                    break
        await _kapso_guardar_tarea_nueva(ident, numero, datos)
        return True

    # ── Siembra (evento agrícola) → gasto de semillas con fecha ─────
    if accion == "siembra" and intencion.get("cultivo"):
        if not _wa_puede(ident, "gastos", "editar"):
            return False
        cultivo = intencion["cultivo"].strip().lower()
        fecha = intencion.get("fecha_siembra") or datetime.now(AR_TZ).date().isoformat()
        # Campo: Claude → fuzzy → único del user.
        campo = _wa_resolver_campo_fuzzy(ident, intencion.get("_texto_crudo") or "",
                                        intencion.get("campo_id"))
        if campo is None:
            datos = {"gasto": {
                "rubro": "Semillas",
                "descripcion": f"Siembra de {cultivo}",
                "cultivo": cultivo,
                "fecha": fecha,
                "total_de_usd": float(intencion.get("monto_usd") or 0),
            }, "confirmar": True}
            if await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos,
                                        f"¿En qué campo sembraste {cultivo}?"):
                return True
            campo = {"id": datos["campo_id"], "nombre": datos["campo_nombre"]}
        if not campo:
            return False
        cid = campo["id"]
        variedad = (intencion.get("variedad") or "").strip()
        desc = f"Siembra de {cultivo}" + (f" · variedad {variedad}" if variedad else "")
        payload = {
            "campo_id": campo["id"],
            "usuario_id": ident["owner_id"],
            "rubro": "semillas",
            "fecha": fecha,
            "total_de_usd": float(intencion.get("monto_usd") or 0),
            "descripcion": desc,
            "cultivo": cultivo,
        }
        await _sb_post("gastos", payload)
        # Si viene variedad, además actualizamos la campaña del año para
        # dejarla registrada en campanas (donde la app la lee).
        if variedad:
            anio = datetime.now(AR_TZ).year
            camps = await _sb_get("campanas", {
                "campo_id": f"eq.{campo['id']}", "anio": f"eq.{anio}",
                "select": "id", "limit": "1"}) or []
            if camps:
                await _sb_patch(f"campanas?id=eq.{camps[0]['id']}",
                                {"variedad": variedad, "fecha_siembra": fecha,
                                 "cultivo": cultivo})
            else:
                await _sb_post("campanas", {
                    "campo_id": campo["id"], "usuario_id": ident["owner_id"],
                    "anio": anio, "cultivo": cultivo, "variedad": variedad,
                    "fecha_siembra": fecha})
        await _wa_clear_conv(numero)
        var_line = f"\n🌱 Variedad: *{variedad}*" if variedad else ""
        await kapso.enviar_texto(
            numero,
            f"🌱 Siembra registrada en *{campo['nombre']}*\n"
            f"Cultivo: *{cultivo.capitalize()}*{var_line}\n"
            f"📅 {_wa_fecha_corta(fecha)}\n\n"
            f"_El evento queda en la timeline del campo (tab Cultivo)._"
        )
        return True

    return False


async def _kapso_tarea_nota(ident: dict, numero: str, tarea_id: str, nota: str) -> None:
    """Deja un comentario en una tarea sin cambiarle el estado."""
    if not nota:
        await kapso.enviar_texto(numero, "¿Qué querés que anote?")
        return
    await _sb_patch(f"tareas?id=eq.{tarea_id}", {"comentario_completado": nota[:1000]})
    await kapso.enviar_texto(numero, f"Anotado 📝\n\n_{nota[:200]}_")


# Frases con las que alguien da por terminada una tarea, sin pasar por Claude.
_RE_TERMINE = re.compile(
    r"\b(termin[eé]|termin[oó]|terminad[ao]|complet[eé]|completad[ao]|"
    r"list[oa]|ya est[aá]|hech[ao]|finalic[eé]|acab[eé]|la hice|lo hice)\b")
_RE_EMPECE = re.compile(r"\b(empec[eé]|empez[oó]|arranqu[eé]|comenc[eé]|estoy haciendo|en eso)\b")
_RE_ANOTAR = re.compile(r"\b(anot[aá]|apunt[aá]|agreg[aá]|not[aá]|coment[aá])\w*\s+(que\s+)?")


def _extraer_nota(texto: str) -> str:
    """Saca el comentario de frases tipo 'la terminé, anotá que sobró medio bidón'."""
    m = _RE_ANOTAR.search(texto)
    if m:
        return texto[m.end():].strip(" .,:")
    # "la terminé, sobró medio bidón" → lo que va después de la coma
    if "," in texto:
        cola = texto.split(",", 1)[1].strip(" .,:")
        if len(cola) > 3:
            return cola
    return ""

# ── Router principal ──────────────────────────

# Con el número de sandbox solo escribían los cinco de siempre. Con un número
# de producción escribe cualquiera: equivocados, curiosos, spam. A cada uno le
# contestamos una vez y después lo dejamos pasar, así no le mandamos el mismo
# texto veinte veces ni pagamos veinte conversaciones.
_WA_DESCONOCIDOS: dict = {}          # número → time.monotonic() del último aviso
_WA_DESCONOCIDO_SILENCIO = 6 * 3600  # segundos hasta volver a contestarle
_WA_DESCONOCIDOS_MAX = 2000


def _wa_corresponde_avisar(numero: str) -> bool:
    """True si a este número desconocido le toca el mensaje de bienvenida."""
    ahora = time.monotonic()
    # Ojo con el default: time.monotonic() arranca cerca de cero cuando el
    # contenedor se acaba de levantar, así que un `.get(numero, 0.0)` haría que
    # durante las primeras 6 horas de cada deploy nadie nuevo reciba respuesta.
    ultimo = _WA_DESCONOCIDOS.get(numero)
    if ultimo is not None and ahora - ultimo < _WA_DESCONOCIDO_SILENCIO:
        return False
    if len(_WA_DESCONOCIDOS) >= _WA_DESCONOCIDOS_MAX:
        # Se descarta el cuarto más viejo en vez de vaciar todo: si no, alcanza
        # con inundarlo de números nuevos para que vuelva a contestarle a todos.
        viejos = sorted(_WA_DESCONOCIDOS.items(), key=lambda kv: kv[1])
        for k, _ in viejos[:_WA_DESCONOCIDOS_MAX // 4]:
            _WA_DESCONOCIDOS.pop(k, None)
    _WA_DESCONOCIDOS[numero] = ahora
    return True


async def _kapso_procesar(m) -> None:
    numero = m.desde
    if not numero:
        return
    try:
        await kapso.marcar_leido(m.id, escribiendo=True)
    except Exception:
        pass

    ident = await _wa_identificar(numero)
    asyncio.create_task(_wa_log_mensaje(ident, numero, m))
    if not ident:
        # El mensaje viejo daba por hecho que ya tenían cuenta. En producción
        # escribe también gente que todavía no se registró, así que cubrimos
        # los tres casos posibles en vez de mandarlos a un menú que no ven.
        if _wa_corresponde_avisar(numero):
            await kapso.enviar_texto(
                numero,
                "¡Hola! Soy el asistente de *Rinde Agro* 🌾\n\n"
                "No encontré ninguna cuenta con este número.\n\n"
                f"• ¿Todavía no tenés cuenta? Creála en {APP_URL} y volvé a escribirme.\n"
                "• ¿Ya tenés cuenta? Entrá a *Mi Plan* y cargá este número en tu "
                "perfil para que te reconozca.\n"
                "• ¿Te sumaron a un equipo? Pedile al dueño de la cuenta que cargue "
                "tu número en *Mi equipo*.",
            )
        else:
            print(f"[KAPSO←] {numero} desconocido y ya avisado, no contesto")
        return

    accion = m.accion or ""
    texto = (m.texto or "").strip()
    n = _norm(texto)

    # STOP / ACTIVAR
    if n in STOP_WORDS:
        await _sb_patch(f"wa_preferencias_notificaciones?user_id=eq.{ident['user_id']}", {"activo": False})
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(
            numero,
            "Listo, no te mando más avisos.\n\nEscribí *ACTIVAR* cuando quieras volver.",
        )
        return
    if n in ACTIVAR_WORDS:
        await _sb_patch(f"wa_preferencias_notificaciones?user_id=eq.{ident['user_id']}", {"activo": True})
        await kapso.enviar_texto(numero, "Avisos reactivados 🌾")
        return

    # Botones para elegir el tipo de un adjunto cuando no pudimos detectar.
    if accion in ("adj_factura", "adj_cheque", "adj_cp"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_adjunto_tipo":
            await kapso.enviar_texto(numero, "Ya no tengo ese archivo en memoria.")
            await _wa_clear_conv(numero)
            return
        # Bajamos el archivo del bucket temporal
        path = datos_tmp.get("path")
        mime = datos_tmp.get("mime") or "image/jpeg"
        nombre = datos_tmp.get("nombre") or "adjunto"
        contenido = None
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(f"{_sb_url()}/storage/v1/object/insumos-facturas/{path}",
                                headers={"Authorization": f"Bearer {_sb_key()}"})
                if r.status_code == 200:
                    contenido = r.content
        except Exception as e:
            print(f"[ADJ] error leyendo {path}: {e}")
        if not contenido:
            await _wa_clear_conv(numero)
            await kapso.enviar_texto(numero, "No pude recuperar el archivo. Mandámelo de nuevo.")
            return
        await _wa_clear_conv(numero)
        if accion == "adj_factura":
            # Reusa el flujo existente de factura (que hace OCR + guarda gasto)
            # Necesitamos un m simulado. Simplificamos: llamamos al OCR directo
            # y guardamos como factura genérica adjunta si no hay items.
            items = await _ocr_factura(contenido, mime)
            if not items:
                datos = {"gasto": {"factura_url":
                                    f"{_sb_url()}/storage/v1/object/public/insumos-facturas/{path}",
                                    "factura_path": path, "factura_tipo": "factura"}}
                if not await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos,
                                                "No pude leer los renglones. "
                                                "¿A qué campo cargamos el gasto?"):
                    await _wa_set_conv(numero, "k_gasto_rubro", datos)
                    await _kapso_pedir_rubro(numero)
                return
            await kapso.enviar_texto(numero, f"🧾 Leí {len(items)} renglones de la factura. "
                                              "Seguimos en la app o mandame el detalle acá.")
            return
        if accion == "adj_cheque":
            await _kapso_procesar_ocr_pago(ident, numero, contenido, mime, nombre, path, "cheque")
            return
        if accion == "adj_cp":
            await _kapso_procesar_ocr_cp(ident, numero, contenido, mime, nombre, path)
            return

    # Botones de confirmación de OCR (carta porte / pago / estructura / activo)
    if accion in ("cp_ok", "cp_cancel"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_cp_confirmar":
            await kapso.enviar_texto(numero, "Ya no tengo esa carta de porte en memoria.")
            await _wa_clear_conv(numero)
            return
        if accion == "cp_ok":
            await _kapso_guardar_flete(ident, numero, datos_tmp)
        else:
            await _wa_clear_conv(numero)
            await kapso.enviar_texto(numero, "Listo, no cargué el flete. 👌")
        return

    if accion in ("pago_ok", "pago_cancel"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_pago_confirmar":
            await kapso.enviar_texto(numero, "Ya no tengo ese pago en memoria.")
            await _wa_clear_conv(numero)
            return
        if accion == "pago_ok":
            if datos_tmp.get("pago"):
                await _kapso_guardar_pago(ident, numero, datos_tmp)
            elif datos_tmp.get("pago_credito"):
                # Mapeamos a la estructura que espera _kapso_guardar_pago_credito
                pc = datos_tmp["pago_credito"]
                await _kapso_guardar_pago_credito(ident, numero, {"pago": {
                    "tipo": "cuota_credito",
                    "descripcion": pc.get("descripcion"),
                    "beneficiario": pc.get("beneficiario"),
                    "importe_cuota": pc.get("cuota_importe"),
                    "moneda": pc.get("moneda"),
                    "cantidad_cuotas": pc.get("n_cuotas"),
                    "frecuencia": pc.get("frecuencia"),
                    "fecha_primera": pc.get("fecha_primera"),
                }})
        else:
            await _wa_clear_conv(numero)
            await kapso.enviar_texto(numero, "Listo, no cargué el pago. 👌")
        return

    if accion in ("estr_ok", "estr_cancel"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_estr_confirmar":
            await kapso.enviar_texto(numero, "Ya no tengo ese gasto en memoria.")
            await _wa_clear_conv(numero)
            return
        if accion == "estr_ok":
            await _kapso_guardar_gasto_estructura(
                ident, numero, {"gasto_estructura": datos_tmp.get("gasto_estructura_ocr")})
        else:
            await _wa_clear_conv(numero)
            await kapso.enviar_texto(numero, "Listo, no cargué el gasto. 👌")
        return

    if accion in ("act_ok", "act_cancel"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_act_confirmar":
            await kapso.enviar_texto(numero, "Ya no tengo ese activo en memoria.")
            await _wa_clear_conv(numero)
            return
        if accion == "act_ok":
            await _kapso_guardar_activo(
                ident, numero, {"activo": datos_tmp.get("activo_ocr")})
        else:
            await _wa_clear_conv(numero)
            await kapso.enviar_texto(numero, "Listo, no cargué el activo. 👌")
        return

    # Botones del flujo de modificación
    if accion in ("mod_ok", "mod_cancel"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_mod_confirmar":
            await kapso.enviar_texto(numero, "Ya no tengo ese cambio en memoria.")
            await _wa_clear_conv(numero)
            return
        if accion == "mod_ok":
            await _kapso_ejecutar_modificar(ident, numero, datos_tmp)
            return
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(numero, "Listo, no cambié nada. 👌")
        return

    # Botones del flujo de eliminación
    if accion in ("elim_ok", "elim_cancel"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_elim_confirmar":
            await kapso.enviar_texto(numero, "Ya no tengo ese registro en memoria.")
            await _wa_clear_conv(numero)
            return
        if accion == "elim_ok":
            await _kapso_ejecutar_eliminar(ident, numero, datos_tmp.get("registro") or {})
            return
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(numero, "Listo, no borré nada. 👌")
        return

    # Botones del flujo de confirmación de ingreso
    if accion in ("ing_ok", "ing_cancel"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_ing_confirmar":
            await kapso.enviar_texto(numero, "Ya no tengo esa venta en memoria. "
                                              "Mandala de nuevo si querés cargarla.")
            await _wa_clear_conv(numero)
            return
        if accion == "ing_ok":
            await _kapso_guardar_ingreso(ident, numero, datos_tmp)
            return
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(numero, "Listo, no cargué la venta. 👌")
        return

    # Botones del flujo de confirmación de lluvia
    if accion in ("llu_ok", "llu_cancel"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_lluvia_confirmar":
            await kapso.enviar_texto(numero, "Ya no tengo esa lluvia en memoria.")
            await _wa_clear_conv(numero)
            return
        if accion == "llu_ok":
            campo = {"id": datos_tmp.get("campo_id"), "nombre": datos_tmp.get("campo_nombre")}
            mm = float(datos_tmp.get("mm") or 0)
            await _kapso_guardar_lluvia(ident, numero, campo, mm)
            return
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(numero, "Listo, no cargué la lluvia. 👌")
        return

    # Botones del flujo multi-insumo (análogo a multi-gasto)
    if accion in ("i_multi_ok", "i_multi_cancel"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_ins_multi_confirmar":
            await kapso.enviar_texto(numero, "Ya no tengo esos insumos en memoria. "
                                              "Mandalos de nuevo si querés cargarlos.")
            await _wa_clear_conv(numero)
            return
        if accion == "i_multi_ok":
            await _kapso_guardar_multi_insumo(ident, numero, datos_tmp)
            return
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(numero, "Listo, no cargué nada. 👌")
        return

    # Botones del flujo multi-gasto: SIEMPRE antes de cualquier otro check de
    # conv/estado, así no se pierden si ya hay una conv activa.
    if accion in ("g_multi_ok", "g_multi_fix", "g_multi_cancel"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_gasto_multi_confirmar":
            await kapso.enviar_texto(numero, "Ya no tengo esa pulverización en memoria. "
                                              "Mandá el audio de nuevo si querés cargarla.")
            await _wa_clear_conv(numero)
            return
        if accion == "g_multi_ok":
            await _kapso_guardar_multi_definitivo(ident, numero, datos_tmp)
            return
        if accion == "g_multi_cancel":
            await _wa_clear_conv(numero)
            await kapso.enviar_texto(numero, "Listo, no cargué nada. 👌")
            return
        # g_multi_fix: lista los items para que el user elija cuál corregir.
        items_tmp = (datos_tmp.get("multi_gasto") or {}).get("items") or []
        if not items_tmp:
            await _wa_clear_conv(numero)
            await _kapso_menu(ident, numero)
            return
        filas = []
        for i, it in enumerate(items_tmp, 1):
            lbl = (it.get("descripcion") or "sin detalle")[:24]
            sub = f"USD {_num(it.get('total_de_usd') or 0)}"
            filas.append((f"g_multi_edit:{i-1}", lbl, sub))
        filas.append(("g_multi_ok", "✅ Cargar como está", "Cancela la edición"))
        await kapso.enviar_lista(numero, "¿Cuál corregís?", "Elegir item", filas, "Items")
        return
    if accion.startswith("g_multi_edit:"):
        conv_tmp = await _wa_get_conv(numero)
        datos_tmp = (conv_tmp or {}).get("datos_parciales") or {}
        if (conv_tmp or {}).get("estado") != "k_gasto_multi_confirmar":
            await kapso.enviar_texto(numero, "Ya no tengo esa pulverización en memoria.")
            await _wa_clear_conv(numero)
            return
        try:
            idx = int(accion.split(":", 1)[1])
        except (ValueError, IndexError):
            idx = -1
        items_tmp = (datos_tmp.get("multi_gasto") or {}).get("items") or []
        if idx < 0 or idx >= len(items_tmp):
            await kapso.enviar_texto(numero, "No reconocí ese item.")
            return
        datos_tmp["_editing_idx"] = idx
        await _wa_set_conv(numero, "k_gasto_multi_edit_texto", datos_tmp)
        nombre_actual = items_tmp[idx].get("descripcion") or "ese item"
        await kapso.enviar_texto(
            numero,
            f"Dale, contame cómo va *{nombre_actual}*. "
            f"Mandame un audio o texto con los datos corregidos "
            f"(nombre, cantidad, unidad, precio por litro / bolsa).",
        )
        return

    # Botón "No recibir más" de las plantillas: igual que escribir STOP.
    if accion == "stop":
        await _sb_patch(f"wa_preferencias_notificaciones?user_id=eq.{ident['user_id']}", {"activo": False})
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(
            numero,
            "Listo, no te mando más avisos.\n\nEscribí *ACTIVAR* cuando quieras volver.",
        )
        return
    if accion == "resumen":
        await _wa_clear_conv(numero)
        if ident.get("es_dueño") or ident.get("rol") == "administrador":
            await _kapso_ver_resumen(ident, numero)
        else:
            await _kapso_menu(ident, numero)
        return

    # Botones de plantilla (tipo "button") → menú completo, no atajo.
    # Meta sólo deja 3 botones por plantilla, así que los que vienen del
    # recordatorio diario (Cargar gasto / Registrar lluvia / Ver tareas) no
    # cubren las 6 opciones del bot. El user pidió que al tocarlos vea el
    # menú entero, no que vaya directo al flujo recortado.
    if m.tipo == "button" and accion in ("gasto", "lluvia", "tareas"):
        await _wa_clear_conv(numero)
        await _kapso_menu(ident, numero)
        return

    # Botones y listas que valen en cualquier momento
    if accion == "menu" or n in SALUDOS_WA:
        await _wa_clear_conv(numero)
        await _kapso_menu(ident, numero)
        return
    if accion == "tareas":
        await _wa_clear_conv(numero)
        await _kapso_tareas(ident, numero)
        return
    if accion == "gasto":
        await _wa_clear_conv(numero)
        await _kapso_gasto_guiado(ident, numero)
        return
    if accion == "lluvia":
        # Primero los mm y después el campo: al revés habría que arrastrar el
        # campo elegido por dos estados para nada. Flag guiado=True para pedir
        # fecha/detalles antes de guardar (los flujos por texto libre guardan
        # directo con defaults).
        await _wa_set_conv(numero, "k_lluvia_mm", {"guiado": True})
        await kapso.enviar_texto(numero, "¿Cuántos mm?")
        return
    if accion == "insumo":
        await _wa_clear_conv(numero)
        await _kapso_insumo_guiado(ident, numero)
        return
    if accion == "tarea_nueva":
        await _wa_clear_conv(numero)
        await _kapso_tarea_nueva_guiado(ident, numero)
        return
    if accion == "ingreso":
        await _wa_clear_conv(numero)
        await _kapso_ingreso_guiado(ident, numero)
        return
    if accion == "pago":
        await _wa_clear_conv(numero)
        await _kapso_pago_guiado(ident, numero)
        return
    if accion.startswith("t:"):
        await _wa_clear_conv(numero)
        await _kapso_tarea_detalle(ident, numero, accion.split(":", 1)[1])
        return
    if accion.startswith("t_ini:"):
        await _kapso_tarea_iniciar(ident, numero, accion.split(":", 1)[1])
        return
    if accion.startswith("t_fin:"):
        await _kapso_tarea_completar(ident, numero, accion.split(":", 1)[1])
        return

    conv = await _wa_get_conv(numero)

    # Si el texto parece ser una nueva intención completa (audio con "cargar
    # X, aplicar X, precios/cantidades"), descartamos el estado pendiente.
    # Esto evita que una conv en curso "¿a qué campo lo cargo?" atrape un
    # audio completamente nuevo del user (caso real 2026-11-23: audio con 3
    # productos cayó como respuesta a un "¿qué campo?" viejo y cargó basura).
    # EXCEPCIÓN: estados de edición interactiva (k_gasto_multi_*) son
    # intencionales: el user está corrigiendo un item del resumen y el audio
    # largo es esperado — no descartamos.
    estados_edicion_activa = {"k_gasto_multi_edit_texto", "k_gasto_multi_confirmar",
                              "k_gasto_multi_campo", "k_ins_multi_confirmar",
                              "k_ing_confirmar", "k_lluvia_confirmar",
                              "k_elim_confirmar", "k_mod_confirmar",
                              "k_tarea_adjunto",
                              "k_adjunto_tipo", "k_cp_confirmar",
                              "k_pago_confirmar", "k_estr_confirmar",
                              "k_act_confirmar"}
    if (conv and texto and m.tipo in ("text", "audio")
            and conv.get("estado") not in estados_edicion_activa
            and _texto_es_nueva_intencion(texto)):
        print(f"[WA] descarto estado '{conv.get('estado')}' — llegó nueva intención en texto largo")
        await _wa_clear_conv(numero)
        conv = None

    # Un adjunto sin conversación en curso: router decide qué OCR usar según
    # el caption del archivo (si hay) y llama a la edge function correcta.
    if m.tiene_media and not conv:
        await _kapso_adjunto_router(ident, numero, m)
        return

    if conv:
        await _kapso_flujo(ident, numero, m, conv)
        return

    # "1".."6" (con o sin emoji) → ejecutan la acción del menú correspondiente,
    # pero solo cuando NO hay conv activa: si estás esperando "¿cuántos mm?",
    # "5" es un valor para el flujo, no una elección del menú. El orden es el
    # mismo que _kapso_menu respeta por permisos.
    if texto:
        n_num = _norm(texto).strip()
        for i, emj in enumerate(["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"], 1):
            n_num = n_num.replace(emj, str(i))
        if n_num in ("1", "2", "3", "4", "5", "6"):
            # MISMO orden que _kapso_menu: ingreso, gasto, insumo, pago, tarea, lluvia.
            # Si el orden de allá cambia, hay que mover éste a la par.
            orden = [
                ("ingreso", "ingresos", "editar"),
                ("gasto", "gastos", "editar"),
                ("insumo", "insumos", "editar"),
                ("pago", "compromisos_pago", "editar"),
                ("tarea_nueva", "tareas", "editar"),
                ("lluvia", "lluvias", "editar"),
            ]
            opciones = [a for a, mod, niv in orden if _wa_puede(ident, mod, niv)]
            idx = int(n_num) - 1
            if 0 <= idx < len(opciones):
                elegida = opciones[idx]
                if elegida == "gasto":
                    await _kapso_gasto_guiado(ident, numero)
                elif elegida == "lluvia":
                    await _wa_set_conv(numero, "k_lluvia_mm", {})
                    await kapso.enviar_texto(numero, "¿Cuántos mm?")
                elif elegida == "insumo":
                    await _kapso_insumo_guiado(ident, numero)
                elif elegida == "tarea_nueva":
                    await _kapso_tarea_nueva_guiado(ident, numero)
                elif elegida == "ingreso":
                    await _kapso_ingreso_guiado(ident, numero)
                elif elegida == "pago":
                    await _kapso_pago_guiado(ident, numero)
                return

    if m.compartido_numero:
        await kapso.enviar_texto(numero, "Para sumar a alguien al equipo escribime *invitar* y su nombre.")
        return

    if not texto:
        await _kapso_menu(ident, numero)
        return

    await _kapso_texto_libre(ident, numero, texto)


# ── Webhook ───────────────────────────────────

@app.post("/whatsapp/kapso")
async def kapso_webhook(request: Request):
    """
    Recibe los mensajes de WhatsApp desde Kapso.

    Hay que contestar 200 en menos de 10 segundos o Kapso reintenta, así que
    el procesamiento va en background y la respuesta sale enseguida.
    """
    body_crudo = await request.body()
    # Un webhook de tipo 'kapso' firma en X-Webhook-Signature; uno de tipo
    # 'meta' usa X-Hub-Signature-256. Aceptamos los dos para que el webhook que
    # se cree para el número de producción funcione sea del tipo que sea.
    firma = (request.headers.get("x-webhook-signature", "")
             or request.headers.get("x-hub-signature-256", ""))
    if not kapso.firma_valida(body_crudo, firma):
        print("[KAPSO←] firma inválida, descartado")
        raise HTTPException(status_code=401, detail="firma inválida")

    idem = request.headers.get("x-idempotency-key", "")
    if idem:
        if idem in _KAPSO_IDEM_VISTOS:
            return {"ok": True, "duplicado": True}
        _KAPSO_IDEM_VISTOS.append(idem)
        if len(_KAPSO_IDEM_VISTOS) > _KAPSO_IDEM_MAX:
            del _KAPSO_IDEM_VISTOS[:len(_KAPSO_IDEM_VISTOS) - _KAPSO_IDEM_MAX]

    evento = request.headers.get("x-webhook-event", "")
    try:
        body = json.loads(body_crudo)
    except Exception:
        return {"ok": True, "ignorado": "json inválido"}

    mensajes = kapso.parsear_entrantes(body)
    print(f"[KAPSO←] evento={evento} mensajes={len(mensajes)}")
    for m in mensajes:
        print(f"[KAPSO←] de={m.desde} tipo={m.tipo} texto={m.texto[:500]!r} accion={m.accion!r}")
        asyncio.create_task(_kapso_procesar(m))

    return {"ok": True, "recibidos": len(mensajes)}


@app.post("/dev/intencion")
async def dev_intencion(request: Request):
    """
    Endpoint de pruebas: simula lo que haría _kapso_procesar para un mensaje de
    texto o audio ya transcripto, SIN guardar nada en la base ni mandar nada por
    WhatsApp. Devuelve la intención cruda de Claude + un simulacro de lo que el
    bot guardaría. Protegido con X-Dev-Secret contra DEV_SECRET del env.
    """
    secret = os.environ.get("DEV_SECRET", "")
    if not secret or request.headers.get("x-dev-secret", "") != secret:
        raise HTTPException(status_code=401, detail="forbidden")
    body = await request.json()
    texto = (body.get("texto") or "").strip()
    numero = body.get("numero") or ""
    if not texto or not numero:
        raise HTTPException(status_code=400, detail="faltan texto o numero")

    ident = await _wa_identificar(kapso.normalizar_numero(numero))
    if not ident:
        return {"ok": False, "motivo": "número no vinculado", "numero": numero}

    tareas = await _wa_tareas_pendientes(ident)
    intencion = await _interpretar_con_claude(texto, ident, tareas, None)

    simulacro = None
    if intencion and intencion.get("accion") == "gasto":
        monto_usd = intencion.get("monto_usd")
        monto_ars = intencion.get("monto_ars")
        p_unit_usd = intencion.get("precio_unit_usd")
        p_unit_ars = intencion.get("precio_unit_ars")
        cantidad = intencion.get("cantidad")
        usd = None
        if monto_usd is not None:
            usd = float(monto_usd)
        elif monto_ars is not None:
            tc = await _dolar_venta()
            usd = round(float(monto_ars) / tc, 2)
        elif p_unit_usd is not None and cantidad is not None:
            usd = round(float(p_unit_usd) * float(cantidad), 2)
        elif p_unit_ars is not None and cantidad is not None:
            tc = await _dolar_venta()
            usd = round(float(p_unit_ars) * float(cantidad) / tc, 2)
        simulacro = {
            "rubro": (intencion.get("rubro") or "Otros").capitalize(),
            "descripcion": intencion.get("descripcion"),
            "total_de_usd_calculado": usd,
            "cantidad": cantidad,
            "unidad": intencion.get("unidad"),
            "precio_unit_usd": p_unit_usd,
            "precio_unit_ars": p_unit_ars,
            "campo_id": intencion.get("campo_id"),
        }
    return {"ok": True, "texto": texto, "intencion": intencion, "simulacro_gasto": simulacro}


@app.get("/whatsapp/kapso/estado")
async def kapso_estado(numeros: bool = False):
    """
    Diagnóstico rápido: ¿está todo configurado?

    Con ?numeros=1 además consulta la Platform API de Kapso y lista los números
    conectados al proyecto. Es la forma de verificar el cambio a producción sin
    adivinar: el `phone_number_id` del número que quieras usar es exactamente lo
    que tiene que valer KAPSO_PHONE_NUMBER_ID.
    """
    pnid = os.environ.get("KAPSO_PHONE_NUMBER_ID", "")
    salida = {
        "api_key": bool(os.environ.get("KAPSO_API_KEY")),
        "phone_number_id": pnid or None,
        "webhook_secret": bool(os.environ.get("KAPSO_WEBHOOK_SECRET")),
        "supabase": bool(_sb_url() and _sb_key()),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "app_url": APP_URL,
        "directorio_wa": {"numeros_indexados": len(_WA_DIR),
                          "armado_hace_seg": round(time.monotonic() - _WA_DIR_TS)
                          if _WA_DIR_TS else None},
    }
    if not salida["webhook_secret"]:
        salida["alerta"] = ("KAPSO_WEBHOOK_SECRET sin configurar: el webhook "
                            "acepta cualquier POST. Obligatorio en producción.")

    if numeros:
        conectados = await kapso.listar_numeros()
        salida["numeros_conectados"] = [
            {
                "phone_number_id": n.get("phone_number_id") or n.get("id"),
                "display_phone_number": n.get("display_phone_number"),
                "display_name": n.get("display_name") or n.get("verified_name"),
                "status": n.get("status"),
                "quality_rating": n.get("quality_rating"),
                "business_account_id": n.get("business_account_id"),
                "en_uso": str(n.get("phone_number_id") or n.get("id") or "") == pnid,
            }
            for n in conectados if isinstance(n, dict)
        ]
        # Las plantillas de los avisos automáticos: hasta que estén APPROVED,
        # los recordatorios solo llegan a quien escribió en las últimas 24 h.
        estados = {p.get("name"): p.get("status") for p in await kapso.listar_plantillas()}
        salida["plantillas"] = {n: estados.get(n, "NO_CREADA") for n in plantillas_wa.PLANTILLAS}
    return salida


# ══════════════════════════════════════════════
# PRECIOS CEREALES — scraping BCR Rosario
# ══════════════════════════════════════════════

async def scrape_cereales(bna: float = 1385):
    """Obtiene precios pizarra Rosario desde Agrofy en ARS → convierte a USD/t."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        for url in [
            "https://news.agrofy.com.ar/granos/precios-pizarra",
            "https://news.agrofy.com.ar/granos/mercado-fisico",
        ]:
            try:
                r = await client.get(url, headers=headers)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                texto = soup.get_text(" ", strip=True)
                print(f"Agrofy ({url[-20:]}) snippet: {texto[:600]}")
                mapeo = {
                    "soja":    ["soja"],
                    "maiz":    ["maíz", "maiz"],
                    "trigo":   ["trigo"],
                    "girasol": ["girasol"],
                    "sorgo":   ["sorgo"],
                }
                encontrados = {}
                import re as _re
                for cereal, aliases in mapeo.items():
                    for alias in aliases:
                        pat = alias + r"[^0-9]{0,60}?(\d{2,3}[\.,]\d{3})"
                        m = _re.search(pat, texto.lower())
                        if m:
                            raw = m.group(1).replace(".", "").replace(",", "")
                            val_pesos = float(raw)
                            if val_pesos > 50000:
                                val_usd = round(val_pesos / bna, 1)
                                if 80 < val_usd < 700:
                                    encontrados[cereal] = val_usd
                                    break
                if len(encontrados) >= 3:
                    print(f"Encontrados (pesos→USD): {encontrados}")
                    return encontrados, "Pizarra Rosario"
            except Exception as e:
                print(f"Agrofy error {url}: {e}")
    return None, None


def parsear_agrofy(data):
    encontrados = {}
    mapeo = {"soja": ["soja"], "maiz": ["maiz", "maíz"], "trigo": ["trigo"], "girasol": ["girasol"], "sorgo": ["sorgo"]}
    items = data if isinstance(data, list) else data.get("data", data.get("items", data.get("precios", [])))
    if isinstance(items, list):
        for item in items:
            nombre = (item.get("nombre") or item.get("cereal") or item.get("grano") or "").lower()
            precio = item.get("precio") or item.get("usd") or item.get("valor")
            if precio:
                for key, aliases in mapeo.items():
                    if any(a in nombre for a in aliases):
                        val = float(precio)
                        if 100 < val < 600:
                            encontrados[key] = round(val, 2)
    return encontrados


def parsear_tabla_agrofy(soup):
    encontrados = {}
    mapeo = {"soja": ["soja"], "maiz": ["maiz", "maíz"], "trigo": ["trigo"], "girasol": ["girasol"], "sorgo": ["sorgo"]}
    tablas = soup.find_all("table")
    for tabla in tablas:
        for fila in tabla.find_all("tr"):
            celdas = fila.find_all(["td", "th"])
            if len(celdas) >= 2:
                nombre = celdas[0].get_text(strip=True).lower()
                for key, aliases in mapeo.items():
                    if any(a in nombre for a in aliases):
                        for celda in celdas[1:]:
                            txt = celda.get_text(strip=True).replace(",", ".").replace("$", "").replace("USD", "").strip()
                            try:
                                val = float(txt)
                                if 100 < val < 600:
                                    encontrados[key] = round(val, 2)
                                    break
                            except Exception:
                                continue
    return encontrados


async def fetch_dolar_bna():
    """Obtiene el dólar divisa tipo comprador BNA."""
    async with httpx.AsyncClient(timeout=8) as client:
        try:
            r = await client.get("https://dolarapi.com/v1/dolares/mayorista")
            if r.status_code == 200:
                j = r.json()
                val = j.get("compra") or j.get("venta")
                if val and float(val) > 100:
                    return round(float(val), 2), "BNA divisa comprador"
        except Exception as e:
            print(f"Dolar mayorista error: {e}")
        try:
            r = await client.get("https://api.argentinadatos.com/v1/cotizaciones/dolar/mayorista")
            if r.status_code == 200:
                j = r.json()
                if isinstance(j, list) and j:
                    j = j[-1]
                val = j.get("compra") or j.get("venta")
                if val and float(val) > 100:
                    return round(float(val), 2), "BNA divisa comprador"
        except Exception as e:
            print(f"Dolar argentinadatos error: {e}")
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            r = await client.get("https://www.bna.com.ar/Personas", headers=headers, timeout=10)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                for row in soup.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) >= 3:
                        texto = cells[0].get_text(strip=True).lower()
                        if "divisa" in texto and ("dolar" in texto or "u.s.a" in texto):
                            compra = cells[1].get_text(strip=True).replace(",", ".")
                            try:
                                val = float(compra)
                                if val > 100:
                                    return round(val, 2), "BNA divisa comprador"
                            except Exception:
                                pass
        except Exception as e:
            print(f"Scraping BNA error: {e}")
    return None, None


# ══════════════════════════════════════════════
# ENDPOINT: GET /precios
# ══════════════════════════════════════════════

@app.get("/precios")
async def get_precios():
    global cache_precios
    ahora  = datetime.now()
    ultima = cache_precios.get("ultima_actualizacion")
    necesita_refresh = ultima is None or (ahora - ultima).total_seconds() > 3600

    if necesita_refresh:
        bna, _ = await fetch_dolar_bna()
        bna_val = bna if bna else cache_precios["bna"]
        cereales, fuente_c = await scrape_cereales(bna=bna_val)
        if cereales:
            cache_precios["cereales"].update(cereales)
            cache_precios["fuente"] = fuente_c or "Pizarra Rosario"
        if bna:
            cache_precios["bna"] = bna
        cache_precios["ultima_actualizacion"] = ahora
        # Disparar alertas de precio en segundo plano
        asyncio.create_task(_wa_check_alertas_precio())

    return {
        "ok": True,
        "cereales": cache_precios["cereales"],
        "bna": cache_precios["bna"],
        "fuente": cache_precios["fuente"],
        "actualizado": cache_precios["ultima_actualizacion"].isoformat() if cache_precios["ultima_actualizacion"] else None,
        "fecha": date.today().isoformat(),
    }


# ── Audio / PDF / IA ──────────────────────────


async def interpretar_con_ia(texto: str, usuario: dict, api_key: str) -> dict:
    campos_nombres = [c["nombre"] for c in usuario.get("campos", [])]
    prompt_sistema = f"""Sos un asistente agrícola argentino. Extraé datos del mensaje del productor.
Campos disponibles: {', '.join(campos_nombres) if campos_nombres else 'ninguno registrado'}.
Respondé SOLO con JSON válido, sin explicaciones. Formato:
{{
  "tipo": "gasto|lluvia|rendimiento",
  "campo_nombre": "nombre exacto del campo o null",
  "datos": {{
    "rubro": "", "descripcion": "", "total_usd": 0,
    "mm": 0, "rendimiento_tha": 0, "precio_usd_t": 0, "fecha": null
  }},
  "confianza": "alta|media|baja",
  "respuesta_usuario": "mensaje corto confirmando lo que entendiste"
}}"""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": prompt_sistema},
                        {"role": "user", "content": texto},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 400,
                },
            )
            if r.status_code == 200:
                contenido = r.json()["choices"][0]["message"]["content"]
                contenido = re.sub(r"```json|```", "", contenido).strip()
                return json.loads(contenido)
    except Exception as e:
        print(f"IA error: {e}")
    return None


# ══════════════════════════════════════════════
# WHATSAPP — NOTIFICACIONES AUTOMÁTICAS (scheduler)
# ══════════════════════════════════════════════

async def _wa_get_destinatarios(flag: str) -> list[dict]:
    """
    Devuelve lista de {user_id, numero_whatsapp, nombre} de usuarios que tienen
    la preferencia `flag` activada y activo=true, con WhatsApp vinculado.
    """
    prefs = await _sb_get(
        "wa_preferencias_notificaciones",
        {flag: "eq.true", "activo": "eq.true", "select": "user_id"},
    )
    if not prefs:
        return []

    destinatarios = []
    for p in prefs:
        uid = p.get("user_id")
        if not uid:
            continue
        rows = await _sb_get("perfiles", {"id": f"eq.{uid}", "select": "id,nombre,telefono"})
        if rows and rows[0].get("telefono"):
            destinatarios.append({
                "user_id": uid,
                "numero":  rows[0]["telefono"],
                "nombre":  rows[0].get("nombre", ""),
            })
    return destinatarios


async def _wa_aviso(numero: str, plantilla: str, parametros: list, texto: str,
                    botones: list) -> bool:
    """
    Manda un mensaje que arrancamos nosotros (no es respuesta a nada).

    Primero intenta con la plantilla aprobada por Meta, que es lo único que se
    puede mandar fuera de la ventana de 24 horas. Si falla (la plantilla
    todavía no está aprobada, o la rechazaron), cae a texto con botones: eso
    solo llega si la persona nos escribió en las últimas 24 horas, pero es
    mejor que no mandar nada mientras se aprueban las plantillas.

    botones: [(id, título)] — el id es lo que vuelve en el webhook cuando lo
    tocan, igual en la plantilla y en el fallback, así el flujo es uno solo.
    """
    r = await kapso.enviar_plantilla(
        numero, plantilla, parametros=parametros,
        botones_payload=[b[0] for b in botones],
    )
    if r:
        return True
    print(f"[WA aviso] plantilla '{plantilla}' falló para {numero}, pruebo texto")
    r = await kapso.enviar_botones(numero, texto, botones[:kapso.MAX_BOTONES])
    return bool(r)


def _wa_nombre_corto(nombre: str) -> str:
    return (nombre or "").strip().split(" ")[0] or "¿cómo va?"


async def _wa_recordatorio_operarios():
    """Job 1 — L/M/V 17:30 AR: recordatorio para operarios."""
    print("[SCHEDULER] Recordatorio operarios")
    plantilla = plantillas_wa.PLANTILLAS["recordatorio_operario"]
    for d in await _wa_get_destinatarios("recordatorio_operario"):
        nombre = _wa_nombre_corto(d["nombre"])
        await _wa_aviso(
            d["numero"], "recordatorio_operario", [nombre],
            plantilla["texto"].replace("{{1}}", nombre),
            plantilla["botones"],
        )
        await asyncio.sleep(0.3)


async def _wa_recordatorio_admins():
    """Job 2 — V 12:30 AR: recordatorio para administradores."""
    print("[SCHEDULER] Recordatorio admins")
    plantilla = plantillas_wa.PLANTILLAS["recordatorio_admin"]
    for d in await _wa_get_destinatarios("recordatorio_admin"):
        nombre = _wa_nombre_corto(d["nombre"])
        await _wa_aviso(
            d["numero"], "recordatorio_admin", [nombre],
            plantilla["texto"].replace("{{1}}", nombre),
            plantilla["botones"],
        )
        await asyncio.sleep(0.3)


def _wa_rango_semana(hoy: date = None) -> tuple:
    """(lunes de esta semana, hoy, texto legible '17 al 22 de nov')."""
    hoy = hoy or datetime.now(AR_TZ).date()
    lunes = hoy - timedelta(days=hoy.weekday())
    meses = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]
    return lunes, hoy, f"{lunes.day} al {hoy.day} de {meses[hoy.month - 1]}"


async def _wa_armar_resumen(uid: str) -> dict | None:
    """
    Calcula el resumen de la semana de un dueño de cuenta.

    Devuelve {"texto": detalle completo, "rango", "gastos_usd", "lluvia_mm"} o
    None si no se encontró el perfil. El detalle se manda recién cuando la
    persona toca "Ver resumen" en la plantilla: la plantilla de Meta no admite
    saltos de línea en las variables, así que el resumen largo no entra ahí.
    """
    lunes, hoy, rango = _wa_rango_semana()
    desde = lunes.isoformat()

    perfiles = await _sb_get("perfiles", {"id": f"eq.{uid}", "select": "nombre,campos(id,nombre)"})
    if not perfiles:
        return None
    perfil = perfiles[0]
    campos = perfil.get("campos") or []

    lineas_lluvias, total_mm = [], 0.0
    total_gastos, rubros = 0.0, {}
    for campo in campos:
        lluvias = await _sb_get("lluvias", {
            "campo_id": f"eq.{campo['id']}", "fecha": f"gte.{desde}", "select": "mm",
        })
        if lluvias:
            mm = sum(float(l.get("mm") or 0) for l in lluvias)
            total_mm += mm
            lineas_lluvias.append(f"{campo['nombre']}: {mm:.0f} mm")

        gastos = await _sb_get("gastos", {
            "campo_id": f"eq.{campo['id']}", "fecha": f"gte.{desde}",
            "select": "total_de_usd,rubro",
        })
        for g in gastos:
            monto = float(g.get("total_de_usd") or 0)
            rubro = g.get("rubro") or "Otros"
            total_gastos += monto
            rubros[rubro] = rubros.get(rubro, 0) + monto

    # La tabla tareas no tiene `completada` (boolean): el estado va en `estado`
    # y la fecha en `completada_at`, igual que en _kapso_tarea_completar.
    tareas_ok = await _sb_get("tareas", {
        "owner_id": f"eq.{uid}",
        "estado": "eq.completada",
        "completada_at": f"gte.{desde}T00:00:00",
        "select": "id",
    })

    lineas = [f"📊 *Resumen semanal — {perfil.get('nombre') or 'Rinde Agro'}*",
              f"Semana del {rango}", ""]
    lineas.append("🌧️ *Lluvias*")
    lineas.extend(lineas_lluvias or ["Sin registros esta semana"])
    lineas.append("")
    lineas.append("💰 *Gastos registrados*")
    if total_gastos > 0:
        lineas.append(f"Total: USD {total_gastos:,.0f}")
        for rubro, monto in sorted(rubros.items(), key=lambda x: -x[1]):
            lineas.append(f"{rubro}: USD {monto:,.0f}")
    else:
        lineas.append("Sin gastos registrados")
    lineas.append("")
    lineas.append("✅ *Tareas completadas*")
    n = len(tareas_ok)
    lineas.append(f"{n} tarea{'s' if n != 1 else ''} lista{'s' if n != 1 else ''}"
                  if n else "Ninguna completada esta semana")
    lineas.append("")
    precios = cache_precios["cereales"]
    lineas.append("📈 *Precios hoy*")
    lineas.append(f"Soja: USD {precios.get('soja', '-')}/t")
    lineas.append(f"Maíz: USD {precios.get('maiz', '-')}/t")
    lineas.append(f"Trigo: USD {precios.get('trigo', '-')}/t")
    lineas.append("")
    lineas.append("Que tengas una buena semana 🌾")

    return {
        "texto": "\n".join(lineas),
        "rango": rango,
        "gastos_usd": total_gastos,
        "lluvia_mm": total_mm,
    }


async def _wa_resumen_semanal():
    """Job 3 — Sábados 9:00 AR: aviso de resumen semanal para cada admin."""
    print("[SCHEDULER] Resumen semanal")
    plantilla = plantillas_wa.PLANTILLAS["resumen_semanal"]
    for d in await _wa_get_destinatarios("resumen_semanal"):
        resumen = await _wa_armar_resumen(d["user_id"])
        if not resumen:
            continue
        params = [
            _wa_nombre_corto(d["nombre"]),
            resumen["rango"],
            f"USD {resumen['gastos_usd']:,.0f}",
            f"{resumen['lluvia_mm']:.0f} mm",
        ]
        texto = plantilla["texto"]
        for i, v in enumerate(params, 1):
            texto = texto.replace(f"{{{{{i}}}}}", v)
        await _wa_aviso(d["numero"], "resumen_semanal", params, texto, plantilla["botones"])
        await asyncio.sleep(0.5)


async def _kapso_ver_resumen(ident: dict, numero: str) -> None:
    """Cuando tocan 'Ver resumen': el detalle completo, ya dentro de la ventana de 24h."""
    resumen = await _wa_armar_resumen(ident["owner_id"])
    if not resumen:
        await kapso.enviar_texto(numero, "No pude armar el resumen ahora. Probá en un rato.")
        return
    await kapso.enviar_botones(numero, resumen["texto"], [("menu", "Cargar algo")])


async def _wa_check_alertas_precio():
    """
    Job 4 — Se llama automáticamente cada vez que se actualizan los precios.
    Si la soja cambió más de 2% desde la última alerta, notifica a quienes
    tienen alerta_precio=true y activo=true.
    """
    precio_actual = cache_precios["cereales"].get("soja")
    if not precio_actual:
        return

    ultimo = cache_precios.get("ultimo_precio_alerta", {}).get("soja")
    if not ultimo:
        # Primera lectura desde que arrancó el server: queda como referencia.
        # Antes alertaba en cada deploy, sin que nada se hubiera movido.
        cache_precios.setdefault("ultimo_precio_alerta", {})["soja"] = precio_actual
        return
    cambio_pct = (precio_actual - ultimo) / ultimo * 100
    if abs(cambio_pct) < 2:
        return

    cache_precios["ultimo_precio_alerta"]["soja"] = precio_actual

    plantilla = plantillas_wa.PLANTILLAS["alerta_precio"]
    cambio = f"{'subió' if cambio_pct > 0 else 'bajó'} {abs(cambio_pct):.1f}%"
    for d in await _wa_get_destinatarios("alerta_precio"):
        params = [_wa_nombre_corto(d["nombre"]), cambio, f"{precio_actual:,.0f}"]
        texto = plantilla["texto"]
        for i, v in enumerate(params, 1):
            texto = texto.replace(f"{{{{{i}}}}}", v)
        await _wa_aviso(d["numero"], "alerta_precio", params, texto, plantilla["botones"])
        await asyncio.sleep(0.3)


# ══════════════════════════════════════════════
# MERCADO PAGO — SUSCRIPCIONES
# ══════════════════════════════════════════════

PLANES = {
    "lote":        {"nombre": "Lote",        "precio_usd": 29,  "precio_usd_anual": 278,  "descripcion": "Hasta 5 campos · Todos los módulos · WhatsApp"},
    "agronomo":    {"nombre": "Agrónomo",     "precio_usd": 36,  "precio_usd_anual": 346,  "descripcion": "20 productores · Panel multi-productor"},
    "corporativo": {"nombre": "Corporativo",  "precio_usd": 45,  "precio_usd_anual": 432,  "descripcion": "Campos ilimitados · 5 usuarios"},
}


@app.post("/mp/crear-suscripcion")
async def crear_suscripcion(request: Request):
    MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
    SERVER_URL      = os.environ.get("SERVER_URL", "https://rindeagro-server-production.up.railway.app")

    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN no configurado")

    body     = await request.json()
    plan_id  = body.get("plan")
    usuario_id = body.get("usuario_id")
    email    = body.get("email")
    es_anual = body.get("anual", False)
    cuotas   = int(body.get("cuotas", 1))
    if not es_anual: cuotas = 1
    cuotas = max(1, min(12, cuotas))

    if plan_id not in PLANES:
        raise HTTPException(status_code=400, detail="Plan inválido")

    plan = PLANES[plan_id]
    bna, _ = await fetch_dolar_bna()
    bna_val = bna if bna else cache_precios["bna"]

    INTERESES = {1:0, 2:0, 3:0, 4:10, 5:14, 6:18, 7:22, 8:26, 9:30, 10:34, 11:38, 12:42}
    interes = INTERESES.get(cuotas, 0) if es_anual else 0

    if es_anual:
        precio_usd = plan["precio_usd_anual"]
        razon = f"Rinde Agro · Plan {plan['nombre']} Anual"
    else:
        precio_usd = plan["precio_usd"]
        razon = f"Rinde Agro · Plan {plan['nombre']} Mensual"

    precio_ars_base = round(precio_usd * bna_val)
    precio_ars      = round(precio_ars_base * (1 + interes / 100))

    async with httpx.AsyncClient(timeout=15) as client:
        if es_anual:
            r = await client.post(
                "https://api.mercadopago.com/checkout/preferences",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"},
                json={
                    "items": [{"title": razon, "quantity": 1, "unit_price": float(precio_ars), "currency_id": "ARS"}],
                    "payer": {"email": email} if email else {},
                    "external_reference": f"{usuario_id}|{plan_id}|anual|{cuotas}c",
                    "back_urls": {
                        "success": "https://juanignaciomanterola.github.io/Rindeagro",
                        "failure": "https://juanignaciomanterola.github.io/Rindeagro",
                        "pending": "https://juanignaciomanterola.github.io/Rindeagro",
                    },
                    "auto_return": "approved",
                    "payment_methods": {"installments": cuotas, "default_installments": cuotas},
                },
            )
        else:
            r = await client.post(
                "https://api.mercadopago.com/preapproval_plan",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"},
                json={
                    "reason": razon,
                    "external_reference": f"{usuario_id}|{plan_id}|mensual",
                    "auto_recurring": {
                        "frequency": 1, "frequency_type": "months",
                        "transaction_amount": precio_ars, "currency_id": "ARS",
                    },
                    "back_url": "https://juanignaciomanterola.github.io/Rindeagro",
                    "notification_url": f"{SERVER_URL}/mp/webhook",
                    "payment_methods_allowed": {
                        "payment_types": [{"id": "credit_card"}, {"id": "debit_card"}]
                    },
                },
            )

        print(f"MP response {r.status_code}: {r.text[:300]}")
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=500, detail=f"Error MP: {r.text}")

        data = r.json()
        init_point = data.get("init_point") or data.get("sandbox_init_point")
        if not init_point:
            raise HTTPException(status_code=500, detail="MP no devolvió URL de pago")

        return {
            "ok": True, "init_point": init_point, "plan": plan_id,
            "precio_usd": precio_usd, "precio_ars": precio_ars,
            "bna": bna_val, "anual": es_anual, "cuotas": cuotas,
        }


@app.post("/mp/webhook")
async def mp_webhook(request: Request):
    MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
    body = await request.json()
    print(f"[MP Webhook] {json.dumps(body)}")

    tipo    = body.get("type")
    data_id = body.get("data", {}).get("id")

    if tipo == "subscription_preapproval" and data_id:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.mercadopago.com/preapproval/{data_id}",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            )
            if r.status_code == 200:
                sus    = r.json()
                estado = sus.get("status")
                ref    = sus.get("external_reference", "")
                if "|" in ref:
                    usuario_id, plan_id = ref.split("|", 1)
                    await _sb_patch(
                        f"perfiles?id=eq.{usuario_id}",
                        {
                            "plan": plan_id if estado == "authorized" else "semilla",
                            "suscripcion_mp_id": data_id,
                            "suscripcion_estado": estado,
                        },
                    )
                    print(f"[MP] Usuario {usuario_id} → plan {plan_id} ({estado})")

    return {"ok": True}


@app.get("/mp/planes")
async def get_planes():
    bna, _ = await fetch_dolar_bna()
    bna_val = bna if bna else cache_precios["bna"]
    planes_con_ars = {
        id: {
            **plan,
            "precio_ars":        round(plan["precio_usd"] * bna_val),
            "precio_ars_anual":  round(plan["precio_usd_anual"] * bna_val),
            "bna": bna_val,
        }
        for id, plan in PLANES.items()
    }
    return {"ok": True, "planes": planes_con_ars, "bna": bna_val}


# ══════════════════════════════════════════════
# ENDPOINT: GET /health
# ══════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "scheduler": scheduler.running,
    }


@app.get("/wa/test")
async def wa_test():
    """
    Diagnóstico rápido de las tablas de WhatsApp en Supabase.
    Llama a GET /wa/test para ver si las tablas existen y están accesibles.
    """
    resultados = {}

    # Test wa_conversaciones
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.get(
            f"{_sb_url()}/rest/v1/wa_conversaciones",
            headers=_sb_headers(),
            params={"limit": "1"},
        )
        resultados["wa_conversaciones"] = {
            "status": r.status_code,
            "ok": r.status_code == 200,
            "detalle": r.json() if r.status_code != 200 else f"{len(r.json())} registros",
        }

    # Test wa_preferencias_notificaciones
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.get(
            f"{_sb_url()}/rest/v1/wa_preferencias_notificaciones",
            headers=_sb_headers(),
            params={"limit": "1"},
        )
        resultados["wa_preferencias_notificaciones"] = {
            "status": r.status_code,
            "ok": r.status_code == 200,
            "detalle": r.json() if r.status_code != 200 else f"{len(r.json())} registros",
        }

    # Test escritura en wa_conversaciones (upsert de un número ficticio)
    test_payload = {
        "numero_whatsapp": "test_diagnostico_borrar",
        "estado": "completado",
        "datos_parciales": {},
        "actualizado_en": datetime.utcnow().isoformat(),
    }
    h = _sb_headers({"Prefer": "resolution=merge-duplicates,return=representation"})
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.post(
            f"{_sb_url()}/rest/v1/wa_conversaciones",
            headers=h,
            json=test_payload,
            params={"on_conflict": "numero_whatsapp"},
        )
        resultados["wa_conversaciones_escritura"] = {
            "status": r.status_code,
            "ok": r.status_code in (200, 201),
            "detalle": r.json() if r.status_code not in (200, 201) else "ok",
        }
        # Limpiar el registro de prueba
        if r.status_code in (200, 201):
            await c.delete(
                f"{_sb_url()}/rest/v1/wa_conversaciones",
                headers=_sb_headers(),
                params={"numero_whatsapp": "eq.test_diagnostico_borrar"},
            )

    todo_ok = all(v["ok"] for v in resultados.values())
    return {"ok": todo_ok, "supabase_url": _sb_url()[:40] + "...", "tablas": resultados}


# ══════════════════════════════════════════════
# STARTUP — inicializar scheduler
# ══════════════════════════════════════════════

@app.on_event("startup")
async def startup_event():
    # Job 1 — Recordatorio operarios: lunes, miércoles y viernes 17:30 AR
    scheduler.add_job(
        _wa_recordatorio_operarios,
        CronTrigger(day_of_week="mon,wed,fri", hour=17, minute=30, timezone=AR_TZ),
        id="recordatorio_operarios", replace_existing=True,
    )
    # Job 2 — Recordatorio admins: viernes 12:30 AR
    scheduler.add_job(
        _wa_recordatorio_admins,
        CronTrigger(day_of_week="fri", hour=12, minute=30, timezone=AR_TZ),
        id="recordatorio_admins", replace_existing=True,
    )
    # Job 3 — Resumen semanal: sábados 9:00 AR
    scheduler.add_job(
        _wa_resumen_semanal,
        CronTrigger(day_of_week="sat", hour=9, minute=0, timezone=AR_TZ),
        id="resumen_semanal", replace_existing=True,
    )
    scheduler.start()
    print("[SCHEDULER] Iniciado con 3 jobs activos")

    if kapso.configurado() and not os.environ.get("KAPSO_WEBHOOK_SECRET"):
        print("[KAPSO] ⚠️  KAPSO_WEBHOOK_SECRET sin configurar: /whatsapp/kapso "
              "acepta cualquier POST. Configuralo antes de salir a producción.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
