import os
import json
import logging
from typing import Dict, Any, Optional

from pywebpush import webpush, WebPushException
from django.conf import settings
from medicine_control.models import MemoriaAstrana

logger = logging.getLogger(__name__)

DEFAULT_VAPID_PUBLIC_KEY = os.getenv(
    'VAPID_PUBLIC_KEY',
    'BPp23vdTmlXMSqp0gFgrdUmQTPef9Kdfd_8ntg14c25vyzbYqH4tD10wXcLyvMyZQHCuMqCQJv5rSTyN4tvSLj8'
)
DEFAULT_VAPID_PRIVATE_KEY = os.getenv(
    'VAPID_PRIVATE_KEY',
    '-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg1ibY1A7S/auadZor\nQoxWzK/ttlqU1VbJT/jMdHCFFuuhRANCAAT6dt73U5pVzEqqdIBYK3VJkEz3n/Sn\nX3f/J7YNeHNub8s22Kh+LQ9dMF3C8rzMmUBwrjKgkCb+a0k8jeLb0i4/\n-----END PRIVATE KEY-----\n'
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
        raise ValueError("La suscripción no contiene endpoint válido.")

    # Guardamos con clave única por endpoint o chat_id
    clave = f"push_sub_{chat_id}"
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
    logger.info("Suscripción Push guardada para chat_id=%s", chat_id)
    return memoria

def enviar_webpush_recordatorio(titulo: str, cuerpo: str, datos: Optional[Dict[str, Any]] = None) -> int:
    """
    Envía una notificación Web Push a todos los navegadores/PWA suscritos.
    Retorna la cantidad de notificaciones enviadas con éxito.
    """
    suscripciones = MemoriaAstrana.objects.filter(
        categoria='contexto',
        clave__startswith='push_sub_',
        activa=True
    )

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
            # Si el endpoint caducó (404 o 410), desactivamos la suscripción
            if ex.response is not None and ex.response.status_code in {404, 410}:
                sub_memoria.activa = False
                sub_memoria.save(update_fields=['activa'])
                logger.info("Suscripción expirada desactivada: %s", sub_memoria.chat_id)
        except Exception as e:
            logger.exception("Error inesperado en Web Push para %s: %s", sub_memoria.chat_id, e)

    return enviados

