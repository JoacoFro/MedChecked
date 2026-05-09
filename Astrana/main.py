import os
import sys
import django
import logging
import asyncio
import pytz
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import google.generativeai as genai
from django.utils import timezone
# Import indispensable para compatibilidad Async-Django
from asgiref.sync import sync_to_async
from django.db import connection  # <--- IMPORTANTE: Agregado para limpiar conexiones

# --- 1. PUENTE CON DJANGO ---
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

load_dotenv(BASE_DIR / ".env")
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from medicine_control.models import Insumo, Pedido, Salida, Envio

# --- 2. WRAPPERS PARA DJANGO (SOLUCIÓN PARA RENDER) ---

@sync_to_async
def obtener_insumos_db():
    """Puente asíncrono para consultar la base de datos de Neon."""
    connection.close_if_unusable_or_obsolete() # Limpieza antes de consulta async
    return list(Insumo.objects.all())

# --- 3. FUNCIONES DE LÓGICA (TOOLS PARA LA IA) ---

def consultar_estado_stock():
    """Consulta el stock detallado de todos los insumos y su autonomía."""
    try:
        connection.close_if_unusable_or_obsolete() # Limpieza antes de consulta
        insumos = Insumo.objects.all()
        if not insumos:
            return "No hay insumos registrados en la base de datos."
        
        reporte = "📊 Estado Actual:\n"
        for i in insumos:
            aut = i.autonomia_smart
            emoji = "🔴" if aut <= 10 else "🟡" if aut <= 15 else "🟢"
            reporte += (f"- {i.nombre}: {i.total_unidades_reales} unidades "
                        f"({i.stock_actual_cajas} cajas, {i.backup_unidades} backup). "
                        f"Autonomía: {emoji} {aut} días.\n")
        return reporte
    except Exception as e:
        return f"Error al consultar stock: {e}"

def registrar_movimiento(accion: str, cantidad: int, tipo_stock: str):
    """Registra carga o descarga de insumos (cajas o unidades)."""
    try:
        connection.close_if_unusable_or_obsolete() # Limpieza antes de escribir
        insumo = Insumo.objects.filter(nombre__icontains="Sonda").first()
        if not insumo: return "Error: No encontré el insumo 'Sonda'."
        ahora = timezone.now()
        
        if accion == "cargar":
            if tipo_stock == "cajas":
                insumo.stock_actual_cajas += cantidad
                Pedido.objects.create(insumo=insumo, tipo='normal', tipo_stock='stock_normal', 
                                      cantidad=cantidad*30, fecha=ahora, lugar_compra="Astrana IA")
            else:
                insumo.backup_unidades += cantidad
                Pedido.objects.create(insumo=insumo, tipo='propio', tipo_stock='seguridad', 
                                      cantidad=cantidad, fecha=ahora, lugar_compra="Astrana IA")
        
        elif accion == "descargar":
            if tipo_stock == "cajas":
                insumo.stock_actual_cajas -= cantidad
                Salida.objects.create(insumo=insumo, cantidad_cajas=cantidad, cantidad=cantidad*30, tipo_stock='stock_normal')
            else:
                insumo.backup_unidades -= cantidad
                Salida.objects.create(insumo=insumo, cantidad_cajas=0, cantidad=cantidad, tipo_stock='seguridad')

        insumo.save()
        return f"✅ Registro exitoso. Nuevo total: {insumo.total_unidades_reales} un. ({insumo.semaforo_estado})"
    except Exception as e:
        return f"Error técnico: {e}"

def obtener_resumen_pedidos():
    """Consulta trámites de OS y Backup pendientes."""
    try:
        connection.close_if_unusable_or_obsolete() # Limpieza antes de consulta
        hoy = timezone.now().date()
        envio_os_mes = Envio.objects.filter(tipo='os', fecha_solicitud__month=hoy.month).last()
        txt = "📋 Estado de Gestión Mensual:\n\n"
        if not envio_os_mes:
            txt += "⚠️ *Atención:* No iniciaste el trámite de OS este mes.\n\n"
        else:
            txt += f"✅ Trámite OS: {envio_os_mes.get_estado_display()}\n\n"
        
        pendientes = Envio.objects.filter(estado='tramite')
        if pendientes.exists():
            txt += "*En curso:*\n"
            for e in pendientes:
                txt += f"🔹 {e.tipo.upper()}: Hace {(hoy - e.fecha_solicitud.date()).days} días.\n"
        return txt
    except Exception as e:
        return f"Error en resumen: {e}"

# --- 4. RUTINA DE MONITOREO AUTOMÁTICO (CORREGIDA) ---

async def rutina_monitoreo_astrana(application):
    """Chequea umbrales a las 10 y 20hs, y envía resumen los viernes."""
    CHAT_ID = 8034926015 
    tz = pytz.timezone('America/Argentina/Buenos_Aires')
    ultimo_chequeo_hora = None
    ultimo_resumen_dia = None

    while True:
        try:
            ahora = datetime.now(tz)
            hoy_str = ahora.strftime('%Y-%m-%d')
            
            # Chequeo Diario (10:00 y 20:00)
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

            # Resumen Semanal (Viernes 10:00)
            if ahora.weekday() == 4 and ahora.hour == 10 and ultimo_resumen_dia != hoy_str:
                insumos = await obtener_insumos_db()
                resumen = "📊 *RESUMEN SEMANAL DE INSUMOS*\n\n"
                for i in insumos:
                    resumen += (f"🔹 *{i.nombre}*\n"
                                f"   • Cajas (OS): {i.stock_actual_cajas}\n"
                                f"   • Backup: {i.backup_unidades} un.\n"
                                f"   • Autonomía: {i.autonomia_smart} días\n\n")
                await application.bot.send_message(chat_id=CHAT_ID, text=resumen, parse_mode='Markdown')
                ultimo_resumen_dia = hoy_str

        except Exception as e:
            print(f"Error en monitoreo: {e}")
        
        await asyncio.sleep(60)

# --- 5. CONFIGURACIÓN DE IA Y BOT ---

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    model_name='models/gemini-flash-latest', 
    tools=[consultar_estado_stock, registrar_movimiento, obtener_resumen_pedidos]
)

historiales = {}

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Limpiamos conexiones antes de procesar cualquier mensaje
    await sync_to_async(connection.close_if_unusable_or_obsolete)()

    if user_id not in historiales:
        prompt = "Sos Astrana, asistente de Joaco. Tono profesional. Usá las herramientas para gestionar stock."
        historiales[user_id] = model.start_chat(history=[], enable_automatic_function_calling=True)
        await asyncio.to_thread(historiales[user_id].send_message, prompt)

    try:
        response = await asyncio.to_thread(historiales[user_id].send_message, update.message.text)
        await update.message.reply_text(response.text)
    except Exception as e:
        print(f"Error en respuesta: {e}")

# --- 6. PUNTO DE ENTRADA ---

async def post_init(application):
    """Inicia la rutina de fondo sin bloquear el bot."""
    asyncio.create_task(rutina_monitoreo_astrana(application))

if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    application.post_init = post_init
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), responder))
    
    print("🚀 Astrana IA desplegando en Render...")
    application.run_polling()