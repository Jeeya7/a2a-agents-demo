"""Actual A2A discovery and JSON-RPC calls over localhost HTTP."""
import os

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_data_parts, new_data_message
from a2a.types import Role, SendMessageRequest, TaskState

from a2a_demo.observer import emit

ROLES = {'planner': 0, 'resources': 1, 'checker': 2}
NAMES = {'planner': 'Study Planner', 'resources': 'Resource Finder', 'checker': 'Schedule Checker'}


class AgentFailure(RuntimeError):
    """A safe, user-readable failure from an agent."""


def endpoint(role: str, base_port: int | None = None) -> str:
    base = base_port if base_port is not None else int(os.getenv('A2A_BASE_PORT', '8000'))
    return f'http://127.0.0.1:{base + ROLES[role]}'


async def call_agent(role: str, payload: dict, context_id: str = '', base_port: int | None = None) -> dict:
    url = endpoint(role, base_port)
    async with httpx.AsyncClient(timeout=100, trust_env=False) as http:
        card = await A2ACardResolver(httpx_client=http, base_url=url).get_agent_card()
        await emit(context_id, 'protocol', role, 'Agent Card discovered', card.name,
            payload={'endpoint': url, 'card_url': url + '/.well-known/agent-card.json'})
        print(f'[A2A discovery] {card.name}: {url}/.well-known/agent-card.json', flush=True)
        client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=http))
        try:
            message = new_data_message(payload, media_type='application/json',
                role=Role.ROLE_USER, context_id=context_id or None)
            print(f'[A2A send] {card.name} message={message.message_id}', flush=True)
            await emit(context_id, 'protocol', role, 'A2A message sent',
                payload={'message_id': message.message_id, 'endpoint': url, 'protocol': 'JSON-RPC / A2A 1.0'})
            async for response in client.send_message(SendMessageRequest(message=message)):
                if not response.HasField('task'):
                    raise AgentFailure(f'{card.name} returned an unexpected response type.')
                task = response.task
                await emit(context_id, 'protocol', role, 'A2A task returned',
                    payload={'task_id': task.id, 'state': TaskState.Name(task.status.state),
                        'artifact_count': len(task.artifacts)})
                outputs = [item for artifact in task.artifacts for item in get_data_parts(artifact.parts)]
                if not outputs:
                    raise AgentFailure(f'{card.name} returned no result artifact.')
                output = outputs[-1]
                print(f'[A2A result] {card.name} task={task.id} state={TaskState.Name(task.status.state)}', flush=True)
                if task.status.state != TaskState.TASK_STATE_COMPLETED or not output.get('ok'):
                    raise AgentFailure(output.get('error', f'{card.name} failed.'))
                return output['data']
            raise AgentFailure(f'{card.name} returned no response.')
        finally:
            await client.close()
