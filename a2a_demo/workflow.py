"""Planner coordination, Gemini scheduling, and bounded checker repair."""
import os

from a2a_demo.domain import (
    CheckRequest,
    Resource,
    ResourceRequest,
    Session,
    StudyPlan,
    StudyRequest,
    build_plan,
    catalog,
)
from a2a_demo.observer import emit
from a2a_demo.transport import AgentFailure, call_agent


def quota_description(exc) -> str:
    body = getattr(exc, 'body', {})
    if isinstance(body, list) and body:
        body = body[0]
    error = body.get('error', body) if isinstance(body, dict) else {}
    for detail in error.get('details', []):
        metadata = detail.get('metadata', {})
        if metadata.get('quota_limit_value') == '0':
            return 'Gemini reports a regional quota of zero. Use --mode offline or resolve the project quota in AI Studio.'
    return 'Gemini rate/quota limit reached. Use --mode offline or check the model quota in AI Studio.'


async def gemini_plan(request: StudyRequest) -> StudyPlan:
    # Only this explicit mode imports the model SDK or accesses an API key.
    from agents import Agent, OpenAIChatCompletionsModel, RunConfig, Runner
    from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
    key = os.getenv('GEMINI_API_KEY', '').strip()
    if not key:
        raise AgentFailure('Set GEMINI_API_KEY in .env or choose --mode offline.')
    async with AsyncOpenAI(api_key=key,
        base_url='https://generativelanguage.googleapis.com/v1beta/openai/',
        timeout=60, max_retries=0) as client:
        agent = Agent(name='Study Planner',
            instructions='Create a study schedule from the supplied constraints and prepared difficulty ratings. '
                'Cover every requested topic. Include every study day exactly once, numbered from 1. '
                'Allocate no more than the daily budget, and at least 10 minutes in total per topic. '
                'Give harder topics more time. Return the requested structured schedule. '
                'Do not invent resource links; a separate agent will supply them.',
            model=OpenAIChatCompletionsModel(model=os.getenv('GEMINI_MODEL', '').strip() or 'gemini-3.1-flash-lite',
                openai_client=client), output_type=StudyPlan)
        try:
            result = await Runner.run(agent,
                'Constraints: ' + request.model_dump_json() + '\nDifficulty ratings: ' + str(catalog()[request.subject]),
                max_turns=2, run_config=RunConfig(tracing_disabled=True))
            return result.final_output
        except RateLimitError as exc:
            raise AgentFailure(quota_description(exc)) from None
        except APIConnectionError:
            raise AgentFailure('Cannot reach Gemini. Use --mode offline or check your connection.') from None
        except APIStatusError as exc:
            raise AgentFailure(f'Gemini returned HTTP {exc.status_code}. Check the key, model access, and project settings.') from None


async def run_planner(request: StudyRequest, context_id: str) -> dict:
    mode_label = 'scripted offline planner, no model calls' if request.mode == 'offline' else 'Gemini planner'
    print(f'[Planner] Drafting schedule ({mode_label})', flush=True)
    await emit(context_id, 'planning', 'planner', 'The planner gets to work',
        f'Planning {request.days} study days with {request.minutes_per_day} minutes each.',
        payload={'request': request.model_dump()})
    plan = build_plan(request) if request.mode == 'offline' else await gemini_plan(request)
    if request.show_repair:
        print('[Demo injection] Adding extra minutes to Day 1 so the checker has a real violation to catch.', flush=True)
        plan.days[0].sessions.append(Session(topic=request.topics[0],
            minutes=request.minutes_per_day, activity='DEMO: deliberately overbooked study time'))
    await emit(context_id, 'draft', 'planner', 'A first draft is ready',
        'This repair example deliberately includes an overbooked first day.' if request.show_repair else 'The topics now have study sessions. Next, find the materials.',
        payload={'plan': plan.model_dump(), 'budget': request.minutes_per_day})
    events = []
    reports = []
    resources = []
    for attempt in range(2):
        resource_request = ResourceRequest(request=request, plan=plan)
        await emit(context_id, 'handoff', 'planner', 'Find materials for this schedule',
            'The planner delegates to the Resource Finder through A2A.', target='resources',
            payload={'round': attempt + 1, 'request': resource_request.model_dump()})
        resource_result = await call_agent('resources', resource_request.model_dump(), context_id)
        resources = [Resource.model_validate(item) for item in resource_result['resources']]
        await emit(context_id, 'resources', 'resources', f'{len(resources)} resources found',
            'Curated readings and practice activities, with links and estimated study time.', target='planner',
            payload={'resources': [r.model_dump() for r in resources], 'round': attempt + 1})
        events.append({'from': 'planner', 'to': 'resources', 'round': attempt + 1,
            'result': f'{len(resources)} curated resources'})
        print(f'[Planner] Resource Finder returned {len(resources)} resources. Sending the complete plan to the Checker.', flush=True)
        check_request = CheckRequest(request=request, plan=plan, resources=resources)
        await emit(context_id, 'handoff', 'planner', 'Check the complete study guide',
            'The checker independently reviews the schedule and resource list.', target='checker',
            payload={'round': attempt + 1})
        report = await call_agent('checker', check_request.model_dump(), context_id)
        await emit(context_id, 'review', 'checker',
            'The checker approves the plan' if report['approved'] else 'The checker catches a problem',
            'All coverage and time-budget checks passed.' if report['approved'] else '; '.join(report['issues']),
            target='planner', payload={'report': report, 'round': attempt + 1, 'plan': plan.model_dump()})
        reports.append(report)
        events.append({'from': 'planner', 'to': 'checker', 'round': attempt + 1,
            'result': 'approved' if report['approved'] else 'needs revision'})
        if report['approved']:
            return {'mode': request.mode, 'planner': mode_label, 'request': request.model_dump(),
                'plan': plan.model_dump(), 'resources': [r.model_dump() for r in resources],
                'validation': report, 'reviews': reports, 'a2a_calls': events,
                'repaired': attempt > 0, 'context_id': context_id,
                'resource_note': 'Curated links, not live web search. Times are local demo estimates for selected study activities.'}
        print('[Checker feedback] ' + '; '.join(report['issues']), flush=True)
        if attempt == 0:
            # Repair uses explicit constraints, rather than trusting another model answer.
            print('[Planner repair] Reallocating minutes and requesting resources and validation again.', flush=True)
            plan = build_plan(request)
            await emit(context_id, 'revision', 'planner', 'The planner fixes the schedule',
                'Minutes are reallocated. Resources and checks will run again on the revised plan.',
                payload={'plan': plan.model_dump(), 'budget': request.minutes_per_day})
    raise AgentFailure('Checker still rejected the revised plan: ' + '; '.join(reports[-1]['issues']))
