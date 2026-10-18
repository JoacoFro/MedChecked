from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def ping(request):
    response = JsonResponse({'status': 'ok'})
    response['Cache-Control'] = 'no-store'
    return response
