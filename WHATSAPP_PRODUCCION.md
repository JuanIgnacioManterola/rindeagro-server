# WhatsApp: pasar de sandbox a producción

Checklist para que el bot atienda a **cualquier cliente que se registre**, sin
cargar números a mano.

> El código ya está listo. Lo que queda son pasos manuales en Meta, en Kapso y
> en Railway que tenés que hacer vos: yo no toco esas consolas.

---

## 1. Por qué hoy solo habla con 5 números

La limitación **no es de Kapso, es de Meta**. Un número de test/sandbox de la
Cloud API solo puede mandar mensajes a un puñado de destinatarios pre-cargados
en el panel de Meta. Además, la doc de Kapso confirma que en su sandbox el envío
de plantillas está deshabilitado y solo se soporta un destinatario a la vez.

Un número de producción conectado a una WABA verificada habla con cualquiera
que le escriba. No hay allowlist que sacar del código: **no existe ninguna en el
repo** (lo verifiqué; los únicos números que aparecen están en comentarios como
ejemplo de formato).

---

## 2. Dos caminos distintos — nosotros vamos por el corto

Esta es la parte que más fácil se confunde, porque la doc de Kapso documenta
los dos y las páginas se parecen.

### Camino largo — "tu infraestructura" / *Connect WhatsApp*

Traés tu propio número y tu propia WABA. Ahí **sí** hacen falta Meta Business
Portfolio completo, **verificación de empresa** (días, con documentación de la
sociedad), WABA propia, revisión de display name y método de pago cargado en la
WABA. La doc es explícita: conectar el número *"no saltea ningún requisito de
Meta"*.

**No es nuestro caso.** No hay que hacer nada de esto.

### Camino corto — "infraestructura de Kapso" / *Instant setup* ← el nuestro

Kapso opera su propia Meta app y su propia WABA, y te presta un número de su
pool. En palabras de la doc de infraestructura de Kapso: *"Kapso runs messaging
and completes onboarding for you"*, y **los clientes no necesitan su propia
verificación de empresa con Meta** — se conectan a través del Multi-Partner
Solution de Kapso.

O sea: **no hay verificación de empresa, no hay Business Manager propio, no hay
WABA propia, no hay método de pago cargado en Meta.** Eso es lo que hace que el
salto a producción sea cuestión de horas y no de días.

### Lo que sí hace falta

1. **Salir del plan gratis.** El plan free solo da el número de sandbox. Un
   número pre-verificado del pool está *"disponible en todos los planes"* pero
   pide *"un depósito chico que va directo a los créditos del proyecto"*.
2. **Créditos para los mensajes.** El free incluye 2.000 mensajes por mes con
   un número conectado; de ahí en adelante se paga por mensaje.

### Lo que no me consta

La doc no aclara qué display name queda en un número del pool ni si cambiarlo
pasa por revisión de Meta. Si el nombre que ven los usuarios importa para el
test (y probablemente importe: van a ver quién les escribe), preguntale a
soporte de Kapso antes de provisionar.

---

## 3. Provisionar el número en Kapso

Ya está decidido que aceptamos un **+1 provisionado por Kapso** (no hace falta
número argentino). Es el flujo de **Instant setup**
(`docs.kapso.ai/docs/platform/phone-numbers/instant-setup`): Kapso mantiene un
pool de números pre-verificados y usa uno de ahí, así no hay que verificar la
línea por SMS ni por llamada. El default de instant setup es US.

Pasos en el panel de Kapso:

1. Poner el depósito / plan que habilita un número del pool.
2. Provisionar el número de producción (instant setup).
3. Anotar su **`phone_number_id`**. Es lo que va en `KAPSO_PHONE_NUMBER_ID`.
   Se puede confirmar por API:
   ```
   GET https://api.kapso.ai/platform/v1/whatsapp/phone_numbers
   X-API-Key: <KAPSO_API_KEY>
   ```
   o, más cómodo, desde el propio server una vez deployado:
   ```
   GET https://<server>/whatsapp/kapso/estado?numeros=1
   ```
   Ese endpoint lista los números conectados y marca con `en_uso: true` el que
   coincide con la variable de entorno actual.

---

## 4. Webhook: **esto es lo que más fácil se olvida**

**Los webhooks de mensajes en Kapso son por número de teléfono, no por
proyecto.** El endpoint para crearlos es:

```
POST https://api.kapso.ai/platform/v1/whatsapp/phone_numbers/{phone_number_id}/webhooks
X-API-Key: <KAPSO_API_KEY>
Content-Type: application/json

{
  "whatsapp_webhook": {
    "url": "https://<tu-server-railway>/whatsapp/kapso",
    "secret_key": "<el mismo valor que KAPSO_WEBHOOK_SECRET>",
    "events": ["whatsapp.message.received"]
  }
}
```

Consecuencia práctica: **cambiar `KAPSO_PHONE_NUMBER_ID` no alcanza.** Si no
creás el webhook para el número nuevo, el server queda sordo: no llega ningún
mensaje y no hay error visible en ningún lado.

Lo bueno: el `secret_key` lo elegís vos al crear el webhook, así que **podés
reusar el `KAPSO_WEBHOOK_SECRET` que ya tenés** y no cambiar esa variable.

La ruta del server (`POST /whatsapp/kapso`) y la validación de firma no cambian
al cambiar de número. El código ahora acepta las dos formas de firma:

- `X-Webhook-Signature` en hexadecimal → webhook de tipo `kapso` (el default).
- `X-Hub-Signature-256` con prefijo `sha256=` → webhook de tipo `meta`.

---

## 5. Variables de entorno en Railway

Estas son las que tenés que setear vos. **No las toqué.**

| Variable | Cambia | Valor |
|---|---|---|
| `KAPSO_PHONE_NUMBER_ID` | **SÍ** | el `phone_number_id` del número de producción |
| `KAPSO_WEBHOOK_SECRET` | no, si reusás el mismo `secret_key` al crear el webhook | secret del webhook |
| `KAPSO_API_KEY` | no | la project API key sigue siendo la misma |
| `APP_URL` | no | `https://rindeagro.app` |

> `KAPSO_WEBHOOK_SECRET` **es obligatorio en producción.** Con el número de
> sandbox, tener el webhook sin firma era inofensivo. Con un número de
> producción la URL es pública y sin secret cualquiera puede postearle mensajes
> falsos al bot. El código sigue aceptando el arranque sin secret (para no
> romper el entorno de prueba) pero ahora avisa por log en cada request y al
> arrancar, y `/whatsapp/kapso/estado` devuelve un campo `alerta`.

---

## 6. Plantillas (message templates)

Regla de Meta: fuera de la **ventana de 24 horas** desde el último mensaje del
usuario, no se puede mandar texto libre. Solo plantillas aprobadas.

Todo lo que el bot contesta *como respuesta* a un mensaje entrante cae dentro de
la ventana y **no necesita plantilla**: menú, tareas, carga de gastos, lluvias,
OCR de facturas. Eso funciona el día uno.

Lo que **sí** necesita plantilla es lo que arrancamos nosotros. Hoy son los tres
jobs del scheduler en `main.py`:

| Job | Cuándo | Plantilla sugerida | Categoría |
|---|---|---|---|
| `_wa_recordatorio_operarios` | L/M/V 17:30 | `recordatorio_operario` | Utility |
| `_wa_recordatorio_admins` | V 12:30 | `recordatorio_admin` | Utility |
| `_wa_resumen_semanal` | Sáb 9:00 | `resumen_semanal` | Utility |

Elegí **Utility**, no Marketing: son recordatorios operativos de un servicio que
el usuario ya contrató. Marcar mal la categoría es una de las causas de rechazo
que lista la doc de Kapso. La revisión de Meta suele tardar hasta 24 horas.

**Estado actual en el código:** `kapso.enviar_plantilla()` ya existe y está
testeada, pero **los tres jobs siguen mandando por Twilio** (`_wa_enviar`). No
los migré a propósito: migrarlos hoy los rompería en silencio, porque sin
plantillas aprobadas Meta rechaza el envío fuera de la ventana de 24h. El orden
correcto es:

1. Crear y aprobar las 3 plantillas.
2. Recién ahí cambiar `_wa_enviar` para que use `kapso.enviar_plantilla(...)`.
3. Dar de baja Twilio.

---

## 7. Qué pasa cuando escribe alguien que no conocemos

Con un número de producción esto deja de ser un caso raro: escribe gente
equivocada, curiosos y spam. El flujo ahora es:

- **No lo reconocemos** → le contestamos **una sola vez cada 6 horas** con un
  mensaje que cubre los tres casos (no tiene cuenta / tiene cuenta pero no cargó
  el número / lo sumaron a un equipo). Antes el mensaje daba por hecho que ya
  tenía cuenta, y se repetía en cada mensaje.
- **Lo reconocemos** → menú normal.

---

## 8. Orden recomendado

Ninguno de estos pasos depende de un trámite lento de Meta. En una tarde se
hace todo menos las plantillas.

1. Poner el depósito en Kapso que habilita un número del pool.
2. Provisionar el número +1 (instant setup).
3. Crear el webhook **para ese número**, con el `secret_key` que ya usás.
4. Cambiar `KAPSO_PHONE_NUMBER_ID` en Railway y redeploy.
5. Verificar con `GET /whatsapp/kapso/estado?numeros=1`:
   - `en_uso: true` en el número correcto,
   - `status: "CONNECTED"`,
   - `webhook_secret: true` y sin campo `alerta`.
6. Probar desde un número que **no** esté en la lista vieja del sandbox. Esta es
   la prueba de fuego: si contesta, el objetivo está cumplido.
7. Confirmar con Kapso qué display name queda y si se puede cambiar.
8. Cargar las 3 plantillas y esperar la aprobación de Meta (hasta 24 h). Esto
   es lo único que tarda, y **no bloquea el test con usuarios**: solo afecta a
   los recordatorios automáticos.
9. Migrar los jobs del scheduler a plantillas y apagar Twilio.
