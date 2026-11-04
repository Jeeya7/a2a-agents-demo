import json
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import uvicorn
from starlette.testclient import TestClient

from a2a_demo.transport import AgentFailure
from visual_demo import create_app


class VisualFailureTests(unittest.TestCase):
    def test_error_is_delivered_as_a_terminal_event(self):
        app = create_app(start_cluster=False)
        with patch('visual_demo.call_agent', new=AsyncMock(side_effect=AgentFailure('Test agent unavailable'))):
            with TestClient(app) as client:
                response = client.post('/api/runs', json={})
                self.assertEqual(response.status_code, 202)
                result = client.get(f'/api/runs/{response.json()["id"]}/stream').text
                self.assertIn('Test agent unavailable', result)
                self.assertIn('"kind": "error"', result)
                self.assertIn('event: end', result)

    def test_invalid_input_does_not_start_a_run(self):
        with TestClient(create_app(start_cluster=False)) as client:
            self.assertEqual(client.post('/api/runs', json={'days': 0}).status_code, 400)
            self.assertEqual(client.post('/api/runs', content='not json').status_code, 400)
            self.assertEqual(client.get('/api/runs/missing/stream').status_code, 404)


class VisualIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = 'http://127.0.0.1:18880'
        cls.server = uvicorn.Server(uvicorn.Config(create_app(base_port=18881, ui_port=18880),
            host='127.0.0.1', port=18880, log_level='critical', access_log=False))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        cls.client = httpx.Client(base_url=cls.url, timeout=45, trust_env=False)
        try:
            deadline = time.monotonic() + 45
            while True:
                try:
                    if cls.client.get('/api/config').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if not cls.thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError('Visual test server did not start.')
                time.sleep(.15)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=15)
        if cls.thread.is_alive():
            raise RuntimeError('Visual test server did not shut down cleanly.')
        cls.client.close()

    def test_page_and_actual_agent_card(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('One goal.', page.text)
        config = self.client.get('/api/config').json()
        self.assertEqual(len(config['agents']), 3)
        card = self.client.get('/api/agents/checker/card').json()
        self.assertEqual(card['skills'][0]['id'], 'checker')
        self.assertEqual(self.client.get('/api/agents/missing/card').status_code, 404)

    def test_real_workflow_stream_contains_rejection_and_repair(self):
        response = self.client.post('/api/runs', json={'show_repair': True})
        self.assertEqual(response.status_code, 202)
        run_id = response.json()['id']
        events = []
        ended = False
        with self.client.stream('GET', f'/api/runs/{run_id}/stream') as stream:
            for line in stream.iter_lines():
                if line.startswith('data: ') and line != 'data: {}':
                    events.append(json.loads(line[6:]))
                if line == 'event: end':
                    ended = True
        self.assertTrue(ended)
        self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(any(e['kind'] == 'protocol' and e['title'] == 'Agent Card discovered' for e in events))
        reviews = [e for e in events if e['kind'] == 'review']
        self.assertFalse(reviews[0]['payload']['report']['approved'])
        self.assertTrue(reviews[1]['payload']['report']['approved'])
        result = events[-1]
        self.assertEqual(result['kind'], 'completed')
        self.assertEqual(result['payload']['validation']['daily_totals'], {'1': 90, '2': 90, '3': 90})
        self.assertEqual(len(result['payload']['resources']), 6)
        self.assertEqual(self.client.post('/api/runs/no-such-run/events', json={}).status_code, 404)
        self.assertEqual(self.client.post(f'/api/runs/{run_id}/events', json={'kind': 'completed'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
