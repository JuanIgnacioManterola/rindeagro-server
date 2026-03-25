from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
import asyncio
from datetime import datetime, date
import os
import json
import re
from bs4 import BeautifulSoup

app = FastAPI(title="RindeAgro Server", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Cache de precios en memoria ──
cache_precios = {
    "cereales": {"soja": 341, "maiz": 181, "trigo": 183, "girasol": 390, "sorgo": 189},
    "bna": 1387,
    "ultima_actualizacion": None,
    "fuente": "referencia"
}

# ──────────────────────────────────────────
# PRECIOS CEREALES — scraping BCR Rosario
# ──────────────────────────────────────────
async def scrape_cereales(bna: float = 1385):
    """
    Obtiene precios pizarra Rosario desde Agrofy.
    Los precios de Agrofy vienen en PESOS — se dividen por el BNA para obtener USD/t.
    Precio tipico: soja ~$465.000 ARS / 1385 BNA = ~335 USD/t (correcto para productor)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:

        # ── Agrofy precios pizarra (en pesos ARS) ──
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
                # Buscar precios en pesos: formato "465.000" o "465000" o "$ 465.000"
                import re as _re
                for cereal, aliases in mapeo.items():
                    for alias in aliases:
                        # patron: nombre del cereal + precio en pesos (6 digitos tipico)
                        pat = alias + r"[^0-9]{0,60}?(\d{2,3}[\.,]\d{3})"
                        m = _re.search(pat, texto.lower())
                        if m:
                            raw = m.group(1).replace(".", "").replace(",", "")
                            val_pesos = float(raw)
                            if val_pesos > 50000:  # precio en pesos razonable (>50k ARS)
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
    """Parsea respuesta JSON de Agrofy"""
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
    """Parsea tabla HTML de precios de Agrofy"""
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
                            except:
                                continue
    return encontrados


async def fetch_dolar_bna():
    """Obtiene el dolar divisa tipo comprador BNA"""
    async with httpx.AsyncClient(timeout=8) as client:
        # 1. dolarapi /mayorista = divisa comprador BNA
        try:
            r = await client.get("https://dolarapi.com/v1/dolares/mayorista")
            if r.status_code == 200:
                j = r.json()
                val = j.get("compra") or j.get("venta")
                if val and float(val) > 100:
                    return round(float(val), 2), "BNA divisa comprador"
        except Exception as e:
            print(f"Dolar mayorista error: {e}")

        # 2. argentinadatos /mayorista
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

        # 3. Scraping BNA directo
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
                            except:
                                pass
        except Exception as e:
            print(f"Scraping BNA error: {e}")

    return None, None


# ──────────────────────────────────────────
# ENDPOINT: GET /precios
# ──────────────────────────────────────────
@app.get("/precios")
async def get_precios():
    global cache_precios
    
    ahora = datetime.now()
    ultima = cache_precios.get("ultima_actualizacion")
    
    # Refrescar si pasaron más de 60 minutos o nunca se actualizó
    necesita_refresh = (
        ultima is None or
        (ahora - ultima).total_seconds() > 3600
    )
    
    if necesita_refresh:
        # Primero obtener BNA, luego usarlo para convertir pesos a USD en cereales
        bna, fuente_d = await fetch_dolar_bna()
        bna_val = bna if bna else cache_precios["bna"]

        cereales, fuente_c = await scrape_cereales(bna=bna_val)

        if cereales:
            cache_precios["cereales"].update(cereales)
            cache_precios["fuente"] = fuente_c or "Pizarra Rosario"

        if bna:
            cache_precios["bna"] = bna

        cache_precios["ultima_actualizacion"] = ahora
    
    return {
        "ok": True,
        "cereales": cache_precios["cereales"],
        "bna": cache_precios["bna"],
        "fuente": cache_precios["fuente"],
        "actualizado": cache_precios["ultima_actualizacion"].isoformat() if cache_precios["ultima_actualizacion"] else None,
        "fecha": date.today().isoformat()
    }


# ──────────────────────────────────────────
# ENDPOINT: POST /whatsapp
# Recibe mensajes de Twilio WhatsApp
# ──────────────────────────────────────────
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    
    from_number = form.get("From", "").replace("whatsapp:", "")
    body = form.get("Body", "").strip()
    media_url = form.get("MediaUrl0", "")
    media_type = form.get("MediaContentType0", "")
    
    print(f"[WA] De: {from_number} | Mensaje: {body[:100]} | Media: {media_type}")
    
    # Respuesta TwiML básica
    response_text = await procesar_mensaje_whatsapp(from_number, body, media_url, media_type)
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{response_text}</Message>
</Response>"""
    
    from fastapi.responses import Response
    return Response(content=twiml, media_type="application/xml")


async def procesar_mensaje_whatsapp(numero: str, texto: str, media_url: str, media_type: str):
    """Procesa el mensaje y lo carga en Supabase"""
    
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    
    if not SUPABASE_URL or not SUPABASE_KEY:
        return "⚠️ Servidor en configuración. Pronto vas a poder cargar datos por acá."
    
    # ── 1. Buscar usuario por número de WhatsApp ──
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/perfiles",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                params={"whatsapp": f"eq.{numero}", "select": "id,nombre,campos(id,nombre)"}
            )
            if r.status_code != 200 or not r.json():
                return f"⚠️ Tu número {numero} no está vinculado a ninguna cuenta RindeAgro. Ingresá a la plataforma y vincular tu WhatsApp en Configuración."
            
            usuario = r.json()[0]
        except Exception as e:
            print(f"Supabase error: {e}")
            return "⚠️ Error de conexión. Intentá de nuevo en unos minutos."
    
    # ── 2. Si es audio, transcribir con Whisper ──
    texto_final = texto
    if media_url and "audio" in media_type and OPENAI_API_KEY:
        texto_final = await transcribir_audio(media_url, OPENAI_API_KEY) or texto
    
    # ── 3. Si es PDF, extraer texto ──
    if media_url and "pdf" in media_type:
        texto_final = await extraer_pdf(media_url) or texto
    
    if not texto_final:
        return "No entendí el mensaje. Podés escribir, mandar audio o adjuntar un PDF."
    
    # ── 4. Interpretar con IA ──
    if OPENAI_API_KEY:
        resultado = await interpretar_con_ia(texto_final, usuario, OPENAI_API_KEY)
        if resultado:
            return await cargar_en_supabase(resultado, usuario, SUPABASE_URL, SUPABASE_KEY)
    
    return "Recibí tu mensaje. Próximamente la carga automática va a estar activa."


async def transcribir_audio(media_url: str, api_key: str) -> str:
    """Transcribe audio con OpenAI Whisper"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Descargar el audio
            audio_resp = await client.get(media_url)
            audio_bytes = audio_resp.content
            
            # Enviar a Whisper
            r = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
                data={"model": "whisper-1", "language": "es"}
            )
            if r.status_code == 200:
                return r.json().get("text", "")
    except Exception as e:
        print(f"Whisper error: {e}")
    return ""


async def extraer_pdf(media_url: str) -> str:
    """Extrae texto de PDF adjunto"""
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
    """Usa GPT para extraer datos estructurados del mensaje"""
    campos_nombres = [c["nombre"] for c in usuario.get("campos", [])]
    
    prompt_sistema = f"""Sos un asistente agrícola argentino. Extraé datos del mensaje del productor.
Campos disponibles: {', '.join(campos_nombres) if campos_nombres else 'ninguno registrado'}.
Respondé SOLO con JSON válido, sin explicaciones. Formato:
{{
  "tipo": "gasto|lluvia|rendimiento|suelo",
  "campo_nombre": "nombre exacto del campo o null",
  "datos": {{
    // Para gasto: "rubro", "descripcion", "cantidad", "unidad", "precio_unitario", "total_usd", "fecha"
    // Para lluvia: "mm", "fecha"
    // Para rendimiento: "rendimiento_tha", "precio_usd_t", "fecha"
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
                        {"role": "user", "content": texto}
                    ],
                    "temperature": 0.1,
                    "max_tokens": 400
                }
            )
            if r.status_code == 200:
                contenido = r.json()["choices"][0]["message"]["content"]
                # Limpiar markdown si viene con ```json
                contenido = re.sub(r"```json|```", "", contenido).strip()
                return json.loads(contenido)
    except Exception as e:
        print(f"IA error: {e}")
    return None


async def cargar_en_supabase(datos: dict, usuario: dict, sb_url: str, sb_key: str) -> str:
    """Inserta los datos en Supabase y devuelve confirmación"""
    
    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    
    # Encontrar ID del campo
    campo_id = None
    campo_nombre = datos.get("campo_nombre")
    if campo_nombre:
        for c in usuario.get("campos", []):
            if campo_nombre.lower() in c["nombre"].lower():
                campo_id = c["id"]
                break
    
    if not campo_id and usuario.get("campos"):
        # Si solo tiene un campo, usarlo por defecto
        if len(usuario["campos"]) == 1:
            campo_id = usuario["campos"][0]["id"]
            campo_nombre = usuario["campos"][0]["nombre"]
    
    tipo = datos.get("tipo")
    d = datos.get("datos", {})
    respuesta = datos.get("respuesta_usuario", "")
    
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            if tipo == "gasto" and campo_id:
                payload = {
                    "campo_id": campo_id,
                    "usuario_id": usuario["id"],
                    "rubro": d.get("rubro", "otros"),
                    "descripcion": d.get("descripcion", ""),
                    "fecha": d.get("fecha") or date.today().isoformat(),
                    "total_de_usd": float(d.get("total_usd", 0)),
                    "cantidad": d.get("cantidad"),
                    "unidad": d.get("unidad"),
                    "precio_unitario": d.get("precio_unitario"),
                    "fuente": "whatsapp"
                }
                r = await client.post(f"{sb_url}/rest/v1/gastos", headers=headers, json=payload)
                if r.status_code in (200, 201):
                    return f"✅ {respuesta}\n\n_Gasto cargado en {campo_nombre or 'tu campo'}_"
            
            elif tipo == "lluvia" and campo_id:
                payload = {
                    "campo_id": campo_id,
                    "usuario_id": usuario["id"],
                    "mm": float(d.get("mm", 0)),
                    "fecha": d.get("fecha") or date.today().isoformat(),
                    "fuente": "whatsapp"
                }
                r = await client.post(f"{sb_url}/rest/v1/lluvias", headers=headers, json=payload)
                if r.status_code in (200, 201):
                    return f"✅ {respuesta}\n\n_Lluvia registrada en {campo_nombre}_"
            
            elif tipo == "rendimiento" and campo_id:
                payload = {"rendimiento": float(d.get("rendimiento_tha", 0))}
                if d.get("precio_usd_t"):
                    payload["precio_venta"] = float(d["precio_usd_t"])
                r = await client.patch(
                    f"{sb_url}/rest/v1/campos?id=eq.{campo_id}",
                    headers=headers, json=payload
                )
                if r.status_code in (200, 204):
                    return f"✅ {respuesta}\n\n_Rendimiento actualizado en {campo_nombre}_"
            
            else:
                if not campo_id:
                    return f"⚠️ No pude identificar el campo. Tus campos: {', '.join(c['nombre'] for c in usuario.get('campos',[]))}"
                return f"⚠️ No entendí el tipo de dato. Podés cargar: gastos, lluvias o rendimientos."
        
        except Exception as e:
            print(f"Supabase insert error: {e}")
            return "⚠️ Error al guardar. Intentá de nuevo."
    
    return "⚠️ No se pudo guardar el dato."


# ──────────────────────────────────────────
# MERCADO PAGO — SUSCRIPCIONES
# ──────────────────────────────────────────

PLANES = {
    "lote":        {"nombre": "Lote",        "precio": 29,  "descripcion": "Hasta 5 campos · Todos los módulos · WhatsApp"},
    "agronomo":    {"nombre": "Agrónomo",     "precio": 36,  "descripcion": "20 productores · Panel multi-productor"},
    "corporativo": {"nombre": "Corporativo",  "precio": 45,  "descripcion": "Campos ilimitados · 5 usuarios"},
}

@app.post("/mp/crear-suscripcion")
async def crear_suscripcion(request: Request):
    """Crea un plan de suscripción en Mercado Pago y devuelve la URL de pago"""
    MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
    SERVER_URL = os.environ.get("SERVER_URL", "https://rindeagro-server-production.up.railway.app")

    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN no configurado")

    body = await request.json()
    plan_id = body.get("plan")
    usuario_id = body.get("usuario_id")
    email = body.get("email")

    if plan_id not in PLANES:
        raise HTTPException(status_code=400, detail="Plan inválido")

    plan = PLANES[plan_id]

    async with httpx.AsyncClient(timeout=15) as client:
        # Crear preapproval_plan — MP genera un link de pago donde el usuario ingresa su tarjeta
        r = await client.post(
            "https://api.mercadopago.com/preapproval_plan",
            headers={
                "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "reason": f"RindeAgro · Plan {plan['nombre']}",
                "external_reference": f"{usuario_id}|{plan_id}",
                "auto_recurring": {
                    "frequency": 1,
                    "frequency_type": "months",
                    "transaction_amount": plan["precio"],
                    "currency_id": "ARS"
                },
                "back_url": "https://juanignaciomanterola.github.io/Rindeagro",
                "notification_url": f"{SERVER_URL}/mp/webhook",
                "payment_methods_allowed": {
                    "payment_types": [{"id": "credit_card"}, {"id": "debit_card"}]
                }
            }
        )

        print(f"MP preapproval_plan response {r.status_code}: {r.text}")

        if r.status_code not in (200, 201):
            raise HTTPException(status_code=500, detail=f"Error MP: {r.text}")

        data = r.json()
        init_point = data.get("init_point") or data.get("sandbox_init_point")

        if not init_point:
            raise HTTPException(status_code=500, detail="MP no devolvió URL de pago")

        return {
            "ok": True,
            "init_point": init_point,
            "plan_mp_id": data.get("id"),
            "plan": plan_id
        }


@app.post("/mp/webhook")
async def mp_webhook(request: Request):
    """Recibe notificaciones de Mercado Pago cuando se procesa un pago"""
    MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

    body = await request.json()
    print(f"[MP Webhook] {json.dumps(body)}")

    tipo = body.get("type")
    data_id = body.get("data", {}).get("id")

    if tipo == "subscription_preapproval" and data_id:
        async with httpx.AsyncClient(timeout=10) as client:
            # Consultar detalles de la suscripción
            r = await client.get(
                f"https://api.mercadopago.com/preapproval/{data_id}",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
            )
            if r.status_code == 200:
                sus = r.json()
                estado = sus.get("status")
                ref = sus.get("external_reference", "")

                if "|" in ref:
                    usuario_id, plan_id = ref.split("|", 1)

                    # Actualizar estado en Supabase
                    if SUPABASE_URL and SUPABASE_KEY:
                        headers = {
                            "apikey": SUPABASE_KEY,
                            "Authorization": f"Bearer {SUPABASE_KEY}",
                            "Content-Type": "application/json"
                        }
                        await client.patch(
                            f"{SUPABASE_URL}/rest/v1/perfiles?id=eq.{usuario_id}",
                            headers=headers,
                            json={
                                "plan": plan_id if estado == "authorized" else "semilla",
                                "suscripcion_mp_id": data_id,
                                "suscripcion_estado": estado
                            }
                        )
                        print(f"[MP] Usuario {usuario_id} → plan {plan_id} ({estado})")

    return {"ok": True}


@app.get("/mp/planes")
async def get_planes():
    """Devuelve los planes disponibles con sus precios"""
    return {"ok": True, "planes": PLANES}


# ──────────────────────────────────────────
# ENDPOINT: GET /debug-env (temporal)
# ──────────────────────────────────────────
@app.get("/debug-env")
async def debug_env():
    token = os.environ.get("MP_ACCESS_TOKEN", "")
    return {
        "MP_ACCESS_TOKEN_set": bool(token),
        "MP_ACCESS_TOKEN_length": len(token),
        "MP_ACCESS_TOKEN_preview": token[:10] + "..." if token else "VACIO"
    }


# ──────────────────────────────────────────
# ENDPOINT: GET /health
# ──────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
