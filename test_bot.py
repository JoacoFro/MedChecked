import os
from telegram.ext import ApplicationBuilder

def main():
    print("🚀 PRUEBA DE ARRANQUE INICIADA")
    token = os.getenv("TELEGRAM_TOKEN")
    print(f"Token encontrado: {token[:5]}...")
    
    app = ApplicationBuilder().token(token).build()
    print("📡 Intentando conectar con Telegram...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()