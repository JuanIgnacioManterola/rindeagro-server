from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
import httpx
import asyncio
from datetime import datetime, date, timedelta
import os
import json
import re
import hmac
import hashlib
import time
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import kapso

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
# HELPERS — Twilio WhatsApp (mensajes proactivos)
# ══════════════════════════════════════════════

async def _wa_enviar(numero: str, mensaje: str) -> bool:
    """Envía un mensaje WhatsApp outbound via Twilio (no es respuesta webhook)."""
    try:
        sid   = os.environ.get("TWILIO_ACCOUNT_SID", "")
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        from_ = os.environ.get("TWILIO_WHATSAPP_FROM", "")
        if not all([sid, token, from_]):
            print(f"[WA→] Twilio no configurado, mensaje no enviado a {numero}")
            return False
        from twilio.rest import Client
        client = Client(sid, token)
        await asyncio.to_thread(
            client.messages.create,
            from_=from_,
            to=f"whatsapp:{numero}",
            body=mensaje,
        )
        print(f"[WA→] {numero}: {mensaje[:60]}")
        return True
    except Exception as e:
        print(f"[WA→] Error enviando a {numero}: {e}")
        return False


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


async def _manejar_stop_activar(numero: str, texto: str) -> str | None:
    """Devuelve respuesta si el mensaje es STOP/ACTIVAR, None si no aplica."""
    n = _norm(texto)
    if n in STOP_WORDS or any(sw in n for sw in STOP_WORDS):
        rows = await _sb_get("perfiles", {"telefono": f"eq.{numero}", "select": "id"})
        if rows:
            await _sb_patch(
                f"wa_preferencias_notificaciones?user_id=eq.{rows[0]['id']}",
                {"activo": False},
            )
        await _wa_clear_conv(numero)
        return (
            "✅ Listo, no vas a recibir más mensajes de Rinde Agro.\n\n"
            "Si querés volver a activarlos escribí *ACTIVAR* en cualquier momento."
        )
    if n in ACTIVAR_WORDS or any(aw in n for aw in ACTIVAR_WORDS):
        rows = await _sb_get("perfiles", {"telefono": f"eq.{numero}", "select": "id"})
        if rows:
            await _sb_patch(
                f"wa_preferencias_notificaciones?user_id=eq.{rows[0]['id']}",
                {"activo": True},
            )
        return "✅ Notificaciones reactivadas. ¡Bienvenido de vuelta! 🌾"
    return None


async def _procesar_flujo(conv: dict, texto: str, usuario: dict) -> str:
    """Procesa el siguiente paso según el estado actual de la conversación."""
    estado  = conv["estado"]
    datos   = conv.get("datos_parciales") or {}
    numero  = conv["numero_whatsapp"]
    n       = _norm(texto)
    campos  = usuario.get("campos", [])

    # Cancelar en cualquier momento
    if n in {"0", "cancelar", "cancel", "salir"}:
        await _wa_clear_conv(numero)
        return f"❌ Cancelado.\n\n{MENU_TEXT}"

    # ── FLUJO GASTO ──────────────────────────────
    if estado == "gasto_campo":
        campo = _buscar_campo(texto, campos)
        if not campo:
            lista = ", ".join(c["nombre"] for c in campos) or "ninguno"
            return f"⚠️ No encontré ese campo. Tus campos: {lista}\n\n¿En qué campo fue el gasto?"
        datos.update({"campo_id": campo["id"], "campo_nombre": campo["nombre"]})
        await _wa_set_conv(numero, "gasto_rubro", datos)
        return "¿Qué rubro?\n\n" + "\n".join(f"• {r}" for r in RUBROS)

    if estado == "gasto_rubro":
        datos["rubro"] = _buscar_rubro(texto)
        await _wa_set_conv(numero, "gasto_monto", datos)
        return "¿Cuánto gastaste? (escribí solo el número en USD, ej: *45*)"

    if estado == "gasto_monto":
        monto = _extraer_numero(texto)
        if monto is None:
            return "No entendí el monto. Escribí solo el número, ej: *45*"
        datos["monto"] = monto
        await _wa_set_conv(numero, "gasto_descripcion", datos)
        return "¿Qué producto o descripción? (ej: Glifosato 4L, urea, soja DM 4210...)"

    if estado == "gasto_descripcion":
        datos["descripcion"] = texto.strip()
        ok, msg = await _guardar_gasto(datos, usuario)
        await _wa_clear_conv(numero)
        return f"{msg}\n\n{MENU_TEXT}"

    # ── FLUJO LLUVIA ─────────────────────────────
    if estado == "lluvia_campo":
        campo = _buscar_campo(texto, campos)
        if not campo:
            lista = ", ".join(c["nombre"] for c in campos) or "ninguno"
            return f"⚠️ No encontré ese campo. Tus campos: {lista}\n\n¿En qué campo llovió?"
        datos.update({"campo_id": campo["id"], "campo_nombre": campo["nombre"]})
        await _wa_set_conv(numero, "lluvia_mm", datos)
        return "¿Cuántos mm registraste?"

    if estado == "lluvia_mm":
        mm = _extraer_numero(texto)
        if mm is None:
            return "No entendí la cantidad. Escribí solo el número, ej: *18*"
        datos["mm"] = mm
        ok, msg = await _guardar_lluvia(datos, usuario)
        await _wa_clear_conv(numero)
        return f"{msg}\n\n{MENU_TEXT}"

    # ── FLUJO COMPLETAR TAREA ────────────────────
    if estado == "tarea_completar":
        tareas = datos.get("tareas", [])
        try:
            idx = int(n) - 1
            if 0 <= idx < len(tareas):
                tarea = tareas[idx]
                await _completar_tarea(tarea["id"])
                await _wa_clear_conv(numero)
                return f"✅ Tarea completada: _{tarea['descripcion']}_\n\n{MENU_TEXT}"
            return f"Número inválido. Escribí un número del 1 al {len(tareas)}."
        except ValueError:
            return f"Respondé con el número de la tarea (1–{len(tareas)})."

    # Estado desconocido → resetear
    await _wa_clear_conv(numero)
    return MENU_TEXT


async def _procesar_opcion_menu(opcion: str, usuario: dict, numero: str) -> str:
    """Arranca el sub-flujo de la opción elegida en el menú principal."""
    campos = usuario.get("campos") or []
    print(f"[DEBUG _procesar_opcion_menu] opcion={repr(opcion)} campos={[c.get('nombre') for c in campos]} numero={numero}")

    if opcion == "1":  # Gasto
        if not campos:
            print(f"[DEBUG _procesar_opcion_menu] → sin campos, retorna advertencia")
            return "⚠️ No tenés campos registrados. Ingresá a rindeagro.lat para crear uno."
        if len(campos) == 1:
            datos = {"campo_id": campos[0]["id"], "campo_nombre": campos[0]["nombre"]}
            print(f"[DEBUG _procesar_opcion_menu] → 1 campo, seteando estado=gasto_rubro datos={datos}")
            await _wa_set_conv(numero, "gasto_rubro", datos)
            return f"Campo: *{campos[0]['nombre']}*\n\n¿Qué rubro?\n\n" + "\n".join(f"• {r}" for r in RUBROS)
        print(f"[DEBUG _procesar_opcion_menu] → {len(campos)} campos, seteando estado=gasto_campo")
        await _wa_set_conv(numero, "gasto_campo", {})
        return "¿En qué campo fue el gasto?\n\n" + "\n".join(f"• {c['nombre']}" for c in campos)

    elif opcion == "2":  # Lluvia
        if not campos:
            return "⚠️ No tenés campos registrados."
        if len(campos) == 1:
            datos = {"campo_id": campos[0]["id"], "campo_nombre": campos[0]["nombre"]}
            await _wa_set_conv(numero, "lluvia_mm", datos)
            return f"Campo: *{campos[0]['nombre']}*\n\n¿Cuántos mm registraste?"
        await _wa_set_conv(numero, "lluvia_campo", {})
        return "¿En qué campo llovió?\n\n" + "\n".join(f"• {c['nombre']}" for c in campos)

    elif opcion == "3":  # Ver tareas
        tareas = await _get_tareas_pendientes(usuario["id"])
        if not tareas:
            return f"✅ No tenés tareas pendientes.\n\n{MENU_TEXT}"
        lineas = [f"{i+1}. {t['descripcion']}" + (f" — _{t['campo_nombre']}_" if t.get("campo_nombre") else "")
                  for i, t in enumerate(tareas[:10])]
        return "📋 *Tareas pendientes:*\n\n" + "\n".join(lineas) + f"\n\n{MENU_TEXT}"

    elif opcion == "4":  # Completar tarea
        tareas = await _get_tareas_pendientes(usuario["id"])
        if not tareas:
            return f"✅ No tenés tareas pendientes.\n\n{MENU_TEXT}"
        await _wa_set_conv(numero, "tarea_completar", {"tareas": tareas[:10]})
        lineas = [f"{i+1}. {t['descripcion']}" + (f" — _{t['campo_nombre']}_" if t.get("campo_nombre") else "")
                  for i, t in enumerate(tareas[:10])]
        return "📋 *¿Cuál completaste?* Respondé con el número:\n\n" + "\n".join(lineas)

    elif opcion == "0":
        await _wa_clear_conv(numero)
        return "👋 Hasta luego. Escribí *hola* o *menú* cuando quieras."

    return f"No entendí esa opción.\n\n{MENU_TEXT}"


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

async def _guardar_gasto(datos: dict, usuario: dict) -> tuple[bool, str]:
    try:
        payload = {
            "campo_id":    datos["campo_id"],
            "usuario_id":  usuario["id"],
            "rubro":       datos.get("rubro", "Otros"),
            "descripcion": datos.get("descripcion", ""),
            "fecha":       date.today().isoformat(),
            "total_de_usd": float(datos.get("monto", 0)),
            "fuente":      "whatsapp",
        }
        await _sb_post("gastos", payload)
        return True, (
            f"✅ Guardado. Gasto de *USD {datos.get('monto')}* en "
            f"{datos.get('rubro', '')} ({datos.get('descripcion', '')}) "
            f"en *{datos.get('campo_nombre', 'tu campo')}*."
        )
    except Exception as e:
        print(f"Error guardando gasto: {e}")
        return False, "⚠️ Error al guardar el gasto. Intentá de nuevo."

async def _guardar_lluvia(datos: dict, usuario: dict) -> tuple[bool, str]:
    try:
        payload = {
            "campo_id":   datos["campo_id"],
            "usuario_id": usuario["id"],
            "mm":         float(datos["mm"]),
            "fecha":      date.today().isoformat(),
            "fuente":     "whatsapp",
        }
        await _sb_post("lluvias", payload)
        return True, f"✅ Guardado. *{datos['mm']}mm* registrados en *{datos.get('campo_nombre', 'tu campo')}*."
    except Exception as e:
        print(f"Error guardando lluvia: {e}")
        return False, "⚠️ Error al guardar. Intentá de nuevo."

async def _get_tareas_pendientes(user_id: str) -> list:
    try:
        rows = await _sb_get("tareas", {
            "usuario_id": f"eq.{user_id}",
            "completada":  "eq.false",
            "select":      "id,descripcion,campos(nombre)",
            "order":       "creado_en.asc",
            "limit":       "10",
        })
        result = []
        for t in rows:
            campo_nombre = ""
            if isinstance(t.get("campos"), dict):
                campo_nombre = t["campos"].get("nombre", "")
            result.append({"id": t["id"], "descripcion": t.get("descripcion", "Sin título"), "campo_nombre": campo_nombre})
        return result
    except Exception as e:
        print(f"Error obteniendo tareas: {e}")
        return []

async def _completar_tarea(tarea_id: str):
    await _sb_patch(
        f"tareas?id=eq.{tarea_id}",
        {"completada": True, "completado_en": datetime.utcnow().isoformat()},
    )


# ══════════════════════════════════════════════
# WHATSAPP VÍA KAPSO — identidad, flujos y webhook
# ══════════════════════════════════════════════
#
# Canal nuevo, en paralelo al de Twilio (POST /whatsapp), que queda intacto.
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

async def _kapso_menu(ident: dict, numero: str, encabezado: str = "") -> None:
    nombre = _wa_primer_nombre(ident)
    if not encabezado:
        encabezado = f"Hola {nombre} 👋" if nombre else "Hola 👋"
    botones = []
    if _wa_puede(ident, "tareas"):
        botones.append(("tareas", "📋 Mis tareas"))
    if _wa_puede(ident, "gastos", "editar"):
        botones.append(("gasto", "💵 Cargar gasto"))
    if _wa_puede(ident, "lluvias", "editar"):
        botones.append(("lluvia", "🌧️ Registrar lluvia"))

    if not botones:
        await kapso.enviar_texto(numero, f"{encabezado}\n\nTu usuario todavía no tiene permisos para cargar datos.")
        return

    texto = (f"{encabezado}\n\n¿Qué querés hacer?\n\n"
             "También podés mandarme una *foto de una factura* o un *audio* y lo cargo solo.")
    await kapso.enviar_botones(numero, texto, botones)


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
        await kapso.enviar_texto(numero, "Guardado 📎 Ya lo pueden ver en la app.")
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


async def _kapso_gasto_desde_archivo(ident: dict, numero: str, m) -> None:
    """Foto o PDF de una factura → gasto cargado."""
    if not _wa_puede(ident, "gastos", "editar"):
        await kapso.enviar_texto(numero, "Tu usuario no tiene permiso para cargar gastos.")
        return

    contenido = await kapso.descargar_media(m.media_url)
    if not contenido:
        await kapso.enviar_texto(numero, "No pude bajar el archivo. ¿Me lo reenviás?")
        return

    items = await _ocr_factura(contenido, m.media_tipo)
    if items is None:
        await kapso.enviar_texto(
            numero,
            "Guardé el archivo pero todavía no puedo leerlo automáticamente.\n\n"
            "Decime el monto en dólares y lo cargo a mano.",
        )
        return
    if not items:
        await kapso.enviar_texto(
            numero,
            "No encontré renglones de insumos en ese documento 🤔\n\n"
            "Probá con una foto más nítida, o decime el monto y lo cargo a mano.",
        )
        return

    total = 0.0
    for it in items:
        cant = float(it.get("cantidad") or 0)
        pu = it.get("precio_unitario")
        if pu is not None:
            total += cant * float(pu)

    nombre_arch = m.media_nombre or ("factura.pdf" if m.es_pdf else "factura.jpg")
    path = f"{ident['owner_id']}/{int(datetime.utcnow().timestamp())}_{nombre_arch}"
    url = await _sb_storage_subir("insumos-facturas", path, contenido, m.media_tipo)

    principal = items[0]
    datos = {
        "gasto": {
            "rubro": (principal.get("tipo") or "otros").capitalize(),
            "descripcion": ", ".join(str(i.get("nombre") or "") for i in items[:4])[:300],
            "total_de_usd": round(total, 2),
            "cantidad": principal.get("cantidad"),
            "unidad": principal.get("unidad"),
            "precio_unitario": principal.get("precio_unitario"),
            "factura_url": url,
            "factura_path": path,
            "factura_tipo": "factura",
        }
    }

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

    if await _kapso_pedir_campo(ident, numero, "k_gasto_campo", datos,
                                "\n".join(resumen) + "\n\n¿A qué campo lo cargo?"):
        return
    await _kapso_guardar_gasto(ident, numero, datos)


async def _kapso_guardar_gasto(ident: dict, numero: str, datos: dict) -> None:
    g = dict(datos.get("gasto") or {})
    campo_id = datos.get("campo_id")
    campo_nombre = datos.get("campo_nombre") or ""

    payload = {
        "campo_id": campo_id,
        "usuario_id": ident["owner_id"],
        "rubro": g.get("rubro") or "Otros",
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

    lineas = [f"✅ Cargado en *{campo_nombre}*", f"{payload['rubro']} · USD {_num(payload['total_de_usd'])}"]
    if payload.get("usd_ha"):
        lineas.append(f"USD {_num(payload['usd_ha'])}/ha")
    if g.get("factura_url"):
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

async def _kapso_texto_libre(ident: dict, numero: str, texto: str,
                             tarea_activa: dict = None) -> None:
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

    if estado == "k_tarea_evidencia":
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
        if datos.get("gasto", {}).get("total_de_usd"):
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
    "flete": ["flete", "fletes", "camion", "camiones", "transporte", "acarreo", "chasis"],
}

# Verbos con los que se anuncia un gasto.
_RE_GASTO = re.compile(
    r"\b(gast[eé]|gast[oó]|compr[eé]|compr[oó]|pagu[eé]|pag[oó]|puse|met[ií]|"
    r"me sali[oó]|sali[oó]|cost[oó]|invert[ií])\b")

# Plata: 1200 · 1.200 · 1,200 · $1200 · usd 1200 · 1200 dolares · 3 mil
_NUM = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_NUM_SUELTO = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d{3,}(?:[.,]\d{1,2})?"

_RE_PLATA = re.compile(
    r"(?:us\$|usd|u\$s|\$)\s*(" + _NUM + r")"
    r"|(" + _NUM + r")\s*(?:mil\b|k\b|usd|u\$s|d[oó]lares|dolares|verdes|palos)"
    r"|(" + _NUM_SUELTO + r")")

_RE_PESOS = re.compile(r"\bpesos\b|\bar\$|\$ar\b")

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
        entero = re.sub(r"[.,]", "", crudo[:dec.start()])
        valor = float(f"{entero}.{dec.group(1)}")
    else:
        valor = float(re.sub(r"[.,]", "", crudo))

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
    for rubro, palabras in _RUBRO_POR_PRODUCTO.items():
        for p in palabras:
            if p in n:
                return rubro.capitalize(), p
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

    if _RE_PESOS.search(n):
        return None  # en pesos no sabemos convertir sin preguntar

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
        "descripcion":     {"type": ["string", "null"]},
        "mm":              {"type": ["number", "null"]},
        "nombre_invitado": {"type": ["string", "null"]},
        "confianza":       {"type": "string", "enum": ["alta", "media", "baja"]},
    },
    "required": ["accion", "tarea_id", "nota", "campo_id", "rubro", "monto_usd",
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
- Los montos son en dólares salvo que diga pesos. Si dice pesos, dejá monto_usd en null.
- confianza baja si estás adivinando. Ante la duda, poné baja: preferimos preguntar antes que cargar mal.
- No inventes datos que el mensaje no dice."""


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

    if accion == "gasto" and intencion.get("monto_usd") is not None:
        if not _wa_puede(ident, "gastos", "editar"):
            return False
        datos = {"gasto": {
            "rubro": (intencion.get("rubro") or "Otros").capitalize(),
            "descripcion": intencion.get("descripcion") or "",
            "total_de_usd": float(intencion["monto_usd"]),
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


# ══════════════════════════════════════════════
# ENDPOINT: POST /whatsapp
# Recibe mensajes de Twilio WhatsApp
# ══════════════════════════════════════════════

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    from_number = form.get("From", "").replace("whatsapp:", "")
    body        = form.get("Body", "").strip()
    media_url   = form.get("MediaUrl0", "")
    media_type  = form.get("MediaContentType0", "")
    print(f"[WA] De: {from_number} | Mensaje: {body[:100]} | Media: {media_type}")
    response_text = await procesar_mensaje_whatsapp(from_number, body, media_url, media_type)
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<Response><Message>{response_text}</Message></Response>"
    )
    return Response(content=twiml, media_type="application/xml")


async def procesar_mensaje_whatsapp(numero: str, texto: str, media_url: str, media_type: str) -> str:
    """Punto central de procesamiento de mensajes WhatsApp entrantes."""
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    print(f"[DEBUG procesar] numero={numero} texto_raw={repr(texto)} media_type={repr(media_type)}")

    # ── 0. STOP / ACTIVAR (no bloquea aunque Supabase falle) ───────────
    stop_resp = await _manejar_stop_activar(numero, texto)
    if stop_resp:
        return stop_resp

    # ── 1. Transcribir audio / PDF antes de cualquier routing ──────────
    texto_final = texto
    if media_url and "audio" in media_type and OPENAI_API_KEY:
        texto_final = await transcribir_audio(media_url, OPENAI_API_KEY) or texto
    if media_url and "pdf" in media_type:
        texto_final = await extraer_pdf(media_url) or texto
    if not texto_final:
        return "No entendí el mensaje. Podés escribir, mandar audio o adjuntar un PDF."

    n = _norm(texto_final)
    print(f"[DEBUG procesar] texto_final={repr(texto_final)} n={repr(n)}")

    # ── 2. Comandos de menú puro → responder sin necesitar Supabase ─────
    if n in MENU_WORDS:
        print(f"[DEBUG procesar] → es MENU_WORD, retorna menú")
        return MENU_TEXT

    # ── 3. Validar que Supabase esté configurado ────────────────────────
    if not _sb_url() or not _sb_key():
        print(f"[DEBUG procesar] ⚠️ SUPABASE_URL o SUPABASE_SERVICE_KEY no configurados")
        return MENU_TEXT

    # ── 4. Identificar usuario ──────────────────────────────────────────
    rows = await _sb_get("perfiles", {"telefono": f"eq.{numero}", "select": "id,nombre,campos(id,nombre)"})
    print(f"[DEBUG procesar] perfiles encontrados: {len(rows)} — ids={[r.get('id') for r in rows]}")
    if not rows:
        return (
            f"⚠️ Tu número {numero} no está vinculado a ninguna cuenta Rinde Agro.\n"
            "Ingresá a rindeagro.lat y vinculá tu WhatsApp en Configuración."
        )
    usuario = rows[0]
    campos = usuario.get("campos") or []
    print(f"[DEBUG procesar] usuario.id={usuario.get('id')} campos={[c.get('nombre') for c in campos]}")

    # ── 5. ¿Hay una conversación activa en curso? ───────────────────────
    conv = await _wa_get_conv(numero)
    print(f"[DEBUG procesar] conversación activa: {conv is not None} — estado={conv.get('estado') if conv else None}")
    if conv:
        print(f"[DEBUG procesar] → entra a _procesar_flujo")
        return await _procesar_flujo(conv, texto_final, usuario)

    # ── 6. Selección de opción del menú principal ───────────────────────
    print(f"[DEBUG procesar] sin conv activa — n={repr(n)} es_opcion={n in {'1','2','3','4','0'}}")
    if n in {"1", "2", "3", "4", "0"}:
        print(f"[DEBUG procesar] → entra a _procesar_opcion_menu(opcion={n})")
        return await _procesar_opcion_menu(n, usuario, numero)

    # ── 7. Mensaje libre → intentar con IA; si no → menú ───────────────
    if OPENAI_API_KEY and len(texto_final) > 8:
        resultado = await interpretar_con_ia(texto_final, usuario, OPENAI_API_KEY)
        if resultado and resultado.get("confianza") in ("alta", "media"):
            return await cargar_en_supabase(resultado, usuario, _sb_url(), _sb_key())

    print(f"[DEBUG procesar] → sin match, retorna menú")
    return MENU_TEXT


# ── Audio / PDF / IA ──────────────────────────

async def transcribir_audio(media_url: str, api_key: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            audio_resp = await client.get(media_url)
            audio_bytes = audio_resp.content
            r = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
                data={"model": "whisper-1", "language": "es"},
            )
            if r.status_code == 200:
                return r.json().get("text", "")
    except Exception as e:
        print(f"Whisper error: {e}")
    return ""


async def extraer_pdf(media_url: str) -> str:
    try:
        import io
        async with httpx.AsyncClient(timeout=20) as client:
            pdf_resp = await client.get(media_url)
            pdf_bytes = pdf_resp.content
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            texto = "\n".join(p.extract_text() or "" for p in pdf.pages[:5])
        return texto[:3000]
    except Exception as e:
        print(f"PDF error: {e}")
    return ""


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


async def cargar_en_supabase(datos: dict, usuario: dict, sb_url: str, sb_key: str) -> str:
    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    campo_id = None
    campo_nombre = datos.get("campo_nombre")
    if campo_nombre:
        for c in usuario.get("campos", []):
            if campo_nombre.lower() in c["nombre"].lower():
                campo_id = c["id"]
                break
    if not campo_id and len(usuario.get("campos", [])) == 1:
        campo_id    = usuario["campos"][0]["id"]
        campo_nombre = usuario["campos"][0]["nombre"]

    tipo = datos.get("tipo")
    d    = datos.get("datos", {})
    resp = datos.get("respuesta_usuario", "")

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            if tipo == "gasto" and campo_id:
                payload = {
                    "campo_id": campo_id, "usuario_id": usuario["id"],
                    "rubro": d.get("rubro", "otros"), "descripcion": d.get("descripcion", ""),
                    "fecha": d.get("fecha") or date.today().isoformat(),
                    "total_de_usd": float(d.get("total_usd", 0)),
                    "cantidad": d.get("cantidad"), "unidad": d.get("unidad"),
                    "precio_unitario": d.get("precio_unitario"), "fuente": "whatsapp",
                }
                r = await client.post(f"{sb_url}/rest/v1/gastos", headers=headers, json=payload)
                if r.status_code in (200, 201):
                    return f"✅ {resp}\n\n_Gasto cargado en {campo_nombre or 'tu campo'}_"

            elif tipo == "lluvia" and campo_id:
                payload = {
                    "campo_id": campo_id, "usuario_id": usuario["id"],
                    "mm": float(d.get("mm", 0)), "fecha": d.get("fecha") or date.today().isoformat(),
                    "fuente": "whatsapp",
                }
                r = await client.post(f"{sb_url}/rest/v1/lluvias", headers=headers, json=payload)
                if r.status_code in (200, 201):
                    return f"✅ {resp}\n\n_Lluvia registrada en {campo_nombre}_"

            elif tipo == "rendimiento" and campo_id:
                payload = {"rendimiento": float(d.get("rendimiento_tha", 0))}
                if d.get("precio_usd_t"):
                    payload["precio_venta"] = float(d["precio_usd_t"])
                r = await client.patch(f"{sb_url}/rest/v1/campos?id=eq.{campo_id}", headers=headers, json=payload)
                if r.status_code in (200, 204):
                    return f"✅ {resp}\n\n_Rendimiento actualizado en {campo_nombre}_"

            else:
                if not campo_id:
                    return f"⚠️ No pude identificar el campo. Tus campos: {', '.join(c['nombre'] for c in usuario.get('campos', []))}"
                return "⚠️ No entendí el tipo de dato. Podés cargar: gastos, lluvias o rendimientos."

        except Exception as e:
            print(f"Supabase insert error: {e}")
            return "⚠️ Error al guardar. Intentá de nuevo."

    return "⚠️ No se pudo guardar el dato."


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


async def _wa_recordatorio_operarios():
    """Job 1 — L/M/V 17:30 AR: recordatorio para operarios."""
    print("[SCHEDULER] Recordatorio operarios")
    destinatarios = await _wa_get_destinatarios("recordatorio_operario")
    mensaje = (
        "📋 ¿Registraste todo lo de hoy?\n\n"
        "Si falta algo mandame un mensaje ahora.\n\n"
        "1️⃣ Cargar gasto\n"
        "2️⃣ Registrar lluvia\n"
        "3️⃣ Ver tareas pendientes\n\n"
        "Respondé *STOP* para no recibir más recordatorios."
    )
    for d in destinatarios:
        await _wa_enviar(d["numero"], mensaje)
        await asyncio.sleep(0.5)  # evitar rate limit de Twilio


async def _wa_recordatorio_admins():
    """Job 2 — V 12:30 AR: recordatorio para administradores."""
    print("[SCHEDULER] Recordatorio admins")
    destinatarios = await _wa_get_destinatarios("recordatorio_admin")
    mensaje = (
        "📊 *Rinde Agro* - Recordatorio semanal\n\n"
        "Mañana recibís tu resumen de la semana.\n"
        "¿Te olvidaste de cargar algo?\n\n"
        "1️⃣ Cargar gasto\n"
        "2️⃣ Registrar lluvia"
    )
    for d in destinatarios:
        await _wa_enviar(d["numero"], mensaje)
        await asyncio.sleep(0.5)


async def _wa_resumen_semanal():
    """Job 3 — Sábados 9:00 AR: resumen semanal por campo para cada admin."""
    print("[SCHEDULER] Resumen semanal")
    destinatarios = await _wa_get_destinatarios("resumen_semanal")
    if not destinatarios:
        return

    hoy          = date.today()
    inicio_semana = (hoy - timedelta(days=hoy.weekday())).isoformat()  # lunes
    fin_semana    = hoy.isoformat()
    fecha_desde   = f"{inicio_semana}T00:00:00"
    fecha_hasta   = f"{fin_semana}T23:59:59"

    # Formatear rango de fechas legible
    dias_semana   = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    meses         = ["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"]
    d_ini = hoy - timedelta(days=hoy.weekday())
    rango = f"{d_ini.day} al {hoy.day} de {meses[hoy.month - 1]}"

    precios = cache_precios["cereales"]

    for dest in destinatarios:
        uid = dest["user_id"]

        # Campos del usuario
        campos_rows = await _sb_get("perfiles", {"id": f"eq.{uid}", "select": "nombre,campos(id,nombre)"})
        if not campos_rows:
            continue
        perfil = campos_rows[0]
        nombre_org = perfil.get("nombre", "")
        campos = perfil.get("campos") or []

        # Lluvias de la semana por campo
        lineas_lluvias = []
        for campo in campos:
            lluvias = await _sb_get("lluvias", {
                "campo_id": f"eq.{campo['id']}",
                "fecha":    f"gte.{inicio_semana}",
                "select":   "mm,fecha",
            })
            if lluvias:
                total_mm = sum(float(l.get("mm", 0)) for l in lluvias)
                lineas_lluvias.append(f"{campo['nombre']}: {total_mm:.0f}mm")

        # Gastos de la semana
        total_gastos  = 0.0
        rubros_gastos: dict[str, float] = {}
        for campo in campos:
            gastos = await _sb_get("gastos", {
                "campo_id":  f"eq.{campo['id']}",
                "fecha":     f"gte.{inicio_semana}",
                "select":    "total_de_usd,rubro",
            })
            for g in gastos:
                monto = float(g.get("total_de_usd", 0))
                rubro = g.get("rubro", "Otros")
                total_gastos += monto
                rubros_gastos[rubro] = rubros_gastos.get(rubro, 0) + monto

        # Tareas completadas esta semana
        tareas_ok = await _sb_get("tareas", {
            "usuario_id":   f"eq.{uid}",
            "completada":   "eq.true",
            "completado_en": f"gte.{fecha_desde}",
            "select":       "id",
        })

        # Armar mensaje
        lineas = [
            f"📊 *Resumen semanal - {nombre_org}*",
            f"Semana del {rango}",
            "",
        ]

        # Lluvias
        if lineas_lluvias:
            lineas.append("🌧️ *Lluvias*")
            lineas.extend(lineas_lluvias)
        else:
            lineas.append("🌧️ *Lluvias*\nSin registros esta semana")
        lineas.append("")

        # Gastos
        lineas.append("💰 *Gastos registrados*")
        if total_gastos > 0:
            lineas.append(f"Total: USD {total_gastos:,.0f}")
            for rubro, monto in sorted(rubros_gastos.items(), key=lambda x: -x[1]):
                lineas.append(f"{rubro}: USD {monto:,.0f}")
        else:
            lineas.append("Sin gastos registrados")
        lineas.append("")

        # Tareas
        lineas.append("✅ *Tareas completadas*")
        n_tareas = len(tareas_ok)
        lineas.append(f"{n_tareas} tarea{'s' if n_tareas != 1 else ''} marcada{'s' if n_tareas != 1 else ''} como lista{'s' if n_tareas != 1 else ''}" if n_tareas else "Ninguna completada esta semana")
        lineas.append("")

        # Precios
        lineas.append("📈 *Precios hoy*")
        lineas.append(f"Soja: USD {precios.get('soja', '-')}/t")
        lineas.append(f"Maíz: USD {precios.get('maiz', '-')}/t")
        lineas.append(f"Trigo: USD {precios.get('trigo', '-')}/t")
        lineas.append("")
        lineas.append("Que tengas una buena semana 🌾")

        await _wa_enviar(dest["numero"], "\n".join(lineas))
        await asyncio.sleep(1)


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
    if ultimo:
        cambio_pct = abs(precio_actual - ultimo) / ultimo * 100
        if cambio_pct < 2:
            return  # cambio menor al 2%, no alertar

    # Actualizar precio de referencia
    cache_precios.setdefault("ultimo_precio_alerta", {})["soja"] = precio_actual

    destinatarios = await _wa_get_destinatarios("alerta_precio")
    if not destinatarios:
        return

    mensaje = (
        f"🔔 *Alerta de precio Rinde Agro*\n\n"
        f"Precio actual de *Soja*: USD {precio_actual}/t\n\n"
        "¿Querés revisar tus márgenes? Entrá a rindeagro.lat"
    )
    for d in destinatarios:
        await _wa_enviar(d["numero"], mensaje)
        await asyncio.sleep(0.5)


# ══════════════════════════════════════════════
# MERCADO PAGO — SUSCRIPCIONES (preapproval / débito automático)
# ══════════════════════════════════════════════
#
# Arquitectura (ver CLAUDE.md → "Suscripciones y cobro"):
#   preapproval SIN plan asociado, status 'pending' → devuelve init_point →
#   el usuario carga la tarjeta EN EL SITIO DE MERCADO PAGO → MP nos avisa por
#   webhook. Elegimos preapproval sin plan porque el monto en ARS sale de
#   USD × dólar BNA vendedor + IVA, o sea que es distinto para cada usuario
#   según el día del alta: un preapproval_plan es un monto fijo compartido.
#
# El WEBHOOK es la única fuente de verdad del plan. El redirect de vuelta a la
# app (back_url) NO otorga nada: solo muestra "estamos confirmando tu pago".
#
# Ninguna de estas rutas ve datos de tarjeta. Todo dato sensible queda en MP.

MP_API = "https://api.mercadopago.com"

# Espejo de PLANES_INFO en index.html. Si tocás precios allá, tocá acá.
# El límite de hectáreas lo aplica el frontend; acá está solo para el
# endpoint /mp/planes que alimenta la landing.
PLANES = {
    "semilla":     {"nombre": "Semilla",     "usd_mensual": 35,  "limite_hectareas": 500,   "descripcion": "Hasta 500 hectáreas"},
    "germinacion": {"nombre": "Germinación", "usd_mensual": 50,  "limite_hectareas": 1000,  "descripcion": "De 500 a 1.000 hectáreas"},
    "floracion":   {"nombre": "Floración",   "usd_mensual": 65,  "limite_hectareas": 2000,  "descripcion": "De 1.000 a 2.000 hectáreas"},
    "maduracion":  {"nombre": "Maduración",  "usd_mensual": 80,  "limite_hectareas": 5000,  "descripcion": "De 2.000 a 5.000 hectáreas"},
    "cosecha":     {"nombre": "Cosecha",     "usd_mensual": 110, "limite_hectareas": 10000, "descripcion": "De 5.000 a 10.000 hectáreas"},
}

IVA_PCT = 21
DESCUENTO_ANUAL_PCT = 20   # anual = mensual × 12 × 0.8
DIAS_GRACIA = 10           # días de acceso completo tras un cobro fallido

# Estados de suscripciones.estado que consideramos "viva" (ocupan el índice
# único parcial idx_suscripciones_una_viva).
ESTADOS_VIVOS = ("pendiente", "activa", "en_gracia", "impaga", "pausada")

# Orígenes a los que aceptamos volver después del checkout. Evita que alguien
# nos use de open redirect metiendo su propio back_url en el body.
ORIGENES_RETORNO = {
    "https://rindeagro.app",
    "https://www.rindeagro.app",
    "https://juanignaciomanterola.github.io",
    "http://localhost:8787",
    "http://127.0.0.1:8787",
}

# UUIDs con acceso a los endpoints /mp/admin/*. Espejo de
# USUARIOS_ADMIN_ILIMITADOS en index.html.
ADMIN_USER_IDS = [
    x.strip() for x in os.environ.get(
        "ADMIN_USER_IDS", "ea80343b-b31d-4cba-a43e-00c3f0a3fa39"
    ).split(",") if x.strip()
]


def _mp_token() -> str:
    return os.environ.get("MP_ACCESS_TOKEN", "")


def _mp_secret() -> str:
    return os.environ.get("MP_WEBHOOK_SECRET", "")


def _mp_entorno() -> str:
    """Los access token de sandbox de MP empiezan con TEST-."""
    return "test" if _mp_token().startswith("TEST-") else "prod"


def _mp_headers(idem: str = "") -> dict:
    h = {"Authorization": f"Bearer {_mp_token()}", "Content-Type": "application/json"}
    if idem:
        h["X-Idempotency-Key"] = idem
    return h


def _ahora_utc() -> datetime:
    return datetime.now(pytz.UTC)


def _iso(d) -> str | None:
    return d.isoformat() if d else None


def _parse_fecha(v):
    """MP devuelve ISO8601 con offset (2026-09-07T11:00:00.000-04:00)."""
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


# ── Cotización ───────────────────────────────────────────────────────────────

async def _dolar_venta_bna() -> float:
    """
    Dólar DIVISA VENDEDOR del BNA — el mismo que muestra el frontend en las
    tarjetas de plan (window._dolarVenta()). Sale de la vista
    v_precio_dolar_actual que llena el cron de Supabase. Si esa vista no
    responde caemos al scraper genérico para no dejar el checkout muerto.
    """
    filas = await _sb_get("v_precio_dolar_actual", {"select": "divisa_venta", "limit": "1"})
    if filas:
        try:
            v = float(filas[0].get("divisa_venta") or 0)
            if 100 < v < 100000:
                return round(v, 2)
        except Exception:
            pass
    bna, _ = await fetch_dolar_bna()
    return float(bna or cache_precios["bna"])


def _precio(plan_id: str, periodo: str, bna: float) -> tuple[int, int, int]:
    """
    Devuelve (usd_base_sin_iva, usd_con_iva, ars_final).

    El redondeo replica EXACTAMENTE el de renderTarjetasPlanes() en
    index.html: se redondea el USD con IVA a entero y recién ahí se
    multiplica por el TC. Si no, el importe que MP debita no coincide con
    el que el usuario vio en la tarjeta.
    """
    p = PLANES[plan_id]
    mensual = p["usd_mensual"]
    if periodo == "anual":
        usd_base = round(mensual * 12 * (1 - DESCUENTO_ANUAL_PCT / 100))
    else:
        usd_base = mensual
    usd_con_iva = round(usd_base * (1 + IVA_PCT / 100))
    ars = round(usd_con_iva * bna)
    return int(usd_base), int(usd_con_iva), int(ars)


def _frecuencia(periodo: str) -> dict:
    """
    Anual = un preapproval cada 12 meses (estándar Stripe/Paddle: una sola
    rail de pago, se auto-renueva y se cancela sola desde la app). MP
    Suscripciones no soporta cuotas; si algún día queremos anual en cuotas
    va por Checkout Pro como proveedor aparte.
    """
    return {"frequency": 12 if periodo == "anual" else 1, "frequency_type": "months"}


# ── Auth ─────────────────────────────────────────────────────────────────────

async def _auth_user(request: Request) -> dict:
    """
    Valida el JWT de Supabase que manda el frontend en Authorization.
    NUNCA confiamos en un usuario_id que venga en el body: con eso cualquiera
    le regalaría un plan a otra cuenta (o a la propia).
    """
    authz = request.headers.get("authorization", "")
    if not authz.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Falta el token de sesión.")
    token = authz[7:].strip()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{_sb_url()}/auth/v1/user",
                headers={"apikey": _sb_key(), "Authorization": f"Bearer {token}"},
            )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"No pudimos validar la sesión: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Sesión inválida o vencida.")
    u = r.json()
    if not u.get("id"):
        raise HTTPException(status_code=401, detail="Sesión inválida.")
    return u


def _validar_retorno(url: str) -> str:
    """
    El frontend manda su propio origin+pathname (nunca hardcodeamos la URL de
    la app — romper eso rompe rindeagro.app). Acá solo verificamos que sea un
    origen nuestro y le sacamos query y fragment.
    """
    from urllib.parse import urlparse
    if not url:
        return APP_URL
    try:
        p = urlparse(url)
        origen = f"{p.scheme}://{p.netloc}"
        if origen not in ORIGENES_RETORNO:
            print(f"[MP] back_url rechazada (origen no permitido): {url}")
            return APP_URL
        return origen + (p.path or "/")
    except Exception:
        return APP_URL


# ── Estado / plan efectivo ───────────────────────────────────────────────────

def _plan_efectivo(sub: dict | None) -> str:
    """
    Qué plan le corresponde HOY al usuario según su suscripción.
    Espejo de window._planEfectivo() en index.html — si cambiás uno, cambiá
    el otro.
    """
    if not sub:
        return "gratis"
    estado = sub.get("estado")
    plan = sub.get("plan") or "gratis"
    if estado in ("activa", "en_gracia"):
        return plan
    if estado == "cancelada":
        # Se dio de baja pero ya pagó el período en curso: conserva el plan
        # hasta que termine. Estándar de la industria.
        fin = _parse_fecha(sub.get("periodo_fin"))
        if fin and fin > _ahora_utc():
            return plan
    return "gratis"


async def _sub_vigente(owner_id: str) -> dict | None:
    filas = await _sb_get("suscripciones", {
        "owner_id": f"eq.{owner_id}",
        "estado": f"in.({','.join(ESTADOS_VIVOS)})",
        "order": "created_at.desc",
        "limit": "1",
    })
    return filas[0] if filas else None


async def _sub_por_preapproval(preapproval_id: str) -> dict | None:
    filas = await _sb_get("suscripciones", {
        "proveedor_id": f"eq.{preapproval_id}", "limit": "1",
    })
    return filas[0] if filas else None


async def _sync_metadata_plan(owner_id: str, sub: dict | None):
    """
    Espeja el plan efectivo en auth.users.user_metadata para que el código
    viejo que lee usuario.user_metadata.plan siga andando. Es un CACHE, no la
    fuente de verdad: la app calcula el plan desde la tabla suscripciones.

    Leemos primero el user completo y reescribimos el metadata mergeado, para
    no pisar nombre / telefono / trial_inicio.
    """
    plan = _plan_efectivo(sub)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{_sb_url()}/auth/v1/admin/users/{owner_id}",
                headers={"apikey": _sb_key(), "Authorization": f"Bearer {_sb_key()}"},
            )
            meta = (r.json().get("user_metadata") or {}) if r.status_code == 200 else {}
            meta["plan"] = plan
            meta["suscripcion_estado"] = (sub or {}).get("estado") or "sin_suscripcion"
            r2 = await c.put(
                f"{_sb_url()}/auth/v1/admin/users/{owner_id}",
                headers={
                    "apikey": _sb_key(),
                    "Authorization": f"Bearer {_sb_key()}",
                    "Content-Type": "application/json",
                },
                json={"user_metadata": meta},
            )
            if r2.status_code not in (200, 201):
                print(f"[MP] sync metadata {owner_id} HTTP {r2.status_code}: {r2.text[:200]}")
            else:
                print(f"[MP] metadata sincronizado: {owner_id} → plan {plan}")
    except Exception as e:
        print(f"[MP] sync metadata excepción: {e}")


# ══════════════════════════════════════════════
# POST /mp/suscripcion/crear
# ══════════════════════════════════════════════

@app.post("/mp/suscripcion/crear")
async def mp_crear_suscripcion(request: Request):
    if not _mp_token():
        raise HTTPException(status_code=503, detail="MP_ACCESS_TOKEN no está configurado en el server.")

    user = await _auth_user(request)
    owner_id = user["id"]
    email = user.get("email") or ""

    body    = await request.json()
    plan_id = (body.get("plan") or "").strip()
    periodo = (body.get("periodo") or "mensual").strip()
    retorno = _validar_retorno(body.get("retorno_url") or "")

    if plan_id not in PLANES:
        raise HTTPException(status_code=400, detail="Plan inválido.")
    if periodo not in ("mensual", "anual"):
        raise HTTPException(status_code=400, detail="Período inválido.")

    # Si ya tiene una suscripción cobrando, esto es un cambio de plan.
    vigente = await _sub_vigente(owner_id)
    if vigente and vigente.get("estado") in ("activa", "en_gracia", "pausada"):
        raise HTTPException(
            status_code=409,
            detail="Ya tenés una suscripción activa. Usá /mp/suscripcion/cambiar-plan.",
        )

    bna = await _dolar_venta_bna()
    usd_base, usd_con_iva, ars = _precio(plan_id, periodo, bna)

    plan = PLANES[plan_id]
    # Es lo que el usuario ve en el checkout de Mercado Pago y en el resumen
    # de la tarjeta: marca "Rinde Agro" (con espacio, sin punto).
    razon = f"Rinde Agro · Plan {plan['nombre']} {'Anual' if periodo == 'anual' else 'Mensual'}"
    ext_ref = f"{owner_id}|{plan_id}|{periodo}"

    payload = {
        "reason": razon,
        "external_reference": ext_ref,
        "payer_email": email,
        "back_url": retorno,
        "status": "pending",   # el usuario carga la tarjeta en el checkout de MP
        "auto_recurring": {
            **_frecuencia(periodo),
            "transaction_amount": float(ars),
            "currency_id": "ARS",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{MP_API}/preapproval",
                headers=_mp_headers(idem=f"{owner_id}:{plan_id}:{periodo}:{int(time.time())}"),
                json=payload,
            )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"No pudimos contactar a Mercado Pago: {e}")

    if r.status_code not in (200, 201):
        print(f"[MP] POST /preapproval HTTP {r.status_code}: {r.text[:500]}")
        raise HTTPException(status_code=502, detail=f"Mercado Pago rechazó la suscripción: {r.text[:300]}")

    mp = r.json()
    init_point = mp.get("init_point") or mp.get("sandbox_init_point")
    if not init_point:
        raise HTTPException(status_code=502, detail="Mercado Pago no devolvió el link de pago.")

    # Cerrar cualquier intento anterior que quedó 'pendiente' (el índice único
    # parcial deja una sola suscripción viva por usuario).
    if vigente and vigente.get("estado") == "pendiente":
        await _sb_patch(f"suscripciones?id=eq.{vigente['id']}",
                        {"estado": "cancelada", "cancelada_en": _iso(_ahora_utc())})

    fila = await _sb_post("suscripciones", {
        "owner_id": owner_id,
        "plan": plan_id,
        "periodo": periodo,
        "estado": "pendiente",
        "proveedor": "mercadopago",
        "proveedor_id": str(mp.get("id")),
        "external_reference": ext_ref,
        "init_point": init_point,
        "entorno": _mp_entorno(),
        "importe_ars": ars,
        "moneda": "ARS",
        "importe_usd": usd_base,
        "iva_pct": IVA_PCT,
        "tc_alta": bna,
        "raw": mp,
    })

    print(f"[MP] preapproval {mp.get('id')} creado para {owner_id} · {plan_id}/{periodo} · ARS {ars}")

    return {
        "ok": True,
        "init_point": init_point,
        "preapproval_id": mp.get("id"),
        "suscripcion_id": (fila or {}).get("id"),
        "plan": plan_id,
        "periodo": periodo,
        "importe_ars": ars,
        "importe_usd": usd_base,
        "importe_usd_con_iva": usd_con_iva,
        "bna": bna,
        "entorno": _mp_entorno(),
    }


# ══════════════════════════════════════════════
# POST /mp/suscripcion/cambiar-plan
# ══════════════════════════════════════════════

@app.post("/mp/suscripcion/cambiar-plan")
async def mp_cambiar_plan(request: Request):
    """
    Mismo período → PUT del monto sobre el preapproval existente (el usuario
    no vuelve a cargar la tarjeta).
    Cambio de período (mensual ↔ anual) → hay que cancelar y crear una
    suscripción nueva, porque la frecuencia no se puede cambiar en caliente.
    """
    if not _mp_token():
        raise HTTPException(status_code=503, detail="MP_ACCESS_TOKEN no está configurado en el server.")

    user     = await _auth_user(request)
    owner_id = user["id"]
    body     = await request.json()
    plan_id  = (body.get("plan") or "").strip()
    periodo  = (body.get("periodo") or "").strip()

    if plan_id not in PLANES:
        raise HTTPException(status_code=400, detail="Plan inválido.")

    sub = await _sub_vigente(owner_id)
    if not sub or sub.get("estado") not in ("activa", "en_gracia", "pausada"):
        raise HTTPException(status_code=404, detail="No tenés una suscripción activa para cambiar.")

    periodo = periodo or sub.get("periodo") or "mensual"
    if periodo not in ("mensual", "anual"):
        raise HTTPException(status_code=400, detail="Período inválido.")

    if periodo != sub.get("periodo"):
        return {
            "ok": False,
            "requiere_nuevo_checkout": True,
            "detalle": "Cambiar entre mensual y anual necesita dar de baja la suscripción actual y contratar de nuevo.",
        }

    bna = await _dolar_venta_bna()
    usd_base, usd_con_iva, ars = _precio(plan_id, periodo, bna)

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.put(
                f"{MP_API}/preapproval/{sub['proveedor_id']}",
                headers=_mp_headers(),
                json={
                    "reason": f"Rinde Agro · Plan {PLANES[plan_id]['nombre']} {periodo.capitalize()}",
                    "auto_recurring": {"transaction_amount": float(ars), "currency_id": "ARS"},
                },
            )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"No pudimos contactar a Mercado Pago: {e}")

    if r.status_code not in (200, 201):
        print(f"[MP] PUT /preapproval/{sub['proveedor_id']} HTTP {r.status_code}: {r.text[:400]}")
        raise HTTPException(status_code=502, detail=f"Mercado Pago rechazó el cambio: {r.text[:300]}")

    await _sb_patch(f"suscripciones?id=eq.{sub['id']}", {
        "plan": plan_id,
        "importe_ars": ars,
        "importe_usd": usd_base,
        "tc_alta": bna,
        "external_reference": f"{owner_id}|{plan_id}|{periodo}",
        "raw": r.json(),
    })
    actualizada = await _sub_vigente(owner_id)
    await _sync_metadata_plan(owner_id, actualizada)

    print(f"[MP] {owner_id} cambió a {plan_id}/{periodo} · ARS {ars}")
    return {"ok": True, "plan": plan_id, "periodo": periodo, "importe_ars": ars, "bna": bna}


# ══════════════════════════════════════════════
# POST /mp/suscripcion/cancelar
# ══════════════════════════════════════════════

@app.post("/mp/suscripcion/cancelar")
async def mp_cancelar_suscripcion(request: Request):
    """
    Baja autogestionada. MP deja de cobrar ya, pero el usuario conserva el
    plan hasta el final del período que ya pagó (periodo_fin) — el
    _plan_efectivo() lo contempla.
    """
    if not _mp_token():
        raise HTTPException(status_code=503, detail="MP_ACCESS_TOKEN no está configurado en el server.")

    user     = await _auth_user(request)
    owner_id = user["id"]
    sub      = await _sub_vigente(owner_id)
    if not sub:
        raise HTTPException(status_code=404, detail="No tenés una suscripción para dar de baja.")

    pid = sub.get("proveedor_id")
    if pid:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.put(f"{MP_API}/preapproval/{pid}", headers=_mp_headers(), json={"status": "cancelled"})
            # La doc de MP escribe el valor de las dos formas según la página.
            if r.status_code not in (200, 201):
                r = await c.put(f"{MP_API}/preapproval/{pid}", headers=_mp_headers(), json={"status": "canceled"})
            if r.status_code not in (200, 201):
                print(f"[MP] cancelar {pid} HTTP {r.status_code}: {r.text[:400]}")
                raise HTTPException(status_code=502, detail=f"Mercado Pago no pudo cancelar: {r.text[:300]}")

    # Si nunca llegó a cobrarse (estado pendiente), la baja es inmediata.
    fin = _parse_fecha(sub.get("proximo_cobro")) or _parse_fecha(sub.get("periodo_fin"))
    patch = {"estado": "cancelada", "cancelada_en": _iso(_ahora_utc())}
    if sub.get("estado") in ("activa", "en_gracia") and fin:
        patch["periodo_fin"] = _iso(fin)
    await _sb_patch(f"suscripciones?id=eq.{sub['id']}", patch)

    nueva = {**sub, **patch}
    await _sync_metadata_plan(owner_id, nueva)

    print(f"[MP] {owner_id} dio de baja la suscripción {pid}")
    return {
        "ok": True,
        "acceso_hasta": patch.get("periodo_fin"),
        "plan_efectivo": _plan_efectivo(nueva),
    }


# ══════════════════════════════════════════════
# POST /mp/webhook — fuente de verdad del plan
# ══════════════════════════════════════════════

def _validar_firma_mp(request: Request, data_id: str) -> bool:
    """
    x-signature: 'ts=1742505638683,v1=<hex>'
    manifest:    'id:<data.id>;request-id:<x-request-id>;ts:<ts>;'
    HMAC-SHA256 hex con MP_WEBHOOK_SECRET (Tus integraciones → Webhooks).
    Los tramos cuyo valor no viene se omiten del manifest.
    """
    secret = _mp_secret()
    if not secret:
        return False

    firma = request.headers.get("x-signature", "")
    req_id = request.headers.get("x-request-id", "")
    ts = v1 = ""
    for parte in firma.split(","):
        if "=" not in parte:
            continue
        k, _, v = parte.partition("=")
        k, v = k.strip(), v.strip()
        if k == "ts":
            ts = v
        elif k == "v1":
            v1 = v
    if not ts or not v1:
        return False

    manifest = ""
    if data_id:
        manifest += f"id:{data_id};"
    if req_id:
        manifest += f"request-id:{req_id};"
    manifest += f"ts:{ts};"

    esperado = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)


async def _mp_get(path: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{MP_API}{path}", headers=_mp_headers())
        if r.status_code == 200:
            return r.json()
        print(f"[MP] GET {path} HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        print(f"[MP] GET {path} excepción: {e}")
    return None


# Mapa status de MP → estado nuestro. 'pending' se queda esperando la tarjeta.
_MAP_ESTADO_MP = {
    "authorized": "activa",
    "pending":    "pendiente",
    "paused":     "pausada",
    "cancelled":  "cancelada",
    "canceled":   "cancelada",
    "finished":   "cancelada",
}


async def _procesar_preapproval(preapproval_id: str):
    mp = await _mp_get(f"/preapproval/{preapproval_id}")
    if not mp:
        return

    ext = mp.get("external_reference") or ""
    partes = ext.split("|")
    owner_id = partes[0] if partes else ""
    plan_id  = partes[1] if len(partes) > 1 else ""
    periodo  = partes[2] if len(partes) > 2 else "mensual"

    sub = await _sub_por_preapproval(str(preapproval_id))
    if not sub and not owner_id:
        print(f"[MP] preapproval {preapproval_id} sin suscripción local ni external_reference — ignorado")
        return
    owner_id = owner_id or sub.get("owner_id")

    estado = _MAP_ESTADO_MP.get(mp.get("status"), "pendiente")
    prox   = _parse_fecha(mp.get("next_payment_date"))
    ini    = _parse_fecha((mp.get("auto_recurring") or {}).get("start_date")) or _parse_fecha(mp.get("date_created"))

    patch = {
        "estado": estado,
        "proveedor_payer_id": str(mp.get("payer_id") or "") or None,
        "proximo_cobro": _iso(prox),
        "periodo_inicio": _iso(ini),
        "periodo_fin": _iso(prox),
        "raw": mp,
    }
    if estado == "activa":
        # Volvió a estar al día: se limpia la gracia.
        patch["gracia_hasta"] = None
    if estado == "cancelada":
        patch["cancelada_en"] = _iso(_ahora_utc())

    if sub:
        await _sb_patch(f"suscripciones?id=eq.{sub['id']}", patch)
    elif plan_id in PLANES:
        # La suscripción se creó fuera de la app (o perdimos la fila).
        monto = float((mp.get("auto_recurring") or {}).get("transaction_amount") or 0)
        await _sb_post("suscripciones", {
            "owner_id": owner_id, "plan": plan_id, "periodo": periodo,
            "proveedor": "mercadopago", "proveedor_id": str(preapproval_id),
            "external_reference": ext, "entorno": _mp_entorno(),
            "importe_ars": monto, "iva_pct": IVA_PCT, **patch,
        })
    else:
        print(f"[MP] preapproval {preapproval_id}: plan '{plan_id}' desconocido — no se aplica")
        return

    actual = await _sub_vigente(owner_id) or await _sub_por_preapproval(str(preapproval_id))
    await _sync_metadata_plan(owner_id, actual)
    print(f"[MP] preapproval {preapproval_id} → {estado} (usuario {owner_id}, plan {plan_id})")


async def _procesar_pago_autorizado(pago_id: str):
    """
    subscription_authorized_payment: cada cobro del débito automático.
    Alimenta el historial (comprobantes) y dispara la gracia si rebota.
    """
    mp = await _mp_get(f"/authorized_payments/{pago_id}")
    if not mp:
        return

    preapproval_id = str(mp.get("preapproval_id") or "")
    sub = await _sub_por_preapproval(preapproval_id) if preapproval_id else None
    if not sub:
        print(f"[MP] authorized_payment {pago_id} sin suscripción local (preapproval {preapproval_id})")
        return

    pago   = mp.get("payment") or {}
    estado_mp = (pago.get("status") or mp.get("status") or "").lower()
    estado = {
        "approved": "aprobado", "processed": "aprobado",
        "rejected": "rechazado", "cancelled": "cancelado", "canceled": "cancelado",
        "refunded": "reintegrado", "charged_back": "reintegrado",
    }.get(estado_mp, "pendiente")

    tarjeta = (pago.get("card") or {})
    await _sb_upsert("suscripcion_pagos", {
        "owner_id": sub["owner_id"],
        "suscripcion_id": sub["id"],
        "proveedor": "mercadopago",
        "proveedor_pago_id": str(pago.get("id") or pago_id),
        "proveedor_preapproval_id": preapproval_id,
        "estado": estado,
        "importe": float(mp.get("transaction_amount") or pago.get("transaction_amount") or 0),
        "moneda": mp.get("currency_id") or "ARS",
        "fecha": _iso(_parse_fecha(mp.get("date_created")) or _ahora_utc()),
        "periodo_desde": _iso(_parse_fecha(mp.get("debit_date"))),
        "metodo": pago.get("payment_method_id"),
        "ultimos4": (tarjeta.get("last_four_digits") or None),
        "motivo_rechazo": pago.get("status_detail") if estado == "rechazado" else None,
        "raw": mp,
    }, on_conflict="proveedor_pago_id")

    ahora = _ahora_utc()
    if estado == "aprobado":
        await _sb_patch(f"suscripciones?id=eq.{sub['id']}", {"estado": "activa", "gracia_hasta": None})
        # Releer el preapproval para tomar el next_payment_date real: el
        # authorized_payment solo trae next_retry_date, que existe cuando el
        # cobro rebota, no cuando sale bien.
        await _procesar_preapproval(preapproval_id)
        return
    elif estado == "rechazado":
        # Primer rebote: arranca la ventana de gracia (acceso completo).
        # Si ya estaba en gracia, se respeta la fecha original.
        if sub.get("estado") == "activa" or not sub.get("gracia_hasta"):
            await _sb_patch(f"suscripciones?id=eq.{sub['id']}", {
                "estado": "en_gracia",
                "gracia_hasta": _iso(ahora + timedelta(days=DIAS_GRACIA)),
            })
            print(f"[MP] cobro rechazado en {sub['id']} → gracia {DIAS_GRACIA} días")

    actual = await _sub_por_preapproval(preapproval_id)
    await _sync_metadata_plan(sub["owner_id"], actual)


@app.post("/mp/webhook")
async def mp_webhook(request: Request):
    # data.id llega por querystring en la mayoría de las notificaciones y en
    # el body en el resto. El manifest de la firma usa el de la querystring.
    try:
        body = await request.json()
    except Exception:
        body = {}

    data_id = request.query_params.get("data.id") or str((body.get("data") or {}).get("id") or "")
    if data_id and data_id.isalnum() and not data_id.isdigit():
        data_id = data_id.lower()

    tipo = (request.query_params.get("type") or body.get("type") or body.get("topic") or "").strip()

    if not _mp_secret():
        # Sin secreto no podemos distinguir una notificación real de una
        # falsificada, y esto otorga planes pagos. Devolvemos 200 para que MP
        # no reintente en loop, pero NO tocamos nada.
        print("[MP][WEBHOOK] ⚠️ MP_WEBHOOK_SECRET no configurado — notificación ignorada por seguridad")
        return {"ok": True, "ignorado": "sin MP_WEBHOOK_SECRET"}

    if not _validar_firma_mp(request, data_id):
        print(f"[MP][WEBHOOK] firma inválida (type={tipo} id={data_id})")
        raise HTTPException(status_code=401, detail="Firma inválida")

    print(f"[MP][WEBHOOK] {tipo} · data.id={data_id}")

    try:
        if tipo == "subscription_preapproval" and data_id:
            await _procesar_preapproval(data_id)
        elif tipo == "subscription_authorized_payment" and data_id:
            await _procesar_pago_autorizado(data_id)
        elif tipo == "subscription_preapproval_plan":
            pass  # no usamos planes preaprobados
        elif tipo == "payment":
            pass  # los cobros de suscripción llegan por authorized_payment
        else:
            print(f"[MP][WEBHOOK] topic no manejado: {tipo}")
    except Exception as e:
        # Nunca devolvemos 500: MP reintentaría y ya guardamos el log.
        print(f"[MP][WEBHOOK] excepción procesando {tipo}/{data_id}: {e}")

    return {"ok": True}


# ══════════════════════════════════════════════
# GET /mp/suscripcion — estado para la app
# ══════════════════════════════════════════════

@app.get("/mp/suscripcion")
async def mp_estado_suscripcion(request: Request):
    """
    El frontend lee la tabla directo con _restGet (RLS filtra por auth.uid()).
    Este endpoint existe para diagnóstico y para clientes que no sean la web.
    """
    user = await _auth_user(request)
    sub  = await _sub_vigente(user["id"])
    return {
        "ok": True,
        "suscripcion": sub,
        "plan_efectivo": _plan_efectivo(sub),
        "dias_gracia": DIAS_GRACIA,
    }


@app.get("/mp/planes")
async def get_planes():
    bna = await _dolar_venta_bna()
    salida = {}
    for pid, p in PLANES.items():
        usd_m, usd_m_iva, ars_m = _precio(pid, "mensual", bna)
        usd_a, usd_a_iva, ars_a = _precio(pid, "anual", bna)
        salida[pid] = {
            **p,
            "precio_usd": usd_m, "precio_ars": ars_m,
            "precio_usd_anual": usd_a, "precio_ars_anual": ars_a,
        }
    return {"ok": True, "planes": salida, "bna": bna, "iva_pct": IVA_PCT,
            "descuento_anual_pct": DESCUENTO_ANUAL_PCT, "entorno": _mp_entorno()}


@app.get("/mp/diagnostico")
async def mp_diagnostico():
    """Chequeo rápido de que Railway tenga las env vars y que MP responda."""
    out = {
        "MP_ACCESS_TOKEN": bool(_mp_token()),
        "MP_WEBHOOK_SECRET": bool(_mp_secret()),
        "entorno": _mp_entorno(),
        "dias_gracia": DIAS_GRACIA,
        "admins": len(ADMIN_USER_IDS),
    }
    if _mp_token():
        r = await _mp_get("/users/me")
        out["mp_cuenta"] = {"id": r.get("id"), "nickname": r.get("nickname"),
                            "site_id": r.get("site_id")} if r else "sin respuesta"
    for t in ("suscripciones", "suscripcion_pagos"):
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                rr = await c.get(f"{_sb_url()}/rest/v1/{t}", headers=_sb_headers(), params={"limit": "1"})
            out[t] = {"status": rr.status_code, "ok": rr.status_code == 200}
        except Exception as e:
            out[t] = {"ok": False, "error": str(e)}
    return out


# ══════════════════════════════════════════════
# Reajuste manual por movimiento del dólar (admin)
# ══════════════════════════════════════════════
# Política elegida: el monto en ARS queda FIJO desde el alta. Cuando el dólar
# se despega, estos endpoints te listan las suscripciones desfasadas y te
# dejan actualizarlas a mano. Nada automático que le cambie el débito al
# cliente sin que vos lo decidas.

async def _exigir_admin(request: Request) -> dict:
    user = await _auth_user(request)
    if user["id"] not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403, detail="Solo administradores.")
    return user


@app.get("/mp/admin/desfasaje")
async def mp_admin_desfasaje(request: Request, umbral: float = 10.0):
    await _exigir_admin(request)
    bna = await _dolar_venta_bna()
    filas = await _sb_get("suscripciones", {
        "estado": "in.(activa,en_gracia,pausada)",
        "select": "id,owner_id,plan,periodo,importe_ars,importe_usd,tc_alta,proximo_cobro",
        "order": "created_at.asc",
    })
    out = []
    for f in filas:
        if f.get("plan") not in PLANES:
            continue
        _, _, ars_hoy = _precio(f["plan"], f.get("periodo") or "mensual", bna)
        actual = float(f.get("importe_ars") or 0)
        if actual <= 0:
            continue
        dif_pct = round((ars_hoy - actual) / actual * 100, 2)
        if abs(dif_pct) >= umbral:
            out.append({**f, "importe_ars_hoy": ars_hoy, "diferencia_pct": dif_pct})
    return {"ok": True, "bna": bna, "umbral": umbral, "desfasadas": out, "total": len(out)}


@app.post("/mp/admin/reajustar")
async def mp_admin_reajustar(request: Request):
    await _exigir_admin(request)
    body = await request.json()
    ids  = body.get("suscripcion_ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="Mandá suscripcion_ids: [...]")

    bna = await _dolar_venta_bna()
    resultados = []
    for sid in ids:
        filas = await _sb_get("suscripciones", {"id": f"eq.{sid}", "limit": "1"})
        if not filas:
            resultados.append({"id": sid, "ok": False, "detalle": "no existe"})
            continue
        sub = filas[0]
        if sub.get("plan") not in PLANES:
            resultados.append({"id": sid, "ok": False, "detalle": "plan desconocido"})
            continue
        usd_base, _, ars = _precio(sub["plan"], sub.get("periodo") or "mensual", bna)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.put(
                f"{MP_API}/preapproval/{sub['proveedor_id']}",
                headers=_mp_headers(),
                json={"auto_recurring": {"transaction_amount": float(ars), "currency_id": "ARS"}},
            )
        if r.status_code in (200, 201):
            await _sb_patch(f"suscripciones?id=eq.{sid}",
                            {"importe_ars": ars, "importe_usd": usd_base, "tc_alta": bna})
            resultados.append({"id": sid, "ok": True, "importe_ars": ars})
        else:
            print(f"[MP] reajuste {sid} HTTP {r.status_code}: {r.text[:300]}")
            resultados.append({"id": sid, "ok": False, "detalle": r.text[:200]})
    return {"ok": True, "bna": bna, "resultados": resultados}


# ══════════════════════════════════════════════
# Job diario: vencer la gracia
# ══════════════════════════════════════════════

async def _suscripciones_vencer_gracia():
    """
    MP no nos avisa cuando se agota la paciencia: pasa en_gracia → impaga
    cuando gracia_hasta quedó atrás. A partir de ahí la app entra en solo
    lectura (los datos siguen todos visibles y exportables).
    """
    ahora = _ahora_utc()
    filas = await _sb_get("suscripciones", {
        "estado": "eq.en_gracia",
        "gracia_hasta": f"lt.{ahora.isoformat()}",
        "select": "id,owner_id,plan,periodo,estado,periodo_fin",
    })
    for f in filas:
        await _sb_patch(f"suscripciones?id=eq.{f['id']}", {"estado": "impaga"})
        await _sync_metadata_plan(f["owner_id"], {**f, "estado": "impaga"})
        print(f"[MP] gracia vencida: suscripción {f['id']} → impaga")
    if filas:
        print(f"[MP] {len(filas)} suscripción(es) pasaron a impaga")


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
    # Job 4 — Suscripciones: vencer la ventana de gracia. Diario 6:00 AR.
    # MP no nos avisa cuando se agota la paciencia; este job pasa
    # en_gracia → impaga y sincroniza el plan efectivo.
    scheduler.add_job(
        _suscripciones_vencer_gracia,
        CronTrigger(hour=6, minute=0, timezone=AR_TZ),
        id="suscripciones_vencer_gracia", replace_existing=True,
    )
    scheduler.start()
    print("[SCHEDULER] Iniciado con 4 jobs activos")

    if kapso.configurado() and not os.environ.get("KAPSO_WEBHOOK_SECRET"):
        print("[KAPSO] ⚠️  KAPSO_WEBHOOK_SECRET sin configurar: /whatsapp/kapso "
              "acepta cualquier POST. Configuralo antes de salir a producción.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
