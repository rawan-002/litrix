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
import logging
import os
import time

import requests
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .ai_tools import TOOLS

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv('GROQ_API_KEY')
GROQ_MODEL = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')
GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'


def _system_prompt():
    # The model has no real clock - without this it silently falls back to
    # a guess from its training data (observed: Llama 3.3 defaulted to
    # 2024 for "this year", which quietly pulled every "current"/"recent"
    # question two years into the past). Rebuilt per-call, not a module
    # constant, so a long-running worker process doesn't go stale at
    # midnight on Dec 31.
    from datetime import datetime
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    year = now.year
    return (
        f"You are Litrix AI, a research-analytics assistant for Al-Baha "
        f"University's College of Computing & IT. Today's date is {today}. "
        f"Spelled out exactly so there's no ambiguity: 'this year' or 'this "
        f"year's papers' means you MUST call get_overview_stats with "
        f"years=[{year}] (or get_publication_trend for a range) - calling a "
        f"tool with no year filter answers 'all time', not 'this year', and "
        f"those are different numbers. 'Last N years' means "
        f"years=[{year - 4}, {year - 3}, {year - 2}, {year - 1}, {year}] for "
        f"N=5 (adjust the count for other N). Never guess a year from your "
        f"training data. Answer using ONLY the tools "
        f"provided - never invent a number. Call a tool ONLY when the user's "
        f"LATEST message actually asks for data (paper counts, citations, top "
        f"researchers, department stats, publication trends), and call the "
        f"SINGLE most relevant tool for that specific question - do not call "
        f"additional unrelated tools just because they exist. The instant "
        f"the result answers the question, respond with that answer; do not "
        f"keep calling more tools 'for completeness' or pad the reply with "
        f"unrelated numbers the user didn't ask for. For a greeting, "
        f"thanks, small talk, or anything not asking for new data, reply "
        f"normally in plain text WITHOUT calling a tool, even if the "
        f"conversation history above mentions data - a short reply like 'hi' "
        f"is not a request to repeat or re-fetch the previous answer. ALWAYS "
        f"state the actual number/answer first, as a direct sentence - never "
        f"skip it. Only after that, if the tool result includes a 'scope' "
        f"note (e.g. affiliation policy), add ONE short second sentence for "
        f"it - the scope note is a footnote, never a replacement for the "
        f"answer itself. Keep answers concise. Reply in the same language as "
        f"the question."
    )

# Tool-call rounds are capped so a confused model can't loop forever burning
# free-tier quota - 3 is enough for any realistic multi-tool question.
MAX_TOOL_ROUNDS = 3
HISTORY_TURNS = 6  # most recent turns kept as context, oldest dropped first - most
# follow-up questions only need the last exchange or two; every extra turn is
# tokens spent on every future message in the conversation, not a one-time cost.


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


def _call_groq(messages, force_final=False):
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
            # 'none' on the last allowed round forces a text answer instead
            # of yet another tool call - smaller models (llama-3.1-8b) call
            # tools more liberally than needed, and without this, hitting
            # MAX_TOOL_ROUNDS just abandoned the conversation with no reply
            # at all, discarding every real tool result already gathered.
            'tool_choice': 'none' if force_final else 'auto',
            'temperature': 0.2,
            # Answers are meant to be a sentence or two (system prompt says
            # "keep answers concise") - capping output tokens enforces that
            # instead of just asking nicely, and directly reduces daily
            # token spend since output tokens count the same as input ones.
            'max_tokens': 400,
        },
        timeout=45,
    )
    resp.raise_for_status()
    return resp.json()


def _groq_error_code(exc):
    resp = getattr(exc, 'response', None)
    if resp is None:
        return None
    try:
        return resp.json().get('error', {}).get('code')
    except ValueError:
        return None


def _call_groq_with_retry(messages, attempts=2, force_final=False):
    """One retry before giving up. Logs every failure with the real
    exception - `except requests.RequestException` alone hid this entirely,
    showing only a generic "temporarily unavailable" with nothing in the
    logs to diagnose it by (this is how a real bug - see below - looked
    identical to a transient network blip until logging was added).

    Two distinct failure modes, handled differently:
    - Groq's own schema validation rejects a tool call (code='tool_use_failed',
      e.g. the model passed years="all" instead of omitting the parameter or
      passing an array) - retrying the IDENTICAL request fails identically,
      so this appends a corrective system nudge before retrying instead.
    - Anything else (timeout, connection error, 5xx) - a plain retry after a
      short pause, for Render free-tier's occasional outbound blips.
    """
    msgs = list(messages)
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return _call_groq(msgs, force_final=force_final)
        except requests.RequestException as e:
            last_exc = e
            code = _groq_error_code(e)
            logger.warning('Groq call failed (attempt %d/%d, code=%s): %s', attempt, attempts, code, e)
            if attempt >= attempts:
                break
            if code == 'tool_use_failed':
                msgs = msgs + [{
                    'role': 'system',
                    'content': (
                        "Your last tool call had invalid arguments and was "
                        "rejected. Re-check the tool's parameter schema and "
                        "call it again with valid arguments - e.g. omit an "
                        "optional array parameter entirely rather than "
                        "passing a string like \"all\"."
                    ),
                }]
            else:
                time.sleep(1.5)
    raise last_exc


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

    messages = [{'role': 'system', 'content': _system_prompt()}]
    for turn in (history or [])[-HISTORY_TURNS:]:
        role = 'assistant' if turn.get('role') == 'assistant' else 'user'
        messages.append({'role': role, 'content': turn.get('text', '')})
    messages.append({'role': 'user', 'content': question})

    try:
        choice = _call_groq_with_retry(messages)['choices'][0]['message']
        rounds = 0
        while choice.get('tool_calls') and rounds < MAX_TOOL_ROUNDS:
            messages.append(choice)
            for call in choice['tool_calls']:
                messages.append({
                    'role': 'tool',
                    'tool_call_id': call['id'],
                    'content': json.dumps(_run_tool_call(call)),
                })
            rounds += 1
            choice = _call_groq_with_retry(messages, force_final=(rounds >= MAX_TOOL_ROUNDS))['choices'][0]['message']
        reply = choice.get('content') or "I couldn't find an answer to that."
    except requests.RequestException as e:
        logger.exception('Litrix AI: Groq call failed after retry')
        if _groq_error_code(e) == 'rate_limit_exceeded':
            reply = "Litrix AI is getting a lot of questions right now (free-tier limit). Please try again in a minute."
        else:
            reply = 'Litrix AI is temporarily unavailable. Please try again shortly.'
    except (KeyError, IndexError, TypeError):
        logger.exception('Litrix AI: unexpected response shape from Groq')
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
