from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

from medicine_control.models import MemoriaAstrana
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
