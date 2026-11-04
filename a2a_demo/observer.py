"""Optional, best-effort event reporting for the local presentation view."""
import os

import httpx


async def emit(context_id: str, kind: str, agent: str, title: str,
    detail: str = '', target: str = '', payload: dict | None = None) -> None:
    url = os.getenv('A2A_OBSERVER_URL', '')
    if not url or not context_id:
        return
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            await client.post(f'{url}/api/runs/{context_id}/events', json={
                'kind': kind, 'agent': agent, 'target': target,
                'title': title, 'detail': detail, 'payload': payload or {},
            })
    except httpx.HTTPError:
        # Presentation instrumentation must never change the agent result.
        pass
