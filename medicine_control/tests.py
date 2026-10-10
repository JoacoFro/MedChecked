from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch
import base64
from datetime import datetime, time
from unittest.mock import Mock
from zoneinfo import ZoneInfo
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
from django.core.files.uploadedfile import SimpleUploadedFile

from medicine_control import views as medicine_views
from medicine_control.models import IngresoPastillero, Insumo, MemoriaAstrana, Pastillero
from Astrana.webpush_utils import _cargar_clave_privada_vapid


class AstranaPwaTests(TestCase):
	def setUp(self):
		self.client.defaults['wsgi.url_scheme'] = 'https'

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
		zone = ZoneInfo('America/Argentina/Buenos_Aires')

		before_schedule = datetime(2026, 10, 5, 7, 14, tzinfo=zone)
		scheduled_time = datetime(2026, 10, 5, 7, 20, tzinfo=zone)
		self.assertEqual(medicine_views._notificar_recordatorios_pastillero(before_schedule), 0)
		self.assertEqual(medicine_views._notificar_recordatorios_pastillero(scheduled_time), 1)
		self.assertEqual(medicine_views._notificar_recordatorios_pastillero(scheduled_time), 0)
		self.assertEqual(send_push.call_count, 1)
		self.assertEqual(send_push.call_args.args[0], 'test-fcm-token')

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

