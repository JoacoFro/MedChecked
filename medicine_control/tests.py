from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.db import IntegrityError, connection, transaction
from unittest.mock import patch
import base64
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.utils import CaptureQueriesContext

from medicine_control import views as medicine_views
from medicine_control.models import (
	Envio,
	AprendizajeAstrana,
	IngresoPastillero,
	Insumo,
	MemoriaAstrana,
	Pastillero,
	Pedido,
	Salida,
	TomaPastillero,
)
from Astrana.webpush_utils import _cargar_clave_privada_vapid


class AstranaPwaTests(TestCase):
	def setUp(self):
		self.client.defaults['wsgi.url_scheme'] = 'https'

	def test_ping_returns_ok_without_database_queries(self):
		with CaptureQueriesContext(connection) as queries:
			response = self.client.get(reverse('ping'), secure=True)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {'status': 'ok'})
		self.assertEqual(len(queries), 0)
		self.assertEqual(response['Cache-Control'], 'no-store')

	def test_ping_only_allows_get(self):
		response = self.client.post(reverse('ping'), secure=True)

		self.assertEqual(response.status_code, 405)

	def test_astrana_chat_renders_the_response_bubble_style(self):
		response = self.client.get(reverse('astrana_pwa'), secure=True)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'astrana-response-bubble')
		self.assertContains(response, 'user-message-bubble')

	def test_intent_evaluation_set_covers_all_intents_without_training_duplicates(self):
		from Astrana.nlp_engine import INTENT_DATASET, normalizar_texto

		evaluation_path = Path(__file__).resolve().parents[1] / 'Astrana' / 'intent_evaluation.json'
		examples = json.loads(evaluation_path.read_text(encoding='utf-8'))
		evaluation_phrases = [normalizar_texto(example['text']) for example in examples]
		training_phrases = {
			normalizar_texto(phrase)
			for phrases in INTENT_DATASET.values()
			for phrase in phrases
		}
		evaluation_counts = {
			intent: sum(example['intent'] == intent for example in examples)
			for intent in INTENT_DATASET
		}

		self.assertEqual({example['intent'] for example in examples}, set(INTENT_DATASET))
		self.assertTrue(all(count >= 5 for count in evaluation_counts.values()))
		self.assertEqual(len(evaluation_phrases), len(set(evaluation_phrases)))
		self.assertFalse(set(evaluation_phrases) & training_phrases)

	def test_local_intent_classifier_is_bounded_without_losing_evaluation_accuracy(self):
		from Astrana.nlp_engine import NLPEngine, normalizar_texto

		evaluation_path = Path(__file__).resolve().parents[1] / 'Astrana' / 'intent_evaluation.json'
		examples = json.loads(evaluation_path.read_text(encoding='utf-8'))
		engine = NLPEngine()
		engine.clasificar_intencion('frase de fallback sin reglas directas')

		classifier = engine.pipeline.named_steps['clf']
		predicciones = engine.pipeline.predict([
			normalizar_texto(example['text'])
			for example in examples
		])
		aciertos = sum(
			prediccion == example['intent']
			for prediccion, example in zip(predicciones, examples)
		)

		self.assertEqual(classifier.solver, 'saga')
		self.assertLessEqual(classifier.max_iter, 100)
		self.assertGreaterEqual(aciertos / len(examples), 0.65)

	@patch.dict('os.environ', {'ASTRANA_SEMANTIC_SHADOW': '0'})
	def test_semantic_shadow_is_disabled_by_default(self):
		from Astrana.nlp_engine import NLPEngine

		engine = NLPEngine()

		self.assertIsNone(engine.clasificar_intencion_semantica('cuánto stock tengo'))
		self.assertIsNone(engine._semantic_model)

	@patch.dict('os.environ', {'ASTRANA_SEMANTIC_SHADOW': '1'})
	@patch('Astrana.nlp_engine.nlp_engine.clasificar_intencion_semantica')
	def test_semantic_shadow_does_not_change_the_chat_response(self, clasificar_semantico):
		clasificar_semantico.return_value = ('consultar_autonomia', 0.99)
		Insumo.objects.create(
			nombre='Sondas',
			stock_actual_cajas=4,
			unidades_por_caja=30,
			consumo_diario=8,
		)

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'cuánto stock tengo'},
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertIn('stock', response.json()['reply'].lower())
		clasificar_semantico.assert_called_once_with('cuánto stock tengo')

	@patch.dict('sys.modules', {'Astrana.main': None})
	def test_pastillero_state_endpoint_does_not_import_astrana_runtime(self):
		Pastillero.objects.create(nombre='Enalapril', cantidad_total=4)

		response = self.client.get(
			reverse('astrana_estado_pastillero_hoy'),
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(len(response.json()['pendientes']), 1)

	@patch.dict('sys.modules', {'Astrana.main': None})
	def test_pastillero_confirmation_does_not_import_astrana_runtime(self):
		medicamento = Pastillero.objects.create(nombre='Enalapril', cantidad_total=4)

		response = self.client.post(
			reverse('astrana_confirmar_toma_api'),
			data=json.dumps({'medicamento_id': medicamento.id}),
			content_type='application/json',
			secure=True,
		)

		medicamento.refresh_from_db()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(medicamento.cantidad_total, 3)

	def test_home_order_actions_allow_one_open_per_type_and_save_both_receipts(self):
		for tipo in ('os', 'backup'):
			response = self.client.post(
				reverse('iniciar_pedido'),
				data=json.dumps({'tipo': tipo}),
				content_type='application/json',
				secure=True,
			)
			self.assertEqual(response.status_code, 200)

		response = self.client.post(
			reverse('iniciar_pedido'),
			data=json.dumps({'tipo': 'os'}),
			content_type='application/json',
			secure=True,
		)
		self.assertEqual(response.status_code, 400)
		self.assertEqual(Envio.objects.filter(estado='tramite').count(), 2)

		for tipo in ('os', 'backup'):
			response = self.client.post(
				reverse('marcar_recibido_home'),
				data=json.dumps({'tipo': tipo}),
				content_type='application/json',
				secure=True,
			)
			self.assertEqual(response.status_code, 200)

		self.assertEqual(Envio.objects.filter(estado='tramite').count(), 0)
		self.assertEqual(Envio.objects.filter(estado='recibido').count(), 2)
		self.assertEqual(
			set(Envio.objects.filter(estado='recibido').values_list('tipo', flat=True)),
			{'os', 'backup'},
		)

	def test_chat_history_is_saved_and_restored_from_session(self):
		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'hola astrana'},
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertIn('reply', response.json())

		history_response = self.client.get(reverse('astrana_chat_api'), secure=True)
		messages = history_response.json()['messages']
		self.assertEqual(history_response.status_code, 200)
		self.assertEqual([message['role'] for message in messages], ['user', 'assistant'])
		self.assertEqual(messages[0]['content'], 'hola astrana')

	def test_chat_reports_the_existing_daily_probe_consumption_metric(self):
		from Astrana import main as astrana
		from Astrana.nlp_engine import nlp_engine

		insumo = Insumo.objects.create(
			nombre='Sondas',
			stock_actual_cajas=4,
			unidades_por_caja=30,
		)
		Salida.objects.create(
			insumo=insumo,
			cantidad=126,
			tipo_stock='stock_normal',
		)
		antigua = Salida.objects.create(
			insumo=insumo,
			cantidad=900,
			tipo_stock='stock_normal',
		)
		Salida.objects.filter(pk=antigua.pk).update(
			fecha=timezone.now() - timedelta(days=15),
		)
		personalizer_client = Mock()
		personalizer_client.models.generate_content.return_value.text = (
			'El consumo fue de 9,0 sondas / día durante los últimos 14 días.'
		)

		with (
			patch.object(astrana, 'gemini_client', personalizer_client),
			patch.object(nlp_engine, 'pipeline', None),
			patch.object(nlp_engine, '_version_aprendizajes', None),
			patch.object(nlp_engine, '_frases_aprendidas', {}),
			patch.object(nlp_engine, '_entrenar_clasificador') as entrenar,
		):
			response = self.client.post(
				reverse('astrana_chat_api'),
				{'message': '¿Cuál es el consumo diario de sondas?'},
				secure=True,
			)
			entrenar.assert_not_called()

		self.assertEqual(response.status_code, 200)
		self.assertEqual(
			response.json()['reply'],
			'El consumo fue de 9,0 sondas / día durante los últimos 14 días.',
		)
		self.assertIn('9,0 sondas / día', response.json()['reply'].lower())
		self.assertIn('últimos 14 días', response.json()['reply'])
		self.assertIn(
			'Resultado técnico de la función',
			personalizer_client.models.generate_content.call_args.kwargs['contents'],
		)

	def test_astrana_learns_global_style_preferences_from_clear_feedback(self):
		from Astrana import main as astrana

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'Astrana, respondé más breve y sin emojis.'},
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertIn('más breve', response.json()['reply'])
		self.assertIn('no usar emojis', response.json()['reply'])
		preferencia = MemoriaAstrana.objects.get(
			chat_id='principal',
			categoria='preferencia',
			clave='estilo_respuesta',
			activa=True,
			confirmada=True,
		)
		self.assertEqual(
			preferencia.valor,
			'longitud=breve;emojis=no',
		)
		self.assertEqual(
			astrana.obtener_preferencia_estilo(),
			{'longitud': 'breve', 'emojis': 'no'},
		)

		astrana.actualizar_preferencia_estilo('Prefiero un tono más formal.')
		self.assertEqual(
			astrana.obtener_preferencia_estilo(),
			{'longitud': 'breve', 'emojis': 'no', 'tono': 'formal'},
		)
		recuerdos = astrana.procesar_comando_memoria('otro-chat', '¿Qué recordás?')
		self.assertIn('estilo_respuesta', recuerdos)
		self.assertIn('tono=formal', recuerdos)

		self.assertEqual(
			astrana.actualizar_preferencia_estilo(
				'Olvidá mis preferencias de respuesta.'
			),
			'Listo, olvidé tus preferencias de estilo.',
		)
		preferencia.refresh_from_db()
		self.assertFalse(preferencia.activa)

	def test_astrana_personalizer_uses_context_and_preserves_function_facts(self):
		from Astrana.response_personalizer import personalizar_respuesta

		resultado = '📈 Consumo IA: 8,6 Sondas / día.'
		client = Mock()
		client.models.generate_content.return_value.text = (
			'Mirá, el consumo es de 8,6 sondas por día.'
		)
		respuesta = personalizar_respuesta(
			'¿Cuál es el consumo diario de sondas?',
			resultado,
			'consultar_consumo',
			contexto=[
				{'role': 'user', 'content': 'Quiero revisar las sondas.'},
				{'role': 'assistant', 'content': 'Dale, ¿qué querés saber?'},
			],
			preferencias_estilo={'longitud': 'breve', 'emojis': 'no'},
			client=client,
		)

		self.assertEqual(respuesta, 'Mirá, el consumo es de 8,6 sondas por día.')
		prompt = client.models.generate_content.call_args.kwargs['contents']
		self.assertIn('Quiero revisar las sondas.', prompt)
		self.assertIn('¿Cuál es el consumo diario de sondas?', prompt)
		self.assertIn(resultado, prompt)
		self.assertIn('Priorizá respuestas breves', prompt)
		self.assertIn('No uses emojis', prompt)
		self.assertEqual(
			client.models.generate_content.call_args.kwargs['config'].http_options.timeout,
			10000,
		)

		client.models.generate_content.return_value.text = (
			'Mirá, el consumo es de 9,0 sondas por día.'
		)
		self.assertEqual(
			personalizar_respuesta(
				'¿Cuál es el consumo diario de sondas?',
				resultado,
				'consultar_consumo',
				client=client,
			),
			resultado,
		)
		client.models.generate_content.return_value.text = (
			'El consumo diario es de 8,6 sondas.'
		)
		self.assertEqual(
			personalizar_respuesta(
				'¿Cuál es el consumo diario de sondas?',
				resultado,
				'consultar_consumo',
				client=client,
			),
			'El consumo diario es de 8,6 sondas.',
		)

		self.assertEqual(
			personalizar_respuesta(
				'Mostrame solo el consumo diario de sondas',
				resultado,
				'consultar_consumo',
				client=client,
			),
			resultado,
		)
		self.assertEqual(client.models.generate_content.call_count, 3)
		self.assertEqual(
			personalizar_respuesta(
				'¿Cuál es el consumo diario de sondas?',
				'❌ Error al consultar el consumo de Sondas.',
				'consultar_consumo',
				client=client,
			),
			'❌ Error al consultar el consumo de Sondas.',
		)
		self.assertEqual(client.models.generate_content.call_count, 3)

		client.models.generate_content.return_value.text = (
			'Drafting the Response:** * Data to Present:** *'
		)
		self.assertEqual(
			personalizar_respuesta(
				'¿Cuál es el consumo diario de sondas?',
				resultado,
				'consultar_consumo',
				client=client,
			),
			resultado,
		)

		client.models.generate_content.side_effect = RuntimeError('Gemini no disponible')
		with self.assertLogs('Astrana.response_personalizer', level='ERROR'):
			self.assertEqual(
				personalizar_respuesta(
					'¿Cuál es el consumo diario de sondas?',
					resultado,
					'consultar_consumo',
					client=client,
				),
				resultado,
			)

	def test_chat_distinguishes_latest_orders_plural_from_latest_order_singular(self):
		from Astrana import main as astrana

		self.assertEqual(
			astrana.detectar_consulta_datos('Che, ¿cuáles son los últimos pedidos?'),
			'movimientos_tramites',
		)
		self.assertEqual(
			astrana.detectar_consulta_datos('¿Cuál fue el último pedido?'),
			'ultimo_pedido',
		)

	def test_chat_routes_stock_question_to_local_stock_consultation(self):
		from Astrana import main as astrana

		Insumo.objects.create(
			nombre='Sondas',
			stock_actual_cajas=4,
			unidades_por_caja=30,
		)
		with patch.object(astrana, 'gemini_client', None):
			response = self.client.post(
				reverse('astrana_chat_api'),
				{'message': 'Che Astrana, ¿me decís el stock actual?'},
				secure=True,
			)

		self.assertEqual(response.status_code, 200)
		self.assertIn('Estado Actual del Stock', response.json()['reply'])
		self.assertIn('120 un.', response.json()['reply'])
		self.assertNotIn('Te paso el stock', response.json()['reply'])

	def test_chat_verifies_named_medication_instead_of_listing_all_takes(self):
		from Astrana import main as astrana

		medicamento = Pastillero.objects.create(
			nombre='Pantoprazol',
			cantidad_total=10,
		)
		TomaPastillero.objects.create(medicamento=medicamento, cantidad=1)
		with patch.object(astrana, 'gemini_client', None):
			response = self.client.post(
				reverse('astrana_chat_api'),
				{'message': 'Tome pantoprazol?'},
				secure=True,
			)

		self.assertEqual(response.status_code, 200)
		self.assertIn('Sí, registraste una toma de Pantoprazol hoy.', response.json()['reply'])
		self.assertNotIn('Tomas de los medicamentos', response.json()['reply'])
		self.assertEqual(TomaPastillero.objects.filter(medicamento=medicamento).count(), 1)

	def test_unknown_chat_message_does_not_fall_through_to_gemini_chat(self):
		from Astrana import main as astrana
		from Astrana.nlp_engine import NLPResult, nlp_engine

		gemini_client = Mock()
		with (
			patch.object(astrana, 'gemini_client', gemini_client),
			patch.object(astrana, 'detectar_consulta_datos', return_value=None),
			patch.object(astrana, 'detectar_consulta_con_memoria', return_value=None),
			patch.object(nlp_engine, 'intencion_aprendida', return_value=None),
			patch.object(nlp_engine, 'interpretar', return_value=NLPResult('otro', 0.9)),
			patch.object(nlp_engine, 'clasificar_intencion_semantica', return_value=None),
		):
			response = self.client.post(
				reverse('astrana_chat_api'),
				{'message': 'Contame un chiste.'},
				secure=True,
			)

		self.assertEqual(response.status_code, 200)
		self.assertIn('No pude resolver esa consulta', response.json()['reply'])
		gemini_client.chats.create.assert_not_called()
		gemini_client.models.generate_content.assert_not_called()

	def test_chat_reports_latest_order_date_even_if_order_flow_is_waiting(self):
		from Astrana import main as astrana

		fecha_base = timezone.localdate() - timedelta(days=10)
		for indice in range(6):
			envio = Envio.objects.create(
				tipo='os' if indice % 2 == 0 else 'backup',
				estado='recibido',
				cantidad_pedida=30 + indice,
			)
			Envio.objects.filter(pk=envio.pk).update(
				fecha_solicitud=fecha_base + timedelta(days=indice),
			)
		fecha_ultimo_pedido = fecha_base + timedelta(days=5)
		session = self.client.session
		session.create()
		MemoriaAstrana.objects.create(
			chat_id=f'web-{session.session_key}',
			categoria='contexto',
			clave='pedido_estado',
			valor='esperando_cantidad',
		)
		session.save()

		for pregunta in (
			'¿Cuándo hice el último pedido?',
			'¿Cuál fue mi pedido más reciente?',
		):
			with patch.object(astrana, 'gemini_client', None):
				response = self.client.post(
					reverse('astrana_chat_api'),
					{'message': pregunta},
					secure=True,
				)

			self.assertEqual(response.status_code, 200)
			self.assertIn(fecha_ultimo_pedido.strftime('%d/%m/%Y'), response.json()['reply'])
			self.assertNotIn(fecha_base.strftime('%d/%m/%Y'), response.json()['reply'])
			self.assertEqual(response.json()['reply'].count('Estado:'), 1)

	def test_chat_reports_average_delay_and_latest_received_order_dates(self):
		from Astrana import main as astrana

		hoy = timezone.localdate()
		pedido_anterior = Envio.objects.create(
			tipo='os',
			estado='recibido',
			cantidad_pedida=30,
		)
		pedido_reciente = Envio.objects.create(
			tipo='backup',
			estado='recibido',
			cantidad_pedida=20,
		)
		Envio.objects.filter(pk=pedido_anterior.pk).update(
			fecha_solicitud=hoy - timedelta(days=20),
			fecha_cierre=hoy - timedelta(days=10),
		)
		fecha_solicitud_ultimo = hoy - timedelta(days=12)
		fecha_recepcion_ultimo = hoy - timedelta(days=6)
		Envio.objects.filter(pk=pedido_reciente.pk).update(
			fecha_solicitud=fecha_solicitud_ultimo,
			fecha_cierre=fecha_recepcion_ultimo,
		)
		Envio.objects.create(
			tipo='os',
			estado='tramite',
			cantidad_pedida=15,
		)

		consultas = (
			(
				'¿Cuál es la demora promedio de los pedidos?',
				'8,0 días',
			),
			(
				'¿Cuánto fue la demora del último pedido?',
				'Demora: 6 días',
			),
			(
				'¿Cuándo llegó el último pedido?',
				f'recibido el {fecha_recepcion_ultimo:%d/%m/%Y}',
			),
		)
		for pregunta, esperado in consultas:
			with self.subTest(pregunta=pregunta):
				with patch.object(astrana, 'gemini_client', None):
					response = self.client.post(
						reverse('astrana_chat_api'),
						{'message': pregunta},
						secure=True,
					)
				self.assertEqual(response.status_code, 200)
				self.assertIn(esperado.lower(), response.json()['reply'].lower())
		self.assertIn(
			f'solicitado el {fecha_solicitud_ultimo:%d/%m/%Y}',
			response.json()['reply'].lower(),
		)

	def test_chat_history_of_medication_works_during_pending_order(self):
		ditropan = Pastillero.objects.create(nombre='Ditropan', cantidad_total=4)
		enalapril = Pastillero.objects.create(nombre='Enalapril', cantidad_total=4)
		TomaPastillero.objects.create(medicamento=ditropan, cantidad=1)
		TomaPastillero.objects.create(medicamento=enalapril, cantidad=1)
		TomaPastillero.objects.create(medicamento=ditropan, cantidad=2)
		session = self.client.session
		session.create()
		MemoriaAstrana.objects.create(
			chat_id=f'web-{session.session_key}',
			categoria='contexto',
			clave='pedido_estado',
			valor='esperando_cantidad',
		)
		session.save()

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'Últimas tomas de Ditropan'},
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertIn('Ditropan', response.json()['reply'])
		self.assertNotIn('Enalapril', response.json()['reply'])
		self.assertNotIn('¿Cuántas', response.json()['reply'])
		self.assertNotIn('no dispongo de registros', response.json()['reply'].lower())

	def test_chat_asks_for_a_medication_when_latest_takes_query_does_not_name_one(self):
		ditropan = Pastillero.objects.create(nombre='Ditropan', cantidad_total=4)
		Pastillero.objects.create(nombre='Enalapril', cantidad_total=4)
		TomaPastillero.objects.create(medicamento=ditropan, cantidad=1)

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'Últimas tomas de cualquier medicamento del pastillero'},
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertIn('¿De cuál medicamento', response.json()['reply'])
		self.assertNotIn('Ditropan', response.json()['reply'])
		self.assertNotIn('Enalapril', response.json()['reply'])

	def test_chat_learns_a_confirmed_correction_for_all_users(self):
		from Astrana.nlp_engine import NLPEngine, nlp_engine

		otro_worker_engine = NLPEngine()
		Insumo.objects.create(
			nombre='Sondas',
			stock_actual_cajas=4,
			unidades_por_caja=30,
			consumo_diario=8,
		)
		frase_original = 'como estamos de stock'

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': frase_original},
			secure=True,
		)
		self.assertEqual(response.status_code, 200)

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'No, quería consultar la autonomía de las sondas'},
			secure=True,
		)
		self.assertIn('¿Querés que guarde', response.json()['reply'])
		self.assertEqual(
			AprendizajeAstrana.objects.filter(chat_id='global', frase=frase_original).count(),
			0,
		)

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'sí'},
			secure=True,
		)
		self.assertIn('Aprendido para todos', response.json()['reply'])
		self.assertEqual(
			AprendizajeAstrana.objects.get(chat_id='global', frase=frase_original).intencion,
			'consultar_autonomia',
		)
		self.assertEqual(nlp_engine.intencion_aprendida(frase_original), 'consultar_autonomia')
		self.assertEqual(
			otro_worker_engine.intencion_aprendida(frase_original),
			'consultar_autonomia',
		)

		otro_usuario = self.client_class()
		response = otro_usuario.post(
			reverse('astrana_chat_api'),
			{'message': frase_original},
			secure=True,
		)
		self.assertEqual(response.status_code, 200)
		self.assertIn('autonomía', response.json()['reply'].lower())

	def test_low_confidence_query_asks_for_intent_before_training(self):
		from Astrana.nlp_engine import INTENT_LABELS

		frase = 'quasar infinito de los cristales rojos'
		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': frase},
			secure=True,
		)
		self.assertIn('prefiero preguntarte', response.json()['reply'])
		self.assertEqual(
			AprendizajeAstrana.objects.filter(chat_id='global', frase=frase).count(),
			0,
		)

		indice_autonomia = list(INTENT_LABELS).index('consultar_autonomia') + 1
		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': str(indice_autonomia)},
			secure=True,
		)
		self.assertIn('¿Querés que guarde', response.json()['reply'])
		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'confirmo'},
			secure=True,
		)

		self.assertEqual(
			AprendizajeAstrana.objects.get(chat_id='global', frase=frase).intencion,
			'consultar_autonomia',
		)

	def test_chat_tramite_history_does_not_call_missing_handlers(self):
		Envio.objects.create(tipo='backup', estado='recibido', cantidad_pedida=30)

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'Mostrame el historial de trámites'},
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertIn('Últimos', response.json()['reply'])
		self.assertIn('Back Up', response.json()['reply']		)

	def test_explicit_teaching_command_requires_confirmation(self):
		frase = 'mi reserva se está terminando'

		response = self.client.post(
			reverse('astrana_chat_api'),
			{
				'message': (
					f'Enseñame que cuando digo "{frase}" quiero consultar autonomía de sondas'
				)
			},
			secure=True,
		)

		self.assertIn('¿Querés que guarde', response.json()['reply'])
		self.assertEqual(
			AprendizajeAstrana.objects.filter(chat_id='global', frase=frase).count(),
			0,
		)

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'confirmo'},
			secure=True,
		)
		self.assertIn('Aprendido para todos', response.json()['reply'])
		self.assertEqual(
			AprendizajeAstrana.objects.get(chat_id='global', frase=frase).intencion,
			'consultar_autonomia',
		)

	def test_pwa_order_flow_requests_type_then_quantity_and_closes_when_received(self):
		insumo = Insumo.objects.create(
			nombre='Sondas',
			stock_actual_cajas=2,
			unidades_por_caja=30,
			backup_unidades=15,
		)
		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'iniciar pedido de sondas'},
			secure=True,
		)
		self.assertEqual(response.status_code, 200)
		self.assertIn('tipo', response.json()['reply'].lower())

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'obra social'},
			secure=True,
		)
		self.assertEqual(response.status_code, 200)
		reply = response.json()['reply'].lower()
		self.assertIn('sondas', reply)
		self.assertIn('pedir', reply)

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': '300 sondas'},
			secure=True,
		)
		self.assertEqual(response.status_code, 200)
		self.assertIn('pedido', response.json()['reply'].lower())
		envio = Envio.objects.get(tipo='os', estado='tramite')
		self.assertEqual(envio.cantidad_pedida, 300)
		self.assertEqual(envio.notas, 'PWA: cantidad expresada en unidades')

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'ya recibi el pedido'},
			secure=True,
		)
		self.assertEqual(response.status_code, 200)
		self.assertIn('recibido', response.json()['reply'].lower())
		envio.refresh_from_db()
		insumo.refresh_from_db()
		self.assertEqual(envio.estado, 'recibido')
		self.assertEqual(insumo.stock_actual_cajas, 12)
		self.assertEqual(
			Pedido.objects.get(lugar_compra='Cierre Trámite Obra Social').cantidad,
			300,
		)

	def test_pwa_backup_order_closes_into_safety_stock_when_receipt_type_is_implicit(self):
		insumo = Insumo.objects.create(
			nombre='Sondas',
			stock_actual_cajas=2,
			unidades_por_caja=30,
			backup_unidades=15,
		)
		self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'iniciar pedido de sondas'},
			secure=True,
		)
		self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'backup'},
			secure=True,
		)
		self.client.post(
			reverse('astrana_chat_api'),
			{'message': '150'},
			secure=True,
		)
		envio = Envio.objects.get(tipo='backup', estado='tramite')
		self.assertEqual(envio.cantidad_pedida, 150)
		self.assertIn('unidades', envio.notas)

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'ya recibí el pedido'},
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		envio.refresh_from_db()
		insumo.refresh_from_db()
		self.assertEqual(envio.estado, 'recibido')
		self.assertEqual(insumo.stock_actual_cajas, 2)
		self.assertEqual(insumo.backup_unidades, 165)
		pedido = Pedido.objects.get(lugar_compra='Cierre Trámite Backup')
		self.assertEqual(pedido.tipo_stock, 'seguridad')
		self.assertEqual(pedido.cantidad, 150)

	def test_only_one_open_order_per_type_and_both_types_are_saved_as_received(self):
		insumo = Insumo.objects.create(
			nombre='Sondas',
			stock_actual_cajas=2,
			unidades_por_caja=30,
			backup_unidades=15,
		)
		envio_abierto = Envio.objects.create(
			tipo='os',
			estado='tramite',
			cantidad_pedida=60,
			notas='PWA: cantidad expresada en unidades',
		)
		Envio.objects.filter(pk=envio_abierto.pk).update(
			fecha_solicitud=timezone.localdate() - timedelta(days=40),
		)

		for tipo, cantidad in [('obra social', 60), ('backup', 30), ('obra social', 90)]:
			self.client.post(
				reverse('astrana_chat_api'),
				{'message': 'iniciar pedido de sondas'},
				secure=True,
			)
			self.client.post(
				reverse('astrana_chat_api'),
				{'message': tipo},
				secure=True,
			)
			response = self.client.post(
				reverse('astrana_chat_api'),
				{'message': str(cantidad)},
				secure=True,
			)
			self.assertEqual(response.status_code, 200)

		self.assertEqual(Envio.objects.filter(tipo='os', estado='tramite').count(), 1)
		self.assertEqual(Envio.objects.filter(tipo='backup', estado='tramite').count(), 1)
		self.assertIn(
			'ya existe un trámite de obra social en curso',
			response.json()['reply'].lower(),
		)
		with self.assertRaises(IntegrityError):
			with transaction.atomic():
				Envio.objects.create(tipo='os', estado='tramite', cantidad_pedida=30)

		for mensaje in ('ya recibí el pedido de obra social', 'ya recibí el pedido backup'):
			response = self.client.post(
				reverse('astrana_chat_api'),
				{'message': mensaje},
				secure=True,
			)
			self.assertEqual(response.status_code, 200)
			self.assertIn('cerrado', response.json()['reply'].lower())

		self.assertEqual(Envio.objects.filter(estado='tramite').count(), 0)
		self.assertEqual(Envio.objects.filter(estado='recibido').count(), 2)
		self.assertEqual(
			set(Envio.objects.filter(estado='recibido').values_list('tipo', flat=True)),
			{'os', 'backup'},
		)
		insumo.refresh_from_db()
		self.assertEqual(insumo.stock_actual_cajas, 4)
		self.assertEqual(insumo.backup_unidades, 45)
		self.assertEqual(
			Pedido.objects.filter(lugar_compra__startswith='Cierre Trámite').count(),
			2,
		)

	def test_pwa_open_order_question_lists_every_open_order_with_details(self):
		Envio.objects.create(
			tipo='os',
			estado='tramite',
			cantidad_pedida=300,
			notas='PWA: cantidad expresada en unidades',
		)
		Envio.objects.create(
			tipo='backup',
			estado='tramite',
			cantidad_pedida=150,
			notas='PWA: cantidad expresada en unidades',
		)

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': '¿Hay pedidos abiertos?'},
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		reply = response.json()['reply'].lower()
		self.assertIn('obra social', reply)
		self.assertIn('backup', reply)
		self.assertIn('300 unidades', reply)
		self.assertIn('150 unidades', reply)
		self.assertIn('inicio:', reply)
		self.assertIn('notas:', reply)

	def test_pwa_asks_which_type_to_close_when_multiple_orders_are_open(self):
		insumo = Insumo.objects.create(
			nombre='Sondas',
			stock_actual_cajas=1,
			unidades_por_caja=30,
			backup_unidades=0,
		)
		Envio.objects.create(tipo='os', estado='tramite', cantidad_pedida=60)
		Envio.objects.create(tipo='backup', estado='tramite', cantidad_pedida=30)

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'ya recibí el pedido'},
			secure=True,
		)
		self.assertIn('más de un tipo', response.json()['reply'].lower())

		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'backup'},
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertIn('cerrado', response.json()['reply'].lower())
		self.assertTrue(Envio.objects.filter(tipo='os', estado='tramite').exists())
		self.assertTrue(Envio.objects.filter(tipo='backup', estado='recibido').exists())
		insumo.refresh_from_db()
		self.assertEqual(insumo.stock_actual_cajas, 1)
		self.assertEqual(insumo.backup_unidades, 30)

	def test_push_subscription_is_saved_for_this_pwa_session(self):
		subscription = {
			'endpoint': 'https://push.example.test/device-1',
			'keys': {'p256dh': 'test-key', 'auth': 'test-auth'},
		}
		response = self.client.post(
			reverse('astrana_pwa_subscribe'),
			data={
				'subscription': subscription,
			},
			content_type='application/json',
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.json()['subscribed'])
		chat_id = f'pwa-{self.client.session.session_key}'
		self.assertTrue(MemoriaAstrana.objects.filter(
			chat_id=chat_id,
			categoria='contexto',
			clave__startswith='push_sub_',
			activa=True,
		).exists())

	def test_push_probe_reports_missing_subscription_for_current_device(self):
		response = self.client.post(
			reverse('astrana_pwa_probar_push'),
			secure=True,
		)

		self.assertEqual(response.status_code, 404)
		self.assertEqual(response.json()['status'], 'error')
		self.assertIn('este dispositivo', response.json()['mensaje'].lower())

	def test_push_probe_targets_the_saved_current_device(self):
		subscription = {
			'endpoint': 'https://push.example.test/device-2',
			'keys': {'p256dh': 'test-key', 'auth': 'test-auth'},
		}
		self.client.post(
			reverse('astrana_pwa_subscribe'),
			data={'subscription': subscription},
			content_type='application/json',
			secure=True,
		)
		expected_chat_id = f'pwa-{self.client.session.session_key}'

		with patch('Astrana.webpush_utils.enviar_webpush_recordatorio', return_value=1) as send_push:
			response = self.client.post(reverse('astrana_pwa_probar_push'), secure=True)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['status'], 'success')
		self.assertEqual(send_push.call_args.kwargs['chat_id'], expected_chat_id)

	def test_push_probe_distinguishes_delivery_failure_from_missing_subscription(self):
		subscription = {
			'endpoint': 'https://push.example.test/device-3',
			'keys': {'p256dh': 'test-key', 'auth': 'test-auth'},
		}
		self.client.post(
			reverse('astrana_pwa_subscribe'),
			data={'subscription': subscription},
			content_type='application/json',
			secure=True,
		)

		def fallo_de_entrega(*args, errores=None, **kwargs):
			errores.append('El proveedor rechazó la autenticación VAPID (HTTP 401/403).')
			return 0

		with patch('Astrana.webpush_utils.enviar_webpush_recordatorio', side_effect=fallo_de_entrega):
			response = self.client.post(reverse('astrana_pwa_probar_push'), secure=True)

		self.assertEqual(response.status_code, 502)
		self.assertIn('401/403', response.json()['mensaje'])

	def test_vapid_endpoint_returns_only_a_validated_public_key(self):
		response = self.client.get(reverse('astrana_vapid_public_key'), secure=True)

		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.json()['public_key'])
		self.assertEqual(response.json()['public_key'], response.json()['publicKey'])

	def test_vapid_private_key_accepts_quoted_escaped_crlf_pem(self):
		generated_key = ec.generate_private_key(ec.SECP256R1())
		pem = generated_key.private_bytes(
			Encoding.PEM,
			PrivateFormat.PKCS8,
			NoEncryption(),
		).decode('utf-8')
		render_value = '"' + pem.replace('\n', '\\r\\n') + '"'

		_, loaded_key = _cargar_clave_privada_vapid(render_value)

		self.assertEqual(
			loaded_key.public_key().public_numbers(),
			generated_key.public_key().public_numbers(),
		)

	def test_vapid_private_key_accepts_hex_and_base64_encoded_pem(self):
		generated_key = ec.generate_private_key(ec.SECP256R1())
		raw_key = generated_key.private_numbers().private_value.to_bytes(32, 'big')
		pem = generated_key.private_bytes(
			Encoding.PEM,
			PrivateFormat.PKCS8,
			NoEncryption(),
		)
		values = [
			raw_key.hex(),
			'0x' + raw_key.hex(),
			base64.urlsafe_b64encode(pem).decode('ascii').rstrip('='),
		]

		for value in values:
			with self.subTest(format=value[:2]):
				_, loaded_key = _cargar_clave_privada_vapid(value)
				self.assertEqual(
					loaded_key.public_key().public_numbers(),
					generated_key.public_key().public_numbers(),
				)


class FcmScheduledNotificationsTests(TestCase):
	def setUp(self):
		MemoriaAstrana.objects.create(
			chat_id='pwa-device-1',
			categoria='contexto',
			clave='fcm_token',
			valor='test-fcm-token',
			activa=True,
			confirmada=True,
		)

	@patch('medicine_control.views._enviar_push_fcm')
	def test_medication_reminder_waits_for_schedule_and_sends_once_per_day(self, send_push):
		Pastillero.objects.create(nombre='Medicación diaria', cantidad_total=5)
		MemoriaAstrana.objects.create(
			chat_id='pwa-old-session',
			categoria='contexto',
			clave='fcm_token',
			valor='test-fcm-token',
			activa=True,
			confirmada=True,
		)
		zone = ZoneInfo('America/Argentina/Buenos_Aires')

		before_schedule = datetime(2026, 10, 5, 7, 14, tzinfo=zone)
		scheduled_time = datetime(2026, 10, 5, 7, 20, tzinfo=zone)
		self.assertEqual(medicine_views._notificar_recordatorios_pastillero(before_schedule), 0)
		self.assertEqual(medicine_views._notificar_recordatorios_pastillero(scheduled_time), 1)
		self.assertEqual(medicine_views._notificar_recordatorios_pastillero(scheduled_time), 0)
		self.assertEqual(send_push.call_count, 1)
		self.assertEqual(send_push.call_args.args[0], 'test-fcm-token')

	def test_fcm_payload_uses_data_only_to_prevent_browser_auto_display_duplicates(self):
		with patch('medicine_control.views._inicializar_firebase_admin'), patch(
			'firebase_admin.messaging.send'
		) as firebase_send:
			medicine_views._enviar_push_fcm(
				'test-fcm-token',
				'Recordatorio',
				'Tomá tu medicación.',
				'/pastillero/',
			)

		message = firebase_send.call_args.args[0]
		self.assertIsNone(message.notification)
		self.assertEqual(message.data, {
			'title': 'Recordatorio',
			'body': 'Tomá tu medicación.',
			'url': '/pastillero/',
		})

	@patch('medicine_control.views._enviar_push_fcm')
	def test_pwa_custom_reminder_time_overrides_default_for_today_only(self, send_push):
		Pastillero.objects.create(nombre='Medicación diaria', cantidad_total=5)
		MemoriaAstrana.objects.create(
			chat_id='principal',
			categoria='preferencia',
			clave='horario_recordatorio_hoy',
			valor='14:30:2026-10-05',
			activa=True,
			confirmada=True,
		)
		zone = ZoneInfo('America/Argentina/Buenos_Aires')

		self.assertEqual(
			medicine_views._notificar_recordatorios_pastillero(datetime(2026, 10, 5, 7, 20, tzinfo=zone)),
			0,
		)
		self.assertEqual(
			medicine_views._notificar_recordatorios_pastillero(datetime(2026, 10, 5, 14, 35, tzinfo=zone)),
			1,
		)
		self.assertEqual(
			medicine_views._notificar_recordatorios_pastillero(datetime(2026, 10, 6, 7, 20, tzinfo=zone)),
			1,
		)
		self.assertEqual(send_push.call_count, 2)

	def test_pwa_chat_saves_custom_reminder_time_for_today(self):
		response = self.client.post(
			reverse('astrana_chat_api'),
			{'message': 'recordame a las 14:30'},
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertIn('hoy', response.json()['reply'].lower())
		self.assertTrue(MemoriaAstrana.objects.filter(
			chat_id='principal',
			categoria='preferencia',
			clave='horario_recordatorio_hoy',
			valor__endswith=f':{timezone.localdate()}',
		).exists())

	@patch('medicine_control.views._enviar_push_fcm')
	def test_medication_specific_time_overrides_default_and_sends_once(self, send_push):
		Pastillero.objects.create(
			nombre='Enalapril',
			cantidad_total=12,
			hora_recordatorio=time(9, 30),
		)
		zone = ZoneInfo('America/Argentina/Buenos_Aires')

		self.assertEqual(
			medicine_views._notificar_recordatorios_pastillero(datetime(2026, 10, 5, 9, 25, tzinfo=zone)),
			0,
		)
		self.assertEqual(
			medicine_views._notificar_recordatorios_pastillero(datetime(2026, 10, 5, 9, 35, tzinfo=zone)),
			1,
		)
		self.assertEqual(
			medicine_views._notificar_recordatorios_pastillero(datetime(2026, 10, 5, 9, 40, tzinfo=zone)),
			0,
		)
		self.assertEqual(send_push.call_count, 1)

	@patch('medicine_control.views._enviar_push_fcm')
	def test_low_normal_stock_alert_sends_once_until_stock_recovers(self, send_push):
		insumo = Insumo.objects.create(
			nombre='Sondas',
			stock_actual_cajas=4,
			unidades_por_caja=30,
			consumo_diario=8,
		)

		self.assertEqual(medicine_views._notificar_stock_bajo_sondas(), 1)
		self.assertEqual(medicine_views._notificar_stock_bajo_sondas(), 0)
		insumo.stock_actual_cajas = 5
		insumo.save(update_fields=['stock_actual_cajas'])
		self.assertEqual(medicine_views._notificar_stock_bajo_sondas(), 0)
		insumo.stock_actual_cajas = 4
		insumo.save(update_fields=['stock_actual_cajas'])
		self.assertEqual(medicine_views._notificar_stock_bajo_sondas(), 1)
		self.assertEqual(send_push.call_count, 2)

	@patch('medicine_control.views._enviar_push_fcm')
	def test_pastillero_low_stock_alert_sends_once_until_refilled_above_threshold(self, send_push):
		medicamento = Pastillero.objects.create(nombre='Enalapril', cantidad_total=5)

		self.assertEqual(medicine_views._notificar_stock_bajo_pastillero(), 1)
		self.assertEqual(medicine_views._notificar_stock_bajo_pastillero(), 0)
		medicamento.cantidad_total = 6
		medicamento.save(update_fields=['cantidad_total'])
		self.assertEqual(medicine_views._notificar_stock_bajo_pastillero(), 0)
		medicamento.cantidad_total = 5
		medicamento.save(update_fields=['cantidad_total'])
		self.assertEqual(medicine_views._notificar_stock_bajo_pastillero(), 1)
		self.assertEqual(send_push.call_count, 2)


class PastilleroManagementTests(TestCase):
	def setUp(self):
		self.client.defaults['wsgi.url_scheme'] = 'https'

	def test_refill_adds_stock_and_records_the_restock(self):
		medicamento = Pastillero.objects.create(nombre='Enalapril', cantidad_total=4)

		response = self.client.post(reverse('pastillero'), {
			'accion': 'recargar',
			'medicamento': medicamento.id,
			'cantidad': 20,
		}, secure=True)

		medicamento.refresh_from_db()
		self.assertEqual(response.status_code, 302)
		self.assertEqual(medicamento.cantidad_total, 24)
		self.assertTrue(IngresoPastillero.objects.filter(medicamento=medicamento, cantidad=20).exists())

	def test_medication_reminder_time_can_be_configured(self):
		medicamento = Pastillero.objects.create(nombre='Enalapril', cantidad_total=12)

		response = self.client.post(reverse('pastillero'), {
			'accion': 'configurar_horario',
			'medicamento': medicamento.id,
			'hora_recordatorio': '08:45',
		}, secure=True)

		medicamento.refresh_from_db()
		self.assertEqual(response.status_code, 302)
		self.assertEqual(medicamento.hora_recordatorio, time(8, 45))

	def test_pastillero_card_shows_saved_reminder_time(self):
		Pastillero.objects.create(
			nombre='Enalapril',
			cantidad_total=12,
			hora_recordatorio=time(8, 45),
		)

		response = self.client.get(reverse('pastillero'), secure=True)

		self.assertContains(response, '08:45')

	def test_astrana_chat_answers_medication_reminder_time(self):
		Pastillero.objects.create(
			nombre='Enalapril',
			cantidad_total=12,
			hora_recordatorio=time(8, 45),
		)

		response = self.client.post(reverse('astrana_chat_api'), {
			'message': '¿A qué hora tengo que tomar Enalapril?'
		}, secure=True)

		self.assertEqual(response.status_code, 200)
		self.assertIn('08:45', response.json()['reply'])

	def test_pwa_chat_refills_existing_medication_by_name(self):
		medicamento = Pastillero.objects.create(nombre='Enalapril', cantidad_total=4)

		response = self.client.post(reverse('astrana_chat_api'), {
			'message': 'recargar Enalapril con 20 pastillas'
		}, secure=True)

		medicamento.refresh_from_db()
		self.assertEqual(response.status_code, 200)
		self.assertIn('24', response.json()['reply'])
		self.assertEqual(medicamento.cantidad_total, 24)
		self.assertTrue(IngresoPastillero.objects.filter(medicamento=medicamento, cantidad=20).exists())

	def test_pwa_chat_sets_recurring_time_for_named_medication(self):
		medicamento = Pastillero.objects.create(nombre='Enalapril', cantidad_total=12)

		response = self.client.post(reverse('astrana_chat_api'), {
			'message': 'recordame tomar Enalapril todos los días a las 09:30'
		}, secure=True)

		medicamento.refresh_from_db()
		self.assertEqual(response.status_code, 200)
		self.assertIn('todos los días', response.json()['reply'].lower())
		self.assertEqual(medicamento.hora_recordatorio, time(9, 30))


class ElevenLabsVoiceTests(TestCase):
	@patch.dict('os.environ', {'ELEVENLABS_API_KEY': 'test-key'})
	@patch('elevenlabs.ElevenLabs')
	def test_speech_engine_token_endpoint_uses_saved_engine_id(self, elevenlabs_client):
		MemoriaAstrana.objects.create(
			chat_id='principal',
			categoria='preferencia',
			clave='elevenlabs_speech_engine_id',
			valor='seng_test_engine',
			activa=True,
			confirmada=True,
		)
		client = elevenlabs_client.return_value
		client.conversational_ai.conversations.get_webrtc_token.return_value = Mock(token='short-token')

		response = self.client.post(reverse('astrana_voice_token'), secure=True)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {'token': 'short-token'})
		client.conversational_ai.conversations.get_webrtc_token.assert_called_once_with(
			agent_id='seng_test_engine',
		)

	def test_speech_engine_token_endpoint_reports_when_engine_is_not_ready(self):
		response = self.client.post(reverse('astrana_voice_token'), secure=True)

		self.assertEqual(response.status_code, 503)
		self.assertIn('iniciando', response.json()['error'])

	@patch.dict('os.environ', {
		'ELEVENLABS_API_KEY': 'test-key',
		'ELEVENLABS_VOICE_ID': 'mPteaOsPT4FrQ0lJIVEm',
	})
	@patch('medicine_control.views.requests.post')
	def test_tts_endpoint_returns_mp3_from_selected_voice(self, post):
		post.return_value = Mock(status_code=200, content=b'fake-mp3')
		response = self.client.post(
			reverse('astrana_voz_api'),
			data={'text': 'Hola, soy Astrana.'},
			content_type='application/json',
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response['Content-Type'], 'audio/mpeg')
		self.assertEqual(response.content, b'fake-mp3')
		self.assertIn('/mPteaOsPT4FrQ0lJIVEm', post.call_args.args[0])

	@patch.dict('os.environ', {'ELEVENLABS_API_KEY': 'test-key'})
	@patch('medicine_control.views.requests.post')
	def test_tts_endpoint_exposes_elevenlabs_rejection_reason(self, post):
		post.return_value = Mock(
			status_code=401,
			json=lambda: {'detail': {'message': 'Invalid API key'}},
		)
		response = self.client.post(
			reverse('astrana_voz_api'),
			data={'text': 'Hola, soy Astrana.'},
			content_type='application/json',
			secure=True,
		)

		self.assertEqual(response.status_code, 502)
		self.assertIn('HTTP 401', response.json()['error'])
		self.assertIn('Invalid API key', response.json()['error'])

	@patch.dict('os.environ', {'ELEVENLABS_API_KEY': 'test-key'})
	@patch('medicine_control.views.requests.post')
	def test_voice_note_is_transcribed_then_processed_as_pwa_chat(self, post):
		post.return_value = Mock(
			status_code=200,
			json=lambda: {'text': 'hola'},
		)
		audio = SimpleUploadedFile('voice.webm', b'audio-bytes', content_type='audio/webm')

		response = self.client.post(
			reverse('astrana_chat_api'),
			data={'audio': audio},
			secure=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['transcription'], 'hola')
		self.assertIn('Hola Joaco', response.json()['reply'])
		self.assertEqual(post.call_args.kwargs['data']['model_id'], 'scribe_v2')
