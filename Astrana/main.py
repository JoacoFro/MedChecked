import os
import sys
import asyncio
import threading
import logging
import json
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

import django
from django.db import connection, transaction
from django.utils import timezone
from asgiref.sync import sync_to_async

# --- 1. PUENTE Y CONFIGURACIÓN CON DJANGO ---
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

load_dotenv(BASE_DIR / ".env")
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from medicine_control.models import (
    Insumo,
    Pedido,
    Salida,
    Envio,
    Pastillero,
    TomaPastillero,
    MemoriaAstrana,
    AprendizajeAstrana,
)
from google import genai
from google.genai import types

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- 2. SERVIDOR DUMMY HTTP PARA RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Astrana Bot Activo y Saludable")

#-- def run_dummy_server():
 #--   port = int(os.environ.get("PORT", 10000))
  #--  server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
 #--   server.serve_forever(

#-- threading.Thread(target=run_dummy_server, daemon=True).start() 

# --- 3. FUNCIONES DE LÓGICA / HERRAMIENTAS DJANGO ---

def consultar_estado_stock():
    """Consulta el stock detallado limpiando la conexión SSL."""
    try:
        connection.close_if_unusable_or_obsolete()
        insumos = Insumo.objects.all()
        if not insumos:
            return "No hay insumos registrados en la base de datos."
        
        reporte = "📊 **Estado Actual del Stock:**\n"
        for i in insumos:
            aut = i.autonomia_smart
            emoji = "🔴" if aut <= 10 else "🟡" if aut <= 15 else "🟢"
            reporte += (f"• **{i.nombre}**: {i.total_unidades_reales} un. "
                        f"({i.stock_actual_cajas} cajas, {i.backup_unidades} backup). "
                        f"Autonomía: {emoji} {aut} días.\n")
        return reporte
    except Exception as e:
        return f"Error al consultar stock: {e}"


def consultar_stock_sondas():
    """Devuelve solo el stock actual de Sondas."""
    try:
        connection.close_if_unusable_or_obsolete()
        insumos = Insumo.objects.filter(nombre__icontains='sonda')
        if not insumos.exists():
            return 'No hay Sondas registradas.'
        return '\n'.join(
            f'📦 {insumo.nombre}: {insumo.total_unidades_reales} unidades '
            f'({insumo.stock_actual_cajas} cajas y {insumo.backup_unidades} de seguridad).'
            for insumo in insumos
        )
    except Exception as error:
        return f'Error al consultar el stock de Sondas: {error}'


def obtener_memoria(chat_id):
    return {
        memoria.clave: memoria.valor
        for memoria in MemoriaAstrana.objects.filter(
            chat_id=str(chat_id), activa=True, confirmada=True
        )
    }


def guardar_memoria(chat_id, categoria, clave, valor):
    memoria, _ = MemoriaAstrana.objects.update_or_create(
        chat_id=str(chat_id),
        categoria=categoria,
        clave=clave,
        defaults={'valor': valor, 'confirmada': True, 'activa': True},
    )
    return memoria


def registrar_aprendizaje(chat_id, frase, intencion, respuesta=''):
    return AprendizajeAstrana.objects.create(
        chat_id=str(chat_id),
        frase=frase,
        intencion=intencion,
        respuesta=respuesta,
        confirmado=True,
    )


def interpretar_aclaracion_sondas(texto):
    palabras = set(re.findall(r'\b\w+\b', normalizar_texto(texto)))
    if palabras & {'stock', 'disponible', 'cantidad'}:
        return 'stock_sondas'
    if palabras & {'movimiento', 'movimientos', 'ingreso', 'ingresos', 'egreso', 'egresos', 'salida', 'salidas'}:
        return 'movimientos_sondas'
    if palabras & {'autonomia', 'dias', 'duracion', 'alcance'}:
        return 'autonomia_sondas'
    if palabras & {'envio', 'envios', 'tramite', 'tramites', 'pedido', 'pedidos'}:
        return 'movimientos_tramites'
    return None


def detectar_consulta_con_memoria(chat_id, texto_usuario):
    """Busca aprendizajes confirmados del chat antes de usar reglas generales."""
    texto = normalizar_texto(texto_usuario)
    palabras_texto = set(re.findall(r'\b\w+\b', texto))
    if not palabras_texto:
        return None

    memorias = MemoriaAstrana.objects.filter(
        chat_id=str(chat_id), categoria='alias', activa=True, confirmada=True
    ).order_by('-fecha_actualizacion')
    for memoria in memorias:
        palabras_frase = set(re.findall(r'\b\w+\b', normalizar_texto(memoria.clave)))
        if palabras_frase and palabras_frase.issubset(palabras_texto):
            return memoria.valor

    aprendizajes = AprendizajeAstrana.objects.filter(
        chat_id=str(chat_id), confirmado=True
    ).order_by('-fecha')
    mejor_intencion = None
    mejor_puntaje = 0
    for aprendizaje in aprendizajes:
        palabras_frase = set(re.findall(r'\b\w+\b', normalizar_texto(aprendizaje.frase)))
        if not palabras_frase:
            continue
        coincidencias = len(palabras_texto & palabras_frase)
        puntaje = coincidencias / len(palabras_frase)
        if puntaje >= 0.6 and puntaje > mejor_puntaje:
            mejor_puntaje = puntaje
            mejor_intencion = aprendizaje.intencion
    return mejor_intencion


def intencion_desde_regla(texto):
    palabras = set(re.findall(r'\b\w+\b', normalizar_texto(texto)))
    if 'sonda' in palabras or 'sondas' in palabras:
        if palabras & {'movimiento', 'movimientos', 'ingreso', 'ingresos', 'egreso', 'egresos', 'salida', 'salidas'}:
            return 'movimientos_sondas'
        if palabras & {'autonomia', 'dias', 'duracion'}:
            return 'autonomia_sondas'
        if palabras & {'stock', 'cantidad', 'disponible'}:
            return 'stock_sondas'
    if palabras & {'tramite', 'tramites', 'envio', 'envios'}:
        return 'movimientos_tramites'
    return None


def procesar_comando_memoria(chat_id, texto_usuario):
    """Procesa comandos explícitos de memoria sin depender de un modelo externo."""
    texto = re.sub(r'[^\w\s]', '', normalizar_texto(texto_usuario)).strip()

    if texto in {'que recordas', 'que recordas de mi', 'que recordas sobre mi'}:
        memorias = MemoriaAstrana.objects.filter(
            chat_id=str(chat_id), activa=True, confirmada=True
        ).order_by('-fecha_actualizacion')[:20]
        aprendizajes = AprendizajeAstrana.objects.filter(
            chat_id=str(chat_id), confirmado=True
        ).order_by('-fecha')[:20]
        if not memorias and not aprendizajes:
            return 'Todavía no tengo recuerdos guardados sobre este chat.'
        partes = ['🧠 Esto es lo que recuerdo:']
        partes.extend(f'• {memoria.clave}: {memoria.valor}' for memoria in memorias)
        partes.extend(f'• “{aprendizaje.frase}” → {aprendizaje.intencion}' for aprendizaje in aprendizajes)
        return '\n'.join(partes)

    patron_guardar = re.search(r'(?:recorda|recuerda) que cuando digo (.+?) quiero (.+)$', texto)
    if patron_guardar:
        frase = patron_guardar.group(1).strip(' "\'')
        significado = patron_guardar.group(2).strip()
        intencion = intencion_desde_regla(significado)
        if not intencion:
            return 'Puedo aprender reglas sobre stock, movimientos, trámites o pastillero. ¿Qué consulta querés asociar?'
        guardar_memoria(chat_id, 'alias', frase, intencion)
        registrar_aprendizaje(chat_id, frase, intencion, significado)
        return f'✅ Lo voy a recordar: cuando digas “{frase}”, entenderé “{significado}”.'

    patron_olvidar = re.search(r'(?:olvida|olvidá|olvidar) (?:que )?(?:cuando digo )?(.+)$', texto)
    if patron_olvidar:
        frase = patron_olvidar.group(1).strip(' "\'')
        memorias = MemoriaAstrana.objects.filter(
            chat_id=str(chat_id), clave__iexact=frase, activa=True
        )
        cantidad = memorias.update(activa=False)
        AprendizajeAstrana.objects.filter(
            chat_id=str(chat_id), frase__iexact=frase, confirmado=True
        ).update(confirmado=False)
        if cantidad:
            return f'🗑️ Olvidé la asociación “{frase}”.'
        return f'No encontré una asociación guardada para “{frase}”.'

    return None


def consultar_autonomia_sondas():
    """Devuelve la autonomía calculada para los insumos de Sondas."""
    try:
        connection.close_if_unusable_or_obsolete()
        insumos = Insumo.objects.filter(nombre__icontains='sonda')
        if not insumos.exists():
            return 'No hay Sondas registradas.'
        return '\n'.join(
            f'📊 {insumo.nombre}: {insumo.autonomia_smart} días de autonomía.'
            for insumo in insumos
        )
    except Exception as error:
        return f'Error al consultar la autonomía de Sondas: {error}'

def consultar_stock_pastillero():
    """Consulta el stock de medicamentos guardado en la tabla Pastillero."""
    try:
        connection.close_if_unusable_or_obsolete()
        medicamentos = Pastillero.objects.order_by('nombre')
        if not medicamentos.exists():
            return "No hay medicamentos registrados en el pastillero."

        reporte = "💊 **Stock del Pastillero:**\n"
        for medicamento in medicamentos:
            reporte += f"• **{medicamento.nombre}**: {medicamento.cantidad_total} pastillas.\n"
        return reporte
    except Exception as e:
        return f"Error al consultar el stock del pastillero: {e}"

def consultar_ultimos_movimientos_sondas():
    """Devuelve los últimos 10 ingresos y egresos registrados para Sondas."""
    try:
        connection.close_if_unusable_or_obsolete()
        ingresos = Pedido.objects.filter(
            insumo__nombre__icontains='sonda'
        ).select_related('insumo').order_by('-fecha', '-id')[:10]
        egresos = Salida.objects.filter(
            insumo__nombre__icontains='sonda'
        ).select_related('insumo').order_by('-fecha', '-id')[:10]

        reporte = "📥 **Últimos 10 ingresos de Sondas:**\n"
        if ingresos:
            for ingreso in ingresos:
                tipo = ingreso.get_tipo_stock_display()
                reporte += (
                    f"• {ingreso.fecha:%d/%m/%Y} | {ingreso.cantidad} un. | "
                    f"{tipo} | {ingreso.lugar_compra or 'Sin origen informado'}\n"
                )
        else:
            reporte += "No hay ingresos registrados.\n"

        reporte += "\n📤 **Últimos 10 egresos de Sondas:**\n"
        if egresos:
            for egreso in egresos:
                tipo = 'Stock normal' if egreso.tipo_stock == 'stock_normal' else 'Stock de seguridad'
                fecha = timezone.localtime(egreso.fecha).strftime('%d/%m/%Y %H:%M')
                reporte += (
                    f"• {fecha} | {egreso.cantidad} un. | {tipo}\n"
                )
        else:
            reporte += "No hay egresos registrados.\n"

        return reporte
    except Exception as e:
        return f"Error al consultar los movimientos de Sondas: {e}"

def consultar_ultimas_tomas():
    """Consulta los últimos registros de toma guardados en Pastillero."""
    try:
        connection.close_if_unusable_or_obsolete()
        tomas = TomaPastillero.objects.select_related('medicamento').order_by('-fecha_hora')[:10]
        if not tomas:
            return "No hay tomas registradas en el pastillero."

        reporte = "🗓 **Últimas tomas:**\n"
        for toma in tomas:
            fecha = timezone.localtime(toma.fecha_hora).strftime('%d/%m/%Y %H:%M')
            reporte += f"• **{toma.medicamento.nombre}**: {toma.cantidad} un. ({fecha})\n"
        return reporte
    except Exception as e:
        return f"Error al consultar las últimas tomas: {e}"

def registrar_movimiento(nombre_insumo: str, accion: str, cantidad: int, tipo_stock: str):
    """
    Registra la carga (pedido) o descarga (consumo) de insumos en el sistema.
    """
    try:
        connection.close_if_unusable_or_obsolete()
        
        nombre_busqueda = nombre_insumo.rstrip('sS') 
        insumo = Insumo.objects.filter(nombre__icontains=nombre_busqueda).first()
        
        if not insumo:
            return f"❌ ERROR: No encontré el insumo '{nombre_insumo}'."

        ahora = timezone.now()
        tipo_usado = ""

        if accion == "descargar":
            if tipo_stock in ["stock_normal", "cajas", "principal", "normal"]:
                insumo.stock_actual_cajas -= cantidad
                Salida.objects.create(
                    insumo=insumo, 
                    cantidad_cajas=cantidad, 
                    cantidad=cantidad * 30, 
                    tipo_stock='stock_normal'
                )
                tipo_usado = "Descarga de Stock Normal (Cajas)"
            else:
                insumo.backup_unidades -= cantidad
                Salida.objects.create(
                    insumo=insumo, 
                    cantidad_cajas=0, 
                    cantidad=cantidad, 
                    tipo_stock='seguridad'
                )
                tipo_usado = "Descarga de Stock de Seguridad (Unidades)"

        elif accion == "cargar":
            if tipo_stock in ["stock_normal", "cajas", "principal", "normal"]:
                insumo.stock_actual_cajas += cantidad
                Pedido.objects.create(
                    insumo=insumo,
                    tipo='normal',
                    tipo_stock='stock_normal',
                    cantidad=cantidad * 30,
                    fecha=ahora,
                    lugar_compra="Astrana IA"
                )
                tipo_usado = "Carga de Stock Normal (Cajas)"
            else:
                insumo.backup_unidades += cantidad
                Pedido.objects.create(
                    insumo=insumo,
                    tipo='propio',
                    tipo_stock='seguridad',
                    cantidad=cantidad,
                    fecha=ahora,
                    lugar_compra="Astrana IA"
                )
                tipo_usado = "Carga de Stock de Seguridad (Unidades)"
        else:
            return f"❌ ERROR: Acción '{accion}' no reconocida. Usar 'cargar' o 'descargar'."

        insumo.save()
        insumo.refresh_from_db()
        return f"✅ Operación exitosa: {tipo_usado} para {insumo.nombre}. Cantidad: {cantidad}. Nuevo total real: {insumo.total_unidades_reales} un."

    except Exception as e:
        return f"❌ Error técnico: {str(e)}"

def iniciar_tramite_pedido(tipo_tramite: str, cantidad: int = None):
    """
    Inicia un trámite mensual ('os' o 'backup') y registra la cantidad pedida.
    """
    try:
        connection.close_if_unusable_or_obsolete()
        
        tipo_normalizado = tipo_tramite.lower().strip()
        if tipo_normalizado in ["obra social", "os", "social"]:
            tipo_final = "os"
            nombre_legible = "Obra Social"
        elif tipo_normalizado in ["backup", "seguridad", "propio"]:
            tipo_final = "backup"
            nombre_legible = "Backup"
        else:
            return f"❌ ERROR: El tipo de trámite '{tipo_tramite}' no es válido."
            
        if cantidad is None or cantidad <= 0:
            return f"❓ ¿Cuántas cajas o unidades vas a solicitar para el trámite de {nombre_legible}?"
            
        hoy = timezone.now()
        tramite_existente = Envio.objects.filter(
            tipo=tipo_final, 
            estado='tramite',
            fecha_solicitud__month=hoy.month,
            fecha_solicitud__year=hoy.year
        ).exists()
        
        if tramite_existente:
            return f"⚠️ ATENCIÓN: Ya existe un trámite de {nombre_legible} en curso para este mes."
            
        nuevo_envio = Envio.objects.create(
            tipo=tipo_final,
            estado='tramite',
            cantidad_pedida=cantidad
        )
        return f"📋 ¡Trámite de {nombre_legible} Iniciado! Registrado con una solicitud de {cantidad} cajas/unidades."

    except Exception as e:
        return f"❌ Error técnico al iniciar trámite: {str(e)}"

def cerrar_tramite_pedido(tipo_tramite: str, tipo_stock: str = "cajas"):
    """
    Cierra un trámite activo pasándolo a 'recibido' e impacta el stock.
    """
    try:
        connection.close_if_unusable_or_obsolete()
        
        tipo_normalizado = tipo_tramite.lower().strip()
        if tipo_normalizado in ["obra social", "os", "social"]:
            tipo_final = "os"
            nombre_legible = "Obra Social"
            insumo_defecto = "Sonda"
        elif tipo_normalizado in ["backup", "seguridad", "propio"]:
            tipo_final = "backup"
            nombre_legible = "Backup"
            insumo_defecto = "Sonda"
        else:
            return f"❌ ERROR: Tipo de trámite '{tipo_tramite}' no reconocido."
            
        tramite = Envio.objects.filter(tipo=tipo_final, estado='tramite').last()
        if not tramite:
            return f"⚠️ No encontré ningún trámite activo de {nombre_legible} en curso para cerrar."
            
        cantidad = tramite.cantidad_pedida
        ahora = timezone.now()
        
        tramite.estado = 'recibido'
        tramite.fecha_cierre = ahora.date()
        tramite.save()

        dias_demora = (tramite.fecha_cierre - tramite.fecha_solicitud).days
        resultado_msg = f"📋 ¡Trámite de {nombre_legible} cerrado con éxito! Estado: 'Recibido'. Joaco el tramite {dias_demora}"

        if cantidad and cantidad > 0:
            insumo = Insumo.objects.filter(nombre__icontains=insumo_defecto).first()
            if not insumo:
                return resultado_msg + f" ⚠️ Trámite cerrado, pero no encontré el insumo '{insumo_defecto}' para actualizar stock."
            
            if tipo_stock in ["stock_normal", "cajas", "principal", "normal"]:
                insumo.stock_actual_cajas += cantidad
                Pedido.objects.create(
                    insumo=insumo,
                    tipo=tipo_final,
                    tipo_stock='stock_normal',
                    cantidad=cantidad,
                    fecha=ahora.date(),
                    lugar_compra=f"Cierre Trámite {nombre_legible}"
                )
                detalle_stock = f"Se sumaron {cantidad} cajas al Stock Normal."
            else:
                insumo.backup_unidades += cantidad
                Pedido.objects.create(
                    insumo=insumo,
                    tipo=tipo_final,
                    tipo_stock='seguridad',
                    cantidad=cantidad,
                    fecha=ahora.date(),
                    lugar_compra=f"Cierre Trámite {nombre_legible}"
                )
                detalle_stock = f"Se sumaron {cantidad} unidades al Stock de Seguridad."
                
            insumo.save()
            insumo.refresh_from_db()
            resultado_msg += f"\n📦 ¡Base de datos actualizada! {detalle_stock}\nNuevo total real disponible: {insumo.total_unidades_reales} un."
        else:
            resultado_msg += f"\n⚠️ El trámite se cerró, pero no tenía cantidad pedida registrada."

        return resultado_msg

    except Exception as e:
        return f"❌ Error técnico al procesar el cierre: {str(e)}"

def obtener_resumen_pedidos():
    """Consulta trámites en curso y los 10 cerrados más recientes."""
    try:
        connection.close_if_unusable_or_obsolete()
        en_curso = Envio.objects.filter(
            estado='tramite'
        ).order_by('-fecha_solicitud', '-id')[:10]
        cerrados = Envio.objects.filter(
            estado='recibido',
            fecha_cierre__isnull=False,
        ).order_by('-fecha_cierre', '-id')[:10]

        reporte = "📋 **Trámites en curso:**\n"
        if en_curso:
            for tramite in en_curso:
                reporte += (
                    f"• **{tramite.get_tipo_display()}** | "
                    f"Estado: {tramite.get_estado_display()} | "
                    f"Cantidad: {tramite.cantidad_pedida} | "
                    f"Inicio: {tramite.fecha_solicitud:%d/%m/%Y} | "
                    f"Demora: {tramite.demora_real} días\n"
                )
        else:
            reporte += "No hay trámites en curso.\n"

        reporte += "\n✅ **Trámites cerrados recientemente:**\n"
        if cerrados:
            for tramite in cerrados:
                reporte += (
                    f"• **{tramite.get_tipo_display()}** | "
                    f"Estado: {tramite.get_estado_display()} | "
                    f"Cantidad: {tramite.cantidad_pedida} | "
                    f"Inicio: {tramite.fecha_solicitud:%d/%m/%Y} | "
                    f"Cierre: {tramite.fecha_cierre:%d/%m/%Y} | "
                    f"Demora: {tramite.demora_real} días\n"
                )
        else:
            reporte += "No hay trámites cerrados recientemente.\n"

        return reporte
    except Exception as e:
        return f"Error en resumen: {e}"

def consultar_ultimos_tramites():
    """Devuelve los últimos 5 movimientos registrados en Envio."""
    try:
        connection.close_if_unusable_or_obsolete()
        tramites = Envio.objects.order_by('-fecha_solicitud', '-id')[:5]

        if not tramites:
            return "📋 No hay movimientos registrados en el historial de trámites."

        reporte = "📋 **Últimos 5 movimientos de trámites:**\n"
        for tramite in tramites:
            cierre = (
                tramite.fecha_cierre.strftime('%d/%m/%Y')
                if tramite.fecha_cierre else 'Sin cerrar'
            )
            reporte += (
                f"• **{tramite.get_tipo_display()}** | "
                f"Estado: {tramite.get_estado_display()} | "
                f"Cantidad: {tramite.cantidad_pedida} | "
                f"Inicio: {tramite.fecha_solicitud:%d/%m/%Y} | "
                f"Cierre: {cierre} | "
                f"Demora: {tramite.demora_real} días\n"
            )
        return reporte
    except Exception as e:
        return f"Error al consultar el historial de trámites: {e}"

# --- 4. CONFIGURACIÓN DE GEMINI Y BOT ---
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
BOT_TIME_ZONE = ZoneInfo('America/Argentina/Buenos_Aires')
recordatorio_tasks = {}


def _estado_pastillero(medicamento_id):
    hoy = timezone.localdate()
    medicamento = Pastillero.objects.get(id=medicamento_id)
    if medicamento.estado_diario_fecha != hoy:
        medicamento.estado_diario = 'pendiente'
        medicamento.estado_diario_fecha = hoy
        medicamento.save(update_fields=['estado_diario', 'estado_diario_fecha'])
    return medicamento


def medicamentos_pendientes_ids():
    connection.close_if_unusable_or_obsolete()
    hoy = timezone.localdate()
    return list(Pastillero.objects.filter(cantidad_total__gt=0).exclude(
        estado_diario_fecha=hoy,
        estado_diario__in=['tomado', 'omitido'],
    ).values_list('id', flat=True))


def registrar_toma_recordatorio(medicamento_id):
    hoy = timezone.localdate()
    with transaction.atomic():
        medicamento = Pastillero.objects.select_for_update().get(id=medicamento_id)
        if medicamento.estado_diario_fecha == hoy and medicamento.estado_diario in ['tomado', 'omitido']:
            return medicamento, False, 'ya_procesado'
        if medicamento.cantidad_total <= 0:
            medicamento.estado_diario = 'omitido'
            medicamento.estado_diario_fecha = hoy
            medicamento.save(update_fields=['estado_diario', 'estado_diario_fecha'])
            return medicamento, False, 'sin_stock'

        medicamento.cantidad_total -= 1
        medicamento.cantidad = 1
        medicamento.fecha_hora = timezone.now()
        medicamento.estado_diario = 'tomado'
        medicamento.estado_diario_fecha = hoy
        medicamento.save(update_fields=[
            'cantidad_total', 'cantidad', 'fecha_hora',
            'estado_diario', 'estado_diario_fecha',
        ])
        TomaPastillero.objects.create(medicamento=medicamento, cantidad=1)
        return medicamento, True, 'tomado'


def omitir_medicamento_hoy(medicamento_id):
    hoy = timezone.localdate()
    with transaction.atomic():
        medicamento = Pastillero.objects.select_for_update().get(id=medicamento_id)
        if medicamento.estado_diario_fecha != hoy or medicamento.estado_diario not in ['tomado', 'omitido']:
            medicamento.estado_diario = 'omitido'
            medicamento.estado_diario_fecha = hoy
            medicamento.save(update_fields=['estado_diario', 'estado_diario_fecha'])
        return medicamento


async def enviar_recordatorio(application, medicamento_id):
    if not TELEGRAM_CHAT_ID:
        return
    medicamento = await sync_to_async(_estado_pastillero)(medicamento_id)
    if medicamento.estado_diario != 'pendiente' or medicamento.cantidad_total <= 0:
        return
    botones = InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Confirmar Toma', callback_data=f'tomar_medicamento:{medicamento_id}')],
        [InlineKeyboardButton('🙈 Olvidar por hoy', callback_data=f'ignorar_medicamento:{medicamento_id}')],
    ])
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f'💊 ¿Tomaste {medicamento.nombre}?',
        reply_markup=botones,
    )


async def reintentar_recordatorio(application, medicamento_id):
    try:
        await enviar_recordatorio(application, medicamento_id)
        while True:
            await asyncio.sleep(3600)
            pendiente = await sync_to_async(medicamentos_pendientes_ids)()
            if medicamento_id not in pendiente:
                return
            await enviar_recordatorio(application, medicamento_id)
    except asyncio.CancelledError:
        return
    except Exception as error:
        print(f'Error en recordatorio del medicamento {medicamento_id}: {error}')
    finally:
        recordatorio_tasks.pop(medicamento_id, None)


async def enviar_recordatorios_del_dia(application):
    for medicamento_id in await sync_to_async(medicamentos_pendientes_ids)():
        tarea_anterior = recordatorio_tasks.get(medicamento_id)
        if tarea_anterior and not tarea_anterior.done():
            continue
        recordatorio_tasks[medicamento_id] = asyncio.create_task(
            reintentar_recordatorio(application, medicamento_id)
        )


def proxima_hora_recordatorio():
    ahora = datetime.now(BOT_TIME_ZONE)
    for dias in range(8):
        fecha = ahora.date() + timedelta(days=dias)
        hora = (11, 0) if fecha.weekday() >= 5 else (7, 15)
        candidato = datetime(
            fecha.year,
            fecha.month,
            fecha.day,
            hora[0],
            hora[1],
            tzinfo=BOT_TIME_ZONE,
        )
        if candidato > ahora:
            return candidato
    raise RuntimeError('No se pudo calcular el próximo horario del pastillero')


async def programador_recordatorios(application):
    try:
        while True:
            siguiente = proxima_hora_recordatorio()
            espera = max(1, (siguiente - datetime.now(BOT_TIME_ZONE)).total_seconds())
            await asyncio.sleep(espera)
            await enviar_recordatorios_del_dia(application)
    except asyncio.CancelledError:
        return


async def iniciar_recordatorios(application):
    application.bot_data['programador_recordatorios'] = asyncio.create_task(
        programador_recordatorios(application)
    )


async def detener_recordatorios(application):
    tarea = application.bot_data.get('programador_recordatorios')
    if tarea:
        tarea.cancel()
    for tarea in list(recordatorio_tasks.values()):
        tarea.cancel()

gemini_client = None
gemini_config = None
gemini_classifier_config = None
if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=30000),
        )
        gemini_config = types.GenerateContentConfig(
            system_instruction=(
                'Sos Astrana. Te llamás Astrana y ayudás a gestionar el stock real. '
                'Nunca inventes cantidades: usa las herramientas disponibles. '
                'Para Sondas usa las herramientas de stock; no mezcles Sondas con Pastillero.'
            ),
            tools=[
                consultar_estado_stock,
                consultar_ultimos_movimientos_sondas,
                registrar_movimiento,
                obtener_resumen_pedidos,
                iniciar_tramite_pedido,
                cerrar_tramite_pedido,
            ],
        )
        gemini_classifier_config = types.GenerateContentConfig(
            system_instruction=(
                'Clasificá el mensaje del usuario en una sola intención. '
                'Respondé únicamente JSON válido con esta forma: '
                '{"intent":"movimientos_sondas|movimientos_tramites|otro"}. '
                'Usá movimientos_sondas para ingresos, egresos, salidas o movimientos '
                'de sondas. Usá movimientos_tramites para reportes, historial, estado, '
                'movimientos o información de trámites y envíos. Para cualquier otra '
                'consulta usá otro.'
            ),
            response_mime_type='application/json',
            temperature=0,
        )
        logger.info('Gemini quedó inicializado correctamente.')
    except Exception:
        logger.exception('No se pudo inicializar Gemini.')
else:
    logger.error('GEMINI_API_KEY no está configurada; el chat libre queda deshabilitado.')

historiales = {}


def normalizar_texto(texto):
    texto = unicodedata.normalize('NFD', texto.lower())
    return ''.join(
        caracter for caracter in texto
        if unicodedata.category(caracter) != 'Mn'
    )


def detectar_consulta_datos(texto_usuario):
    """Simula una clasificación de intención para enrutar consultas a Django."""
    texto = normalizar_texto(texto_usuario)
    palabras = set(re.findall(r'\b\w+\b', texto))

    habla_de_sondas = 'sonda' in palabras or 'sondas' in palabras
    habla_de_movimientos = bool(palabras & {
        'movimiento', 'movimientos', 'ingreso', 'ingresos',
        'egreso', 'egresos', 'salida', 'salidas',
    })
    if habla_de_sondas and habla_de_movimientos:
        return 'movimientos_sondas'

    if habla_de_sondas and bool(palabras & {
        'reporte', 'informacion', 'información', 'stock', 'estado',
        'autonomia', 'autonomía', 'situacion', 'situación',
    }):
        return 'aclarar_sondas'

    habla_de_tramites = bool(palabras & {
        'tramite', 'tramites', 'envio', 'envios',
    })
    consulta_informacion = bool(palabras & {
        'reporte', 'ultimo', 'ultimos', 'movimiento', 'movimientos',
        'historial', 'registro', 'registros', 'estado', 'informacion',
        'situacion', 'cual', 'cuales', 'abierto', 'abiertos',
        'pendiente', 'pendientes', 'cerrado', 'cerrados', 'hay', 'tengo',
        'paso', 'pasaron', 'sucedio', 'sucede',
    })
    accion_operativa = bool(palabras & {
        'iniciar', 'inicia', 'cerrar', 'cierra', 'crear',
        'agregar', 'agrega', 'quitar', 'quita',
    })
    if habla_de_tramites and consulta_informacion and not accion_operativa:
        return 'movimientos_tramites'

    return None


def es_reporte_ambiguo(texto_usuario):
    """Detecta cualquier solicitud de reporte para pedir el tipo de informe."""
    palabras = set(re.findall(r'\b\w+\b', normalizar_texto(texto_usuario)))
    return 'reporte' in palabras


def clasificar_intencion(texto_usuario):
    """Usa Gemini solo para interpretar la intención; los datos los consulta Django."""
    if gemini_client is None or gemini_classifier_config is None:
        return 'otro'
    respuesta = gemini_client.models.generate_content(
        model='gemini-3.6-flash',
        contents=texto_usuario,
        config=gemini_classifier_config,
    )
    datos = json.loads(respuesta.text)
    intencion = datos.get('intent')
    if intencion in {'movimientos_sondas', 'movimientos_tramites'}:
        return intencion
    return 'otro'

# --- 5. MENÚS MULTINIVEL (ÁRBOLES DE NAVEGACIÓN) ---

async def mostrar_menu_principal(update: Update, context: ContextTypes.DEFAULT_TYPE, saludo: str = "Hola Joaco, ¿cómo te ayudo?"):
    keyboard = [
        [InlineKeyboardButton("📦 Stock", callback_data="menu_stock")],
        [InlineKeyboardButton("💊 Pastillero", callback_data="menu_pastillero")],
        [InlineKeyboardButton("📋 Trámites", callback_data="menu_tramites")],
        [InlineKeyboardButton("💬 Hablar libremente con Astrana", callback_data="op_chat")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text(saludo, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(saludo, reply_markup=reply_markup)

async def mostrar_submenu_stock(query):
    keyboard = [
        [InlineKeyboardButton("📊 Consultar Stock", callback_data="op_stock_consultar")],
        [InlineKeyboardButton("➕ Agregar Stock", callback_data="op_stock_agregar")],
        [InlineKeyboardButton("➖ Quitar Stock", callback_data="op_stock_quitar")],
        [InlineKeyboardButton("🔙 Volver al Menú Principal", callback_data="menu_principal")]
    ]
    await query.edit_message_text("📦 **Menú de Stock:**\nSeleccioná una opción:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def mostrar_submenu_pastillero(query):
    keyboard = [
        [InlineKeyboardButton("💊 Stock Pastillas", callback_data="op_pastillero_stock")],
        [InlineKeyboardButton("🗓 Últimas tomas", callback_data="op_pastillero_tomas")],
        [InlineKeyboardButton("🔙 Volver al Menú Principal", callback_data="menu_principal")]
    ]
    await query.edit_message_text("📦 **Menú de Stock:**\nSeleccioná una opción:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def mostrar_submenu_tramites(query):
    keyboard = [
        [InlineKeyboardButton("ℹ️ Estado de trámites", callback_data="op_tramites_estado")],
        [InlineKeyboardButton("📝 Iniciar trámite de OS", callback_data="op_tramites_iniciar_os")],
        [InlineKeyboardButton("🔄 Iniciar trámite backup", callback_data="op_tramites_iniciar_backup")],
        [InlineKeyboardButton("✅ Cerrar trámites abiertos", callback_data="op_tramites_cerrar")],
        [InlineKeyboardButton("🔙 Volver al Menú Principal", callback_data="menu_principal")]
    ]
    await query.edit_message_text("📋 **Menú de Trámites:**\nSeleccioná una opción:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

def obtener_boton_volver():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Volver al Menú Principal", callback_data="menu_principal")]])


def obtener_opciones_sondas():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📦 Stock de Sondas', callback_data='aclaracion_sondas:stock')],
        [InlineKeyboardButton('📥 Movimientos de entrada y salida de Sondas', callback_data='aclaracion_sondas:movimientos')],
        [InlineKeyboardButton('📋 Envíos', callback_data='aclaracion_sondas:envios')],
        [InlineKeyboardButton('📊 Autonomía actual', callback_data='aclaracion_sondas:autonomia')],
    ])

# --- 6. MANEJADOR DE BOTONES Y ACCIONES ---

async def manejar_botones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await sync_to_async(connection.close_if_unusable_or_obsolete)()
    opcion = query.data

    # Navegación
    if opcion == "menu_principal":
        await mostrar_menu_principal(update, context)
    elif opcion == "menu_stock":
        await mostrar_submenu_stock(query)
    elif opcion == "menu_pastillero":
        await mostrar_submenu_pastillero(query)
    elif opcion == "menu_tramites":
        await mostrar_submenu_tramites(query)

    elif opcion.startswith('aclaracion_sondas:'):
        opcion_sondas = opcion.split(':', 1)[1]
        chat_id = str(query.message.chat_id)
        if opcion_sondas == 'stock':
            intencion = 'stock_sondas'
            reporte = await sync_to_async(consultar_stock_sondas)()
        elif opcion_sondas == 'movimientos':
            intencion = 'movimientos_sondas'
            reporte = await sync_to_async(consultar_ultimos_movimientos_sondas)()
        elif opcion_sondas == 'envios':
            intencion = 'movimientos_tramites'
            reporte = await sync_to_async(consultar_ultimos_tramites)()
        else:
            intencion = 'autonomia_sondas'
            reporte = await sync_to_async(consultar_autonomia_sondas)()
        context.user_data.pop('aclaracion_pendiente', None)
        await query.edit_message_text(reporte, reply_markup=obtener_boton_volver())
        return

    elif opcion.startswith('tomar_medicamento:'):
        medicamento_id = int(opcion.split(':', 1)[1])
        try:
            medicamento, registrado, motivo = await sync_to_async(registrar_toma_recordatorio)(medicamento_id)
            tarea = recordatorio_tasks.pop(medicamento_id, None)
            if tarea:
                tarea.cancel()
            if motivo == 'tomado':
                texto = f'✅ Toma registrada: {medicamento.nombre}. Quedan {medicamento.cantidad_total} pastillas.'
            elif motivo == 'sin_stock':
                texto = f'⚠️ {medicamento.nombre} no tiene stock disponible.'
            else:
                texto = f'ℹ️ {medicamento.nombre} ya fue procesado hoy.'
            await query.edit_message_text(texto, reply_markup=obtener_boton_volver())
        except Pastillero.DoesNotExist:
            await query.edit_message_text('⚠️ Ese medicamento ya no existe.', reply_markup=obtener_boton_volver())
        return

    elif opcion.startswith('ignorar_medicamento:'):
        medicamento_id = int(opcion.split(':', 1)[1])
        try:
            medicamento = await sync_to_async(omitir_medicamento_hoy)(medicamento_id)
            tarea = recordatorio_tasks.pop(medicamento_id, None)
            if tarea:
                tarea.cancel()
            await query.edit_message_text(
                f'🙈 Entendido. No te avisaré nuevamente por {medicamento.nombre} hoy.',
                reply_markup=obtener_boton_volver(),
            )
        except Pastillero.DoesNotExist:
            await query.edit_message_text('⚠️ Ese medicamento ya no existe.', reply_markup=obtener_boton_volver())
        return

    # Submenú Stock
    elif opcion == "op_stock_consultar":
        res = await sync_to_async(consultar_estado_stock)()
        await query.edit_message_text(res, reply_markup=obtener_boton_volver(), parse_mode="Markdown")
    elif opcion == "op_stock_agregar":
        await query.edit_message_text("➕ **Agregar Stock:**\nEscribime qué insumo ingresó (ejemplo: *'Ingresaron 2 cajas de sondas'*).", reply_markup=obtener_boton_volver(), parse_mode="Markdown")
    elif opcion == "op_stock_quitar":
        await query.edit_message_text("➖ **Quitar Stock:**\nEscribime qué insumo retiraste (ejemplo: *'Descontar 2 cajas de sondas'*).", reply_markup=obtener_boton_volver(), parse_mode="Markdown")

    # Submenú Pastillero
    elif opcion == "op_pastillero_stock":
        res = await sync_to_async(consultar_stock_pastillero)()
        await query.edit_message_text(res, reply_markup=obtener_boton_volver(), parse_mode="Markdown")
    elif opcion == "op_pastillero_tomas":
        res = await sync_to_async(consultar_ultimas_tomas)()
        await query.edit_message_text(res, reply_markup=obtener_boton_volver(), parse_mode="Markdown")

    # Submenú Trámites
    elif opcion == "op_tramites_estado":
        res = await sync_to_async(obtener_resumen_pedidos)()
        await query.edit_message_text(res, reply_markup=obtener_boton_volver(), parse_mode="Markdown")
    elif opcion == "op_tramites_iniciar_os":
        res = await sync_to_async(iniciar_tramite_pedido)(tipo_tramite="os", cantidad=10)
        await query.edit_message_text(res, reply_markup=obtener_boton_volver(), parse_mode="Markdown")
    elif opcion == "op_tramites_iniciar_backup":
        res = await sync_to_async(iniciar_tramite_pedido)(tipo_tramite="backup", cantidad=150)
        await query.edit_message_text(res, reply_markup=obtener_boton_volver(), parse_mode="Markdown")
    elif opcion == "op_tramites_cerrar":
        res = await sync_to_async(cerrar_tramite_pedido)(tipo_tramite="os", tipo_stock="cajas")
        await query.edit_message_text(res, reply_markup=obtener_boton_volver(), parse_mode="Markdown")

    # Modo Chat Libre
    elif opcion == "op_chat":
        await query.edit_message_text("💬 **Modo Chat con IA Activado:**\nPodés escribirme cualquier consulta libremente.", reply_markup=obtener_boton_volver())

# --- 7. ATENCIÓN DE MENSAJES Y CHAT ---

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_usuario = update.message.text.strip()
    texto_lower = texto_usuario.lower()

    # Disparador para mostrar el menú
    if "hola astrana" in texto_lower or texto_lower in ["/start", "/menu"]:
        await mostrar_menu_principal(update, context)
        return

    try:
        if es_reporte_ambiguo(texto_usuario):
            context.user_data['aclaracion_pendiente'] = 'sondas'
            await update.message.reply_text(
                '¿A qué reporte te estás refiriendo?\n\n'
                '• Stock de Sondas\n'
                '• Movimientos de entrada y salida de Sondas\n'
                '• Envíos\n'
                '• Reporte de la autonomía actual',
                reply_markup=obtener_opciones_sondas(),
            )
            return

        comando_memoria = await sync_to_async(procesar_comando_memoria)(
            str(update.effective_chat.id), texto_usuario
        )
        if comando_memoria:
            await update.message.reply_text(
                comando_memoria,
                reply_markup=obtener_boton_volver(),
            )
            return

        chat_id = str(update.effective_chat.id)
        intencion = await sync_to_async(detectar_consulta_con_memoria)(chat_id, texto_usuario)
        intencion = intencion or detectar_consulta_datos(texto_usuario)
        if intencion == 'movimientos_sondas':
            reporte = await sync_to_async(consultar_ultimos_movimientos_sondas)()
            await update.message.reply_text(reporte, reply_markup=obtener_boton_volver())
            return

        if intencion == 'aclarar_sondas':
            context.user_data['aclaracion_pendiente'] = 'sondas'
            await update.message.reply_text(
                'Claro. ¿A qué reporte te referís?\n\n'
                '• Stock de Sondas\n'
                '• Movimientos de entrada y salida de Sondas\n'
                '• Envíos\n'
                '• Reporte de la autonomía actual',
                reply_markup=obtener_opciones_sondas(),
            )
            return

        if es_reporte_ambiguo(texto_usuario):
            context.user_data['aclaracion_pendiente'] = 'sondas'
            await update.message.reply_text(
                '¿A qué reporte te estás refiriendo?\n\n'
                '• Stock de Sondas\n'
                '• Movimientos de entrada y salida de Sondas\n'
                '• Envíos\n'
                '• Reporte de la autonomía actual',
                reply_markup=obtener_opciones_sondas(),
            )
            return

        if intencion == 'movimientos_tramites':
            reporte = await sync_to_async(consultar_ultimos_tramites)()
            await update.message.reply_text(reporte, reply_markup=obtener_boton_volver())
            return

        if gemini_client is None:
            await update.message.reply_text(
                '⚠️ El chat de Astrana no está disponible porque Gemini no se inicializó.'
            )
            return

        # Si es texto libre, crea una conversación independiente por usuario.
        user_id = update.effective_user.id
        if user_id not in historiales:
            historiales[user_id] = gemini_client.chats.create(
                model='gemini-3.6-flash',
                config=gemini_config,
            )

        await sync_to_async(connection.close_if_unusable_or_obsolete)()
        response = await asyncio.wait_for(
            asyncio.to_thread(
                historiales[user_id].send_message,
                texto_usuario,
                config=gemini_config,
            ),
            timeout=35,
        )
        
        if response.text:
            await update.message.reply_text(response.text, reply_markup=obtener_boton_volver())
        else:
            await update.message.reply_text("✅ Movimiento procesado en la base de datos.", reply_markup=obtener_boton_volver())
            
    except asyncio.TimeoutError:
        logger.error('Gemini tardó más de 45 segundos en responder.')
        await update.message.reply_text('⏳ Gemini está tardando demasiado. Probá de nuevo en unos segundos.')
    except Exception:
        logger.exception('Error en respuesta IA.')
        await update.message.reply_text(
            'No terminé de entender la consulta. ¿Podés decirme si querés '
            'consultar stock, movimientos, trámites o pastillero?',
            reply_markup=obtener_boton_volver(),
        )


async def manejar_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error('Error no controlado de Telegram: %s', context.error, exc_info=context.error)

# --- 8. PUNTO DE ENTRADA ---

def main():
    if not TELEGRAM_TOKEN:
        print("❌ ERROR: No se encontró TELEGRAM_TOKEN.")
        return

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(iniciar_recordatorios)
        .post_shutdown(detener_recordatorios)
        .build()
    )
    
    application.add_handler(CommandHandler(["start", "menu"], responder))
    application.add_handler(CallbackQueryHandler(manejar_botones))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), responder))
    application.add_error_handler(manejar_error)
    
    print("🚀 Astrana IA (Híbrido Menú Árbol + Herramientas) desplegando...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()