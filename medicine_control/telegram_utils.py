import requests

def enviar_alerta(mensaje):
    token = '8701141296:AAGjRcTHOXA5bBoa3IYaa3boKB78scx0g_Y'
    chat_id = '8034926015'
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    data = {
        'chat_id': chat_id,
        'text': mensaje,
        'parse_mode': 'Markdown'
    }
    
    try:
        # Usamos post para enviar los datos de forma segura
        requests.post(url, data=data)
    except Exception as e:
        print(f"Error enviando a Telegram: {e}")