"""Shared message contracts and deterministic planning/checking logic."""
from collections import Counter
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[1]


def catalog() -> dict:
    return json.loads((ROOT / 'topics.json').read_text(encoding='utf-8'))


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class StudyRequest(Contract):
    subject: str = 'calculus'
    topics: list[str] = Field(default_factory=lambda: ['limits', 'derivatives', 'integrals'], min_length=1, max_length=10)
    days: int = Field(default=3, ge=1, le=14)
    minutes_per_day: int = Field(default=90, ge=10, le=240)
    mode: Literal['offline', 'gemini'] = 'offline'
    show_repair: bool = False

    @field_validator('subject')
    @classmethod
    def normalize_subject(cls, value):
        return value.strip().lower()

    @field_validator('topics')
    @classmethod
    def normalize_topics(cls, values):
        values = [v.strip().lower() for v in values]
        if len(set(values)) != len(values):
            raise ValueError('Choose each topic only once.')
        return values

    @model_validator(mode='after')
    def check_catalog(self):
        known = catalog().get(self.subject)
        if not known:
            raise ValueError('Available subjects: ' + ', '.join(catalog()))
        unknown = set(self.topics) - set(known)
        if unknown:
            raise ValueError('Unknown topics: ' + ', '.join(sorted(unknown)))
        if self.days * self.minutes_per_day < len(self.topics) * 10:
            raise ValueError('Allow at least 10 minutes per requested topic in total.')
        return self


class Session(Contract):
    topic: str
    minutes: int = Field(ge=1, le=240)
    activity: str


class StudyDay(Contract):
    day: int = Field(ge=1, le=14)
    sessions: list[Session]


class StudyPlan(Contract):
    days: list[StudyDay]


class Resource(Contract):
    topic: str
    title: str
    url: str
    description: str
    estimated_minutes: int = Field(ge=1)


class ResourceRequest(Contract):
    request: StudyRequest
    plan: StudyPlan


class CheckRequest(ResourceRequest):
    resources: list[Resource]


def topic_budgets(plan: StudyPlan) -> dict[str, int]:
    totals = Counter()
    for day in plan.days:
        for session in day.sessions:
            totals[session.topic] += session.minutes
    return dict(totals)


def build_plan(request: StudyRequest) -> StudyPlan:
    """Allocate all available minutes using prepared difficulty weights."""
    difficulties = catalog()[request.subject]
    weights = {'easy': 1, 'medium': 2, 'hard': 3}
    total = request.days * request.minutes_per_day
    remaining = total - 10 * len(request.topics)
    denominator = sum(weights[difficulties[t]] for t in request.topics)
    budgets = {t: 10 + remaining * weights[difficulties[t]] // denominator for t in request.topics}
    spare = total - sum(budgets.values())
    order = sorted(request.topics, key=lambda t: -weights[difficulties[t]])
    for i in range(spare):
        budgets[order[i % len(order)]] += 1
    days = []
    for number in range(1, request.days + 1):
        left = request.minutes_per_day
        sessions = []
        while left:
            topic = max(order, key=lambda t: budgets[t])
            minutes = min(30, left, budgets[topic])
            sessions.append(Session(topic=topic, minutes=minutes, activity='Review notes and solve practice problems'))
            budgets[topic] -= minutes
            left -= minutes
        days.append(StudyDay(day=number, sessions=sessions))
    return StudyPlan(days=days)


def resource_catalog() -> dict:
    return json.loads((ROOT / 'a2a_demo' / 'resources.json').read_text(encoding='utf-8'))


def find_resources(data: ResourceRequest) -> list[Resource]:
    """Assemble a complete list from curated URLs, within each topic's budget."""
    budgets = topic_budgets(data.plan)
    found = []
    for topic in data.request.topics:
        remaining = budgets.get(topic, 0)
        for item in resource_catalog()[data.request.subject][topic]:
            resource = Resource(topic=topic, **item)
            if resource.estimated_minutes <= remaining:
                found.append(resource)
                remaining -= resource.estimated_minutes
    return found


def check_plan(data: CheckRequest) -> dict:
    issues = []
    request, plan = data.request, data.plan
    numbers = [d.day for d in plan.days]
    if sorted(numbers) != list(range(1, request.days + 1)):
        issues.append('Schedule must contain each requested day exactly once.')
    for day in plan.days:
        total = sum(s.minutes for s in day.sessions)
        if total > request.minutes_per_day:
            issues.append(f'Day {day.day} totals {total} minutes; limit is {request.minutes_per_day}.')
        if not day.sessions:
            issues.append(f'Day {day.day} has no study sessions.')
    budgets = topic_budgets(plan)
    missing = set(request.topics) - set(budgets)
    unexpected = set(budgets) - set(request.topics)
    if missing:
        issues.append('Missing topics: ' + ', '.join(sorted(missing)))
    if unexpected:
        issues.append('Unexpected topics: ' + ', '.join(sorted(unexpected)))
    known = resource_catalog()[request.subject]
    seen = set()
    resource_time = Counter()
    for resource in data.resources:
        if resource.topic not in request.topics:
            issues.append(f'Resource has unexpected topic: {resource.topic}.')
        if resource.url in seen:
            issues.append('Duplicate resource: ' + resource.title)
        seen.add(resource.url)
        if not resource.url.startswith('https://'):
            issues.append('Resource needs an HTTPS URL: ' + resource.title)
        if not any(resource.model_dump(exclude={'topic'}) == item for item in known.get(resource.topic, [])):
            issues.append('Resource differs from the curated catalog: ' + resource.title)
        resource_time[resource.topic] += resource.estimated_minutes
    for topic in request.topics:
        if not resource_time[topic]:
            issues.append(f'No resources supplied for {topic}.')
        elif resource_time[topic] > budgets.get(topic, 0):
            issues.append(f'Resources for {topic} need {resource_time[topic]} minutes; allocated {budgets.get(topic, 0)}.')
    return {
        'approved': not issues,
        'issues': issues,
        'daily_totals': {str(d.day): sum(s.minutes for s in d.sessions) for d in plan.days},
        'topic_minutes': budgets,
        'checks': ['day coverage', 'daily time limits', 'topic coverage', 'curated resource identity', 'resource time', 'duplicate URLs'],
        'limits': 'Checks catalog membership and time estimates, not live link availability or learning quality.',
    }
