import os
import sys
import django
import logging
import asyncio
import pytz
import requests
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from django.utils import timezone
from asgiref.sync import sync_to_async
from django.db import connection

# --- 1. CONFIGURACIÓN DE ENTORNO ---

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

load_dotenv(BASE_DIR / ".env")
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

# Carga de variables (Render -> Environment)
VOICEMONKEY_ACCESS_TOKEN = os.getenv("VOICEMONKEY_ACCESS_TOKEN")
VOICEMONKEY_SECRET_TOKEN = os.getenv("VOICEMONKEY_SECRET_TOKEN")
MONKEY_NAME = os.getenv("MONKEY_NAME", "astranacritico")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "8034926015"))

from medicine_control.models import Insumo

# --- 2. WRAPPERS ASÍNCRONOS ---
@sync_to_async
def obtener_insumos_db():
    connection.close_if_unusable_or_obsolete()
    return list(Insumo.objects.all())

# --- 3. RUTINA DE MONITOREO AUTOMÁTICO ---

async def rutina_monitoreo_astrana(application):
    """Chequea umbrales, envía resumen los viernes y prepara bienvenida para Alexa."""
    tz = pytz.timezone('America/Argentina/Buenos_Aires')
    ultimo_chequeo_hora = None
    ultimo_resumen_dia = None
    ultima_bienvenida_dia = None

    print("🚀 Sistema de monitoreo Astrana iniciado...")

    while True:
        try:
            ahora = datetime.now(tz)
            hoy_str = ahora.strftime('%Y-%m-%d')
            
            # --- A. PREPARAR BIENVENIDA PARA ALEXA (06:00 AM) ---
            if ahora.hour == 6 and ahora.minute == 0 and ultima_bienvenida_dia != hoy_str:
                insumos = await obtener_insumos_db()
                alertas_criticas = [i.nombre for i in insumos if i.autonomia_smart <= 10]
                es_viernes = (ahora.weekday() == 4)
                
                if alertas_criticas:
                    nombres = ", ".join(alertas_criticas)
                    texto_alexa = f"Atención Joaco, Astrana informa stock crítico en: {nombres}. Por favor, revisá Telegram."
                elif es_viernes:
                    detalles = [f"{i.nombre} con {i.autonomia_smart} días de autonomía." for i in insumos]
                    texto_alexa = f"Buen día Joaco. Hoy es viernes. Reporte: {' '.join(detalles)}"
                else:
                    texto_alexa = "Buen día Joaco. Astrana informa que el stock se encuentra estable."

                try:
                    url_anuncio = (
                        f"https://voicemonkey.io/trigger/announcement?"
                        f"access_token={VOICEMONKEY_ACCESS_TOKEN}&"
                        f"secret_token={VOICEMONKEY_SECRET_TOKEN}&"
                        f"monkey={MONKEY_NAME}&"
                        f"announcement={requests.utils.quote(texto_alexa)}"
                    )
                    await asyncio.to_thread(requests.get, url_anuncio)
                    print(f"📢 Alexa preparada: {texto_alexa}")
                    ultima_bienvenida_dia = hoy_str
                except Exception as alexa_e:
                    print(f"❌ Error en Voice Monkey: {alexa_e}")

            # --- B. CHEQUEO DIARIO A TELEGRAM (10:00 y 20:00) ---
            if ahora.hour in [10, 20] and ultimo_chequeo_hora != ahora.hour:
                insumos = await obtener_insumos_db()
                alertas = []
                for i in insumos:
                    if (i.stock_actual_cajas * 30) <= 30:
                        alertas.append(f"📦 Stock: Solo queda {i.stock_actual_cajas} caja de {i.nombre}.")
                    if i.autonomia_smart <= 10:
                        alertas.append(f"🚨 Crítico: {i.nombre} con {i.autonomia_smart} días.")

                if alertas:
                    msg = "⚠️ *ASTRANA: ALERTAS DE SISTEMA*\n\n" + "\n".join(alertas)
                    await application.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
                ultimo_chequeo_hora = ahora.hour

        except Exception as e:
            print(f"❌ Error en bucle de monitoreo: {e}")
        
        await asyncio.sleep(60)

# --- 4. INTEGRACIÓN CON EL BOT ---

async def iniciar_rutina_job(context: ContextTypes.DEFAULT_TYPE):
    """Lanzador de la rutina compatible con el JobQueue del bot."""
    await rutina_monitoreo_astrana(context.application)

def main():
    """Función principal para inicializar Astrana."""
    token_bot = os.getenv("TELEGRAM_TOKEN")
    if not token_bot:
        print("❌ ERROR: No se encontró TELEGRAM_TOKEN.")
        return

    # 1. Construimos la aplicación
    application = ApplicationBuilder().token(token_bot).build()

    print("🤖 Astrana preparando motores...")

    # 2. Programamos la rutina en el JobQueue (se activa a los 5 seg de iniciar)
    if application.job_queue:
        application.job_queue.run_once(iniciar_rutina_job, when=5)
        print("🚀 Tarea de monitoreo vinculada al motor principal.")
    else:
        print("⚠️ Advertencia: JobQueue no disponible. Revisá 'apscheduler'.")

    print("📡 Iniciando polling de Telegram...")

    # 3. Iniciamos el bot
    # drop_pending_updates=True es fundamental para limpiar errores de "Conflict"
        
# Dentro de tu función main()
    print("📡 Iniciando polling de Telegram...")


# El parámetro interno de la librería cerrará otras sesiones abiertas
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        print("👋 Astrana se está apagando...")
    except Exception as e:
        print(f"❌ Error crítico: {e}")