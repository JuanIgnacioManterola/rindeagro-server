from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from datetime import datetime, date, timedelta
import os
import json
import re
import time
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


async def _kapso_guardar_insumo(ident: dict, numero: str, datos: dict) -> None:
    ins = datos.get("insumo", {})
    cantidad = float(ins.get("cantidad") or 0)
    unidad = ins.get("unidad") or "unidades"
    tipo = ins.get("tipo") or "otros"
    payload = {
        "owner_id": ident["owner_id"],
        "nombre": ins.get("nombre") or "Sin nombre",
        "tipo": tipo,
        "unidad": unidad,
        "stock_actual": cantidad,
        "ubicacion": ins.get("ubicacion") or None,
    }
    if ins.get("costo_unitario"):
        payload["costo_unitario"] = float(ins["costo_unitario"])
    if ins.get("notas"):
        payload["notas"] = ins["notas"][:500]
    await _sb_post("stock_insumos", payload)
    await _wa_clear_conv(numero)
    tipo_label = next((v for k, v in TIPOS_INSUMO if k == tipo), tipo.capitalize())
    partes = [f"✅ Agregaste *{_num(cantidad)} {unidad}* de *{ins.get('nombre')}* al stock.",
              f"📦 Rubro: {tipo_label}"]
    if ins.get("ubicacion"):
        partes.append(f"📍 {ins['ubicacion']}")
    if ins.get("costo_unitario"):
        partes.append(f"💰 USD {_num(ins['costo_unitario'])} por {unidad[:-1] if unidad.endswith('s') else unidad}")
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
    await _sb_post("tareas", payload)
    await _wa_clear_conv(numero)
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
    await kapso.enviar_texto(numero, "\n".join(partes))


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


async def _kapso_texto_libre(ident: dict, numero: str, texto: str,
                             tarea_activa: dict = None, saltar_parecidos: bool = False) -> None:
    """
    Intenta resolver el mensaje sin menú: primero lluvia (que es un patrón
    clarísimo), después la IA si está configurada, y si no, el menú.
    """
    n = _norm(texto)

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

    if any(p in n for p in ("invitar", "sumar a", "agregar a", "dar de alta")):
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

    # Ver tareas
    if _wa_puede(ident, "tareas") and _RE_VER_TAREAS.search(n) and len(n) < 60:
        await _kapso_tareas(ident, numero)
        return

    # Gasto dicho en criollo, sin IA: "gasté 1200 de urea en La Esperanza"
    if _wa_puede(ident, "gastos", "editar"):
        parseado = _parsear_gasto(texto, ident.get("campos") or [])
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
    if os.environ.get("ANTHROPIC_API_KEY"):
        tareas = await _wa_tareas_pendientes(ident)
        intencion = await _interpretar_con_claude(texto, ident, tareas, tarea_activa)
        if intencion and await _kapso_aplicar_intencion(ident, numero, intencion):
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
        unidad_singular = unidad[:-1] if unidad.endswith("s") else unidad
        await kapso.enviar_texto(
            numero,
            f"¿Cuánto cuesta por {unidad_singular}? (ej: _USD 3.5_ por litro, o escribí _sin costo_)")
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
        # Pedir campo (opcional)
        campos = ident.get("campos") or []
        if not campos:
            await _kapso_guardar_tarea_nueva(ident, numero, datos)
            return
        if len(campos) == 1:
            datos["tarea_nueva"]["campo_id"] = campos[0]["id"]
            datos["tarea_nueva"]["campo_nombre"] = campos[0]["nombre"]
            await _kapso_guardar_tarea_nueva(ident, numero, datos)
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
        await _wa_set_conv(numero, "k_tar_prioridad", datos)
        filas = [(f"tar_prio:{k}", v) for k, v in PRIORIDADES_TAREA]
        filas.append(("tar_prio:media", "Dejarla en media por default"))
        await kapso.enviar_lista(
            numero, "¿Qué prioridad tiene?", "Elegir prioridad", filas[:6], "Prioridad")
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

MODELO_CLAUDE = os.environ.get("CLAUDE_MODEL", "claude-opus-5")

# Todo lo que el bot sabe hacer. Claude elige una acción y completa los datos.
ESQUEMA_INTENCION = {
    "type": "object",
    "properties": {
        "accion": {
            "type": "string",
            "enum": ["completar_tarea", "iniciar_tarea", "nota_tarea", "ver_tareas",
                     "gasto", "lluvia", "invitar", "menu", "nada",
                     # Nuevas intenciones:
                     "ingreso", "pago", "insumo", "tarea_nueva", "siembra"],
        },
        "tarea_id":        {"type": ["string", "null"]},
        "nota":            {"type": ["string", "null"]},
        "campo_id":        {"type": ["string", "null"]},
        "rubro":           {"type": ["string", "null"]},
        "monto_usd":       {"type": ["number", "null"]},
        "monto_ars":       {"type": ["number", "null"]},
        "descripcion":     {"type": ["string", "null"]},
        "mm":              {"type": ["number", "null"]},
        "nombre_invitado": {"type": ["string", "null"]},
        # Campos nuevos: ingreso (venta de cereal)
        "cultivo":         {"type": ["string", "null"]},
        "toneladas":       {"type": ["number", "null"]},
        "precio_tn_usd":   {"type": ["number", "null"]},
        "comprador":       {"type": ["string", "null"]},
        # Pago
        "tipo_pago":       {"type": ["string", "null"]},
        "beneficiario":    {"type": ["string", "null"]},
        "fecha_venc":      {"type": ["string", "null"]},
        # Insumo
        "tipo_insumo":     {"type": ["string", "null"]},
        "cantidad":        {"type": ["number", "null"]},
        "unidad":          {"type": ["string", "null"]},
        # Tarea nueva
        "titulo_tarea":    {"type": ["string", "null"]},
        "prioridad":       {"type": ["string", "null"]},
        # Siembra (evento agrícola)
        "fecha_siembra":   {"type": ["string", "null"]},
        "confianza":       {"type": "string", "enum": ["alta", "media", "baja"]},
    },
    "required": ["accion", "tarea_id", "nota", "campo_id", "rubro", "monto_usd", "monto_ars",
                 "descripcion", "mm", "nombre_invitado",
                 "cultivo", "toneladas", "precio_tn_usd", "comprador",
                 "tipo_pago", "beneficiario", "fecha_venc",
                 "tipo_insumo", "cantidad", "unidad",
                 "titulo_tarea", "prioridad", "fecha_siembra",
                 "confianza"],
    "additionalProperties": False,
}

INSTRUCCIONES_CLAUDE = """Sos el asistente de Rinde Agro, una app de gestión agropecuaria argentina, \
atendiendo por WhatsApp a productores y a sus empleados de campo.

Tu trabajo es leer un mensaje escrito como habla la gente en el campo y decidir qué acción corresponde.

Acciones:
- completar_tarea: dice que terminó/hizo algo. Poné el tarea_id de la lista. Si además comenta algo \
("la terminé, sobró medio bidón"), ese comentario va en nota.
- iniciar_tarea: dice que arrancó o está haciendo algo.
- nota_tarea: quiere dejar un comentario sobre una tarea sin darla por terminada.
- ver_tareas: pregunta qué tiene pendiente.
- gasto: compró o pagó algo del campo. monto_usd en dólares, rubro entre herbicidas, fungicidas, \
insecticidas, fertilizantes, semillas, laboreo, flete, empleados, arrendamiento, riego, otros. \
descripcion es el producto. Si menciona cantidad y unidad ("30 litros de glifosato"), ponelas en \
cantidad y unidad.
- lluvia: llovió. mm es la cantidad. Si menciona fecha ("ayer", "15/12"), ponela en fecha_venc.
- ingreso: vendió cereal. cultivo (soja/maiz/trigo/girasol/sorgo), toneladas, precio_tn_usd, \
comprador (si menciona), campo_id si lo indica.
- pago: cheque, crédito u otro compromiso. tipo_pago entre cheque, cuota_credito, otro. \
beneficiario, monto_usd o monto_ars, fecha_venc (vencimiento o cobro).
- insumo: cargó al stock un producto. tipo_insumo entre herbicidas, fungicidas, insecticidas, \
fertilizantes, semillas, otros. descripcion = nombre del producto. cantidad + unidad (litros/kg/bolsas/dosis).
- tarea_nueva: pide crear una tarea nueva. titulo_tarea con el título. descripcion si hay detalle. \
prioridad entre alta, media, baja. fecha_venc si menciona cuándo.
- siembra: dice que se sembró un campo ("sembré La Escondida con soja el 10/12"). cultivo, campo_id, \
fecha_siembra (YYYY-MM-DD). Se guarda como gasto de semillas con la fecha.
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
- confianza baja si estás adivinando. Ante la duda, poné baja: preferimos preguntar antes que cargar mal.
- Dejá en null los campos que el mensaje no menciona. No inventes datos."""


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


def _contexto_para_claude(ident: dict, tareas: list, tarea_activa: dict = None) -> str:
    partes = []
    campos = ident.get("campos") or []
    if campos:
        partes.append("Campos del productor:")
        for c in campos:
            partes.append(f"- id={c['id']} · {c['nombre']}")
    else:
        partes.append("El productor no tiene campos cargados.")

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


async def _interpretar_con_claude(texto: str, ident: dict, tareas: list,
                                  tarea_activa: dict = None) -> dict | None:
    """Traduce un mensaje libre a una acción concreta. None si no hay clave o falla."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        print("[CLAUDE] falta el paquete anthropic")
        return None

    try:
        cliente = anthropic.AsyncAnthropic()
        r = await cliente.messages.create(
            model=MODELO_CLAUDE,
            max_tokens=1000,
            system=INSTRUCCIONES_CLAUDE,
            messages=[{
                "role": "user",
                "content": (f"{_contexto_para_claude(ident, tareas, tarea_activa)}\n\n"
                            f"Mensaje recibido:\n{texto}"),
            }],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": ESQUEMA_INTENCION},
            },
        )
        crudo = next((b.text for b in r.content if b.type == "text"), "")
        return json.loads(crudo) if crudo else None
    except Exception as e:
        print(f"[CLAUDE] error interpretando: {e}")
        return None


async def _kapso_aplicar_intencion(ident: dict, numero: str, intencion: dict) -> bool:
    """Ejecuta lo que Claude entendió. Devuelve False si no se pudo hacer nada."""
    accion = intencion.get("accion") or "nada"
    if accion == "nada" or intencion.get("confianza") == "baja":
        return False

    ids_campos = {c["id"] for c in ident.get("campos", [])}

    if accion == "menu":
        await _kapso_menu(ident, numero)
        return True

    if accion == "ver_tareas":
        await _kapso_tareas(ident, numero)
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
        cid = intencion.get("campo_id")
        if cid in ids_campos:
            campo = next(c for c in ident["campos"] if c["id"] == cid)
            await _kapso_guardar_lluvia(ident, numero, campo, float(intencion["mm"]))
            return True
        datos = {"mm": float(intencion["mm"])}
        if await _kapso_pedir_campo(ident, numero, "k_lluvia_campo", datos,
                                    f"¿Dónde llovieron {_num(intencion['mm'])} mm?"):
            return True
        await _kapso_guardar_lluvia(
            ident, numero, {"id": datos["campo_id"], "nombre": datos["campo_nombre"]},
            float(intencion["mm"]))
        return True

    if accion == "gasto" and (intencion.get("monto_usd") is not None
                              or intencion.get("monto_ars") is not None):
        if not _wa_puede(ident, "gastos", "editar"):
            return False
        descripcion = intencion.get("descripcion") or ""
        if intencion.get("monto_usd") is not None:
            usd = float(intencion["monto_usd"])
        else:
            # En pesos: lo pasamos a dólares con el BNA vendedor del día, el
            # mismo que usa la web para los egresos, y lo dejamos anotado.
            ars = float(intencion["monto_ars"])
            tc = await _dolar_venta()
            usd = round(ars / tc, 2)
            descripcion = (f"{descripcion} (ARS {_miles(ars)} al BNA ${_miles(tc)})").strip()
        datos = {"gasto": {
            "rubro": (intencion.get("rubro") or "Otros").capitalize(),
            "descripcion": descripcion,
            "total_de_usd": usd,
        }}
        cid = intencion.get("campo_id")
        if cid in ids_campos:
            campo = next(c for c in ident["campos"] if c["id"] == cid)
            datos["campo_id"] = campo["id"]
            datos["campo_nombre"] = campo["nombre"]
        else:
            datos["confirmar"] = True
            if await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos, "¿A qué campo lo cargo?"):
                return True

        # Lo interpretó la IA: mostramos lo que entendimos antes de guardar.
        await _kapso_confirmar_gasto(numero, datos)
        return True

    if accion == "invitar":
        await _kapso_invitar_inicio(ident, numero, intencion.get("nombre_invitado") or "")
        return True

    # ── Ingreso (venta de cereal) ────────────────────
    if accion == "ingreso" and intencion.get("toneladas") and intencion.get("precio_tn_usd"):
        if not _wa_puede(ident, "ingresos", "editar"):
            return False
        cultivo = (intencion.get("cultivo") or "").strip().lower() or "otro"
        datos = {"ingreso": {
            "cultivo": cultivo,
            "tn": float(intencion["toneladas"]),
            "precio": float(intencion["precio_tn_usd"]),
        }}
        if intencion.get("comprador"):
            datos["ingreso"]["comprador"] = intencion["comprador"]
        cid = intencion.get("campo_id")
        if cid in ids_campos:
            campo = next(c for c in ident["campos"] if c["id"] == cid)
            datos["ingreso"]["campo_id"] = campo["id"]
            datos["ingreso"]["campo_nombre"] = campo["nombre"]
            await _kapso_guardar_ingreso(ident, numero, datos)
            return True
        # No tiene campo → preguntar
        if await _kapso_pedir_campo(ident, numero, "k_ing_campo", datos,
                                    f"¿De qué campo sale la venta de *{_num(intencion['toneladas'])} tn* de {cultivo}?"):
            return True
        datos["ingreso"]["campo_id"] = datos["campo_id"]
        datos["ingreso"]["campo_nombre"] = datos["campo_nombre"]
        await _kapso_guardar_ingreso(ident, numero, datos)
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
            "fecha": intencion.get("fecha_venc") or datetime.now(AR_TZ).date().isoformat(),
        }}
        if intencion.get("descripcion"):
            datos["pago"]["descripcion"] = intencion["descripcion"]
        await _kapso_guardar_pago(ident, numero, datos)
        return True

    # ── Insumo al stock ─────────────────────────────
    if accion == "insumo" and intencion.get("descripcion") and intencion.get("cantidad"):
        if not _wa_puede(ident, "insumos", "editar"):
            return False
        tipo = (intencion.get("tipo_insumo") or "otros").lower()
        if tipo not in ("herbicidas", "fungicidas", "insecticidas", "fertilizantes", "semillas", "otros"):
            tipo = "otros"
        unidad = (intencion.get("unidad") or "unidades").lower()
        if unidad not in ("litros", "kg", "dosis", "bolsas", "unidades"):
            unidad = "unidades"
        datos = {"insumo": {
            "tipo": tipo,
            "nombre": intencion["descripcion"][:150],
            "cantidad": float(intencion["cantidad"]),
            "unidad": unidad,
        }}
        if intencion.get("monto_usd"):
            datos["insumo"]["costo_unitario"] = float(intencion["monto_usd"])
        await _kapso_guardar_insumo(ident, numero, datos)
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
        cid = intencion.get("campo_id")
        if cid in ids_campos:
            campo = next(c for c in ident["campos"] if c["id"] == cid)
            datos["tarea_nueva"]["campo_id"] = campo["id"]
            datos["tarea_nueva"]["campo_nombre"] = campo["nombre"]
        await _kapso_guardar_tarea_nueva(ident, numero, datos)
        return True

    # ── Siembra (evento agrícola) → gasto de semillas con fecha ─────
    if accion == "siembra" and intencion.get("cultivo"):
        if not _wa_puede(ident, "gastos", "editar"):
            return False
        cultivo = intencion["cultivo"].strip().lower()
        fecha = intencion.get("fecha_siembra") or datetime.now(AR_TZ).date().isoformat()
        cid = intencion.get("campo_id")
        if cid not in ids_campos:
            # Sin campo → pedir
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
            cid = datos["campo_id"]
        campo = next((c for c in ident["campos"] if c["id"] == cid), None)
        if not campo:
            return False
        payload = {
            "campo_id": campo["id"],
            "usuario_id": ident["owner_id"],
            "rubro": "semillas",
            "fecha": fecha,
            "total_de_usd": float(intencion.get("monto_usd") or 0),
            "descripcion": f"Siembra de {cultivo}",
            "cultivo": cultivo,
        }
        await _sb_post("gastos", payload)
        await _wa_clear_conv(numero)
        await kapso.enviar_texto(
            numero,
            f"🌱 Siembra registrada en *{campo['nombre']}*\n"
            f"Cultivo: *{cultivo.capitalize()}*\n"
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

    # Un adjunto sin conversación en curso es, casi siempre, una factura
    if m.tiene_media and not conv:
        await _kapso_gasto_desde_archivo(ident, numero, m)
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
        print(f"[KAPSO←] de={m.desde} tipo={m.tipo} texto={m.texto[:80]!r} accion={m.accion!r}")
        asyncio.create_task(_kapso_procesar(m))

    return {"ok": True, "recibidos": len(mensajes)}


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
