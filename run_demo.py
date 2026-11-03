"""Launch three local A2A agents and run a study-planning request."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import httpx

from a2a_demo.domain import StudyRequest
from a2a_demo.transport import AgentFailure, NAMES, ROLES, call_agent, endpoint

ROOT = Path(__file__).resolve().parent


class DemoCluster:
    """Own and stop only the child processes started by this launcher."""
    def __init__(self, base_port=8000, quiet=False):
        if not 1024 <= base_port <= 65533:
            raise ValueError('Base port must be between 1024 and 65533.')
        self.base_port, self.quiet = base_port, quiet
        self.processes, self.threads = [], []

    def __enter__(self):
        for offset in ROLES.values():
            with socket.socket() as sock:
                try:
                    sock.bind(('127.0.0.1', self.base_port + offset))
                except OSError:
                    raise AgentFailure(f'Port {self.base_port + offset} is in use. Try --base-port 8100.') from None
        try:
            for role in ROLES:
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                process = subprocess.Popen([sys.executable, '-u', '-m', 'a2a_demo.server', role,
                    '--base-port', str(self.base_port)], cwd=ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding='utf-8', errors='replace', creationflags=flags)
                self.processes.append(process)
                thread = threading.Thread(target=self._forward, args=(role, process), daemon=True)
                thread.start()
                self.threads.append(thread)
            deadline = time.monotonic() + 40
            with httpx.Client(timeout=1, trust_env=False) as client:
                pending = set(ROLES)
                while pending:
                    for role in list(pending):
                        try:
                            response = client.get(endpoint(role, self.base_port) + '/health')
                            if response.is_success and response.json().get('agent') == role:
                                pending.remove(role)
                        except httpx.HTTPError:
                            pass
                    if any(p.poll() is not None for p in self.processes):
                        raise AgentFailure('An agent stopped during startup. See its error above.')
                    if time.monotonic() >= deadline:
                        raise AgentFailure('Timed out waiting for agents: ' + ', '.join(sorted(pending)))
                    if pending:
                        time.sleep(0.15)
            return self
        except BaseException:
            self.stop()
            raise

    def _forward(self, role, process):
        for line in process.stdout:
            if not self.quiet:
                print(f'[{role}] {line.rstrip()}', flush=True)

    def stop(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for thread in self.threads:
            thread.join(timeout=1)

    def __exit__(self, *args):
        self.stop()


def display(result):
    print('\n' + '=' * 64 + '\nCOMPLETE STUDY GUIDE')
    print('Planner: ' + result['planner'])
    print('Checker: APPROVED' + (' after revision' if result['repaired'] else ''))
    for day in result['plan']['days']:
        total = sum(s['minutes'] for s in day['sessions'])
        print(f'\nDay {int(day["day"])}: {int(total)} minutes')
        for session in day['sessions']:
            print(f'  {int(session["minutes"]):3} min  {session["topic"]}: {session["activity"]}')
    print('\nRESOURCE LIST')
    for item in result['resources']:
        print(f'\n  {item["topic"]}: {item["title"]} (~{int(item["estimated_minutes"])} min)')
        print('  ' + item['description'] + '\n  ' + item['url'])
    print('\n' + result['resource_note'])
    print(f'A2A specialist calls: {len(result["a2a_calls"])}; review rounds: {len(result["reviews"])}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['offline', 'gemini'], default='offline')
    parser.add_argument('--subject', default='calculus')
    parser.add_argument('--topics', nargs='+', default=['limits', 'derivatives', 'integrals'])
    parser.add_argument('--days', type=int, default=3)
    parser.add_argument('--minutes', type=int, default=90, help='Available minutes per day')
    parser.add_argument('--show-repair', action='store_true', help='Inject a labeled time-budget error and demonstrate correction')
    parser.add_argument('--base-port', type=int, default=8000)
    parser.add_argument('--serve', action='store_true', help='Keep servers running until Ctrl+C, without a sample request')
    parser.add_argument('--client-only', action='store_true', help='Send a request to servers already running')
    parser.add_argument('--json', action='store_true', help='Also print the complete structured result')
    args = parser.parse_args()
    try:
        if args.serve and args.client_only:
            parser.error('--serve and --client-only cannot be combined.')
        request = StudyRequest(subject=args.subject, topics=args.topics, days=args.days,
            minutes_per_day=args.minutes, mode=args.mode, show_repair=args.show_repair)
        print('A2A AGENTS DEMO: planner + resource finder + checker', flush=True)
        print('Mode: ' + ('OFFLINE / scripted planning, real A2A HTTP' if args.mode == 'offline' else 'GEMINI / live model requests'), flush=True)

        def run_request():
            result = asyncio.run(call_agent('planner', request.model_dump(), base_port=args.base_port))
            display(result)
            if args.json:
                print(json.dumps(result, indent=2))

        if args.client_only:
            run_request()
        else:
            with DemoCluster(args.base_port):
                print('\nAgent Cards:', flush=True)
                for role in ROLES:
                    print(f'  {NAMES[role]}: {endpoint(role, args.base_port)}/.well-known/agent-card.json', flush=True)
                if args.serve:
                    print('\nServers ready. Use --client-only in another terminal. Ctrl+C stops all three.', flush=True)
                    while True:
                        time.sleep(1)
                else:
                    run_request()
    except KeyboardInterrupt:
        print('\nStopped the local agents.')
        return 0
    except (ValueError, AgentFailure) as exc:
        print('\nDemo could not complete: ' + str(exc))
        return 1
    except Exception as exc:
        print(f'\nDemo could not complete ({type(exc).__name__}). Check that all agents are reachable.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
