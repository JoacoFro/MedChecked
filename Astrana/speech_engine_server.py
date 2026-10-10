import asyncio
import hashlib
import json
import logging
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from asgiref.sync import sync_to_async
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory
from elevenlabs import AsyncElevenLabs

from medicine_control.models import MemoriaAstrana
from medicine_control.views import astrana_chat_api

logger = logging.getLogger(__name__)
ENGINE_MEMORY_CHAT_ID = 'principal'
ENGINE_MEMORY_KEY = 'elevenlabs_speech_engine_id'


def _speech_engine_ws_url():
    configured_url = os.getenv('SPEECH_ENGINE_WS_URL', '').strip()
    if configured_url:
        return configured_url

    hostname = os.getenv('RENDER_EXTERNAL_HOSTNAME', '').strip()
    if hostname:
        return f'wss://{hostname}/ws'
    raise RuntimeError(
        'Definí SPEECH_ENGINE_WS_URL para desarrollo local o ejecutá el servicio en Render.'
    )


def _get_engine_id_from_database():
    memory = MemoriaAstrana.objects.filter(
        chat_id=ENGINE_MEMORY_CHAT_ID,
        categoria='preferencia',
        clave=ENGINE_MEMORY_KEY,
        activa=True,
    ).first()
    return memory.valor if memory else None


def _save_engine_id(engine_id):
    MemoriaAstrana.objects.update_or_create(
        chat_id=ENGINE_MEMORY_CHAT_ID,
        categoria='preferencia',
        clave=ENGINE_MEMORY_KEY,
        defaults={
            'valor': engine_id,
            'activa': True,
            'confirmada': True,
        },
    )


def _run_astrana_chat_turn(text, conversation_id):
    session_key = hashlib.sha256(
        f'elevenlabs:{conversation_id}'.encode('utf-8')
    ).hexdigest()[:32]
    request = RequestFactory().post(
        '/api/astrana/chat/',
        {'message': text},
    )
    request.session = SessionStore(session_key=session_key)
    response = astrana_chat_api(request)
    request.session.save()

    if response.status_code >= 400:
        raise RuntimeError(f'Astrana no pudo procesar el turno (HTTP {response.status_code}).')
    payload = json.loads(response.content.decode('utf-8'))
    return payload.get('reply') or 'No pude preparar una respuesta. ¿Podés repetirlo?'


async def _ensure_speech_engine(client):
    engine_id = await sync_to_async(_get_engine_id_from_database, thread_sensitive=True)()
    if engine_id:
        return engine_id

    voice_id = os.getenv('ELEVENLABS_VOICE_ID', 'mPteaOsPT4FrQ0lJIVEm').strip()
    created = await client.speech_engine.create(
        name='Astrana PWA Voice',
        speech_engine={'ws_url': _speech_engine_ws_url()},
        asr={'provider': 'scribe_realtime'},
        tts={
            'model_id': 'eleven_flash_v2_5',
            'voice_id': voice_id,
            'stability': 0.5,
            'similarity_boost': 0.8,
            'speed': 1.0,
        },
        language='es',
        conversation={
            'max_duration_seconds': 600,
            'client_events': [
                'audio', 'interruption', 'agent_response', 'user_transcript',
                'agent_chat_response_part', 'ping',
            ],
        },
        privacy={
            'record_voice': False,
            'delete_audio': True,
        },
        tags=['astrana', 'pwa'],
    )
    await sync_to_async(_save_engine_id, thread_sensitive=True)(created.engine_id)
    logger.info('Speech Engine de Astrana creado y guardado en la base compartida.')
    return created.engine_id


async def _on_transcript(transcript, session):
    ultimo_usuario = next(
        (mensaje for mensaje in reversed(transcript) if mensaje.role == 'user'),
        None,
    )
    if not ultimo_usuario or not ultimo_usuario.content.strip():
        return

    try:
        respuesta = await sync_to_async(
            _run_astrana_chat_turn,
            thread_sensitive=True,
        )(ultimo_usuario.content.strip(), session.conversation_id or 'voice-session')
        await session.send_response(respuesta)
    except Exception:
        logger.exception('Falló el turno de voz de Astrana.')
        await session.send_response('Tuve un problema al procesar eso. Probemos de nuevo.')


async def main():
    api_key = os.getenv('ELEVENLABS_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('Falta ELEVENLABS_API_KEY en el servicio de voz de Render.')

    client = AsyncElevenLabs(api_key=api_key)
    engine_id = await _ensure_speech_engine(client)
    engine = await client.speech_engine.get(engine_id)
    port = int(os.getenv('PORT', '10000'))
    logger.info('Speech Engine de Astrana listo en el puerto %s.', port)
    await engine.serve(
        port=port,
        path='/ws',
        debug=os.getenv('SPEECH_ENGINE_DEBUG', 'False').lower() == 'true',
        on_transcript=_on_transcript,
        on_error=lambda error, session: logger.error('Error del Speech Engine: %s', error),
    )


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())