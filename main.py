from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
import httpx
import asyncio
from datetime import datetime, date, timedelta
import os
import json
import re
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

app = FastAPI(title="RindeAgro Server", version="2.0.0")
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
    if not rows:
        return None
    conv = rows[0]
    estado = conv.get("estado", "completado")
    if estado == "completado":
        return None
    # Verificar timeout de 30 minutos
    actualizado = conv.get("actualizado_en", "")
    if actualizado:
        try:
            dt = datetime.fromisoformat(actualizado.replace("Z", "+00:00"))
            diff_min = (datetime.now(pytz.utc) - dt).total_seconds() / 60
            if diff_min > CONV_TIMEOUT_MIN:
                await _wa_clear_conv(numero)
                return None
        except Exception:
            pass
    conv["numero_whatsapp"] = numero
    return conv

async def _wa_set_conv(numero: str, estado: str, datos: dict = None):
    """Crea o actualiza el estado de conversación."""
    await _sb_upsert(
        "wa_conversaciones",
        {
            "numero_whatsapp": numero,
            "estado": estado,
            "datos_parciales": datos or {},
            "actualizado_en": datetime.utcnow().isoformat(),
        },
        on_conflict="numero_whatsapp",
    )

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
    "🌾 *RindeAgro* - ¿Qué querés hacer?\n\n"
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
        rows = await _sb_get("perfiles", {"whatsapp": f"eq.{numero}", "select": "id"})
        if rows:
            await _sb_patch(
                f"wa_preferencias_notificaciones?user_id=eq.{rows[0]['id']}",
                {"activo": False},
            )
        await _wa_clear_conv(numero)
        return (
            "✅ Listo, no vas a recibir más mensajes de RindeAgro.\n\n"
            "Si querés volver a activarlos escribí *ACTIVAR* en cualquier momento."
        )
    if n in ACTIVAR_WORDS or any(aw in n for aw in ACTIVAR_WORDS):
        rows = await _sb_get("perfiles", {"whatsapp": f"eq.{numero}", "select": "id"})
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
    campos = usuario.get("campos", [])

    if opcion == "1":  # Gasto
        if not campos:
            return "⚠️ No tenés campos registrados. Ingresá a rindeagro.lat para crear uno."
        if len(campos) == 1:
            datos = {"campo_id": campos[0]["id"], "campo_nombre": campos[0]["nombre"]}
            await _wa_set_conv(numero, "gasto_rubro", datos)
            return f"Campo: *{campos[0]['nombre']}*\n\n¿Qué rubro?\n\n" + "\n".join(f"• {r}" for r in RUBROS)
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

    # ── 0. STOP / ACTIVAR (no bloquea aunque Supabase falle) ───────────
    stop_resp = await _manejar_stop_activar(numero, texto)
    if stop_resp:
        return stop_resp

    # ── 1. Transcribir audio / PDF antes de cualquier routing ──────────
    # (no necesita Supabase)
    texto_final = texto
    if media_url and "audio" in media_type and OPENAI_API_KEY:
        texto_final = await transcribir_audio(media_url, OPENAI_API_KEY) or texto
    if media_url and "pdf" in media_type:
        texto_final = await extraer_pdf(media_url) or texto
    if not texto_final:
        return "No entendí el mensaje. Podés escribir, mandar audio o adjuntar un PDF."

    n = _norm(texto_final)

    # ── 2. Comandos de menú puro → responder sin necesitar Supabase ─────
    # "hola", "menú", "start", etc. muestran el menú siempre, incluso si
    # Supabase no está configurado o hay un error de conexión.
    if n in MENU_WORDS:
        return MENU_TEXT

    # ── 3. Validar que Supabase esté configurado ────────────────────────
    # A partir de acá todas las operaciones necesitan la BD.
    if not _sb_url() or not _sb_key():
        print(f"[WA] ⚠️ SUPABASE_URL o SUPABASE_SERVICE_KEY no configurados")
        return MENU_TEXT  # fallback seguro: al menos el usuario ve el menú

    # ── 4. Identificar usuario ──────────────────────────────────────────
    rows = await _sb_get("perfiles", {"whatsapp": f"eq.{numero}", "select": "id,nombre,campos(id,nombre)"})
    if not rows:
        return (
            f"⚠️ Tu número {numero} no está vinculado a ninguna cuenta RindeAgro.\n"
            "Ingresá a rindeagro.lat y vinculá tu WhatsApp en Configuración."
        )
    usuario = rows[0]

    # ── 5. ¿Hay una conversación activa en curso? ───────────────────────
    conv = await _wa_get_conv(numero)
    if conv:
        return await _procesar_flujo(conv, texto_final, usuario)

    # ── 6. Selección de opción del menú principal ───────────────────────
    if n in {"1", "2", "3", "4", "0"}:
        return await _procesar_opcion_menu(n, usuario, numero)

    # ── 7. Mensaje libre → intentar con IA; si no → menú ───────────────
    if OPENAI_API_KEY and len(texto_final) > 8:
        resultado = await interpretar_con_ia(texto_final, usuario, OPENAI_API_KEY)
        if resultado and resultado.get("confianza") in ("alta", "media"):
            return await cargar_en_supabase(resultado, usuario, _sb_url(), _sb_key())

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
        rows = await _sb_get("perfiles", {"id": f"eq.{uid}", "select": "id,nombre,whatsapp"})
        if rows and rows[0].get("whatsapp"):
            destinatarios.append({
                "user_id": uid,
                "numero":  rows[0]["whatsapp"],
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
        "📊 *RindeAgro* - Recordatorio semanal\n\n"
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
        f"🔔 *Alerta de precio RindeAgro*\n\n"
        f"Precio actual de *Soja*: USD {precio_actual}/t\n\n"
        "¿Querés revisar tus márgenes? Entrá a rindeagro.lat"
    )
    for d in destinatarios:
        await _wa_enviar(d["numero"], mensaje)
        await asyncio.sleep(0.5)


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
        razon = f"RindeAgro · Plan {plan['nombre']} Anual"
    else:
        precio_usd = plan["precio_usd"]
        razon = f"RindeAgro · Plan {plan['nombre']} Mensual"

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
