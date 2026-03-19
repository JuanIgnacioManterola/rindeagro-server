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
async def scrape_cereales():
    urls = [
        "https://www.bcr.com.ar/es/mercados/granos/granos-disponible-cpp",
        "https://cac.bcr.com.ar/es/precios-de-granos",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    mapeo = {
        "soja": ["soja", "soybean"],
        "maiz": ["maiz", "maíz", "corn", "maize"],
        "trigo": ["trigo", "wheat"],
        "girasol": ["girasol", "sunflower"],
        "sorgo": ["sorgo", "sorghum"],
    }
    
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                texto = soup.get_text(" ", strip=True).lower()
                
                encontrados = {}
                # Buscar patrones como "soja 341" o "soja USD 341"
                for cereal, aliases in mapeo.items():
                    for alias in aliases:
                        # Patron: nombre seguido de número en rango 50-1000 (USD/t)
                        patron = rf"{alias}[^0-9]{{0,30}}?(\d{{2,4}}(?:[.,]\d{{1,2}})?)"
                        m = re.search(patron, texto)
                        if m:
                            val = float(m.group(1).replace(",", "."))
                            if 50 < val < 1000:
                                encontrados[cereal] = round(val, 2)
                                break
                
                if len(encontrados) >= 3:
                    return encontrados, url
            except Exception as e:
                print(f"Error scraping {url}: {e}")
                continue
    return None, None


# ──────────────────────────────────────────
# DÓLAR BNA
# ──────────────────────────────────────────
async def fetch_dolar_bna():
    apis = [
        "https://dolarapi.com/v1/dolares/oficial",
        "https://api.argentinadatos.com/v1/cotizaciones/dolar/oficial",
        "https://api.bluelytics.com.ar/v2/latest",
    ]
    async with httpx.AsyncClient(timeout=6) as client:
        for url in apis:
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    continue
                j = r.json()
                # dolarapi y argentinadatos
                val = j.get("compra") or j.get("oficial", {}).get("value_buy") if isinstance(j.get("oficial"), dict) else None
                if not val:
                    val = j.get("compra") or j.get("venta")
                if val and float(val) > 100:
                    return round(float(val), 2), url
            except Exception as e:
                print(f"Dólar API error {url}: {e}")
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
        # Actualizar en paralelo
        cereales_task = scrape_cereales()
        dolar_task = fetch_dolar_bna()
        (cereales, fuente_c), (bna, fuente_d) = await asyncio.gather(cereales_task, dolar_task)
        
        if cereales:
            cache_precios["cereales"].update(cereales)
            cache_precios["fuente"] = "BCR Rosario"
        
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
# ENDPOINT: GET /health
# ──────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
