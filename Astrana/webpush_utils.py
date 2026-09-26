import os
import json
import logging
import hashlib
import base64
from typing import Dict, Any, Optional

from django.conf import settings
from medicine_control.models import MemoriaAstrana
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

logger = logging.getLogger(__name__)

# Configuración de Claves VAPID
VAPID_CLAIMS = {
    'sub': os.getenv('VAPID_ADMIN_EMAIL', 'mailto:admin@astrana.ai')
}


def obtener_configuracion_vapid():
    """Carga y valida que la pública y privada VAPID sean el mismo par."""
    clave_publica = os.getenv('VAPID_PUBLIC_KEY', '').strip()
    clave_privada = os.getenv('VAPID_PRIVATE_KEY', '').replace('\\n', '\n').strip()
    faltantes = []
    if not clave_publica:
        faltantes.append('VAPID_PUBLIC_KEY')
    if not clave_privada:
        faltantes.append('VAPID_PRIVATE_KEY')
    if faltantes:
        raise ValueError(f'Faltan variables de entorno: {", ".join(faltantes)}.')

    try:
        llave_privada = serialization.load_pem_private_key(
            clave_privada.encode('utf-8'), password=None
        )
        publica_derivada = llave_privada.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )
        publica_configurada = base64.urlsafe_b64decode(
            clave_publica + '=' * (-len(clave_publica) % 4)
        )
    except Exception as error:
        raise ValueError('El formato de las variables VAPID no es válido.') from error

    if publica_derivada != publica_configurada:
        raise ValueError('VAPID_PUBLIC_KEY y VAPID_PRIVATE_KEY no pertenecen al mismo par.')

    return clave_publica, clave_privada


def obtener_vapid_public_key() -> str:
    clave_publica, _ = obtener_configuracion_vapid()
    return clave_publica


def guardar_suscripcion_push(chat_id: str, sub_info: Dict[str, Any]) -> MemoriaAstrana:
    """Guarda o actualiza la suscripción Push de un dispositivo en MemoriaAstrana."""
    endpoint = sub_info.get('endpoint', '')
    if not endpoint:
        raise ValueError("La suscripción no contiene un endpoint válido.")

    # Guardamos con clave única por dispositivo/endpoint
    endpoint_hash = hashlib.sha256(endpoint.encode('utf-8')).hexdigest()[:12]
    clave = f"push_sub_{chat_id}_{endpoint_hash}"

    MemoriaAstrana.objects.filter(
        chat_id=str(chat_id),
        categoria='contexto',
        clave__startswith=f'push_sub_{chat_id}_',
        activa=True,
    ).exclude(clave=clave).update(activa=False)
    
    memoria, _ = MemoriaAstrana.objects.update_or_create(
        chat_id=str(chat_id),
        categoria='contexto',
        clave=clave,
        defaults={
            'valor': json.dumps(sub_info),
            'confirmada': True,
            'activa': True,
        }
    )
    logger.info("Suscripción Push guardada para chat_id=%s (hash=%s)", chat_id, endpoint_hash)
    return memoria


def enviar_webpush_recordatorio(
    titulo: str, 
    cuerpo: str, 
    datos: Optional[Dict[str, Any]] = None,
    chat_id: Optional[str] = None,
    errores: Optional[list] = None,
) -> int:
    """
    Envía una notificación Web Push a los navegadores/PWA suscritos.
    Si se especifica `chat_id`, solo enviará a las suscripciones de ese usuario.
    Retorna la cantidad de notificaciones enviadas con éxito.
    """
    filtros = {
        'categoria': 'contexto',
        'clave__startswith': 'push_sub_',
        'activa': True
    }
    if chat_id:
        filtros['chat_id'] = str(chat_id)

    suscripciones = MemoriaAstrana.objects.filter(**filtros)

    if not suscripciones.exists():
        logger.warning("No hay suscripciones Web Push activas para enviar recordatorios.")
        if errores is not None:
            errores.append('No hay suscripciones activas para el dispositivo.')
        return 0

    payload = json.dumps({
        'title': titulo,
        'body': cuerpo,
        'icon': '/astrana/icon-192.png',
        'badge': '/astrana/icon-192.png',
        'data': datos or {},
        'actions': [
            {'action': 'confirmar_toma', 'title': '✅ Confirmar Toma'},
            {'action': 'abrir_astrana', 'title': '✨ Abrir Astrana'}
        ]
    })

    from pywebpush import WebPushException, webpush
    _, clave_privada = obtener_configuracion_vapid()

    enviados = 0
    for sub_memoria in suscripciones:
        try:
            sub_info = json.loads(sub_memoria.valor)
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=clave_privada,
                vapid_claims=VAPID_CLAIMS,
                timeout=10,
            )
            enviados += 1
            logger.info("Notificación Web Push enviada a %s", sub_memoria.chat_id)
        except WebPushException as ex:
            status_code = ex.response.status_code if ex.response is not None else None
            motivo = clasificar_error_webpush(status_code)
            logger.error(
                "Web Push falló para chat_id=%s, provider_status=%s, motivo=%s",
                sub_memoria.chat_id,
                status_code or 'sin respuesta HTTP',
                motivo,
            )
            if errores is not None:
                errores.append(motivo)
            # Si el endpoint caducó o no existe (404 Not Found o 410 Gone), se desactiva
            if status_code in {404, 410}:
                sub_memoria.activa = False
                sub_memoria.save(update_fields=['activa'])
                logger.info("Suscripción expirada desactivada para chat_id=%s", sub_memoria.chat_id)
        except Exception as e:
            logger.exception("Error inesperado en Web Push para chat_id=%s", sub_memoria.chat_id)
            if errores is not None:
                errores.append('Error interno preparando o enviando el mensaje push.')

    return enviados


def clasificar_error_webpush(status_code):
    if status_code in {401, 403}:
        return 'El proveedor rechazó la autenticación VAPID (HTTP 401/403). Verificá que Render tenga el mismo par de claves y reiniciá el servicio.'
    if status_code in {404, 410}:
        return 'El proveedor marcó la suscripción como vencida (HTTP 404/410). Volvé a activar las notificaciones desde ese dispositivo.'
    if status_code == 413:
        return 'El proveedor rechazó el tamaño del mensaje push (HTTP 413).'
    if status_code == 429:
        return 'El proveedor limitó temporalmente el envío (HTTP 429). Intentá nuevamente más tarde.'
    if status_code is not None and status_code >= 500:
        return f'El proveedor de notificaciones no está disponible (HTTP {status_code}). Intentá nuevamente más tarde.'
    if status_code is not None:
        return f'El proveedor rechazó la notificación (HTTP {status_code}).'
    return 'No se pudo establecer conexión con el proveedor push.'