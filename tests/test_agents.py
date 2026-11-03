import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from a2a_demo.domain import (CheckRequest, ResourceRequest, StudyRequest, build_plan,
    check_plan, find_resources, topic_budgets)
from a2a_demo.transport import AgentFailure, call_agent, endpoint
from a2a_demo.workflow import gemini_plan, quota_description
from run_demo import DemoCluster


class ConstraintTests(unittest.TestCase):
    def bundle(self, request=None):
        request = request or StudyRequest()
        plan = build_plan(request)
        resources = find_resources(ResourceRequest(request=request, plan=plan))
        return CheckRequest(request=request, plan=plan, resources=resources)

    def test_uneven_budget_and_difficulty_allocation(self):
        bundle = self.bundle(StudyRequest(days=2, minutes_per_day=37))
        report = check_plan(bundle)
        self.assertTrue(report['approved'], report['issues'])
        self.assertEqual(list(report['daily_totals'].values()), [37, 37])
        budgets = topic_budgets(bundle.plan)
        self.assertGreater(budgets['integrals'], budgets['derivatives'])

    def test_smallest_supported_budget(self):
        bundle = self.bundle(StudyRequest(topics=['limits'], days=1, minutes_per_day=10))
        self.assertTrue(check_plan(bundle)['approved'])
        self.assertEqual(len(bundle.resources), 1)

    def test_checker_detects_overbooking(self):
        bundle = self.bundle()
        bundle.plan.days[0].sessions[0].minutes += 15
        report = check_plan(bundle)
        self.assertFalse(report['approved'])
        self.assertIn('Day 1 totals 105 minutes; limit is 90.', report['issues'])

    def test_checker_rejects_missing_and_forged_resources(self):
        bundle = self.bundle()
        bundle.resources = [r for r in bundle.resources if r.topic != 'limits']
        bundle.resources[0].url = 'https://example.com/invented'
        issues = check_plan(bundle)['issues']
        self.assertIn('No resources supplied for limits.', issues)
        self.assertTrue(any('differs from the curated catalog' in issue for issue in issues))

    def test_checker_rejects_missing_day_and_duplicate_url(self):
        bundle = self.bundle()
        bundle.plan.days.pop()
        bundle.resources.append(bundle.resources[0])
        issues = check_plan(bundle)['issues']
        self.assertTrue(any('each requested day' in issue for issue in issues))
        self.assertTrue(any('Duplicate resource' in issue for issue in issues))

    def test_invalid_student_constraints(self):
        for payload in [{'days': 0}, {'topics': ['limits', 'limits']},
            {'topics': ['unknown']}, {'days': 1, 'minutes_per_day': 10}]:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                StudyRequest(**payload)


class GeminiAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_structured_schedule_without_network(self):
        from openai import AsyncOpenAI
        request = StudyRequest(mode='gemini')
        expected = build_plan(request)
        calls = []

        def fake_response(http_request):
            self.assertEqual(http_request.url.host, 'generativelanguage.googleapis.com')
            payload = json.loads(http_request.content)
            self.assertEqual(payload['model'], 'gemini-3.1-flash-lite')
            calls.append(payload)
            return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0,
                'model': payload['model'], 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': expected.model_dump_json()}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})

        client = AsyncOpenAI(api_key='offline-test-only',
            base_url='https://generativelanguage.googleapis.com/v1beta/openai/',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake_response)))
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'offline-test-only', 'GEMINI_MODEL': 'gemini-3.1-flash-lite'}), \
            patch('openai.AsyncOpenAI', return_value=client):
            actual = await gemini_plan(request)
        self.assertEqual(actual, expected)
        self.assertEqual(len(calls), 1)

    async def test_zero_quota_diagnostic(self):
        class Error:
            body = [{'error': {'details': [{'metadata': {'quota_limit_value': '0'}}]}}]
        self.assertIn('regional quota of zero', quota_description(Error()))


class A2AIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # These tests exercise real processes, cards, JSON-RPC, tasks, and artifacts.
        cls.cluster = DemoCluster(base_port=19000, quiet=True)
        cls.cluster.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.cluster.stop()

    def send(self, role, data):
        return asyncio.run(call_agent(role, data, base_port=19000))

    def test_all_three_cards_publish_their_skills(self):
        with httpx.Client(trust_env=False) as client:
            for role in ('planner', 'resources', 'checker'):
                card = client.get(endpoint(role, 19000) + '/.well-known/agent-card.json').json()
                self.assertEqual(card['skills'][0]['id'], role)
                self.assertEqual(card['supportedInterfaces'][0]['protocolBinding'], 'JSONRPC')

    def test_planner_repairs_checker_rejection_over_a2a(self):
        result = self.send('planner', StudyRequest(show_repair=True).model_dump())
        self.assertTrue(result['repaired'])
        self.assertFalse(result['reviews'][0]['approved'])
        self.assertTrue(result['reviews'][1]['approved'])
        self.assertEqual(len(result['a2a_calls']), 4)
        self.assertEqual(len(result['resources']), 6)
        self.assertEqual(list(result['validation']['daily_totals'].values()), [90, 90, 90])

    def test_python_request_without_repair(self):
        request = StudyRequest(subject='python', topics=['variables', 'loops', 'functions', 'recursion'],
            days=2, minutes_per_day=60)
        result = self.send('planner', request.model_dump())
        self.assertTrue(result['validation']['approved'])
        self.assertFalse(result['repaired'])
        self.assertEqual(len(result['a2a_calls']), 2)
        self.assertEqual({r['topic'] for r in result['resources']}, set(request.topics))

    def test_repair_at_maximum_daily_budget(self):
        result = self.send('planner', StudyRequest(days=1, minutes_per_day=240, show_repair=True).model_dump())
        self.assertTrue(result['repaired'])
        self.assertTrue(result['validation']['approved'])
        self.assertEqual(result['validation']['daily_totals']['1'], 240)

    def test_invalid_remote_request_fails_without_hanging(self):
        with self.assertRaises(AgentFailure) as caught:
            self.send('planner', {'subject': 'chemistry'})
        self.assertIn('Available subjects', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
