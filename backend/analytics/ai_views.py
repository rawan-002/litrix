"""Litrix AI chat: an open-source model (served free by Groq, not a paid
Anthropic/OpenAI account) grounded in Litrix's own data via tool/function
calling. The model never invents numbers - every data question is answered
by calling one of ai_tools.TOOLS, which runs a real SQL query against the
same tables (and the same AffiliationVerified policy) the dashboard uses.

Without GROQ_API_KEY set, chat degrades to the original stub reply - this
lets the frontend ship independently of whether the key has been configured
yet (see .env.example).
"""
import json
import os

import requests
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .ai_tools import TOOLS

GROQ_API_KEY = os.getenv('GROQ_API_KEY')
GROQ_MODEL = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')
GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'

SYSTEM_PROMPT = (
    "You are Litrix AI, a research-analytics assistant for Al-Baha "
    "University's College of Computing & IT. Answer using ONLY the tools "
    "provided - never invent a number. Call a tool whenever the question "
    "needs real data (paper counts, citations, top researchers, department "
    "stats, publication trends). If a tool result includes a 'scope' note "
    "(e.g. affiliation policy), mention it when it materially affects the "
    "answer. Keep answers concise. Reply in the same language as the question."
)

# Tool-call rounds are capped so a confused model can't loop forever burning
# free-tier quota - 3 is enough for any realistic multi-tool question.
MAX_TOOL_ROUNDS = 3
HISTORY_TURNS = 10  # most recent turns kept as context, oldest dropped first


def _tool_schema():
    return [
        {
            'type': 'function',
            'function': {
                'name': name,
                'description': spec['description'],
                'parameters': spec['parameters'],
            },
        }
        for name, spec in TOOLS.items()
    ]


def _call_groq(messages):
    resp = requests.post(
        GROQ_URL,
        headers={
            'Authorization': f'Bearer {GROQ_API_KEY}',
            'Content-Type': 'application/json',
        },
        json={
            'model': GROQ_MODEL,
            'messages': messages,
            'tools': _tool_schema(),
            'tool_choice': 'auto',
            'temperature': 0.2,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _run_tool_call(call):
    name = call['function']['name']
    try:
        args = json.loads(call['function'].get('arguments') or '{}')
    except json.JSONDecodeError:
        args = {}
    # A tool with no parameters sometimes gets called with the JSON literal
    # "null" instead of "{}" - json.loads('null') is a valid parse to None,
    # and **None raises TypeError, so this isn't caught by the except above.
    if not isinstance(args, dict):
        args = {}
    spec = TOOLS.get(name)
    if not spec:
        return {'error': f'unknown tool: {name}'}
    try:
        return spec['fn'](**args)
    except TypeError as e:
        return {'error': f'bad arguments for {name}: {e}'}


def answer_question(question, history, user):
    if not GROQ_API_KEY:
        return {
            'reply': 'Litrix AI is not configured yet.',
            'sources': [],
        }

    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    for turn in (history or [])[-HISTORY_TURNS:]:
        role = 'assistant' if turn.get('role') == 'assistant' else 'user'
        messages.append({'role': role, 'content': turn.get('text', '')})
    messages.append({'role': 'user', 'content': question})

    try:
        choice = _call_groq(messages)['choices'][0]['message']
        rounds = 0
        while choice.get('tool_calls') and rounds < MAX_TOOL_ROUNDS:
            messages.append(choice)
            for call in choice['tool_calls']:
                messages.append({
                    'role': 'tool',
                    'tool_call_id': call['id'],
                    'content': json.dumps(_run_tool_call(call)),
                })
            choice = _call_groq(messages)['choices'][0]['message']
            rounds += 1
        reply = choice.get('content') or "I couldn't find an answer to that."
    except requests.RequestException:
        reply = 'Litrix AI is temporarily unavailable. Please try again shortly.'
    except (KeyError, IndexError, TypeError):
        reply = 'Something went wrong answering that. Please rephrase your question.'

    return {'reply': reply, 'sources': []}


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def chat(request):
    question = (request.data.get('message') or '').strip()
    if not question:
        return Response({'error': 'message is required'}, status=400)

    history = request.data.get('history') or []
    result = answer_question(question, history, request.user)
    return Response(result)
