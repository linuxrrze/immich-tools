"""Fake OpenAI-compatible chat API that streams a fixed answer as SSE."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = 'Qwen/Qwen2.5-VL-7B-Instruct'  # contains '/', like many vLLM model names
ANSWER = 'A red square.'


class FakeLLM:
    def __init__(self, answer=ANSWER):
        self.answer = answer
        self.requests = []  # {'auth': ..., 'body': ...} of every chat request
        self._httpd = ThreadingHTTPServer(('127.0.0.1', 0), _make_handler(self))
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, args=(0.05,), daemon=True)

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self._httpd.server_address[1]}/v1'

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def prompts(self):
        """The text part of each chat request."""
        return [part['text'] for r in self.requests
                for part in r['body']['messages'][0]['content'] if part['type'] == 'text']


def _make_handler(llm):
    class Handler(BaseHTTPRequestHandler):
        disable_nagle_algorithm = True
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != '/v1/models':
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({'object': 'list', 'data': [
                {'id': MODEL, 'object': 'model', 'created': 0, 'owned_by': 'test'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            llm.requests.append({'auth': self.headers.get('Authorization'), 'body': body})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()

            def event(delta, finish=None, choices=True):
                chunk = {'id': '1', 'object': 'chat.completion.chunk', 'created': 0, 'model': body['model'],
                         'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}] if choices else []}
                self.wfile.write(f'data: {json.dumps(chunk)}\n\n'.encode())

            # Role-only and usage-only chunks carry no text, like real servers
            event({'role': 'assistant', 'content': ''})
            half = len(llm.answer) // 2
            event({'content': llm.answer[:half]})
            event({'content': llm.answer[half:]})
            event({}, 'stop')
            event({}, choices=False)
            self.wfile.write(b'data: [DONE]\n\n')
            self.close_connection = True

    return Handler
