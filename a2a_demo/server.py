"""One executable A2A server; run separately for each of the three roles."""
import argparse
import os

import uvicorn
from a2a.helpers import (
    get_data_parts,
    new_data_part,
    new_task_from_user_message,
    new_text_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
)
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_demo.domain import (
    ROOT,
    CheckRequest,
    ResourceRequest,
    StudyRequest,
    check_plan,
    find_resources,
)
from a2a_demo.transport import NAMES, ROLES, AgentFailure, endpoint
from a2a_demo.workflow import run_planner

DESCRIPTIONS = {
    'planner': 'Creates a schedule, requests curated resources, and coordinates independent checking and repair.',
    'resources': 'Assembles topic-specific resources from a curated catalog within the supplied study budgets.',
    'checker': 'Independently checks topic coverage, time limits, and resource constraints.',
}


def agent_card(role: str) -> AgentCard:
    return AgentCard(name=NAMES[role], description=DESCRIPTIONS[role], version='1.0.0',
        default_input_modes=['application/json'], default_output_modes=['application/json'],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(url=endpoint(role), protocol_binding='JSONRPC', protocol_version='1.0')],
        skills=[AgentSkill(id=role, name=NAMES[role], description=DESCRIPTIONS[role], tags=['study', 'a2a', role])])


class StudyExecutor(AgentExecutor):
    def __init__(self, role):
        self.role = role

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.update_status(TaskState.TASK_STATE_WORKING, message=new_text_message('Working'))
        print(f'[{NAMES[self.role]}] Received A2A task={task.id}', flush=True)
        try:
            parts = get_data_parts(context.message.parts)
            if len(parts) != 1 or not isinstance(parts[0], dict):
                raise ValueError('Send exactly one JSON data part using the documented request contract.')
            payload = parts[0]
            if self.role == 'planner':
                result = await run_planner(StudyRequest.model_validate(payload), task.context_id)
            elif self.role == 'resources':
                resources = find_resources(ResourceRequest.model_validate(payload))
                result = {'resources': [r.model_dump() for r in resources]}
                print(f'[Resource Finder] Selected {len(resources)} curated resources.', flush=True)
            else:
                result = check_plan(CheckRequest.model_validate(payload))
                print('[Schedule Checker] ' + ('APPROVED' if result['approved'] else '; '.join(result['issues'])), flush=True)
            await updater.add_artifact(parts=[new_data_part({'ok': True, 'data': result}, 'application/json')], name='result')
            await updater.update_status(TaskState.TASK_STATE_COMPLETED)
        except Exception as exc:  # noqa: BLE001
            # No raw provider payloads or credentials enter artifacts or console logs.
            message = str(exc)[:1600] if isinstance(exc, (ValueError, AgentFailure)) else f'{NAMES[self.role]} failed ({type(exc).__name__}). Check that all agents are running.'
            await updater.add_artifact(parts=[new_data_part({'ok': False, 'error': message}, 'application/json')], name='error')
            await updater.update_status(TaskState.TASK_STATE_FAILED, message=new_text_message(message))
            print(f'[{NAMES[self.role]}] FAILED: {message}', flush=True)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id, context_id=context.context_id)
        await updater.update_status(TaskState.TASK_STATE_CANCELED)


def create_app(role: str) -> Starlette:
    card = agent_card(role)
    handler = DefaultRequestHandler(agent_executor=StudyExecutor(role), task_store=InMemoryTaskStore(), agent_card=card)

    async def health(request):
        return JSONResponse({'status': 'ok', 'agent': role})

    return Starlette(routes=[Route('/health', health), *create_agent_card_routes(card), *create_jsonrpc_routes(handler, '/')])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=ROLES)
    parser.add_argument('--base-port', type=int, default=8000)
    args = parser.parse_args()
    os.environ['A2A_BASE_PORT'] = str(args.base_port)
    load_dotenv(ROOT / '.env')
    app = create_app(args.role)
    print(f'{NAMES[args.role]} ready at {endpoint(args.role)}', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=args.base_port + ROLES[args.role], log_level='warning', access_log=False)


if __name__ == '__main__':
    main()
