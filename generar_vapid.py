import base64
from py_vapid import Vapid

vapid = Vapid()
vapid.generate_keys()

# Obtener bytes puros en formato Uncompressed Point (P-256)
pub_bytes = vapid.public_key.public_bytes(
    encoding=vapid.public_key.key_size,
    format=vapid.public_key.format
) if hasattr(vapid.public_key, 'public_bytes') else None

# Convertir a Base64 URL-Safe sin relleno '='
public_b64 = base64.urlsafe_b64encode(
    vapid.public_key.to_string() if hasattr(vapid.public_key, 'to_string') else pub_bytes
).decode('utf-8').rstrip('=')

# Obtener clave privada PEM
private_pem = vapid.private_key_pem.decode('utf-8')

print("=== COPIA ESTOS VALORES EN RENDER ===")
print("VAPID_PUBLIC_KEY:")
print(public_b64)
print("\nVAPID_PRIVATE_KEY:")
print(private_pem)