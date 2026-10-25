import logging
import re
from collections import Counter
from typing import Any, Mapping, Sequence

from .personality import ASTRANA_PERSONALIDAD, INTENCIONES_PERSONALIZABLES

logger = logging.getLogger(__name__)

_DATOS_NUMERICOS = re.compile(r'(?<!\w)\d+(?:[.,]\d+)?(?:/\d+(?:/\d+)?)?(?!\w)')
_TEXTO_INTERNO_GEMINI = re.compile(
    r'\b(?:drafting the response|data to present|chain of thought|'
    r'let me think|analysis|standard argentine assistant tone)\b',
    re.IGNORECASE,
)
_FINAL_INCOMPLETO = re.compile(
    r'(?:\b(?:que|de|del|con|para|por|y|o|el|la|los|las|un|una|es|son|'
    r'ten[eé]s|tiene|tienen|registraste|fue|recibido)\s*|\*{1,2}|:)$',
    re.IGNORECASE,
)
_INSTRUCCIONES_ESTILO = {
    'longitud': {
        'breve': 'Priorizá respuestas breves y directas.',
        'detallada': 'Incluí más explicación cuando el resultado lo permita.',
    },
    'emojis': {
        'no': 'No uses emojis.',
        'si': 'Podés usar emojis con moderación.',
    },
    'tono': {
        'cercano': 'Usá un tono cercano e informal.',
        'formal': 'Usá un tono más formal.',
    },
}


def _datos_numericos(texto: str) -> Counter[str]:
    return Counter(
        valor.replace(',', '.')
        for valor in _DATOS_NUMERICOS.findall(texto)
    )


def _agrega_datos_numericos(respuesta: str, resultado: str) -> bool:
    return bool(
        _datos_numericos(respuesta) - _datos_numericos(resultado)
    )


def _omite_datos_numericos(respuesta: str, resultado: str) -> bool:
    return bool(
        _datos_numericos(resultado) - _datos_numericos(respuesta)
    )


def _respuesta_parece_incompleta(respuesta: str) -> bool:
    texto = respuesta.rstrip()
    if _FINAL_INCOMPLETO.search(texto):
        return True
    return not re.search(r'[.!?…][”"\')\]]*$', texto)


def personalizar_respuesta(
    consulta: str,
    resultado: str,
    intencion: str,
    contexto: Sequence[Mapping[str, str]] = (),
    preferencias_estilo: Mapping[str, str] | None = None,
    client: Any = None,
) -> str:
    respuesta = str(resultado).strip()
    if not respuesta:
        return respuesta
    if intencion not in INTENCIONES_PERSONALIZABLES:
        logger.info(
            'Se omitió la personalización de Astrana para una intención no permitida.'
        )
        return respuesta
    if client is None:
        logger.warning(
            'Se omitió la personalización de Astrana porque Gemini no está disponible.'
        )
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
    instrucciones_estilo = [
        _INSTRUCCIONES_ESTILO[clave][valor]
        for clave, valor in (preferencias_estilo or {}).items()
        if clave in _INSTRUCCIONES_ESTILO
        and valor in _INSTRUCCIONES_ESTILO[clave]
    ]
    prompt = (
        f'Intención ya reconocida: {intencion}\n'
        'Preferencias de estilo del usuario (solo guían la redacción):\n'
        f'{instrucciones_estilo or ["Sin preferencias guardadas."]}\n\n'
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
                max_output_tokens=350,
                http_options=types.HttpOptions(timeout=10000),
            ),
        )
        respuesta_natural = (response.text or '').strip()
        if not respuesta_natural:
            logger.warning('Gemini devolvió una respuesta vacía al personalizar Astrana.')
            return respuesta
        candidates = getattr(response, 'candidates', None)
        candidate = (
            candidates[0]
            if isinstance(candidates, (list, tuple)) and candidates
            else None
        )
        finish_reason = getattr(candidate, 'finish_reason', None)
        if str(finish_reason).upper().endswith('MAX_TOKENS'):
            logger.warning(
                'Se descartó una respuesta personalizada de Astrana truncada por límite de tokens.'
            )
            return respuesta
        if _TEXTO_INTERNO_GEMINI.search(respuesta_natural):
            logger.warning(
                'Se descartó una respuesta de Gemini que contenía texto interno de generación.'
            )
            return respuesta
        if _respuesta_parece_incompleta(respuesta_natural):
            logger.warning(
                'Se descartó una respuesta personalizada de Astrana que parecía incompleta.'
            )
            return respuesta
        if _agrega_datos_numericos(respuesta_natural, respuesta):
            logger.warning(
                'Se descartó una respuesta personalizada de Astrana porque agregó '
                'cantidades o fechas que no estaban en el resultado técnico.'
            )
            return respuesta
        if _omite_datos_numericos(respuesta_natural, respuesta):
            logger.warning(
                'Se descartó una respuesta personalizada de Astrana porque omitió '
                'cantidades o fechas del resultado técnico.'
            )
            return respuesta
        if respuesta_natural == respuesta:
            logger.info(
                'Gemini devolvió el resultado técnico de Astrana sin cambios.'
            )
        return respuesta_natural
    except Exception:
        logger.exception('No se pudo personalizar la respuesta de Astrana con Gemini.')
        return respuesta