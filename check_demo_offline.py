import asyncio
import json
import httpx
from openai import AsyncOpenAI
from agents import Runner, RunConfig
import main

calls = []

def response(request):
    assert request.url.host == 'generativelanguage.googleapis.com'
    assert request.url.path == '/v1beta/openai/chat/completions'
    body = json.loads(request.content)
    assert body['model'] == main.DEFAULT_MODEL
    calls.append(body)
    if len(calls) == 1:
        assert body['tools'][0]['function']['name'] == 'get_topic_difficulty'
        message = {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call_demo', 'type': 'function', 'function': {
                'name': 'get_topic_difficulty', 'arguments': '{"subject":"calculus"}'}}
        ]}
        finish = 'tool_calls'
    else:
        tool_message = next(m for m in body['messages'] if m['role'] == 'tool')
        assert json.loads(tool_message['content'])['topics']['integrals'] == 'hard'
        message = {'role': 'assistant', 'content': 'Simulated study plan: give integrals more time.'}
        finish = 'stop'
    return httpx.Response(200, json={'id': 'mock-completion', 'object': 'chat.completion',
        'created': 0, 'model': main.DEFAULT_MODEL,
        'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})

async def check():
    client = AsyncOpenAI(api_key='offline-test-only', base_url=main.GEMINI_BASE_URL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)))
    original = main.AsyncOpenAI
    main.AsyncOpenAI = lambda **kwargs: client
    try:
        agent = main.create_agent('offline-test-only')
        result = await asyncio.wait_for(Runner.run(agent, main.SAMPLE, max_turns=3,
            run_config=RunConfig(tracing_disabled=True)), timeout=15)
        assert result.final_output == 'Simulated study plan: give integrals more time.'
        assert len(calls) == 2
        print('PASS: Gemini endpoint configuration, tool request, actual local tool execution, and final response handling.')
        print('All model responses were simulated. No external API request was made.')
    finally:
        main.AsyncOpenAI = original
        await client.close()

asyncio.run(check())
