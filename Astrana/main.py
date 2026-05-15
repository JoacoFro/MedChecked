import os
import sys
import django
import asyncio
import pytz
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import google.generativeai as genai
from django.utils import timezone
from asgiref.sync import sync_to_async
# IMPORTANTE: Necesitamos esto para limpiar conexiones muertas
from django.db import connection

# --- 1. PUENTE CON DJANGO ---
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

load_dotenv(BASE_DIR / ".env")
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from medicine_control.models import Insumo, Pedido, Salida, Envio

# --- 2. WRAPPERS PARA DJANGO ---

@sync_to_async
def obtener_insumos_db():
    """Puente asíncrono que limpia la conexión SSL antes de consultar."""
    connection.close_if_unusable_or_obsolete()
    return list(Insumo.objects.all())

# --- 3. FUNCIONES DE LÓGICA (TOOLS CORREGIDAS) ---

def consultar_estado_stock():
    """Consulta el stock detallado limpiando la conexión SSL."""
    try:
        connection.close_if_unusable_or_obsolete()
        insumos = Insumo.objects.all()
        if not insumos:
            return "No hay insumos registrados."
        
        reporte = "📊 Estado Actual:\n"
        for i in insumos:
            aut = i.autonomia_smart
            emoji = "🔴" if aut <= 10 else "🟡" if aut <= 15 else "🟢"
            reporte += (f"- {i.nombre}: {i.total_unidades_reales} un. "
                        f"({i.stock_actual_cajas} cajas, {i.backup_unidades} backup). "
                        f"Autonomía: {emoji} {aut} días.\n")
        return reporte
    except Exception as e:
        return f"Error al consultar stock: {e}"

def registrar_movimiento(accion: str, cantidad: int, tipo_stock: str, nombre_insumo: str = "Sonda"):
    try:
        connection.close_if_unusable_or_obsolete()
        
        # LIMPIEZA: Si Gemini manda "Sondas", le sacamos la 's' para que machee mejor
        nombre_busqueda = nombre_insumo.rstrip('sS') 
        
        insumo = Insumo.objects.filter(nombre__icontains=nombre_busqueda).first()
        
        if not insumo:
            # ERROR CLARO: Para que Gemini no invente el éxito
            return f"❌ ERROR: No encontré el insumo '{nombre_insumo}' (busqué como '{nombre_busqueda}'). Verificá el nombre en el panel."

        ahora = timezone.now()
        
        if accion == "descargar":
            if tipo_stock in ["principal", "cajas"]:
                insumo.stock_actual_cajas -= cantidad
                Salida.objects.create(
                    insumo=insumo, 
                    cantidad_cajas=cantidad, 
                    cantidad=cantidad*30, 
                    tipo_stock='stock_normal'
                )
            else:
                insumo.backup_unidades -= cantidad
                Salida.objects.create(
                    insumo=insumo, 
                    cantidad_cajas=0, 
                    cantidad=cantidad, 
                    tipo_stock='seguridad'
                )

        insumo.save()
        insumo.refresh_from_db() # Sincronizamos con la DB
        
        return f"✅ Descarga exitosa en {insumo.nombre}. Nuevo total: {insumo.total_unidades_reales} un."

    except Exception as e:
        return f"❌ Error técnico: {str(e)}"

def obtener_resumen_pedidos():
    """Consulta trámites con limpieza de conexión."""
    try:
        connection.close_if_unusable_or_obsolete()
        hoy = timezone.now().date()
        envio_os_mes = Envio.objects.filter(tipo='os', fecha_solicitud__month=hoy.month).last()
        txt = "📋 *Estado de Gestión Mensual:*\n\n"
        if not envio_os_mes:
            txt += "⚠️ *Atención:* No iniciaste el trámite de OS este mes.\n\n"
        else:
            txt += f"✅ *Trámite OS:* {envio_os_mes.get_estado_display()}\n\n"
        
        pendientes = Envio.objects.filter(estado='tramite')
        if pendientes.exists():
            txt += "*En curso:*\n"
            for e in pendientes:
                txt += f"🔹 {e.tipo.upper()}: Hace {(hoy - e.fecha_solicitud.date()).days} días.\n"
        return txt
    except Exception as e:
        return f"Error en resumen: {e}"

# --- 4. RUTINA DE MONITOREO ---

async def rutina_monitoreo_astrana(application):
    CHAT_ID = 8034926015 
    tz = pytz.timezone('America/Buenos_Aires')
    ultimo_chequeo_hora = None
    ultimo_resumen_dia = None

    while True:
        try:
            ahora = datetime.now(tz)
            if ahora.hour in [10, 20] and ultimo_chequeo_hora != ahora.hour:
                insumos = await obtener_insumos_db()
                alertas = []
                for i in insumos:
                    if (i.stock_actual_cajas * 30) <= 30:
                        alertas.append(f"📦 *Stock Normal:* Queda {i.stock_actual_cajas} caja de {i.nombre}.")
                    if i.backup_unidades <= 56:
                        alertas.append(f"🛡️ *Seguridad:* {i.nombre} tiene solo {i.backup_unidades} un. de backup.")
                    if i.autonomia_smart <= 10:
                        alertas.append(f"🚨 *Crítico:* {i.nombre} con autonomía de {i.autonomia_smart} días.")

                if alertas:
                    msg = "⚠️ *ASTRANA: ALERTAS DE SISTEMA*\n\n" + "\n".join(alertas)
                    await application.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
                ultimo_chequeo_hora = ahora.hour
        except Exception as e:
            print(f"Error en monitoreo: {e}")
        await asyncio.sleep(60)

# --- 5. CONFIGURACIÓN DE IA Y BOT ---

# --- 5. CONFIGURACIÓN DE IA Y BOT ---

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

genai.configure(api_key=GEMINI_API_KEY)

# ESTO ES LO NUEVO: Las instrucciones "grabadas" en el cerebro de Astrana
instrucciones_sistema = (
    "Sos Astrana, la asistente técnica de Joaco para MedChecked. "
    "Tu única fuente de verdad es la base de datos. "
    "NUNCA calcules el stock manualmente. Si el usuario te dice que usó algo, "
    "DEBES llamar a la función 'registrar_movimiento'. "
    "Si el usuario dice 'Sondas', usá el nombre 'Sonda' para la herramienta. "
    "Confirma siempre mostrando el 'Nuevo total' que te devuelva la función."
)

model = genai.GenerativeModel(
    model_name='models/gemini-flash-latest', 
    tools=[consultar_estado_stock, registrar_movimiento, obtener_resumen_pedidos],
    system_instruction=instrucciones_sistema 
)


historiales = {}

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Si el usuario no tiene historial, iniciamos el chat (ya tiene las instrucciones del model)
    if user_id not in historiales:
        historiales[user_id] = model.start_chat(history=[], enable_automatic_function_calling=True)

    try:
        # Enviamos el mensaje del usuario a Gemini
        response = await asyncio.to_thread(historiales[user_id].send_message, update.message.text)
        
        # Si Gemini responde con texto, lo mandamos a Telegram
        if response.text:
            await update.message.reply_text(response.text)
        else:
            # En caso de que ejecute una tool y no genere texto extra
            await update.message.reply_text("Movimiento procesado correctamente en la base de datos.")
            
    except Exception as e:
        print(f"Error en respuesta: {e}")
        await update.message.reply_text("Tuve un problema técnico. ¿Podés repetir el comando?")

async def post_init(application):
    asyncio.create_task(rutina_monitoreo_astrana(application))

if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.post_init = post_init
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), responder))
    print("🚀 Astrana IA con corrección SSL y Stock activa...")
    application.run_polling()