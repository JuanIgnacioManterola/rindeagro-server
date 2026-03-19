# RindeAgro Server

Servidor backend para RindeAgro. Maneja:
- 📈 Precios de cereales (scraping BCR Rosario)
- 💵 Dólar BNA
- 📱 Carga de datos por WhatsApp (texto, audio, PDF)

---

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | /precios | Precios de cereales y dólar BNA |
| POST | /whatsapp | Webhook de Twilio (mensajes WhatsApp) |
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
TWILIO_ACCOUNT_SID=<de Twilio console>
TWILIO_AUTH_TOKEN=<de Twilio console>
```

### Paso 4 — Conectar Twilio
1. En Twilio Console → Messaging → WhatsApp Sandbox
2. Webhook URL: https://tu-app.railway.app/whatsapp

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
