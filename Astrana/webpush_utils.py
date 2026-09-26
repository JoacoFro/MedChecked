import os
import json
import logging
import hashlib
from typing import Dict, Any, Optional

from django.conf import settings
from medicine_control.models import MemoriaAstrana

logger = logging.getLogger(__name__)

# Configuración de Claves VAPID
DEFAULT_VAPID_PUBLIC_KEY = os.getenv(
    'VAPID_PUBLIC_KEY',
    'BPp23vdTmlXMSqp0gFgrdUmQTPef9Kdfd_8ntg14c25vyzbYqH4tD10wXcLyvMyZQHCuMqCQJv5rSTyN4tvSLj8'
)

DEFAULT_VAPID_PRIVATE_KEY = os.getenv(
    'VAPID_PRIVATE_KEY',
    ''  # Se recomienda definir únicamente vía variable de entorno en producción
).replace('\\n', '\n')

VAPID_CLAIMS = {
    'sub': os.getenv('VAPID_ADMIN_EMAIL', 'mailto:admin@astrana.ai')
}


def obtener_vapid_public_key() -> str:
    return DEFAULT_VAPID_PUBLIC_KEY


def guardar_suscripcion_push(chat_id: str, sub_info: Dict[str, Any]) -> MemoriaAstrana:
    """Guarda o actualiza la suscripción Push de un dispositivo en MemoriaAstrana."""
    endpoint = sub_info.get('endpoint', '')
    if not endpoint:
        raise ValueError("La suscripción no contiene un endpoint válido.")

    # Guardamos con clave única por dispositivo/endpoint
    endpoint_hash = hashlib.sha256(endpoint.encode('utf-8')).hexdigest()[:12]
    clave = f"push_sub_{chat_id}_{endpoint_hash}"
    
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
    chat_id: Optional[str] = None
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

    enviados = 0
    for sub_memoria in suscripciones:
        try:
            sub_info = json.loads(sub_memoria.valor)
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=DEFAULT_VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS,
                timeout=10,
            )
            enviados += 1
            logger.info("Notificación Web Push enviada a %s", sub_memoria.chat_id)
        except WebPushException as ex:
            logger.error("Error enviando Web Push a %s: %s", sub_memoria.chat_id, ex)
            # Si el endpoint caducó o no existe (404 Not Found o 410 Gone), se desactiva
            if ex.response is not None and ex.response.status_code in {404, 410}:
                sub_memoria.activa = False
                sub_memoria.save(update_fields=['activa'])
                logger.info("Suscripción expirada desactivada para chat_id=%s", sub_memoria.chat_id)
        except Exception as e:
            logger.exception("Error inesperado en Web Push para %s: %s", sub_memoria.chat_id, e)

    return enviados