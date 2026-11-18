"""Connettore Aruba 3.8.1: le risposte non si bloccano più sulle intestazioni dell'originale spezzate su più righe (References, From, Message-ID).

aruba_fwd.py è il vecchio aruba_tools.py 3.7.0 (inoltro) senza modifiche; index.py importa questo modulo.
Il link temporaneo è servito da api/allegato.py.
"""
import base64
import contextvars
import email
import hashlib
import json
import os
import secrets
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aruba_fwd import *  # noqa: F401,F403
from aruba_fwd import _files
import aruba_core as core

VERSION = '3.8.1'
BASE = os.environ.get('PUBLIC_URL', 'https://aruba-mcp-studio.vercel.app')
LINK_TTL = 900
TEXT_INLINE = 200 * 1024
_KEY = hashlib.sha256(os.environ.get('SECRET_KEY', '').encode()).digest()


def seal(data, ttl=LINK_TTL):
    nonce = secrets.token_bytes(12)
    payload = json.dumps({'k': 'att', 'exp': int(time.time()) + ttl, 'd': data}).encode()
    return base64.urlsafe_b64encode(nonce + AESGCM(_KEY).encrypt(nonce, payload, b'att')).rstrip(b'=').decode()


def unseal(token):
    try:
        raw = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4))
        obj = json.loads(AESGCM(_KEY).decrypt(raw[:12], raw[12:], b'att'))
    except Exception:
        return None
    if obj.get('k') != 'att' or obj.get('exp', 0) < time.time():
        return None
    return obj['d']


def _original(acc, folder, uid):
    with core.Box(acc) as c:
        core._select(c, folder)
        typ, data = c.uid('fetch', core._ids({'ids': [uid]}), '(BODY.PEEK[])')
        if typ != 'OK' or not data or not isinstance(data[0], tuple):
            raise RuntimeError('Email non trovata')
        return email.message_from_bytes(data[0][1])


def _is_text(name, ctype):
    return ctype.startswith('text/') or ctype in ('application/xml', 'application/json') or \
        name.lower().endswith(('.xml', '.csv', '.txt', '.json'))


def attachment_bytes(acc, folder, uid, name):
    """Usato dal link temporaneo: restituisce (nome, tipo, dati) di un allegato."""
    for f in _files(_original(acc, folder, uid)):
        if f[0] == name:
            return f
    raise RuntimeError('Allegato non trovato')


def download_attachment(a):
    acc = core._accounts(a.get('account'))[0]
    folder = a.get('folder')
    uid = str(a.get('id') or '').strip()
    files = _files(_original(acc, folder, uid))
    wanted = a.get('attachments')
    if isinstance(wanted, str):
        wanted = [wanted]
    if wanted:
        keys = {str(w).strip().lower() for w in wanted}
        missing = keys - {f[0].lower() for f in files}
        if missing:
            raise RuntimeError('Allegati non trovati: %s. Presenti: %s' % (', '.join(sorted(missing)), ', '.join(f[0] for f in files) or 'nessuno'))
        files = [f for f in files if f[0].lower() in keys]
    pwd = core.CURRENT.get().get(acc)
    out = []
    for name, ctype, data in files:
        item = {'name': name, 'type': ctype, 'size_kb': round(len(data) / 1024)}
        if pwd:
            item['url'] = '%s/api/allegato?t=%s' % (BASE, seal({'a': acc, 'p': pwd, 'f': folder or '', 'i': uid, 'n': name}))
        if _is_text(name, ctype) and len(data) <= TEXT_INLINE:
            item['text'] = data.decode('utf-8', errors='replace')
        out.append(item)
    return {'account': acc, 'id': uid, 'attachments': out,
            'note': 'I link scadono dopo 15 minuti e scaricano il file direttamente dalla casella: non viene inviata nessuna email.'}


core.TOOLS.append({
    'name': 'download_attachment',
    'description': "Scarica gli allegati di un'email Aruba senza inoltrarla: per ogni allegato restituisce un link temporaneo (15 minuti) "
                   "che scarica il file direttamente dalla casella, e il contenuto testuale per XML, CSV e TXT piccoli. Non invia email e non lascia copie.",
    'inputSchema': {'type': 'object', 'required': ['account', 'id'], 'properties': {
        'account': {'type': 'string', 'description': "Casella in cui si trova l'email"},
        'id': {'type': 'string', 'description': "Id dell'email (da list_emails o search_emails)"},
        'folder': {'type': 'string', 'description': "Cartella dell'email, predefinita INBOX"},
        'attachments': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Nomi dei soli allegati da scaricare; se omesso tutti'},
    }},
    'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False},
})
core.HANDLERS['download_attachment'] = download_attachment
