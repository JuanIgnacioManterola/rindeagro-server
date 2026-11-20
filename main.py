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
CONFIRMAR_ANTES_DE_GUARDAR = False  # False = carga y avisa, con botón para corregir
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

    # Algo útil de entrada: cuántas tareas tiene y cuándo vence la próxima.
    tareas = await _wa_tareas_pendientes(ident) if _wa_puede(ident, "tareas") else []
    lineas = [saludo]
    if tareas:
        n = len(tareas)
        # _wa_tareas_pendientes trae hasta 10: con 10 puede haber más.
        cuantas = "10 o más" if n >= 10 else str(n)
        linea = f"Tenés *{cuantas} tarea{'s' if n != 1 else ''} pendiente{'s' if n != 1 else ''}*"
        venc = _wa_vencimiento_txt(tareas[0]) if tareas[0].get("fecha_vencimiento") else ""
        lineas.append(f"{linea}, la próxima {venc}." if venc else f"{linea}.")

    botones = []
    if _wa_puede(ident, "tareas"):
        botones.append(("tareas", f"📋 Mis tareas ({len(tareas) if len(tareas) < 10 else '10+'})"
                        if tareas else "📋 Mis tareas"))
    if _wa_puede(ident, "gastos", "editar"):
        botones.append(("gasto", "💵 Cargar gasto"))
    if _wa_puede(ident, "lluvias", "editar"):
        botones.append(("lluvia", "🌧️ Registrar lluvia"))

    if not botones:
        await kapso.enviar_texto(numero, "\n".join(lineas) +
                                 "\n\nTu usuario todavía no tiene permisos para cargar datos.")
        return

    campo = ((ident.get("campos") or [{}])[0].get("nombre") or "La Loma")
    lineas += [
        "",
        "¿Qué cargamos hoy?",
        "",
        f"También podés escribirme como le hablarías a alguien, por ejemplo "
        f"_llovieron 25 mm en {campo}_, o mandarme la *foto de una factura*.",
    ]
    await kapso.enviar_botones(numero, "\n".join(lineas), botones)


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

async def _kapso_guardar_lluvia(ident: dict, numero: str, campo: dict, mm: float) -> None:
    await _sb_post("lluvias", {
        "campo_id": campo["id"],
        "usuario_id": ident["owner_id"],
        "mm": float(mm),
        "fecha": datetime.now(AR_TZ).date().isoformat(),
        "fuente": "whatsapp",
    })
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
    await kapso.enviar_texto(
        numero,
        f"✅ *{_num(mm)} mm* en {campo['nombre']}, hoy.\n\n_Van {_num(total)} mm en {meses.get(mes, mes)}._",
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

RUBROS_WA = ["Herbicidas", "Fungicidas", "Insecticidas", "Fertilizantes",
             "Semillas", "Laboreo", "Flete", "Otros"]


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
        "fecha": datetime.now(AR_TZ).date().isoformat(),
        "total_de_usd": g.get("total_de_usd") or 0,
        "descripcion": g.get("descripcion") or "",
    }
    for k in ("cantidad", "unidad", "precio_unitario", "factura_url", "factura_path", "factura_tipo"):
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


async def _kapso_gasto_guiado(ident: dict, numero: str) -> None:
    """Arranca la carga manual de un gasto, paso a paso."""
    datos = {"gasto": {}}
    if await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos, "¿En qué campo fue el gasto?"):
        return
    await _wa_set_conv(numero, "k_gasto_rubro", datos)
    await _kapso_pedir_rubro(numero)


async def _kapso_pedir_rubro(numero: str) -> None:
    filas = [(f"rubro:{r.lower()}", r) for r in RUBROS_WA]
    await kapso.enviar_lista(numero, "¿Qué rubro?", "Elegir rubro", filas, "Rubros")


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


async def _kapso_texto_libre(ident: dict, numero: str, texto: str,
                             tarea_activa: dict = None, saltar_parecidos: bool = False) -> None:
    """
    Intenta resolver el mensaje sin menú: primero lluvia (que es un patrón
    clarísimo), después la IA si está configurada, y si no, el menú.
    """
    n = _norm(texto)

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
        elif await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos, "¿A qué campo lo cargo?"):
            return True

        if CONFIRMAR_ANTES_DE_GUARDAR:
            await _wa_set_conv(numero, "k_gasto_confirmar", datos)
            g = datos["gasto"]
            await kapso.enviar_botones(
                numero,
                f"Entendí esto:\n{g['rubro']} · {g['descripcion']}\n"
                f"*USD {_num(g['total_de_usd'])}* · {datos['campo_nombre']}\n\n¿Lo cargo así?",
                [("g_ok", "Sí, cargalo"), ("menu", "Cambiar algo")],
            )
        else:
            await _kapso_guardar_gasto(ident, numero, datos)
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
        await _kapso_guardar_lluvia(
            ident, numero, {"id": datos["campo_id"], "nombre": datos["campo_nombre"]}, mm)
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
            await _kapso_guardar_lluvia(ident, numero, campo, float(datos.get("mm") or 0))
            return
        if datos.get("gastos") or datos.get("gasto", {}).get("total_de_usd"):
            await _kapso_guardar_gasto(ident, numero, datos)
        else:
            await _wa_set_conv(numero, "k_gasto_rubro", datos)
            await _kapso_pedir_rubro(numero)
        return

    if estado == "k_gasto_rubro":
        rubro = accion.split(":", 1)[1].capitalize() if accion.startswith("rubro:") else _buscar_rubro(texto)
        datos.setdefault("gasto", {})["rubro"] = rubro
        await _wa_set_conv(numero, "k_gasto_monto", datos)
        await kapso.enviar_texto(numero, "¿Cuánto fue en dólares? Escribí solo el número.")
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
        datos.setdefault("gasto", {})["descripcion"] = texto.strip()[:300]
        await _kapso_guardar_gasto(ident, numero, datos)
        return

    if estado == "k_gasto_confirmar":
        if accion == "g_ok":
            await _kapso_guardar_gasto(ident, numero, datos)
        else:
            await _wa_clear_conv(numero)
            await _kapso_menu(ident, numero, "Dale, empecemos de nuevo.")
        return

    if estado == "k_inv_rol":
        rol_ui = accion.split(":", 1)[1] if accion.startswith("inv_rol:") else _norm(texto)
        if rol_ui not in ROLES_INVITACION:
            await kapso.enviar_texto(numero, "Tocá una de las dos opciones de arriba.")
            return
        await _kapso_invitar_crear(ident, numero, rol_ui, datos)
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
                     "gasto", "lluvia", "invitar", "menu", "nada"],
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
        "confianza":       {"type": "string", "enum": ["alta", "media", "baja"]},
    },
    "required": ["accion", "tarea_id", "nota", "campo_id", "rubro", "monto_usd", "monto_ars",
                 "descripcion", "mm", "nombre_invitado", "confianza"],
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
- gasto: compró o pagó algo. monto_usd en dólares, rubro entre herbicidas, fungicidas, insecticidas, \
fertilizantes, semillas, laboreo, flete u otros. descripcion es el producto.
- lluvia: llovió. mm es la cantidad.
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
- confianza baja si estás adivinando. Ante la duda, poné baja: preferimos preguntar antes que cargar mal.
- No inventes datos que el mensaje no dice."""


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
        elif await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos, "¿A qué campo lo cargo?"):
            return True

        if CONFIRMAR_ANTES_DE_GUARDAR:
            await _wa_set_conv(numero, "k_gasto_confirmar", datos)
            g = datos["gasto"]
            await kapso.enviar_botones(
                numero,
                f"Entendí esto:\n{g['rubro']} · {g['descripcion']}\n"
                f"*USD {_num(g['total_de_usd'])}* · {datos['campo_nombre']}\n\n¿Lo cargo así?",
                [("g_ok", "Sí, cargalo"), ("menu", "Cambiar algo")])
        else:
            await _kapso_guardar_gasto(ident, numero, datos)
        return True

    if accion == "invitar":
        await _kapso_invitar_inicio(ident, numero, intencion.get("nombre_invitado") or "")
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
        # campo elegido por dos estados para nada.
        await _wa_set_conv(numero, "k_lluvia_mm", {})
        await kapso.enviar_texto(numero, "¿Cuántos mm?")
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
