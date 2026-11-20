"""
Plantillas (message templates) de WhatsApp para los mensajes que arranca el bot.

Meta solo deja mandar texto libre dentro de las 24 horas desde el último mensaje
de la persona. Todo lo que mandamos nosotros por nuestra cuenta (recordatorios,
resumen semanal, alertas de precio) tiene que ir con una plantilla aprobada.

Este archivo es la fuente de verdad: lo usa main.py para mandar y
`crear_plantillas.py` para darlas de alta en Kapso. Si cambiás un texto, hay que
volver a crear la plantilla (Meta no deja editar una aprobada sin re-revisarla).

Reglas de Meta que condicionan cómo están escritas:
- Las variables {{1}}, {{2}}... no pueden llevar saltos de línea, por eso el
  resumen semanal es un aviso corto y el detalle se manda al tocar el botón.
- El cuerpo no puede empezar ni terminar con una variable.
- Cada variable necesita un ejemplo para la revisión.
- Botones de respuesta rápida: hasta 25 caracteres de título.
- Categoría UTILITY: son avisos de un servicio que la persona ya usa. Marcarlos
  como MARKETING encarece cada mensaje; marcarlos mal es causa de rechazo.
"""

IDIOMA = "es_AR"

# botones: [(id, título)]. El id vuelve en el webhook cuando lo tocan y es el
# mismo que usan los botones interactivos del bot, así un solo flujo atiende
# las dos cosas (main.py → _kapso_procesar).
PLANTILLAS = {
    "recordatorio_operario": {
        "categoria": "UTILITY",
        "texto": (
            "Hola {{1}} 👋 ¿Registraste todo lo de hoy en Rinde Agro?\n\n"
            "Si falta algo, cargalo ahora desde acá."
        ),
        "ejemplos": ["Juan"],
        "botones": [
            ("gasto", "Cargar gasto"),
            ("lluvia", "Registrar lluvia"),
            ("tareas", "Ver tareas"),
            ("stop", "No recibir más"),
        ],
    },
    "recordatorio_admin": {
        "categoria": "UTILITY",
        "texto": (
            "Hola {{1}} 👋 Mañana te llega el resumen semanal de Rinde Agro.\n\n"
            "¿Te quedó algo sin cargar esta semana?"
        ),
        "ejemplos": ["Juan"],
        "botones": [
            ("gasto", "Cargar gasto"),
            ("lluvia", "Registrar lluvia"),
            ("stop", "No recibir más"),
        ],
    },
    "resumen_semanal": {
        "categoria": "UTILITY",
        "texto": (
            "Hola {{1}} 📊 Ya está tu resumen de la semana del {{2}} en Rinde Agro.\n\n"
            "Gastos cargados: {{3}}\n"
            "Lluvia registrada: {{4}}\n\n"
            "Tocá *Ver resumen* para el detalle por campo y los precios del día."
        ),
        "ejemplos": ["Juan", "17 al 22 de nov", "USD 4,300", "25 mm"],
        "botones": [
            ("resumen", "Ver resumen"),
            ("stop", "No recibir más"),
        ],
    },
    "alerta_precio": {
        "categoria": "UTILITY",
        "texto": (
            "Hola {{1}} 🔔 La soja {{2}} desde el último aviso: hoy está en "
            "USD {{3}}/t.\n\n"
            "Revisá cómo te cambian los márgenes en rindeagro.app"
        ),
        "ejemplos": ["Juan", "subió 2.4%", "345"],
        "botones": [
            ("stop", "No recibir más"),
        ],
    },
}


def componentes_meta(nombre: str) -> list:
    """La plantilla en el formato de componentes que espera la API de Meta."""
    p = PLANTILLAS[nombre]
    comps = [{
        "type": "BODY",
        "text": p["texto"],
        "example": {"body_text": [p["ejemplos"]]},
    }]
    if p["botones"]:
        comps.append({
            "type": "BUTTONS",
            "buttons": [{"type": "QUICK_REPLY", "text": t} for _, t in p["botones"]],
        })
    return comps


def definicion_meta(nombre: str) -> dict:
    """Body completo para crear la plantilla."""
    return {
        "name": nombre,
        "language": IDIOMA,
        "category": PLANTILLAS[nombre]["categoria"],
        "components": componentes_meta(nombre),
    }
