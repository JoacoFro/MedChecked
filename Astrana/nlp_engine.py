import re
import unicodedata
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any, List, Tuple

from rapidfuzz import process, fuzz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

import django
from django.utils import timezone

logger = logging.getLogger(__name__)

# --- 1. NORMALIZACIÓN Y UTILIDADES LINGÜÍSTICAS ---

def normalizar_texto(texto: str) -> str:
    """Elimina acentos, pasa a minúsculas y remueve puntuación superflua."""
    if not texto:
        return ""
    texto = unicodedata.normalize('NFD', texto.lower())
    texto_sin_acentos = ''.join(c for c in texto if unicodedata.category(c) != 'Mn')
    texto_limpio = re.sub(r'[^\w\s]', ' ', texto_sin_acentos)
    return re.sub(r'\s+', ' ', texto_limpio).strip()

NUMEROS_PALABRAS = {
    'un': 1, 'uno': 1, 'una': 1, 'medio': 1, 'media': 1,
    'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5,
    'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10,
    'once': 11, 'doce': 12, 'trece': 13, 'catorce': 14, 'quince': 15,
    'dieciseis': 16, 'diecisiete': 17, 'dieciocho': 18, 'diecinueve': 19,
    'veinte': 20, 'veinticinco': 25, 'treinta': 30, 'cuarenta': 40,
    'cincuenta': 50, 'sesenta': 60, 'setenta': 70, 'ochenta': 80,
    'noventa': 90, 'cien': 100, 'ciento': 100, 'doscientos': 200,
    'trescientos': 300, 'quinientos': 500,
}

def extraer_cantidad(texto: str) -> Optional[int]:
    """Extrae la cantidad numérica en dígitos o palabras en español."""
    norm = normalizar_texto(texto)
    # 1. Búsqueda de dígitos
    match_digito = re.search(r'\b(\d+)\b', norm)
    if match_digito:
        return int(match_digito.group(1))

    # 2. Búsqueda de palabras numéricas
    palabras = norm.split()
    for palabra in palabras:
        if palabra in NUMEROS_PALABRAS:
            return NUMEROS_PALABRAS[palabra]

    return None

def extraer_tipo_stock(texto: str) -> str:
    """
    Determina si se refiere a 'stock_normal' (cajas) o 'seguridad' (backup/unidades).
    Retorna 'stock_normal' o 'seguridad'. Por defecto 'stock_normal'.
    """
    norm = normalizar_texto(texto)
    palabras = set(norm.split())

    es_seguridad = bool(palabras & {
        'backup', 'seguridad', 'reserva', 'propio', 'sueltas', 'suelta', 'unidades', 'unidad'
    })
    es_normal = bool(palabras & {
        'normal', 'caja', 'cajas', 'bna', 'principal', 'general'
    })

    if es_seguridad and not es_normal:
        return 'seguridad'
    return 'stock_normal'

def extraer_tipo_tramite(texto: str) -> str:
    """Detecta si el trámite es de 'os' (Obra Social) o 'backup'."""
    norm = normalizar_texto(texto)
    palabras = set(norm.split())
    if palabras & {'backup', 'propio', 'reserva', 'seguridad'}:
        return 'backup'
    return 'os'

def extraer_rango_fechas(texto: str) -> Tuple[Optional[date], Optional[date]]:
    """Extrae período temporal: hoy, ayer, esta semana."""
    hoy = timezone.localdate()
    norm = normalizar_texto(texto)

    if 'ayer' in norm:
        ayer = hoy - timedelta(days=1)
        return ayer, ayer
    if 'esta semana' in norm or 'la semana' in norm:
        inicio_semana = hoy - timedelta(days=hoy.weekday())
        return inicio_semana, hoy
    if 'hoy' in norm or 'ahora' in norm or 'recien' in norm:
        return hoy, hoy
    return None, None

def extraer_hora_personalizada(texto: str) -> Optional[Tuple[int, int]]:
    """Extrae una hora y minuto (HH, MM) de expresiones como 'a las 14', 'a las 15:30', 'a las 9 hs', 'a las 7 de la tarde'."""
    # 1. Buscar primero en texto original con dos puntos o punto (ej: 14:30 o 8.15)
    match_hhmm_orig = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', texto)
    if match_hhmm_orig:
        return int(match_hhmm_orig.group(1)), int(match_hhmm_orig.group(2))

    norm = normalizar_texto(texto)

    # 2. Formato con espacio tras normalizar: "a las 14 30"
    match_espacio = re.search(r'\b(?:a\s+las?|para\s+las?)\s+([01]?\d|2[0-3])\s+([0-5]\d)\b', norm)
    if match_espacio:
        return int(match_espacio.group(1)), int(match_espacio.group(2))

    # 3. Expresiones "y media" / "y cuarto"
    match_media = re.search(r'\b(?:a\s+las?|para\s+las?)\s+([01]?\d|2[0-3])\s+y\s+media\b', norm)
    if match_media:
        return int(match_media.group(1)), 30

    match_cuarto = re.search(r'\b(?:a\s+las?|para\s+las?)\s+([01]?\d|2[0-3])\s+y\s+cuarto\b', norm)
    if match_cuarto:
        return int(match_cuarto.group(1)), 15

    # 4. Contexto de tarde/noche (ej: "a las 7 de la tarde" -> 19:00)
    match_tarde = re.search(r'\b(?:a\s+las?|para\s+las?)\s+([1-9]|1[0-2])\s+(?:de la tarde|de la noche|pm)\b', norm)
    if match_tarde:
        h = int(match_tarde.group(1))
        if h < 12:
            h += 12
        return h, 0

    # 5. Contexto de mañana (ej: "a las 10 de la manana" -> 10:00)
    match_manana = re.search(r'\b(?:a\s+las?|para\s+las?)\s+([1-9]|1[0-2])\s+(?:de la manana|am)\b', norm)
    if match_manana:
        return int(match_manana.group(1)), 0

    # 6. Formato simple "a las 14", "a las 9 hs", "las 15 horas"
    match_hora = re.search(r'\b(?:a\s+las?|para\s+las?|las?)\s+([01]?\d|2[0-3])(?:\s*(?:hs|horas|h))?\b', norm)
    if match_hora:
        return int(match_hora.group(1)), 0

    # 7. Formato directo "14 hs", "9 hs", "15hs"
    match_directo = re.search(r'\b([01]?\d|2[0-3])\s*(?:hs|horas|h)\b', norm)
    if match_directo:
        return int(match_directo.group(1)), 0

    return None

# --- 2. DATASET DE ENTRENAMIENTO DE INTENCIONES ---

INTENT_DATASET: Dict[str, List[str]] = {
    'consultar_stock': [
        'cuanto stock hay',
        'cuantas sondas me quedan',
        'stock de sondas',
        'cuantas sondas tengo',
        'mostrar stock',
        'stock actual',
        'que stock tengo',
        'cuanto queda en stock',
        'cuantas cajas de sondas hay',
        'hay stock de sondas',
        'estado del stock',
        'consultar stock',
        'dame el stock de sondas',
        'cuantas unidades quedan',
        'que cantidad de sondas tenemos',
        'stock general',
        'como estamos de stock',
        'ver stock',
    ],
    'agregar_stock': [
        'ingresaron 2 cajas de sondas',
        'cargue 30 sondas al backup',
        'agrega 1 caja al stock normal',
        'sumar 2 cajas de sondas',
        'entran 60 sondas de seguridad',
        'ingreso de 3 cajas',
        'anota que llegaron 2 cajas de sondas',
        'cargar stock',
        'agregar stock de sondas',
        'llegaron 4 cajas',
        'anotar ingreso de 10 sondas',
        'sumar 30 unidades a backup',
        'agrega 5 cajas',
        'ingresaron sondas',
        'poner 2 cajas en el stock',
        'recibi 3 cajas de sondas',
    ],
    'quitar_stock': [
        'descontar 1 caja de sondas',
        'saque 10 sondas de seguridad',
        'gaste 1 caja de sondas',
        'retirar 2 cajas',
        'descuenta 5 sondas del backup',
        'consumo de 1 caja',
        'use 8 sondas',
        'saque sondas',
        'anota salida de 1 caja de sondas',
        'descontar stock',
        'quitar stock',
        'gaste sondas',
        'consumi 10 sondas sueltas',
        'sacar 1 caja',
        'descontar 30 sondas',
        'anotar egreso de stock',
        'salida de 2 cajas',
    ],
    'consultar_autonomia': [
        'cuantos dias de autonomia me quedan',
        'para cuantos dias me alcanzan las sondas',
        'autonomia actual',
        'como viene la autonomia',
        'cuanto me van a durar las sondas',
        'semaforo de autonomia',
        'dias restantes de sondas',
        'autonomia de sondas',
        'cuantos dias tengo cubiertos',
        'para cuando se me terminan las sondas',
        'reporte de autonomia',
        'cuanto tiempo de stock tengo',
    ],
    'movimientos_sondas': [
        'ultimos movimientos de sondas',
        'historial de ingresos y egresos de sondas',
        'movimientos de sondas',
        'que movimientos hubo de sondas',
        'mostrar movimientos de sondas',
        'entradas y salidas de sondas',
        'historial de sondas',
        'ver registros de salidas e ingresos',
        'ultimas salidas de sondas',
        'ultimos ingresos de sondas',
    ],
    'consultar_tramites': [
        'estado de tramites',
        'como viene el pedido de la obra social',
        'resumen de tramites',
        'tramites pendientes',
        'como esta el tramite de os',
        'que tramites hay en curso',
        'historial de tramites',
        'demora de los envios',
        'cuando llega el pedido',
        'como va el pedido',
        'hay tramites abiertos',
        'consultar envios',
        'estado de los pedidos',
    ],
    'iniciar_tramite': [
        'iniciar tramite de obra social',
        'inicie pedido de os con 10 cajas',
        'abrir tramite de backup',
        'inicie el tramite',
        'pedir 12 cajas a la obra social',
        'arrancar tramite de seguridad',
        'nuevo pedido de obra social',
        'iniciar tramite backup con 150 unidades',
        'iniciar pedido de sondas',
        'iniciar tramite',
        'hacer pedido mensual',
    ],
    'cerrar_tramite': [
        'llego el pedido de la obra social',
        'cerrar tramite de os',
        'ya recibi el pedido',
        'cerrar tramite',
        'recibi las sondas de la obra social',
        'dar por recibido el pedido',
        'cerrar tramite de backup',
        'ya me llegaron las sondas',
        'marcar pedido como recibido',
        'finalizar tramite',
    ],
    'pastillero_stock': [
        'stock de pastillas',
        'cuantas pastillas me quedan',
        'stock del pastillero',
        'mostrar pastillero',
        'que remedios tengo',
        'cuantos comprimidos quedan',
        'medicamentos disponibles',
        'stock de enalapril',
        'pastillas restantes',
        'inventario de pastillas',
        'cuanto clonazepam me queda',
        'ver pastillero',
    ],
    'consultar_si_tomo': [
        'tome el enalapril hoy',
        'tome la pastilla de la manana',
        'me tome el remedio',
        'ya tome la medicacion hoy',
        'tome el clonazepam',
        'me tome las pastillas de hoy',
        'tome enalapril ayer',
        'ya me tome la pastilla',
        'tome mi pastilla',
        'habre tomado el clonazepam hoy',
        'me tome la pastilla de la presion',
    ],
    'consultar_tomas': [
        'ultimas tomas de pastillas',
        'historial de tomas',
        'que pastillas tome hoy',
        'a que hora tome el enalapril',
        'tomas registradas',
        'ver tomas de pastillas',
        'que remedios tome ayer',
        'registro de tomas',
        'que pastillas tome esta semana',
        'ultimas tomas registradas',
    ],
    'registrar_toma': [
        'me tome 1 enalapril',
        'anota toma de clonazepam',
        'ya me tome 1 pastilla',
        'tome 2 pastillas de enalapril',
        'acabo de tomar el remedio',
        'registra que tome la pastilla',
        'tome 1 de clonazepam',
        'registra toma de enalapril',
        'me acabo de tomar el clonazepam',
        'anota que tome 1 pastilla',
        'tomada la de enalapril',
        'registra toma de remedio',
    ],
    'agregar_medicamento': [
        'agregar medicamento nuevo',
        'nuevo remedio aspirina 30 unidades',
        'crear pastilla nueva',
        'dar de alta un medicamento',
        'agregar pastilla al pastillero',
        'anotar nuevo medicamento',
        'incorporar remedio nuevo',
    ],
    'cambiar_horario_recordatorio': [
        'haceme acordar a las 14',
        'hoy avisame a las 15 hs',
        'recordame a las 9',
        'recordame tomar las pastillas a las 12',
        'cambia el horario del pastillero a las 11',
        'hoy recordame a las 16:30',
        'avisame de las pastillas a las 8',
        'haceme acordar mas tarde a las 13',
        'posponer recordatorio para las 15',
        'hoy avisame de las pastillas a las 14',
        'recordame hoy a las 10',
        'podes hacerme acordar a las 18',
        'cambiar horario de recordatorio a las 9',
        'haceme acordar a las 12 del mediodia',
        'hoy tomare la pastilla a las 15',
        'acordame a las 8',
        'avisame a las 19',
        'recordame a las 20 hs',
        'cambia la hora del recordatorio a las 14',
    ],
    'saludo': [
        'hola',
        'buen dia',
        'buenas tardes',
        'buenas noches',
        'hola astrana',
        'menu',
        'iniciar',
        'ayuda',
        'que podes hacer',
        'como me podes ayudar',
        'comandos',
    ],
}

@dataclass
class NLPResult:
    intent: str
    confidence: float
    entities: Dict[str, Any] = field(default_factory=dict)
    original_text: str = ""

# --- 3. MOTOR DE CLASIFICACIÓN Y EXTRACCIÓN ---

class NLPEngine:
    def __init__(self):
        self.pipeline: Optional[Pipeline] = None
        self._entrenar_clasificador()

    def _entrenar_clasificador(self):
        """Entrena el modelo local TF-IDF + LogisticRegression."""
        textos = []
        etiquetas = []

        for intencion, frases in INTENT_DATASET.items():
            for frase in frases:
                texto_norm = normalizar_texto(frase)
                if texto_norm:
                    textos.append(texto_norm)
                    etiquetas.append(intencion)

        try:
            from medicine_control.models import AprendizajeAstrana
            aprendizajes = AprendizajeAstrana.objects.filter(confirmado=True)
            for ap in aprendizajes:
                if not ap.intencion or not ap.frase:
                    continue
                texto_norm = normalizar_texto(ap.frase)
                if texto_norm:
                    textos.append(texto_norm)
                    etiquetas.append(ap.intencion)
        except Exception:
            pass

        if not textos:
            logger.warning("No hay muestras para entrenar el NLP local; se deja el modelo sin datos.")
            return

        self.pipeline = Pipeline([
            ('tfidf', TfidfVectorizer(
                ngram_range=(1, 2),
                sublinear_tf=True,
                strip_accents='unicode'
            )),
            ('clf', LogisticRegression(
                max_iter=1000,
                class_weight='balanced',
                C=2.0
            ))
        ])
        self.pipeline.fit(textos, etiquetas)
        logger.info("Motor NLP local entrenado exitosamente con %d muestras.", len(textos))

    def registrar_aprendizaje(self, frase: str, intencion: str, chat_id: str = 'principal', confirmado: bool = True):
        """Guarda una frase confirmada por el usuario y vuelve a entrenar el modelo."""
        if not frase or not intencion:
            return None

        frase_limpia = frase.strip()
        if not frase_limpia:
            return None

        try:
            from medicine_control.models import AprendizajeAstrana
            aprendizaje, _ = AprendizajeAstrana.objects.get_or_create(
                chat_id=str(chat_id),
                frase=frase_limpia,
                intencion=intencion,
                defaults={'confirmado': confirmado},
            )
            aprendizaje.confirmado = confirmado
            aprendizaje.save(update_fields=['confirmado'])
            self.reentrenar()
            return aprendizaje
        except Exception:
            logger.exception("No se pudo registrar el aprendizaje del NLP local.")
            return None

    def reentrenar(self):
        """Permite reentrenar en caliente si se agregan nuevos aprendizajes."""
        self._entrenar_clasificador()

    def clasificar_intencion(self, texto: str) -> Tuple[str, float]:
        """Clasifica la intención del texto normalizado y devuelve (intención, probabilidad)."""
        if not self.pipeline:
            self._entrenar_clasificador()

        norm = normalizar_texto(texto)
        if not norm:
            return 'saludo', 1.0

        probs = self.pipeline.predict_proba([norm])[0]
        max_idx = probs.argmax()
        intencion = self.pipeline.classes_[max_idx]
        confianza = float(probs[max_idx])
        return intencion, confianza

    def buscar_medicamento_fuzzy(self, texto: str, chat_id: str = "principal") -> Optional[Any]:
        """
        Busca medicamentos en Pastillero y alias de MemoriaAstrana usando rapidfuzz.
        Retorna la instancia del modelo Pastillero encontrada o None.
        """
        from medicine_control.models import Pastillero, MemoriaAstrana

        medicamentos = list(Pastillero.objects.all())
        if not medicamentos:
            return None

        norm_texto = normalizar_texto(texto)
        palabras_texto = set(norm_texto.split())

        # 1. Chequeo de alias en memoria (ej. 'presion' -> 'Enalapril')
        try:
            memorias = MemoriaAstrana.objects.filter(
                chat_id=str(chat_id), categoria='alias', activa=True, confirmada=True
            )
            for m in memorias:
                alias_norm = normalizar_texto(m.clave)
                if alias_norm in norm_texto:
                    target = m.valor.strip()
                    for med in medicamentos:
                        if normalizar_texto(med.nombre) == normalizar_texto(target):
                            return med
        except Exception:
            pass

        # 2. Búsqueda exacta de substring
        for med in medicamentos:
            med_norm = normalizar_texto(med.nombre)
            if med_norm and med_norm in norm_texto:
                return med

        # 3. Fuzzy matching por token con rapidfuzz
        nombres_map = {normalizar_texto(med.nombre): med for med in medicamentos}
        nombres_lista = list(nombres_map.keys())

        # Probar contra palabras individuales o bigramas del texto
        tokens = norm_texto.split()
        candidatos = tokens + [
            f"{tokens[i]} {tokens[i+1]}" for i in range(len(tokens)-1)
        ]

        mejor_match = None
        mejor_score = 0.0

        for cand in candidatos:
            resultado = process.extractOne(
                cand,
                nombres_lista,
                scorer=fuzz.WRatio,
                score_cutoff=75
            )
            if resultado and resultado[1] > mejor_score:
                mejor_score = resultado[1]
                mejor_match = nombres_map[resultado[0]]

        return mejor_match

    def buscar_insumo_fuzzy(self, texto: str) -> Optional[Any]:
        """Busca insumos en la tabla Insumo usando rapidfuzz."""
        from medicine_control.models import Insumo

        insumos = list(Insumo.objects.all())
        if not insumos:
            return None

        norm_texto = normalizar_texto(texto)

        # Si menciona 'sonda' o 'sondas', suele ser el insumo principal
        for insumo in insumos:
            if normalizar_texto(insumo.nombre) in norm_texto:
                return insumo

        # Matching difuso
        insumos_map = {normalizar_texto(i.nombre): i for i in insumos}
        resultado = process.extractOne(
            norm_texto,
            list(insumos_map.keys()),
            scorer=fuzz.partial_ratio,
            score_cutoff=70
        )
        if resultado:
            return insumos_map[resultado[0]]

        # Si no hay match pero hay insumo 'Sondas', tomar el primero si habla de stock/cajas
        if any(w in norm_texto for w in ['caja', 'cajas', 'sonda', 'sondas', 'unidades']):
            return insumos[0]
        return None

    def interpretar(self, texto_usuario: str, chat_id: str = "principal") -> NLPResult:
        """
        Punto de entrada principal:
        Clasifica intención y extrae todas las entidades pertinentes.
        """
        intencion, confianza = self.clasificar_intencion(texto_usuario)
        cantidad = extraer_cantidad(texto_usuario)
        tipo_stock = extraer_tipo_stock(texto_usuario)
        tipo_tramite = extraer_tipo_tramite(texto_usuario)
        fecha_inicio, fecha_fin = extraer_rango_fechas(texto_usuario)

        medicamento = self.buscar_medicamento_fuzzy(texto_usuario, chat_id=chat_id)
        insumo = self.buscar_insumo_fuzzy(texto_usuario)

        # Reglas de desambiguación contextual:
        # Si menciona explícitamente un medicamento y un verbo de toma, priorizar registrar_toma o consultar_si_tomo
        norm = normalizar_texto(texto_usuario)
        palabras = set(norm.split())

        if medicamento:
            if palabras & {'tome', 'tomaste', 'tomado', 'tomo', 'tomar', 'anota', 'registra'}:
                if any(w in norm for w in ['?', 'si tome', 'habre tomado', 'tome el', 'tome la']):
                    intencion = 'consultar_si_tomo'
                    confianza = max(confianza, 0.90)
                else:
                    intencion = 'registrar_toma'
                    confianza = max(confianza, 0.90)

        hora_personalizada = extraer_hora_personalizada(texto_usuario)
        if hora_personalizada and (palabras & {'acordar', 'recordar', 'recordame', 'avisame', 'pastillero', 'pastillas', 'posponer', 'horario', 'hora'}):
            intencion = 'cambiar_horario_recordatorio'
            confianza = max(confianza, 0.95)

        # Si no se especificó cantidad para registrar_toma, por defecto suele ser 1
        if intencion == 'registrar_toma' and cantidad is None:
            cantidad = 1

        entidades = {
            'cantidad': cantidad,
            'tipo_stock': tipo_stock,
            'tipo_tramite': tipo_tramite,
            'fecha_inicio': fecha_inicio,
            'fecha_fin': fecha_fin,
            'medicamento': medicamento,
            'insumo': insumo,
            'hora_personalizada': hora_personalizada,
        }

        return NLPResult(
            intent=intencion,
            confidence=confianza,
            entities=entidades,
            original_text=texto_usuario,
        )

# Instancia global única
nlp_engine = NLPEngine()

