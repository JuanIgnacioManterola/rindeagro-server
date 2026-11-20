"""
Da de alta en Kapso las plantillas de plantillas_wa.py para que Meta las revise.

Uso (con las mismas variables que tiene Railway):

    KAPSO_API_KEY=... KAPSO_PHONE_NUMBER_ID=... python3 crear_plantillas.py

Es seguro correrlo más de una vez: saltea las que ya existen y al final muestra
el estado de cada una. Meta suele aprobarlas en menos de 24 horas.
"""

import asyncio

import kapso
import plantillas_wa


async def main() -> None:
    waba = await kapso.waba_id()
    if not waba:
        print("No encontré la WABA. Revisá KAPSO_API_KEY y KAPSO_PHONE_NUMBER_ID.")
        return
    print(f"WABA: {waba}\n")

    existentes = {(p.get("name"), p.get("language")) for p in await kapso.listar_plantillas()}
    for nombre in plantillas_wa.PLANTILLAS:
        if (nombre, plantillas_wa.IDIOMA) in existentes:
            print(f"· {nombre}: ya existe, la salteo")
            continue
        ok, resp = await kapso.crear_plantilla(plantillas_wa.definicion_meta(nombre))
        print(f"{'✓' if ok else '✗'} {nombre}: {resp if not ok else 'enviada a revisión'}")

    print("\nEstado actual:")
    nuestras = set(plantillas_wa.PLANTILLAS)
    for p in await kapso.listar_plantillas():
        if p.get("name") in nuestras:
            print(f"  {p.get('name'):24} {p.get('language'):6} {p.get('status')}")


if __name__ == "__main__":
    asyncio.run(main())
