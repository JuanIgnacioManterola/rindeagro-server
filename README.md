# Rinde Agro Server

Servidor backend para Rinde Agro. Maneja:
- 📈 Precios de cereales (scraping BCR Rosario)
- 💵 Dólar BNA
- 📱 Bot de WhatsApp vía Kapso (gastos, lluvias, tareas, facturas, avisos)

---

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | /precios | Precios de cereales y dólar BNA |
| POST | /whatsapp/kapso | Webhook de Kapso (mensajes WhatsApp) |
| GET | /whatsapp/kapso/estado | Diagnóstico de WhatsApp (`?numeros=1` lista números y plantillas) |
| GET | /health | Estado del servidor |

---

## Deploy en Railway

### Paso 1 — Crear cuenta
1. Ir a railway.app
2. Login with GitHub
3. New Project → Deploy from GitHub repo

### Paso 2 — Subir este código a GitHub
1. Crear repo nuevo en github.com (ej: "rindeagro-server")
2. Subir todos estos archivos

### Paso 3 — Variables de entorno en Railway
En tu proyecto Railway → Settings → Variables, agregar:

```
SUPABASE_URL=https://kmfydetiwatnwwzjnhyq.supabase.co
SUPABASE_SERVICE_KEY=<tu service role key de Supabase>
OPENAI_API_KEY=<tu API key de OpenAI>
KAPSO_API_KEY=<project API key de Kapso>
KAPSO_PHONE_NUMBER_ID=<id del número de WhatsApp>
KAPSO_WEBHOOK_SECRET=<secret del webhook>
```

### Paso 4 — Conectar WhatsApp
Ver `WHATSAPP_PRODUCCION.md`.

---

## Cómo obtener SUPABASE_SERVICE_KEY
1. Ir a supabase.com → tu proyecto
2. Settings → API
3. Copiar "service_role" key (NO la anon key)

---

## SQL necesario en Supabase
Agregar columna whatsapp a perfiles:
```sql
ALTER TABLE perfiles ADD COLUMN IF NOT EXISTS whatsapp text;
ALTER TABLE gastos ADD COLUMN IF NOT EXISTS fuente text DEFAULT 'web';
ALTER TABLE lluvias ADD COLUMN IF NOT EXISTS fuente text DEFAULT 'web';
```
