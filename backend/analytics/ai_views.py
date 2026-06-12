from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


def answer_question(question, history, user):
    return {
        'reply': 'Litrix AI is not configured yet.',
        'sources': [],
    }


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def chat(request):
    question = (request.data.get('message') or '').strip()
    if not question:
        return Response({'error': 'message is required'}, status=400)

    history = request.data.get('history') or []
    result = answer_question(question, history, request.user)
    return Response(result)
