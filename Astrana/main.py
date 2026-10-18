import os
import sys
import logging
import json
import re
import unicodedata
from datetime import timedelta
from pathlib import Path
from dotenv import load_dotenv
import django
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

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
from medicine_control.pastillero_services import (
    medicamentos_pendientes_ids,
    registrar_toma_recordatorio,
)
from Astrana.nlp_engine import nlp_engine
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

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
    """Busca alias confirmados del chat antes de usar el clasificador compartido."""
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
    return None


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
            chat_id='global', confirmado=True
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

        reporte = "💊 Stock del Pastillero:\n"
        for medicamento in medicamentos:
            reporte += f"• {medicamento.nombre}: {medicamento.cantidad_total} pastillas.\n"
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

        reporte += "\n📤 Últimos 10 egresos de Sondas:\n"
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

        reporte = "🗓 Últimas tomas:\n"
        for toma in tomas:
            fecha = timezone.localtime(toma.fecha_hora).strftime('%d/%m/%Y %H:%M')
            reporte += f"• **{toma.medicamento.nombre}**: {toma.cantidad} un. ({fecha})\n"
        return reporte
    except Exception as e:
        return f"Error al consultar las últimas tomas: {e}"


def rango_fecha_consulta(texto):
    hoy = timezone.localdate()
    texto = normalizar_texto(texto)
    if 'ayer' in texto:
        return hoy - timedelta(days=1), hoy - timedelta(days=1)
    if 'esta semana' in texto or 'semana' in texto:
        inicio = hoy - timedelta(days=hoy.weekday())
        return inicio, hoy
    if 'hoy' in texto or 'dia' in texto or 'día' in texto:
        return hoy, hoy
    return None, None


def medicamento_mencionado(texto):
    texto_normalizado = normalizar_texto(texto)
    medicamentos = sorted(
        Pastillero.objects.all(),
        key=lambda medicamento: len(normalizar_texto(medicamento.nombre)),
        reverse=True,
    )
    for medicamento in medicamentos:
        nombre = normalizar_texto(medicamento.nombre)
        if nombre and re.search(rf'(?<!\w){re.escape(nombre)}(?!\w)', texto_normalizado):
            return medicamento
    return None


def consulta_medicamento_generica(texto):
    texto = normalizar_texto(texto)
    return bool(re.search(r'\b(algun|alguno|alguna|cualquier|medicamentos?)\b', texto))


def respuesta_si(texto):
    return normalizar_texto(texto).strip(' .!?') in {
        'si', 's', 'dale', 'claro', 'por favor', 'agregalo', 'agregalo por favor',
    }


def respuesta_no(texto):
    return normalizar_texto(texto).strip(' .!?') in {
        'no', 'n', 'por ahora no', 'dejalo', 'dejalo asi',
    }


def crear_medicamento_pastillero(nombre, cantidad):
    return Pastillero.objects.create(
        nombre=nombre.strip(),
        cantidad=0,
        cantidad_total=cantidad,
        fecha_hora=timezone.now(),
        estado_diario='pendiente',
    )


def consultar_tomas_medicamentos(texto_usuario, solo_ultimas=False):
    """Consulta tomas reales, opcionalmente filtradas por medicamento y fecha."""
    try:
        connection.close_if_unusable_or_obsolete()
        fecha_inicio, fecha_fin = rango_fecha_consulta(texto_usuario)
        medicamento = medicamento_mencionado(texto_usuario)
        if solo_ultimas and medicamento is None:
            return '¿De cuál medicamento querés ver las últimas tomas? Decime su nombre y consulto solo ese historial.'

        tomas = TomaPastillero.objects.select_related('medicamento').all()
        if medicamento:
            tomas = tomas.filter(medicamento_id=medicamento.id)
        if fecha_inicio:
            tomas = tomas.filter(fecha_hora__date__gte=fecha_inicio, fecha_hora__date__lte=fecha_fin)
        elif not solo_ultimas and any(
            palabra in normalizar_texto(texto_usuario).split()
            for palabra in ('tome', 'tome', 'tomaste', 'tomado')
        ):
            hoy = timezone.localdate()
            tomas = tomas.filter(fecha_hora__date=hoy)
            fecha_inicio = fecha_fin = hoy
        tomas = tomas.order_by('-fecha_hora')[:10]

        nombre = medicamento.nombre if medicamento else 'los medicamentos registrados'
        periodo = 'hoy' if fecha_inicio == timezone.localdate() else ''
        if fecha_inicio and fecha_inicio != fecha_fin:
            periodo = f'del {fecha_inicio:%d/%m/%Y} al {fecha_fin:%d/%m/%Y}'
        elif fecha_inicio:
            periodo = f'el {fecha_inicio:%d/%m/%Y}'

        if solo_ultimas:
            encabezado = f'🗓 **Últimas tomas de {nombre}:**'
        else:
            encabezado = f'💊 **Tomas de {nombre} {periodo}:**'.replace('  ', ' ')
        if not tomas:
            return f'No hay tomas registradas de {nombre} {periodo}.'.replace('  ', ' ')

        reporte = encabezado + '\n'
        for toma in tomas:
            fecha = timezone.localtime(toma.fecha_hora).strftime('%d/%m/%Y %H:%M')
            reporte += f'• **{toma.medicamento.nombre}**: {toma.cantidad} un. ({fecha})\n'
        return reporte
    except Exception as error:
        return f'Error al consultar las tomas del pastillero: {error}'


def consultar_si_tome_medicamento(texto_usuario):
    """Responde si hubo al menos una toma en el período pedido."""
    medicamento = medicamento_mencionado(texto_usuario)
    if medicamento is None:
        return consultar_tomas_medicamentos(texto_usuario)
    fecha_inicio, fecha_fin = rango_fecha_consulta(texto_usuario)
    if fecha_inicio is None:
        fecha_inicio = fecha_fin = timezone.localdate()
    tomas = TomaPastillero.objects.filter(medicamento=medicamento)
    if fecha_inicio:
        tomas = tomas.filter(fecha_hora__date__gte=fecha_inicio, fecha_hora__date__lte=fecha_fin)
    existe = tomas.exists()
    periodo = 'hoy' if fecha_inicio == timezone.localdate() else 'en el período consultado'
    if existe:
        return f'✅ Sí, registraste una toma de {medicamento.nombre} {periodo}.'
    return f'❌ No encontré una toma de {medicamento.nombre} {periodo}.'

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

def iniciar_tramite_pedido(
    tipo_tramite: str,
    cantidad: int = None,
    cantidad_en_unidades: bool = False,
):
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
            
        with transaction.atomic():
            tramite_existente = Envio.objects.filter(
                tipo=tipo_final,
                estado='tramite',
            ).exists()
            if tramite_existente:
                return f"⚠️ ATENCIÓN: Ya existe un trámite de {nombre_legible} en curso."

            Envio.objects.create(
                tipo=tipo_final,
                estado='tramite',
                cantidad_pedida=cantidad,
                notas='PWA: cantidad expresada en unidades' if cantidad_en_unidades else '',
            )
        unidad = 'unidades' if cantidad_en_unidades else 'cajas/unidades'
        return f"📋 ¡Trámite de {nombre_legible} iniciado! Registrado en Envíos con una solicitud de {cantidad} {unidad}."

    except IntegrityError:
        return f"⚠️ ATENCIÓN: Ya existe un trámite de {nombre_legible} en curso."
    except Exception as e:
        return f"❌ Error técnico al iniciar trámite: {str(e)}"

def cerrar_tramite_pedido(tipo_tramite: str, tipo_stock: str = None):
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
            
        with transaction.atomic():
            tramite = Envio.objects.select_for_update().filter(
                tipo=tipo_final,
                estado='tramite',
            ).order_by('-fecha_solicitud', '-id').first()
            if not tramite:
                return f"⚠️ No encontré ningún trámite activo de {nombre_legible} en curso para cerrar."

            cantidad = tramite.cantidad_pedida
            if not cantidad or cantidad <= 0:
                return "⚠️ El trámite no tiene una cantidad válida; sigue abierto y no se modificó el stock."

            insumo = Insumo.objects.select_for_update().filter(
                nombre__icontains=insumo_defecto,
            ).first()
            if not insumo:
                return (
                    f"⚠️ No encontré el insumo '{insumo_defecto}'. "
                    "El trámite sigue abierto y el stock no se modificó."
                )

            es_backup = tipo_final == 'backup'
            cantidad_en_unidades = tramite.notas == 'PWA: cantidad expresada en unidades'
            if es_backup:
                destino_stock = 'seguridad'
            elif tipo_stock is None and cantidad_en_unidades:
                destino_stock = 'stock_normal'
            else:
                destino_stock = (
                    'stock_normal'
                    if tipo_stock in [None, 'stock_normal', 'cajas', 'principal', 'normal']
                    else 'seguridad'
                )

            if destino_stock == 'stock_normal':
                if cantidad_en_unidades:
                    if cantidad % insumo.unidades_por_caja:
                        return (
                            f"⚠️ La cantidad de Obra Social debe ser múltiplo de "
                            f"{insumo.unidades_por_caja} unidades para registrarla en cajas. "
                            "El trámite sigue abierto y el stock no se modificó."
                        )
                    cajas = cantidad // insumo.unidades_por_caja
                else:
                    cajas = cantidad
                insumo.stock_actual_cajas += cajas
                detalle_stock = (
                    f"Se sumaron {cantidad} sondas ({cajas} cajas) al Stock Normal."
                    if cantidad_en_unidades
                    else f"Se sumaron {cajas} cajas al Stock Normal."
                )
            else:
                insumo.backup_unidades += cantidad
                detalle_stock = f"Se sumaron {cantidad} unidades al Stock de Seguridad."

            ahora = timezone.now()
            tramite.estado = 'recibido'
            tramite.fecha_cierre = ahora.date()
            tramite.save(update_fields=['estado', 'fecha_cierre'])
            Pedido.objects.create(
                insumo=insumo,
                tipo=tipo_final,
                tipo_stock=destino_stock,
                cantidad=cantidad,
                fecha=ahora.date(),
                lugar_compra=f"Cierre Trámite {nombre_legible}",
            )
            insumo.save()
            insumo.refresh_from_db()
            dias_demora = (tramite.fecha_cierre - tramite.fecha_solicitud).days
            return (
                f"📋 ¡Trámite de {nombre_legible} cerrado con éxito y registrado como recibido "
                f"en Envíos! Demora: {dias_demora} días.\n📦 {detalle_stock}\n"
                f"Nuevo total real disponible: {insumo.total_unidades_reales} sondas."
            )

    except Exception as e:
        return f"❌ Error técnico al procesar el cierre: {str(e)}"

def obtener_resumen_pedidos():
    """Consulta trámites en curso y los 10 cerrados más recientes con toda la info."""
    try:
        connection.close_if_unusable_or_obsolete()
        en_curso = Envio.objects.filter(
            estado='tramite'
        ).order_by('-fecha_solicitud', '-id')
        cerrados = Envio.objects.filter(
            estado='recibido',
            fecha_cierre__isnull=False,
        ).order_by('-fecha_cierre', '-id')[:10]

        # --- PEDIDOS ABIERTOS ---
        if en_curso:
            reporte = f"📋 Pedidos abiertos ({en_curso.count()}):\n"
            for tramite in en_curso:
                tipo_legible = '🔄 Backup / Propio' if tramite.tipo == 'backup' else '🏥 Obra Social'
                cantidad_en_unidades = tramite.notas == 'PWA: cantidad expresada en unidades'
                unidad = 'unidades' if cantidad_en_unidades else 'cajas'
                dias_en_tramite = tramite.demora_real
                reporte += (
                    f"\n▸ Tipo: {tipo_legible}\n"
                    f"  Cantidad pedida: {tramite.cantidad_pedida} {unidad}\n"
                    f"  Fecha inicio: {tramite.fecha_solicitud:%d/%m/%Y}\n"
                    f"  Días en trámite: {dias_en_tramite} días\n"
                    f"  Notas: {tramite.notas or '—'}\n"
                )
        else:
            reporte = "📋 Pedidos abiertos:** Ninguno en curso.\n"

        # --- ÚLTIMOS CERRADOS ---
        reporte += "\n✅ Últimos trámites cerrados:\n"
        if cerrados:
            for tramite in cerrados:
                tipo_legible = '🔄 Backup / Propio' if tramite.tipo == 'backup' else '🏥 Obra Social'
                reporte += (
                    f"\n▸ Tipo: {tipo_legible}\n"
                    f"  Cantidad: {tramite.cantidad_pedida}\n"
                    f"  Inicio → Cierre: {tramite.fecha_solicitud:%d/%m/%Y} → {tramite.fecha_cierre:%d/%m/%Y}\n"
                    f"  Demora: {tramite.demora_real} días\n"
                )
        else:
            reporte += "Sin trámites cerrados registrados.\n"

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
                f"• {tramite.get_tipo_display()} | "
                f"Estado: {tramite.get_estado_display()} | "
                f"Cantidad: {tramite.cantidad_pedida} | "
                f"Inicio: {tramite.fecha_solicitud:%d/%m/%Y} | "
                f"Cierre: {cierre} | "
                f"Demora: {tramite.demora_real} días\n"
            )
        return reporte
    except Exception as e:
        return f"Error al consultar el historial de trámites: {e}"


def consultar_ultimo_pedido_solicitado():
    """Indica cuándo se inició el pedido más reciente registrado en Envio."""
    try:
        connection.close_if_unusable_or_obsolete()
        tramite = Envio.objects.order_by('-fecha_solicitud', '-id').first()
        if tramite is None:
            return 'No hay pedidos registrados en el historial de Envíos.'

        return (
            f"El último pedido registrado fue de {tramite.get_tipo_display()}, "
            f"iniciado el {tramite.fecha_solicitud:%d/%m/%Y}. "
            f"Estado: {tramite.get_estado_display()}."
        )
    except Exception as error:
        return f'Error al consultar el último pedido: {error}'


# --- 4. CONFIGURACIÓN DE GEMINI Y BOT ---
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')


def omitir_medicamento_hoy(medicamento_id):
    hoy = timezone.localdate()
    with transaction.atomic():
        medicamento = Pastillero.objects.select_for_update().get(id=medicamento_id)
        if medicamento.estado_diario_fecha != hoy or medicamento.estado_diario not in ['tomado', 'omitido']:
            medicamento.estado_diario = 'omitido'
            medicamento.estado_diario_fecha = hoy
            medicamento.save(update_fields=['estado_diario', 'estado_diario_fecha'])
        return medicamento


gemini_client = None
gemini_config = None
gemini_classifier_config = None
if GEMINI_API_KEY and genai is not None:
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

    medicamento_en_pastillero = medicamento_mencionado(texto_usuario)
    habla_de_pastillero = bool(palabras & {
        'pastillero', 'pastillas', 'medicamento', 'medicamentos',
        'medicacion', 'medicación', 'tomas', 'toma', 'tableta', 'comprimido',
        'tome', 'tomé', 'tomaste', 'tomado',
    }) or medicamento_en_pastillero is not None
    consulta_tomas = bool(palabras & {
        'toma', 'tomas', 'tomaste', 'tome', 'tomé', 'tomado',
        'ultimas', 'últimas', 'historial', 'registrada', 'registre',
    })
    if habla_de_pastillero and consulta_tomas:
        if palabras & {'ultima', 'ultimas', 'última', 'últimas', 'historial'}:
            return 'ultimas_tomas_pastillero'
        return 'verificar_toma_medicamento'

    habla_de_tramites = bool(palabras & {
        'pedido', 'pedidos', 'tramite', 'tramites', 'envio', 'envios',
    })
    consulta_ultimo_pedido = habla_de_tramites and (
        bool(palabras & {'ultimo', 'ultimos', 'reciente', 'recientes'})
        or (
            bool(palabras & {'cuando', 'fecha'})
            and bool(palabras & {'hice', 'pedi', 'solicite', 'realice', 'inicie'})
        )
    )
    if consulta_ultimo_pedido:
        return 'ultimo_pedido'

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
