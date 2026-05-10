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
import os
from dotenv import load_dotenv

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
    """Chequea umbrales a las 10 y 20hs, envía resumen los viernes y prepara bienvenida."""
    CHAT_ID = 8034926015 
    tz = pytz.timezone('America/Argentina/Buenos_Aires')
    ultimo_chequeo_hora = None
    ultimo_resumen_dia = None
    ultima_bienvenida_dia = None

    while True:
        try:
            ahora = datetime.now(tz)
            hoy_str = ahora.strftime('%Y-%m-%d')
            
            # --- 1. PREPARAR BIENVENIDA (06:00 AM) ---
            if ahora.hour == 6 and ahora.minute == 0 and ultima_bienvenida_dia != hoy_str:
                insumos = await obtener_insumos_db()
                alertas_criticas = [i.nombre for i in insumos if i.autonomia_smart <= 10]
                es_viernes = (ahora.weekday() == 4)
                
                # Definimos el mensaje personalizado según la situación
                if alertas_criticas:
                    nombres = ", ".join(alertas_criticas)
                    texto_alexa = f"Atención Joaco, Astrana informa stock crítico en: {nombres}. Por favor, revisá Telegram."
                
                elif es_viernes:
                    # Reporte detallado para los viernes
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
                    import requests
                    # Usamos el endpoint 'announcement' para que el texto quede disponible
                    url_anuncio = (
                        f"https://voicemonkey.io/trigger/announcement?"
                        f"access_token={VOICEMONKEY_ACCESS_TOKEN}&"
                        f"secret_token={VOICEMONKEY_SECRET_TOKEN}&"
                        f"monkey={MONKEY_NAME}&"
                        f"announcement={requests.utils.quote(texto_alexa)}"
                    )
                    await asyncio.to_thread(requests.get, url_anuncio)
                    print(f"📢 Mensaje de bienvenida preparado: {texto_alexa}")
                    ultima_bienvenida_dia = hoy_str
                except Exception as alexa_e:
                    print(f"Error al preparar bienvenida: {alexa_e}")

            # --- 2. CHEQUEO DIARIO (10:00 y 20:00) ---
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
                    # Enviar a Telegram
                    msg = "⚠️ *ASTRANA: ALERTAS DE SISTEMA*\n\n" + "\n".join(alertas)
                    await application.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
                    
                    # Alerta inmediata a Alexa solo si es crítico fuera del horario de bienvenida
                    if any("🚨 Crítico" in a for a in alertas):
                        try:
                            import requests
                            url_critica = (
                                f"https://voicemonkey.io/trigger/monkeyslot?"
                                f"access_token={VOICEMONKEY_ACCESS_TOKEN}&"
                                f"secret_token={VOICEMONKEY_SECRET_TOKEN}&"
                                f"monkey={MONKEY_NAME}"
                            )
                            await asyncio.to_thread(requests.get, url_critica)
                        except: pass

                ultimo_chequeo_hora = ahora.hour

            # --- 3. RESUMEN SEMANAL (Viernes 10:00) ---
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
            print(f"Error en monitoreo: {e}")
        
        await asyncio.sleep(60)