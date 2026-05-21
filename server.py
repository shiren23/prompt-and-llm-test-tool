#!/usr/bin/env python3
"""Local proxy server for the grading experiment tool.

Usage:
  python3 server.py [port]

Then open http://localhost:8080 in your browser.
"""

import json
import os
import sys
import time
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

PORT = 8080
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
RESULTS_FILE = os.path.join(DATA_DIR, 'results.json')

# Provider domains mapped to their default rate limits (requests per second)
PROVIDER_DOMAINS = {
    'dashscope.aliyuncs.com': 'qwen',
    'api.deepseek.com': 'deepseek',
    'ark.cn-beijing.volces.com': 'doubao',
    'api.openai.com': 'openai',
}

# Default rate: 5 requests/second per provider (= 300 QPM, safe for most tiers)
DEFAULT_RPS = 5


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _read_json(path):
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _write_json(path, data):
    _ensure_data_dir()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class TokenBucket:
    """Per-provider token bucket rate limiter."""

    def __init__(self, rate, burst=None):
        self.rate = rate          # tokens per second
        self.burst = burst or max(rate, 1)  # max burst size
        self.tokens = self.burst
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        """Block until a token is available. Returns wait time in seconds."""
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0
            # Need to wait for a token
            wait = (1 - self.tokens) / self.rate
            self.tokens = 0
            # Release lock while sleeping
        time.sleep(wait)
        return wait


# Global rate limiters: keyed by provider name
_limiters = {}
_limiters_lock = threading.Lock()


def _get_limiter(provider):
    with _limiters_lock:
        if provider not in _limiters:
            _limiters[provider] = TokenBucket(DEFAULT_RPS)
        return _limiters[provider]


def _extract_provider(url):
    """Extract provider key from target URL."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ''
    for domain, key in PROVIDER_DOMAINS.items():
        if domain in host:
            return key
    return host  # unknown provider, use hostname


class ProxyHandler(SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path == '/api/status':
            self._handle_status()
        elif self.path == '/api/load-config':
            self._handle_load_config()
        elif self.path == '/api/load-results':
            self._handle_load_results()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == '/api/call':
            self._handle_proxy()
        elif self.path == '/api/save-config':
            self._handle_save_config()
        elif self.path == '/api/save-results':
            self._handle_save_results()
        else:
            self.send_error(404)

    # ---------- Status ----------

    def _handle_status(self):
        body = json.dumps({'status': 'ok'}).encode()
        self._send_json(200, body)

    # ---------- Config persistence ----------

    def _handle_save_config(self):
        try:
            data = self._read_body()
            _write_json(CONFIG_FILE, data)
            self._send_json(200, json.dumps({'ok': True, 'file': CONFIG_FILE}).encode())
        except Exception as e:
            self._send_json(500, json.dumps({'ok': False, 'error': str(e)}).encode())

    def _handle_load_config(self):
        data = _read_json(CONFIG_FILE)
        if data is None:
            self._send_json(200, json.dumps({'exists': False}).encode())
        else:
            self._send_json(200, json.dumps({'exists': True, 'data': data}).encode())

    # ---------- Results persistence ----------

    def _handle_save_results(self):
        try:
            data = self._read_body()
            _write_json(RESULTS_FILE, data)
            self._send_json(200, json.dumps({'ok': True, 'file': RESULTS_FILE}).encode())
        except Exception as e:
            self._send_json(500, json.dumps({'ok': False, 'error': str(e)}).encode())

    def _handle_load_results(self):
        data = _read_json(RESULTS_FILE)
        if data is None:
            self._send_json(200, json.dumps({'exists': False}).encode())
        else:
            self._send_json(200, json.dumps({'exists': True, 'data': data}).encode())

    # ---------- API Proxy with rate limiting ----------

    def _handle_proxy(self):
        try:
            data = self._read_body()
            target_url = data.get('targetUrl', '')
            api_key = data.get('apiKey', '')
            req_body = data.get('body', {})
            timeout = data.get('timeout', 120)

            if not target_url:
                self._send_error(400, 'Missing targetUrl')
                return

            # Rate limit by provider
            provider = _extract_provider(target_url)
            _get_limiter(provider).acquire()

            # NOTE: urllib.request.urlopen is synchronous blocking I/O.
            # Each request occupies a thread for its entire duration (5-30s).
            # This is fine for current concurrency levels (4-10). If you need
            # 50+ concurrent requests in the future, replace urllib with aiohttp
            # and switch from ThreadingMixIn to an async server (e.g. aiohttp.web
            # or uvicorn) to avoid spawning hundreds of threads (~8MB stack each).
            headers = {'Content-Type': 'application/json'}
            if api_key:
                headers['Authorization'] = f'Bearer {api_key}'

            req = urllib.request.Request(
                target_url,
                data=json.dumps(req_body).encode('utf-8'),
                headers=headers,
                method='POST'
            )

            resp = urllib.request.urlopen(req, timeout=timeout)
            resp_body = resp.read()

            self.send_response(200)
            self.send_header('Content-Type', resp.headers.get('Content-Type', 'application/json'))
            self.send_header('Content-Length', str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

        except urllib.error.HTTPError as e:
            err_body = e.read()
            try:
                err_text = err_body.decode('utf-8')
            except UnicodeDecodeError:
                err_text = err_body.decode('latin-1')

            err_json = json.dumps({
                'proxyError': False,
                'status': e.code,
                'body': err_text,
                'retryAfter': int(e.headers.get('Retry-After', 0)) if e.code == 429 else 0
            }).encode('utf-8')
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(err_json)))
            if e.code == 429:
                ra = e.headers.get('Retry-After', '5')
                self.send_header('X-RateLimit-RetryAfter', ra)
            self.end_headers()
            self.wfile.write(err_json)

        except urllib.error.URLError as e:
            msg = str(e.reason) if hasattr(e, 'reason') else str(e)
            self._send_error(502, f'Upstream connection failed: {msg}')

        except Exception as e:
            self._send_error(500, str(e))

    # ---------- Helpers ----------

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length))

    def _send_json(self, code, body_bytes):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_error(self, code, message):
        body = json.dumps({'proxyError': True, 'status': code, 'body': message}).encode('utf-8')
        self._send_json(code, body)

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        super().end_headers()

    def log_message(self, fmt, *args):
        msg = fmt % args
        if '/api/' in msg:
            sys.stderr.write(f"[{self.log_date_time_string()}] {msg}\n")
            sys.stderr.flush()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    _ensure_data_dir()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    server = ThreadedHTTPServer(('localhost', port), ProxyHandler)
    print(f'  Grading Experiment Tool - Proxy Server')
    print(f'  Local:   http://localhost:{port}')
    print(f'  Data:    {DATA_DIR}')
    print(f'  Rate Limit: {DEFAULT_RPS} req/s per provider')
    print(f'  Press Ctrl+C to stop\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  Shutting down...')
        server.shutdown()


if __name__ == '__main__':
    main()
