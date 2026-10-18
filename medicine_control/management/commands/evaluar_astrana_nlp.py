import json
import os
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from Astrana.nlp_engine import INTENT_DATASET, NLPEngine, normalizar_texto
from medicine_control.models import AprendizajeAstrana


class Command(BaseCommand):
    help = (
        'Evalúa el clasificador de intenciones de Astrana con un conjunto separado '
        'de ejemplos. Agregá --semantic para comparar Sentence Transformers.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--semantic',
            action='store_true',
            help='Incluye Sentence Transformers en la comparación (requiere el modelo descargado).',
        )

    def handle(self, *args, **options):
        dataset_path = Path(settings.BASE_DIR) / 'Astrana' / 'intent_evaluation.json'
        try:
            dataset = json.loads(dataset_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f'No se pudo leer el conjunto de evaluación {dataset_path}: {exc}') from exc

        if not isinstance(dataset, list) or not dataset:
            raise CommandError('El conjunto de evaluación debe ser una lista JSON no vacía.')

        ejemplos = []
        for indice, ejemplo in enumerate(dataset, start=1):
            if (
                not isinstance(ejemplo, dict)
                or not isinstance(ejemplo.get('text'), str)
                or not ejemplo['text'].strip()
                or not isinstance(ejemplo.get('intent'), str)
                or ejemplo.get('intent') not in INTENT_DATASET
            ):
                raise CommandError(
                    f'El ejemplo {indice} debe incluir text no vacío e intent válido.'
                )
            ejemplos.append({
                'text': ejemplo['text'].strip(),
                'intent': ejemplo['intent'],
            })
        intenciones_evaluadas = {ejemplo['intent'] for ejemplo in ejemplos}
        if intenciones_evaluadas != set(INTENT_DATASET):
            faltantes = sorted(set(INTENT_DATASET) - intenciones_evaluadas)
            raise CommandError(
                'El conjunto debe incluir todas las intenciones; faltan: '
                + ', '.join(faltantes)
            )

        frases_entrenamiento = {
            normalizar_texto(frase)
            for frases in INTENT_DATASET.values()
            for frase in frases
        }
        frases_entrenamiento.update(
            normalizar_texto(frase)
            for frase in AprendizajeAstrana.objects.filter(
                chat_id='global',
                confirmado=True,
                intencion__in=INTENT_DATASET,
            ).values_list('frase', flat=True)
        )
        filtradas = [
            ejemplo['text']
            for ejemplo in ejemplos
            if normalizar_texto(ejemplo['text']) in frases_entrenamiento
        ]
        if filtradas:
            raise CommandError(
                'Hay frases del conjunto que también aparecen en entrenamiento/aprendizajes: '
                + '; '.join(filtradas)
            )

        engine = NLPEngine()
        predicciones_actuales = []
        predicciones_semanticas = []
        for ejemplo in ejemplos:
            prediccion, confianza = engine.clasificar_intencion(ejemplo['text'])
            predicciones_actuales.append((str(prediccion), confianza))

        if options['semantic']:
            os.environ['ASTRANA_SEMANTIC_SHADOW'] = '1'
            for ejemplo in ejemplos:
                resultado = engine.clasificar_intencion_semantica(ejemplo['text'])
                if resultado is None:
                    raise CommandError(
                        'No se pudo ejecutar Sentence Transformers. Verificá la instalación '
                        'opcional y que el modelo esté descargado localmente.'
                    )
                predicciones_semanticas.append(resultado)

        self.stdout.write(f'Ejemplos de evaluación: {len(ejemplos)}')
        self.stdout.write('Clasificador actual (TF-IDF):')
        self._mostrar_metricas(ejemplos, predicciones_actuales)
        if options['semantic']:
            self.stdout.write('Sentence Transformers:')
            self._mostrar_metricas(ejemplos, predicciones_semanticas)
            self._mostrar_desacuerdos(ejemplos, predicciones_actuales, predicciones_semanticas)
        else:
            self.stdout.write(
                self.style.WARNING(
                    'Comparación semántica omitida. Usá --semantic para incluirla.'
                )
            )

    def _mostrar_metricas(self, ejemplos, predicciones):
        etiquetas = list(INTENT_DATASET)
        total = len(ejemplos)
        aciertos = sum(
            ejemplo['intent'] == prediccion[0]
            for ejemplo, prediccion in zip(ejemplos, predicciones)
        )
        verdaderos = Counter()
        predichos = Counter(prediccion[0] for prediccion in predicciones)
        soportes = Counter(ejemplo['intent'] for ejemplo in ejemplos)
        for ejemplo, prediccion in zip(ejemplos, predicciones):
            if ejemplo['intent'] == prediccion[0]:
                verdaderos[ejemplo['intent']] += 1

        macro_f1 = []
        self.stdout.write(f'  Exactitud: {aciertos}/{total} ({aciertos / total:.1%})')
        self.stdout.write('  Intención                         P      R      F1     N')
        for etiqueta in etiquetas:
            tp = verdaderos[etiqueta]
            precision = tp / predichos[etiqueta] if predichos[etiqueta] else 0.0
            recall = tp / soportes[etiqueta] if soportes[etiqueta] else 0.0
            f1 = (
                2 * precision * recall / (precision + recall)
                if precision + recall
                else 0.0
            )
            macro_f1.append(f1)
            self.stdout.write(
                f'  {etiqueta:<32} {precision:>5.1%} {recall:>6.1%} {f1:>6.1%} '
                f'{soportes[etiqueta]:>5}'
            )
        self.stdout.write(f'  Macro-F1: {sum(macro_f1) / len(macro_f1):.1%}')

        errores = [
            (ejemplo, prediccion)
            for ejemplo, prediccion in zip(ejemplos, predicciones)
            if ejemplo['intent'] != prediccion[0]
        ]
        if errores:
            self.stdout.write('  Errores:')
            for ejemplo, (prediccion, confianza) in errores:
                self.stdout.write(
                    f'    esperaba {ejemplo["intent"]}, predijo {prediccion} '
                    f'(confianza {confianza:.3f}): {ejemplo["text"]}'
                )

    def _mostrar_desacuerdos(self, ejemplos, actuales, semanticas):
        desacuerdos = [
            (ejemplo, actual[0], semantica[0], semantica[1])
            for ejemplo, actual, semantica in zip(ejemplos, actuales, semanticas)
            if actual[0] != semantica[0]
        ]
        self.stdout.write(
            f'Desacuerdos entre clasificadores: {len(desacuerdos)}/{len(ejemplos)}'
        )
        for ejemplo, actual, semantica, similitud in desacuerdos:
            self.stdout.write(
                f'  actual={actual}, semántico={semantica} ({similitud:.3f}), '
                f'esperado={ejemplo["intent"]}: {ejemplo["text"]}'
            )
