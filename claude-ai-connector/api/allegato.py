"""Link temporaneo per scaricare un allegato Aruba (creato da download_attachment)."""
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, os.path.dirname(__file__))
import aruba_tools  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype='application/json', headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        d = aruba_tools.unseal((q.get('t') or [''])[0])
        if not d:
            return self._send(403, '{"error": "Link scaduto o non valido"}')
        aruba_tools.CURRENT.set({d['a']: d['p']})
        try:
            name, ctype, data = aruba_tools.attachment_bytes(d['a'], d['f'] or None, d['i'], d['n'])
        except Exception as e:
            return self._send(404, '{"error": "%s"}' % str(e).replace('"', "'"))
        return self._send(200, data, ctype or 'application/octet-stream',
                          {'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name)})
