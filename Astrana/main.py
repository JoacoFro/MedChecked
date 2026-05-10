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
import google.generativeai as genai
from django.utils import timezone
from asgiref.sync import sync_to_async
from django.db import connection
import threading

# --- 1. CONFIGURACIÓN DE ENTORNO ---
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

load_dotenv(BASE_DIR / ".env")
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

# Carga de variables para Voice Monkey (Asegúrate de que estén en Render -> Environment)
VOICEMONKEY_ACCESS_TOKEN = os.getenv("VOICEMONKEY_ACCESS_TOKEN")
VOICEMONKEY_SECRET_TOKEN = os.getenv("VOICEMONKEY_SECRET_TOKEN")
MONKEY_NAME = os.getenv("MONKEY_NAME", "astranacritico")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "8034926015"))

from medicine_control.models import Insumo, Pedido, Salida, Envio

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
            
            # --- 1. PREPARAR BIENVENIDA PARA ALEXA (06:00 AM) ---
            if ahora.hour == 6 and ahora.minute == 0 and ultima_bienvenida_dia != hoy_str:
                insumos = await obtener_insumos_db()
                alertas_criticas = [i.nombre for i in insumos if i.autonomia_smart <= 10]
                es_viernes = (ahora.weekday() == 4)
                
                if alertas_criticas:
                    nombres = ", ".join(alertas_criticas)
                    texto_alexa = f"Atención Joaco, Astrana informa stock crítico en: {nombres}. Por favor, revisá Telegram."
                elif es_viernes:
                    detalles = []
                    for i in insumos:
                        detalles.append(f"{i.nombre} con {i.total_unidades_reales} unidades y {i.autonomia_smart} días de autonomía.")
                    reporte_stock = " ".join(detalles)
                    texto_alexa = (
                        f"Buen día Joaco. Hoy es viernes y el reporte de Astrana es el siguiente: "
                        f"{reporte_stock} Recordá que ya tenés disponible tu resumen semanal en el bot."
                    )
                else:
                    texto_alexa = "Buen día Joaco. Astrana te informa que no hay alertas pendientes y el stock se encuentra estable."

                try:
                    url_anuncio = (
                        f"https://voicemonkey.io/trigger/announcement?"
                        f"access_token={VOICEMONKEY_ACCESS_TOKEN}&"
                        f"secret_token={VOICEMONKEY_SECRET_TOKEN}&"
                        f"monkey={MONKEY_NAME}&"
                        f"announcement={requests.utils.quote(texto_alexa)}"
                    )
                    # Usamos to_thread para no bloquear el loop asíncrono
                    await asyncio.to_thread(requests.get, url_anuncio)
                    print(f"📢 Alexa preparada: {texto_alexa}")
                    ultima_bienvenida_dia = hoy_str
                except Exception as alexa_e:
                    print(f"❌ Error en Voice Monkey: {alexa_e}")

            # --- 2. CHEQUEO DIARIO A TELEGRAM (10:00 y 20:00) ---
            if ahora.hour in [10, 20] and ultimo_chequeo_hora != ahora.hour:
                insumos = await obtener_insumos_db()
                alertas = []
                for i in insumos:
                    if (i.stock_actual_cajas * 30) <= 30:
                        alertas.append(f"📦 Stock Normal: Solo queda {i.stock_actual_cajas} caja de {i.nombre}.")
                    if i.backup_unidades <= 56:
                        alertas.append(f"🛡️ Seguridad: {i.nombre} tiene solo {i.backup_unidades} un. de backup.")
                    if i.autonomia_smart <= 10:
                        alertas.append(f"🚨 Crítico: {i.nombre} con autonomía de {i.autonomia_smart} días.")

                if alertas:
                    msg = "⚠️ *ASTRANA: ALERTAS DE SISTEMA*\n\n" + "\n".join(alertas)
                    await application.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
                
                ultimo_chequeo_hora = ahora.hour

            # --- 3. RESUMEN SEMANAL EN TELEGRAM (Viernes 10:00) ---
            if ahora.weekday() == 4 and ahora.hour == 10 and ultimo_resumen_dia != hoy_str:
                insumos = await obtener_insumos_db()
                resumen = "📊 *RESUMEN SEMANAL DE INSUMOS*\n\n"
                for i in insumos:
                    resumen += (f"🔹 *{i.nombre}*\n"
                                f"   • Cajas (OS): {i.stock_actual_cajas + 1}\n" 
                                f"   • Backup: {i.backup_unidades} un.\n"
                                f"   • Autonomía: {i.autonomia_smart} días\n\n")
                await application.bot.send_message(chat_id=CHAT_ID, text=resumen, parse_mode='Markdown')
                ultimo_resumen_dia = hoy_str

        except Exception as e:
            print(f"❌ Error en bucle de monitoreo: {e}")
        
        # Dormir 60 segundos antes de la próxima vuelta
        await asyncio.sleep(60)



# --- 4. INICIO DEL BOT (ESTRUCTURA DE HILO INDEPENDIENTE) ---

def lanzar_monitoreo(application):
    """Crea un nuevo loop de eventos para el hilo de monitoreo."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(rutina_monitoreo_astrana(application))

def main():
    """Función principal que lanza el bot."""
    token_bot = os.getenv("TELEGRAM_TOKEN")
    if not token_bot:
        print("❌ ERROR: No se encontró TELEGRAM_TOKEN.")
        return

    # 1. Construimos la aplicación
    application = ApplicationBuilder().token(token_bot).build()

    print("🤖 Astrana preparando motores...")

    # 2. Lanzamos el monitoreo en un HILO aparte (Thread)
    # Esto evita el error de "no current event loop" de forma definitiva
    t = threading.Thread(target=lanzar_monitoreo, args=(application,), daemon=True)
    t.start()

    print("🚀 Hilo de monitoreo lanzado. Iniciando Telegram...")

    # 3. Iniciamos el bot
    # drop_pending_updates=True sigue siendo vital
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        print("👋 Astrana se está apagando...")
    except Exception as e:
        print(f"❌ Error crítico: {e}")