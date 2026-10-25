import logging
import re
from collections import Counter
from typing import Any, Mapping, Sequence

from .personality import ASTRANA_PERSONALIDAD, INTENCIONES_PERSONALIZABLES

logger = logging.getLogger(__name__)

_DATOS_NUMERICOS = re.compile(r'(?<!\w)\d+(?:[.,]\d+)?(?:/\d+(?:/\d+)?)?(?!\w)')


def _datos_numericos(texto: str) -> Counter[str]:
    return Counter(
        valor.replace(',', '.')
        for valor in _DATOS_NUMERICOS.findall(texto)
    )


def personalizar_respuesta(
    consulta: str,
    resultado: str,
    intencion: str,
    contexto: Sequence[Mapping[str, str]] = (),
    client: Any = None,
) -> str:
    respuesta = str(resultado).strip()
    if (
        not respuesta
        or intencion not in INTENCIONES_PERSONALIZABLES
        or client is None
    ):
        return respuesta

    mensaje_normalizado = re.sub(r'^[^\w]+', '', respuesta.casefold())
    if mensaje_normalizado.startswith((
        'error',
        'no hay',
        'no pude',
        'no encontré',
        'todavía no hay',
    )):
        return respuesta

    consulta_normalizada = re.sub(r'[^\w\s]', ' ', consulta.lower())
    if set(consulta_normalizada.split()) & {'solo', 'solamente'}:
        return respuesta

    contexto_reciente = [
        {'role': mensaje.get('role', ''), 'content': mensaje.get('content', '')}
        for mensaje in contexto[-6:]
        if mensaje.get('role') in {'user', 'assistant'}
        and mensaje.get('content')
    ]
    prompt = (
        f'Intención ya reconocida: {intencion}\n'
        f'Contexto reciente de esta conversación (puede estar vacío):\n'
        f'{contexto_reciente}\n\n'
        f'Consulta actual:\n{consulta}\n\n'
        f'Resultado técnico de la función (fuente factual):\n{respuesta}'
    )

    try:
        from google.genai import types

        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=ASTRANA_PERSONALIDAD,
                temperature=0.3,
                max_output_tokens=220,
                http_options=types.HttpOptions(timeout=10000),
            ),
        )
        respuesta_natural = (response.text or '').strip()
        if not respuesta_natural:
            logger.warning('Gemini devolvió una respuesta vacía al personalizar Astrana.')
            return respuesta
        if _datos_numericos(respuesta_natural) != _datos_numericos(respuesta):
            logger.warning(
                'Se descartó una respuesta personalizada de Astrana porque alteró '
                'las cantidades o fechas del resultado técnico.'
            )
            return respuesta
        return respuesta_natural
    except Exception:
        logger.exception('No se pudo personalizar la respuesta de Astrana con Gemini.')
        return respuesta