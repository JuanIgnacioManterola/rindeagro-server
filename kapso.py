"""
Transporte de WhatsApp vía Kapso (proxy oficial de la Cloud API de Meta).

Este módulo NO tiene lógica de negocio: solo sabe mandar mensajes, validar la
firma de los webhooks y normalizar los payloads entrantes a algo cómodo de
consumir. La lógica conversacional vive en main.py.

Variables de entorno:
  KAPSO_API_KEY         — project API key (Integrations → API keys)
  KAPSO_PHONE_NUMBER_ID — id del número conectado (Phone numbers → ID: ...)
  KAPSO_WEBHOOK_SECRET  — secret del webhook, para validar la firma

Docs: https://docs.kapso.ai/api/introduction
"""

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field

import httpx

BASE_URL = "https://api.kapso.ai/meta/whatsapp/v24.0"

# Límites de la plataforma de WhatsApp (Meta), no de Kapso.
MAX_BOTONES        = 3
MAX_BOTON_TITULO   = 20
MAX_FILAS_LISTA    = 10
MAX_FILA_TITULO    = 24
MAX_FILA_DESC      = 72
MAX_BOTON_LISTA    = 20


def _api_key() -> str:
    return os.environ.get("KAPSO_API_KEY", "")


def _phone_number_id() -> str:
    return os.environ.get("KAPSO_PHONE_NUMBER_ID", "")


def _webhook_secret() -> str:
    return os.environ.get("KAPSO_WEBHOOK_SECRET", "")


def configurado() -> bool:
    return bool(_api_key() and _phone_number_id())


# ══════════════════════════════════════════════════════════════
# NORMALIZACIÓN DE NÚMEROS
# ══════════════════════════════════════════════════════════════

def normalizar_numero(numero: str) -> str:
    """
    Devuelve el número en la forma en que WhatsApp identifica a la persona:
    solo dígitos, sin '+'.

    Ojo Argentina: se marca con un 9 después del código de país
    (+54 9 2944 56-5308) pero WhatsApp identifica al usuario SIN ese 9
    (542944565308). Los perfiles en Supabase están guardados con el 9, así que
    sin esta normalización el bot no reconoce a nadie.

    Mismo caso en México, que usa un 1 en la misma posición.
    """
    if not numero:
        return ""
    d = re.sub(r"\D", "", numero)
    # 00 como prefijo internacional
    if d.startswith("00"):
        d = d[2:]
    # Argentina: 54 9 XXXXXXXXXX → 54 XXXXXXXXXX
    if d.startswith("549") and len(d) == 13:
        d = "54" + d[3:]
    # México: 52 1 XXXXXXXXXX → 52 XXXXXXXXXX
    if d.startswith("521") and len(d) == 13:
        d = "52" + d[3:]
    return d


def mismo_numero(a: str, b: str) -> bool:
    """Compara dos teléfonos escritos en cualquier formato."""
    na, nb = normalizar_numero(a), normalizar_numero(b)
    return bool(na) and na == nb


# ══════════════════════════════════════════════════════════════
# ENVÍO DE MENSAJES
# ══════════════════════════════════════════════════════════════

async def _post(payload: dict) -> dict:
    """POST a /{phone_number_id}/messages. Devuelve {} si falla."""
    if not configurado():
        print("[KAPSO→] falta KAPSO_API_KEY o KAPSO_PHONE_NUMBER_ID, no se envía")
        return {}
    url = f"{BASE_URL}/{_phone_number_id()}/messages"
    headers = {"X-API-Key": _api_key(), "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, headers=headers, json=payload)
            if r.status_code in (200, 201):
                return r.json()
            print(f"[KAPSO→] HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        print(f"[KAPSO→] excepción: {e}")
    return {}


async def enviar_texto(numero: str, texto: str) -> dict:
    return await _post({
        "messaging_product": "whatsapp",
        "to": normalizar_numero(numero),
        "type": "text",
        "text": {"body": texto},
    })


async def enviar_botones(numero: str, texto: str, botones: list) -> dict:
    """
    botones: [(id, titulo), ...] — hasta 3, títulos de hasta 20 caracteres.
    Si te pasás, se recorta en vez de fallar: mejor un título corto que un 400.
    """
    items = []
    for bid, titulo in botones[:MAX_BOTONES]:
        items.append({
            "type": "reply",
            "reply": {"id": str(bid)[:256], "title": str(titulo)[:MAX_BOTON_TITULO]},
        })
    return await _post({
        "messaging_product": "whatsapp",
        "to": normalizar_numero(numero),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": items},
        },
    })


async def enviar_lista(numero: str, texto: str, boton_texto: str, filas: list,
                       titulo_seccion: str = "Opciones") -> dict:
    """
    filas: [(id, titulo, descripcion), ...] — hasta 10.
    Si hay más de 10 opciones hay que paginar antes de llamar acá.
    """
    rows = []
    for fila in filas[:MAX_FILAS_LISTA]:
        fid, titulo = fila[0], fila[1]
        desc = fila[2] if len(fila) > 2 else ""
        row = {"id": str(fid)[:200], "title": str(titulo)[:MAX_FILA_TITULO]}
        if desc:
            row["description"] = str(desc)[:MAX_FILA_DESC]
        rows.append(row)
    return await _post({
        "messaging_product": "whatsapp",
        "to": normalizar_numero(numero),
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": texto},
            "action": {
                "button": str(boton_texto)[:MAX_BOTON_LISTA],
                "sections": [{"title": str(titulo_seccion)[:24], "rows": rows}],
            },
        },
    })


async def marcar_leido(message_id: str, escribiendo: bool = False) -> dict:
    """
    Marca el mensaje como leído. Con escribiendo=True muestra 'escribiendo…'
    hasta que mandemos la respuesta o pasen ~25 segundos — útil cuando atrás
    hay un OCR o una llamada a la IA que tarda unos segundos.
    """
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    if escribiendo:
        payload["typing_indicator"] = {"type": "text"}
    return await _post(payload)


async def descargar_media(url: str) -> bytes:
    """
    Baja un adjunto desde la media_url que viene en el webhook.
    La URL ya trae la autenticación embebida y es de vida corta, así que hay
    que usarla apenas llega el mensaje.
    """
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(url, headers={"X-API-Key": _api_key()})
            if r.status_code == 200:
                return r.content
            print(f"[KAPSO media] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[KAPSO media] excepción: {e}")
    return b""


# ══════════════════════════════════════════════════════════════
# WEBHOOK — FIRMA
# ══════════════════════════════════════════════════════════════

def firma_valida(body_crudo: bytes, firma: str) -> bool:
    """
    Valida el HMAC-SHA256 que Kapso manda en X-Webhook-Signature.

    Si no hay secret configurado devolvemos True: permite levantar el webhook
    y probar antes de configurar el secret. Apenas se setea KAPSO_WEBHOOK_SECRET
    la validación pasa a ser obligatoria.
    """
    secret = _webhook_secret()
    if not secret:
        return True
    if not firma:
        return False

    esperada = hmac.new(secret.encode(), body_crudo, hashlib.sha256).hexdigest()
    if hmac.compare_digest(firma, esperada):
        return True

    # Los ejemplos de la documentación firman sobre el JSON re-serializado en
    # vez del cuerpo crudo. Probamos esa variante antes de rechazar, así un
    # cambio de formato del lado de Kapso no nos deja sordos.
    try:
        recompacto = json.dumps(json.loads(body_crudo), separators=(",", ":")).encode()
        alterna = hmac.new(secret.encode(), recompacto, hashlib.sha256).hexdigest()
        if hmac.compare_digest(firma, alterna):
            return True
        reespaciado = json.dumps(json.loads(body_crudo)).encode()
        alterna2 = hmac.new(secret.encode(), reespaciado, hashlib.sha256).hexdigest()
        if hmac.compare_digest(firma, alterna2):
            return True
    except Exception:
        pass
    return False


# ══════════════════════════════════════════════════════════════
# WEBHOOK — PARSEO
# ══════════════════════════════════════════════════════════════

@dataclass
class MensajeWA:
    """Un mensaje entrante, ya masticado."""
    id: str = ""
    tipo: str = ""            # text | image | audio | document | interactive | contacts | location
    desde: str = ""           # solo dígitos, como identifica WhatsApp
    texto: str = ""           # lo tipeado, la transcripción del audio, o el título del botón tocado
    accion: str = ""          # id del botón o de la fila de lista elegida ("" si no aplica)
    media_url: str = ""
    media_tipo: str = ""      # content_type (image/jpeg, application/pdf, ...)
    media_nombre: str = ""
    contacto_nombre: str = ""    # nombre del que escribe, según su perfil de WhatsApp
    compartido_nombre: str = ""  # si compartió un contacto, el nombre
    compartido_numero: str = ""  # si compartió un contacto, el número
    crudo: dict = field(default_factory=dict)

    @property
    def tiene_media(self) -> bool:
        return bool(self.media_url)

    @property
    def es_imagen(self) -> bool:
        return self.tipo == "image" or self.media_tipo.startswith("image/")

    @property
    def es_pdf(self) -> bool:
        return "pdf" in (self.media_tipo or "")


def _payloads(body: dict) -> list:
    """Kapso puede mandar un evento suelto o un lote. Devolvemos siempre lista."""
    if isinstance(body, dict) and body.get("batch") and isinstance(body.get("data"), list):
        return body["data"]
    return [body]


def parsear_entrantes(body: dict) -> list:
    """
    Convierte el cuerpo del webhook en una lista de MensajeWA.
    Ignora todo lo que no sea un mensaje entrante (acuses de entrega, lecturas,
    eventos de conversación).
    """
    salida = []
    for p in _payloads(body):
        if not isinstance(p, dict):
            continue
        msg = p.get("message") or {}
        if not msg:
            continue
        kap = msg.get("kapso") or {}
        if kap.get("direction") and kap["direction"] != "inbound":
            continue

        conv = p.get("conversation") or {}
        m = MensajeWA(
            id=msg.get("id", ""),
            tipo=msg.get("type", ""),
            desde=normalizar_numero(msg.get("from") or conv.get("phone_number") or ""),
            contacto_nombre=conv.get("contact_name", "") or "",
            media_url=kap.get("media_url", "") or "",
            crudo=p,
        )

        media = kap.get("media_data") or {}
        m.media_tipo = media.get("content_type", "") or ""
        m.media_nombre = media.get("filename", "") or ""

        # ── Texto según el tipo ──────────────────────────────
        if m.tipo == "text":
            m.texto = (msg.get("text") or {}).get("body", "") or ""

        elif m.tipo == "interactive":
            inter = msg.get("interactive") or {}
            reply = inter.get("button_reply") or inter.get("list_reply") or {}
            m.accion = reply.get("id", "") or ""
            m.texto = reply.get("title", "") or ""

        elif m.tipo == "audio":
            # Kapso transcribe los audios y manda el texto listo.
            m.texto = ((kap.get("transcript") or {}).get("text", "") or "").strip()

        elif m.tipo in ("image", "video", "document"):
            m.texto = ((kap.get("message_type_data") or {}).get("caption", "")
                       or (msg.get(m.tipo) or {}).get("caption", "") or "")

        elif m.tipo == "contacts":
            contactos = msg.get("contacts") or []
            if contactos:
                c = contactos[0]
                m.compartido_nombre = (c.get("name") or {}).get("formatted_name", "") or ""
                telefonos = c.get("phones") or []
                if telefonos:
                    m.compartido_numero = normalizar_numero(
                        telefonos[0].get("wa_id") or telefonos[0].get("phone") or ""
                    )
                m.texto = m.compartido_nombre

        elif m.tipo == "location":
            loc = msg.get("location") or {}
            m.texto = loc.get("name") or loc.get("address") or ""

        salida.append(m)
    return salida
