"""Connettore Aruba, estensioni: inoltro di un'email con i suoi allegati.

Il codice di base (lettura, invio, risposta, etichette) sta in aruba_core.py ed è identico alla versione 3.6.0.
Questo modulo lo carica e aggiunge lo strumento forward_email; lo stesso inoltro si ottiene da send_email
con forward_id, per le chat che hanno ancora l'elenco strumenti vecchio. api/index.py importa aruba_tools.
"""
import email
import email.utils
import mimetypes
import re
from email.message import EmailMessage

import aruba_core as core

VERSION = '3.7.0'
NL = chr(10)
# Oltre questa soglia Gmail e molti server rifiutano il messaggio (la codifica lo gonfia di un terzo)
MAX_FORWARD = 18 * 1024 * 1024

CURRENT = core.CURRENT
TOOLS = core.TOOLS
HANDLERS = core.HANDLERS
check_login = core.check_login


def _leaves(msg):
    """Parti finali del messaggio, senza entrare nelle email allegate (message/rfc822)."""
    payload = msg.get_payload()
    if msg.get_content_maintype() == 'multipart' and isinstance(payload, list):
        for sub in payload:
            yield from _leaves(sub)
    else:
        yield msg


def _clean(name):
    """Nome di file senza virgolette, barre e caratteri di controllo."""
    out = ''.join('_' if (ch in '"/' or ord(ch) < 32 or ord(ch) == 92) else ch for ch in name)
    return out.strip() or 'allegato'


def _files(orig):
    """Allegati dell'email originale come (nome, tipo, dati)."""
    out = []
    for part in _leaves(orig):
        ctype = part.get_content_type()
        name = core._dec(part.get_filename())
        disp = str(part.get('Content-Disposition', '')).lower()
        if ctype == 'message/rfc822':
            inner = part.get_payload()
            if not (isinstance(inner, list) and inner):
                continue
            data = inner[0].as_bytes()
            name = name or (core._dec(inner[0].get('Subject')) or 'messaggio')[:80] + '.eml'
            ctype = 'application/octet-stream'
        elif name or disp.startswith('attachment'):
            data = part.get_payload(decode=True)
            if not data:
                continue
        else:
            continue
        if not name:
            name = 'allegato-%d%s' % (len(out) + 1, mimetypes.guess_extension(ctype) or '')
        out.append((_clean(name), ctype, data))
    return out


def forward_email(a):
    acc = core._accounts(a.get('account'))[0]
    to, cc, bcc = core._addrs(a.get('to')), core._addrs(a.get('cc')), core._addrs(a.get('bcc'))
    if not to:
        raise RuntimeError('Nessun destinatario')
    with core.Box(acc) as c:
        core._select(c, a.get('folder'))
        typ, data = c.uid('fetch', core._ids({'ids': [a.get('id')]}), '(BODY.PEEK[])')
        if typ != 'OK' or not data or not isinstance(data[0], tuple):
            raise RuntimeError('Email da inoltrare non trovata')
        orig = email.message_from_bytes(data[0][1])
    files = _files(orig)
    wanted = a.get('attachments')
    if isinstance(wanted, str):
        wanted = [wanted]
    if wanted:
        keys = {str(w).strip().lower() for w in wanted}
        missing = keys - {f[0].lower() for f in files}
        if missing:
            raise RuntimeError('Allegati non trovati: %s. Presenti: %s' % (', '.join(sorted(missing)), ', '.join(f[0] for f in files) or 'nessuno'))
        files = [f for f in files if f[0].lower() in keys]
    total = sum(len(f[2]) for f in files)
    if total > MAX_FORWARD:
        raise RuntimeError('Allegati troppo pesanti (%.1f MB, limite 18 MB): indica in attachments solo i file che servono' % (total / 1048576))
    subj0 = core._dec(orig.get('Subject'))
    subj = (a.get('subject') or '').strip()
    if not subj:
        subj = subj0 if re.match('(?i)(fwd?|i|inoltro) *:', subj0) else 'Fwd: ' + subj0
    head = ['---------- Messaggio inoltrato ----------', 'Da: ' + core._dec(orig.get('From')), 'Data: ' + str(orig.get('Date', '')),
            'Oggetto: ' + subj0, 'A: ' + core._dec(orig.get('To'))]
    if orig.get('Cc'):
        head.append('Cc: ' + core._dec(orig.get('Cc')))
    note = (a.get('body') or '').strip()
    top = core._sign(acc, note, a) if (note or a.get('signature') is True) else ''
    body = (top.rstrip() + NL + NL if top.strip() else '') + NL.join(head) + NL + NL + core._text(orig, 20000) + NL
    msg = EmailMessage()
    msg['From'] = core._from(acc)
    msg['To'] = ', '.join(to)
    if cc:
        msg['Cc'] = ', '.join(cc)
    msg['Subject'] = subj
    msg['Date'] = email.utils.formatdate(localtime=True)
    msg['Message-ID'] = email.utils.make_msgid(domain=acc.split('@')[-1])
    msg.set_content(body)
    names = []
    for name, ctype, data in files:
        main, _, sub = ctype.partition('/')
        msg.add_attachment(data, maintype=main or 'application', subtype=sub or 'octet-stream', filename=name)
        names.append(name)
    names += core._attach(msg, a.get('attachment_urls'))
    core._smtp(acc, msg, to + cc + bcc)
    return {'account': acc, 'forwarded_to': to + cc + bcc, 'subject': subj, 'attachments': names,
            'size_kb': round(total / 1024), 'saved_in': core._save_sent(acc, msg)}


_send_email = core.HANDLERS['send_email']


def send_email(a):
    # Inoltro anche da send_email, per i client che hanno ancora l'elenco strumenti vecchio
    if a.get('forward_id'):
        return forward_email({**a, 'id': a['forward_id'], 'folder': a.get('forward_folder')})
    return _send_email(a)


core.TOOLS.append({
    'name': 'forward_email',
    'description': "Inoltra un'email esistente insieme ai suoi allegati (oggetto Fwd:) dalla casella in cui si trova e salva la copia negli Inviati. "
                   "Serve anche a far arrivare gli allegati su un'altra casella. Prima di usarlo mostra a Giulio destinatari e testo e attendi il suo ok.",
    'inputSchema': {'type': 'object', 'required': ['account', 'id', 'to'], 'properties': {
        'account': {'type': 'string', 'description': "Casella in cui si trova l'email da inoltrare"},
        'id': {'type': 'string', 'description': "Id dell'email da inoltrare (da list_emails o search_emails)"},
        'folder': {'type': 'string', 'description': "Cartella dell'email, predefinita INBOX"},
        'to': {'type': 'array', 'items': {'type': 'string'}},
        'cc': {'type': 'array', 'items': {'type': 'string'}},
        'bcc': {'type': 'array', 'items': {'type': 'string'}},
        'body': {'type': 'string', 'description': 'Nota da scrivere sopra il messaggio inoltrato (facoltativa)'},
        'subject': {'type': 'string', 'description': "Oggetto, se diverso da Fwd: seguito dall'oggetto originale"},
        'attachments': {'type': 'array', 'items': {'type': 'string'},
                        'description': 'Nomi dei soli allegati da inoltrare; se omesso li inoltra tutti (fino a 18 MB)'},
        'signature': {'type': 'boolean', 'description': "Aggiungi la firma della casella (predefinito: solo se c'è una nota)"},
    }},
    'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': True},
})
for _t in core.TOOLS:
    if _t['name'] == 'send_email':
        _t['inputSchema']['properties']['forward_id'] = {
            'type': 'string', 'description': "Per inoltrare: id di un'email della stessa casella; ne allega gli allegati e ne riporta il testo sotto il tuo (come forward_email)"}
        _t['inputSchema']['properties']['forward_folder'] = {'type': 'string', 'description': "Cartella dell'email da inoltrare, predefinita INBOX"}
core.HANDLERS['forward_email'] = forward_email
core.HANDLERS['send_email'] = send_email


def rpc(req):
    out = core.rpc(req)
    if isinstance(out, dict) and req.get('method') == 'initialize' and 'result' in out:
        out['result']['serverInfo']['version'] = VERSION
    return out
