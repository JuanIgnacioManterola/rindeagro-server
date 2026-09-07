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

## 2. Pasos manuales en Meta (los hacés vos, antes de tocar Kapso)

Según `docs.kapso.ai/docs/how-to/whatsapp/connect-whatsapp`, conectar el número
**no saltea ningún requisito de Meta**. Hay que tener resuelto:

1. **Cuenta de Meta / Facebook** con permisos de administrador.
2. **Meta Business Portfolio** (Business Manager) completo:
   - razón social,
   - domicilio físico,
   - teléfono de la empresa,
   - **sitio web público con HTTPS** → `https://rindeagro.app` sirve.
3. **Verificación de empresa** (Business Verification). Es el paso más lento:
   Meta pide documentación de la sociedad. Arrancalo primero, tarda días.
4. **WABA** (WhatsApp Business Account) creada o elegida.
5. **Display name**: el nombre que ven los usuarios ("Rinde.Agro"). Pasa por
   revisión de Meta. No elijas la opción "solo display name".
6. **Método de pago cargado en la WABA.** Sin esto no se manda nada en
   producción.
7. **Sin incidencias abiertas** en la sección de números ni en la cuenta.

Ojo con dos límites que aparecen en la doc de Kapso:

- Un portfolio nuevo arranca con capacidad para **2 números** registrados. Meta
  lo sube a 20 después de verificar o de llegar a ciertos volúmenes.
- Aunque el número quede conectado, *"la revisión del display name, la
  verificación de empresa, la revisión de la WABA, la elegibilidad de pago, la
  revisión de plantillas y las restricciones por país o cuenta pueden seguir
  bloqueando el envío en producción"*.

---

## 3. Provisionar el número en Kapso

Ya está decidido que aceptamos un **+1 provisionado por Kapso** (no hace falta
número argentino). Eso es el flujo de **Instant setup**
(`docs.kapso.ai/docs/platform/phone-numbers/instant-setup`): Kapso provisiona el
número y, cuando hay disponible, usa uno pre-verificado para que no haya que
hacer la verificación por SMS o llamada. El default de instant setup es US.

Pasos en el panel de Kapso:

1. Provisionar el número de producción (instant setup).
2. Anotar su **`phone_number_id`**. Es lo que va en `KAPSO_PHONE_NUMBER_ID`.
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

1. Verificación de empresa en Meta (arrancá por acá, es lo más lento).
2. Display name + método de pago en la WABA.
3. Provisionar el número en Kapso (instant setup, +1).
4. Crear el webhook **para ese número**, con el `secret_key` que ya usás.
5. Cambiar `KAPSO_PHONE_NUMBER_ID` en Railway y redeploy.
6. Verificar con `GET /whatsapp/kapso/estado?numeros=1`:
   - `en_uso: true` en el número correcto,
   - `status: "CONNECTED"`,
   - `webhook_secret: true` y sin campo `alerta`.
7. Probar desde un número que **no** esté en la lista vieja del sandbox.
8. Cargar las 3 plantillas y esperar la aprobación.
9. Migrar los jobs del scheduler a plantillas y apagar Twilio.
