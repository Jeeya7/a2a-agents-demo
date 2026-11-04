"""Local browser presentation of the real three-agent A2A workflow."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import httpx
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn

from a2a_demo.domain import StudyRequest, catalog
from a2a_demo.transport import AgentFailure, NAMES, ROLES, call_agent, endpoint
from run_demo import DemoCluster

WEB = Path(__file__).parent / 'a2a_demo' / 'web'


class Run:
    def __init__(self, request):
        self.id = str(uuid.uuid4())
        self.request = request
        self.events = []
        self.result = None
        self.state = 'running'
        self.changed = asyncio.Condition()
        self.task = None

    async def add(self, event):
        async with self.changed:
            event = {key: event[key] for key in ('kind', 'agent', 'target', 'title', 'detail', 'payload') if key in event}
            event.update(seq=len(self.events) + 1, timestamp=datetime.now(timezone.utc).isoformat())
            if event.get('kind') == 'completed':
                self.state = 'completed'
            elif event.get('kind') == 'error':
                self.state = 'failed'
            self.events.append(event)
            self.changed.notify_all()


def create_app(base_port=8100, ui_port=8080, start_cluster=True):
    runs = {}

    async def execute(run):
        try:
            await run.add({'kind': 'started', 'agent': 'student', 'target': 'planner',
                'title': 'Your study request is on its way',
                'detail': f'{run.request.days} days, {run.request.minutes_per_day} minutes per day.',
                'payload': {'request': run.request.model_dump()}})
            run.result = await call_agent('planner', run.request.model_dump(),
                context_id=run.id, base_port=base_port)
            await run.add({'kind': 'completed', 'agent': 'planner', 'title': 'Your study guide is ready',
                'detail': 'The checker has approved the schedule and materials.', 'payload': run.result})
        except asyncio.CancelledError:
            await run.add({'kind': 'error', 'agent': 'planner', 'title': 'Run stopped', 'detail': 'The local server is shutting down.'})
            raise
        except Exception as exc:
            message = str(exc)[:1600] if isinstance(exc, (AgentFailure, ValueError)) else f'Could not complete the run ({type(exc).__name__}). Check that the local agents are running.'
            await run.add({'kind': 'error', 'agent': 'planner', 'title': 'This run could not finish', 'detail': message})

    @asynccontextmanager
    async def lifespan(app):
        cluster = DemoCluster(base_port, quiet=True,
            environment={'A2A_OBSERVER_URL': f'http://127.0.0.1:{ui_port}'}) if start_cluster else None
        try:
            if cluster:
                await asyncio.to_thread(cluster.__enter__)
            yield
        finally:
            tasks = [r.task for r in runs.values() if r.task and not r.task.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if cluster:
                await asyncio.to_thread(cluster.stop)

    async def index(request):
        return FileResponse(WEB / 'index.html')

    async def config(request):
        return JSONResponse({'subjects': catalog(), 'agents': [
            {'id': role, 'name': NAMES[role], 'url': endpoint(role, base_port)} for role in ROLES]})

    async def card(request):
        role = request.path_params['role']
        if role not in ROLES:
            return JSONResponse({'error': 'Unknown agent'}, status_code=404)
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                response = await client.get(endpoint(role, base_port) + '/.well-known/agent-card.json')
                response.raise_for_status()
                return JSONResponse(response.json())
        except (httpx.HTTPError, ValueError):
            return JSONResponse({'error': 'Agent Card unavailable. Check that the agent is running.'}, status_code=503)

    async def start(request):
        if any(r.state == 'running' for r in runs.values()):
            return JSONResponse({'error': 'A run is already in progress.'}, status_code=409)
        raw = await request.body()
        if len(raw) > 16384:
            return JSONResponse({'error': 'Request is too large.'}, status_code=413)
        try:
            study_request = StudyRequest.model_validate_json(raw)
        except ValidationError as exc:
            return JSONResponse({'error': '; '.join(e['msg'] for e in exc.errors())}, status_code=400)
        if any(r.state == 'running' for r in runs.values()):
            return JSONResponse({'error': 'A run is already in progress.'}, status_code=409)
        while len(runs) >= 20:
            del runs[next(iter(runs))]
        run = Run(study_request)
        runs[run.id] = run
        run.task = asyncio.create_task(execute(run))
        return JSONResponse({'id': run.id, 'state': run.state}, status_code=202)

    async def collect(request):
        run = runs.get(request.path_params['run_id'])
        if not run:
            return JSONResponse({'error': 'Unknown run'}, status_code=404)
        raw = await request.body()
        if len(raw) > 65536:
            return JSONResponse({'error': 'Event is too large'}, status_code=413)
        try:
            event = json.loads(raw)
            if (not isinstance(event, dict)
                or event.get('kind') not in {'protocol', 'planning', 'draft', 'handoff', 'resources', 'review', 'revision'}
                or event.get('agent') not in ROLES
                or not isinstance(event.get('payload', {}), dict)):
                raise ValueError()
        except (ValueError, TypeError):
            return JSONResponse({'error': 'Invalid event'}, status_code=400)
        await run.add(event)
        return JSONResponse({'ok': True})

    async def stream(request):
        run = runs.get(request.path_params['run_id'])
        if not run:
            return JSONResponse({'error': 'Unknown run'}, status_code=404)

        async def events():
            cursor = 0
            while True:
                batch = []
                heartbeat = False
                async with run.changed:
                    if cursor == len(run.events) and run.state == 'running':
                        try:
                            await asyncio.wait_for(run.changed.wait(), timeout=10)
                        except asyncio.TimeoutError:
                            heartbeat = True
                    batch = run.events[cursor:]
                    cursor += len(batch)
                    finished = run.state != 'running' and cursor == len(run.events)
                for event in batch:
                    yield 'data: ' + json.dumps(event) + '\n\n'
                if finished:
                    yield 'event: end\ndata: {}\n\n'
                    break
                if heartbeat:
                    yield ': heartbeat\n\n'
                if await request.is_disconnected():
                    break
        return StreamingResponse(events(), media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    app = Starlette(lifespan=lifespan, routes=[Route('/', index), Route('/api/config', config),
        Route('/api/agents/{role}/card', card), Route('/api/runs', start, methods=['POST']),
        Route('/api/runs/{run_id}/events', collect, methods=['POST']),
        Route('/api/runs/{run_id}/stream', stream), Mount('/assets', StaticFiles(directory=WEB))])
    app.state.runs = runs
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--base-port', type=int, default=8100)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 1024 <= args.base_port <= 65533:
        parser.error('Use valid unprivileged ports.')
    if args.port in range(args.base_port, args.base_port + 3):
        parser.error('The browser port must differ from all three agent ports.')
    print(f'Open http://127.0.0.1:{args.port} for the visual A2A demo.', flush=True)
    uvicorn.run(create_app(args.base_port, args.port), host='127.0.0.1', port=args.port,
        log_level='warning', access_log=False)
