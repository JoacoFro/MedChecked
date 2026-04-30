import os
import django
import telebot
from telebot import types
from django.utils import timezone

# 1. Configuración de Django (SIEMPRE PRIMERO)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

# Importamos los modelos después de setup
from medicine_control.models import Insumo, Pedido, Salida

# 2. Inicializar el bot
TOKEN = '8701141296:AAGjRcTHOXA5bBoa3IYaa3boKB78scx0g_Y'
bot = telebot.TeleBot(TOKEN)

print("🤖 Astrana online. 'stock' para todo, 'autonomia' para los días.")

# --- FUNCIÓN DE APOYO ---
def obtener_sonda():
    return Insumo.objects.filter(nombre__icontains="Sonda").first()

# --- OPCIÓN 1: TODO EL DETALLE DE STOCK ---
@bot.message_handler(func=lambda message: "stock" in message.text.lower())
def responder_stock(message):
    try:
        insumos = Insumo.objects.all()
        txt = "📊 *Estado Actual de Stock*\n\n"
        for i in insumos:
            txt += (f"🔹 *{i.nombre}:*\n"
                    f"   • Total: {i.total_unidades_reales} unidades\n"
                    f"   • En Cajas: {i.stock_actual_cajas}\n"
                    f"   • En Backup: {i.backup_unidades}\n\n")
        bot.reply_to(message, txt, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")

# --- OPCIÓN 2: SOLO AUTONOMÍA ---
@bot.message_handler(func=lambda message: "autonomia" in message.text.lower())
def responder_autonomia(message):
    try:
        insumos = Insumo.objects.all()
        txt = "⏳ *Autonomía Estimada*\n\n"
        for i in insumos:
            aut = i.autonomia_smart
            emoji = "🟢" if aut >= 15 else "🟡" if aut >= 7 else "🔴"
            txt += (f"🔹 *{i.nombre}*\n"
                    f"   • {emoji} Te quedan: *{aut} días*\n\n")
        
        txt += "_Basado en tu consumo promedio._"
        bot.reply_to(message, txt, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")

# --- OPCIÓN 3: CARGAS Y SALIDAS ---
@bot.message_handler(func=lambda message: any(w in message.text.lower() for w in ["cargar", "salida", "gasto", "descargar"]))
def iniciar_movimiento(message):
    texto = message.text.lower()
    try:
        partes = texto.split()
        cantidad = next(int(s) for s in partes if s.isdigit())
        accion = "cargar" if "cargar" in texto else "salida"
        
        markup = types.InlineKeyboardMarkup()
        btn_os = types.InlineKeyboardButton("📦 Obra Social (Cajas)", callback_data=f"{accion}_{cantidad}_normal")
        btn_bk = types.InlineKeyboardButton("🛡️ Seguridad (Unidades)", callback_data=f"{accion}_{cantidad}_backup")
        markup.add(btn_os, btn_bk)
        
        bot.send_message(message.chat.id, f"¿Registro {accion} de {cantidad} en qué stock?", reply_markup=markup)
    except StopIteration:
        bot.reply_to(message, "⚠️ Astrana: Necesito un número. Ej: 'cargar 10'")

# --- PROCESADOR DE BOTONES (CALLBACK) ---
@bot.callback_query_handler(func=lambda call: True)
def procesar_callback(call):
    accion, cantidad, tipo = call.data.split("_")
    cantidad = int(cantidad)
    insumo = obtener_sonda()
    
    if not insumo:
        bot.answer_callback_query(call.id, "No encontré el insumo.")
        return

    ahora = timezone.now()

    if accion == "cargar":
        if tipo == "normal":
            insumo.stock_actual_cajas += 10 # Tu lógica de 10 cajas
            unidades_log = 300
            Pedido.objects.create(insumo=insumo, tipo='normal', tipo_stock='stock_normal', 
                                 cantidad=unidades_log, fecha=ahora, lugar_compra="Obra Social (Bot)")
        else:
            insumo.backup_unidades += cantidad
            unidades_log = cantidad
            Pedido.objects.create(insumo=insumo, tipo='propio', tipo_stock='seguridad', 
                                 cantidad=unidades_log, fecha=ahora, lugar_compra="Backup (Bot)")
    
    else: # SALIDA
        if tipo == "normal":
            unidades_log = cantidad * 30
            insumo.stock_actual_cajas -= cantidad
            Salida.objects.create(insumo=insumo, cantidad_cajas=cantidad, 
                                 cantidad=unidades_log, tipo_stock='stock_normal')
        else:
            unidades_log = cantidad
            insumo.backup_unidades -= unidades_log
            Salida.objects.create(insumo=insumo, cantidad_cajas=0, 
                                 cantidad=unidades_log, tipo_stock='seguridad')

    insumo.save()
    
    msg = f"✅ **Astrana reporta:**\nSe registró {accion} de {cantidad} en stock {tipo}.\n\n"
    msg += f"📊 **Total Real:** {insumo.total_unidades_reales} unidades.\n"
    msg += f"🚦 **Estado:** {insumo.semaforo_estado}"
    
    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=msg, parse_mode="Markdown")

# 3. Lanzar el bot (ESTA LÍNEA SIEMPRE VA ÚLTIMA)
bot.infinity_polling()