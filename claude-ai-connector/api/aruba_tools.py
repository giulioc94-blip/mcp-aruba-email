"""Strumenti email per le caselle Aruba (lettura, invio, spostamento, cestino). Il server HTTP e l'OAuth sono in api/index.py."""
import contextvars
import email
import email.utils
import imaplib
import json
import os
import re
import smtplib
import time
from email.message import EmailMessage
from datetime import datetime, timedelta
from email.header import decode_header
from html import unescape

IMAP_HOST = os.environ.get("ARUBA_IMAP_HOST", "imaps.aruba.it")
SMTP_HOST = os.environ.get("ARUBA_SMTP_HOST", "smtps.aruba.it")
PROTOCOL = "2025-06-18"
# Caselle della richiesta in corso, prese dal token OAuth cifrato: {email: password}
CURRENT = contextvars.ContextVar("aruba_accounts", default={})
# Firme e nomi mittente per casella, dalla variabile SIGNATURES su Vercel:
# {"info@...": {"name": "Studio Curcio Terremoto", "signature": "..."}, ...}
try:
    SIGNATURES = {k.lower(): v for k, v in json.loads(os.environ.get("SIGNATURES") or "{}").items()}
except Exception:
    SIGNATURES = {}

ACCOUNT_PARAM = {"type": "string", "description": "Indirizzo della casella (es. info@... o giulio@...). Se omesso: tutte le caselle collegate."}

TOOLS = [
    {
        "name": "list_emails",
        "description": "Elenca le email più recenti delle caselle Aruba dello Studio (tutte, o una sola con account), dalla più nuova. "
                       "Restituisce casella, id, data, mittente, destinatari, oggetto e un'anteprima del testo.",
        "inputSchema": {"type": "object", "properties": {
            "account": ACCOUNT_PARAM,
            "folder": {"type": "string", "description": "Cartella IMAP, predefinita INBOX"},
            "days": {"type": "integer", "description": "Solo email degli ultimi N giorni (predefinito 2)"},
            "limit": {"type": "integer", "description": "Massimo numero di email (predefinito 20, massimo 50)"},
            "unread_only": {"type": "boolean", "description": "Solo non lette"},
            "label": {"type": "string", "description": "Solo email con questa etichetta (keyword IMAP)"},
            "unlabeled_only": {"type": "boolean", "description": "Solo email senza nessuna etichetta"},
        }},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "search_emails",
        "description": "Cerca email nelle caselle Aruba per testo (oggetto o corpo) e/o mittente, negli ultimi N giorni.",
        "inputSchema": {"type": "object", "properties": {
            "account": ACCOUNT_PARAM,
            "text": {"type": "string", "description": "Parola o frase da cercare in oggetto e corpo"},
            "sender": {"type": "string", "description": "Indirizzo o nome del mittente"},
            "folder": {"type": "string"},
            "days": {"type": "integer", "description": "Predefinito 30"},
            "limit": {"type": "integer", "description": "Predefinito 20, massimo 50"},
        }},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "read_email",
        "description": "Legge il testo completo di un'email Aruba dati casella (account) e id (da list_emails o search_emails). Non la segna come letta.",
        "inputSchema": {"type": "object", "required": ["id", "account"], "properties": {
            "account": {"type": "string", "description": "Casella da cui leggere (il campo account restituito da list_emails o search_emails)"},
            "id": {"type": "string"}, "folder": {"type": "string"},
        }},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_folders",
        "description": "Elenca le caselle Aruba collegate e le loro cartelle.",
        "inputSchema": {"type": "object", "properties": {"account": ACCOUNT_PARAM}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "send_email",
        "description": "Invia una NUOVA email da una casella Aruba via SMTP e ne salva una copia nella cartella Inviati. "
                       "Prima di usarlo mostra a Giulio destinatari, oggetto e testo e attendi il suo ok.",
        "inputSchema": {"type": "object", "required": ["account", "to", "subject", "body"], "properties": {
            "account": {"type": "string", "description": "Casella mittente"},
            "to": {"type": "array", "items": {"type": "string"}},
            "cc": {"type": "array", "items": {"type": "string"}},
            "bcc": {"type": "array", "items": {"type": "string"}},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Testo semplice, senza firma: la firma della casella viene aggiunta in automatico"},
            "signature": {"type": "boolean", "description": "Aggiungi la firma della casella (predefinito sì)"},
        }},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "reply_email",
        "description": "Risponde a un'email (stessa conversazione, oggetto Re:) dalla casella in cui è arrivata, e salva la copia negli Inviati. "
                       "Prima di usarlo mostra a Giulio il testo e attendi il suo ok.",
        "inputSchema": {"type": "object", "required": ["account", "id", "body"], "properties": {
            "account": {"type": "string"}, "id": {"type": "string"}, "folder": {"type": "string"},
            "body": {"type": "string", "description": "Testo della risposta, senza firma: viene aggiunta in automatico"},
            "reply_all": {"type": "boolean", "description": "Rispondi a tutti (predefinito no)"},
            "signature": {"type": "boolean", "description": "Aggiungi la firma della casella (predefinito sì)"},
        }},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "move_email",
        "description": "Sposta una o più email in un'altra cartella della stessa casella (usa list_folders per i nomi).",
        "inputSchema": {"type": "object", "required": ["account", "ids", "to_folder"], "properties": {
            "account": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}},
            "folder": {"type": "string", "description": "Cartella di partenza, predefinita INBOX"},
            "to_folder": {"type": "string"},
        }},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "delete_email",
        "description": "Sposta una o più email nel Cestino della casella (recuperabili). Con permanent=true le elimina definitivamente: chiedi sempre conferma a Giulio.",
        "inputSchema": {"type": "object", "required": ["account", "ids"], "properties": {
            "account": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}},
            "folder": {"type": "string"},
            "permanent": {"type": "boolean", "description": "Eliminazione definitiva (predefinito no: va nel Cestino)"},
        }},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "list_labels",
        "description": "Elenca le etichette (keyword IMAP) usate in una cartella, con quante email le hanno, analizzando le email degli ultimi N giorni. Serve a capire chi segue cosa.",
        "inputSchema": {"type": "object", "properties": {
            "account": ACCOUNT_PARAM, "folder": {"type": "string"},
            "days": {"type": "integer", "description": "Predefinito 365"},
        }},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "label_email",
        "description": "Aggiunge o toglie etichette (keyword IMAP) a una o più email, come le etichette dei collaboratori o di settore (es. Paghe).",
        "inputSchema": {"type": "object", "required": ["account", "ids"], "properties": {
            "account": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}},
            "folder": {"type": "string"},
            "add": {"type": "array", "items": {"type": "string"}},
            "remove": {"type": "array", "items": {"type": "string"}},
        }},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "mark_email",
        "description": "Segna una o più email come lette/non lette o contrassegnate/non contrassegnate, e aggiunge o toglie etichette (add_labels, remove_labels).",
        "inputSchema": {"type": "object", "required": ["account", "ids"], "properties": {
            "account": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}},
            "folder": {"type": "string"},
            "read": {"type": "boolean"}, "flagged": {"type": "boolean"},
            "add_labels": {"type": "array", "items": {"type": "string"}, "description": "Etichette da aggiungere"},
            "remove_labels": {"type": "array", "items": {"type": "string"}, "description": "Etichette da togliere"},
        }},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]


def _dec(value):
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        out.append(part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part)
    return "".join(out).strip()


def _text(msg, limit=None):
    plain, html = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or "attachment" in str(part.get("Content-Disposition", "")):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        txt = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (plain if part.get_content_type() == "text/plain" else html if part.get_content_type() == "text/html" else []).append(txt)
    body = "\n".join(plain)
    if not body.strip() and html:
        body = re.sub(r"(?is)<(script|style).*?</\1>", "", "\n".join(html))
        body = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", body)
        body = unescape(re.sub(r"<[^>]+>", "", body))
    body = re.sub(r"\n\s*\n+", "\n\n", body).strip()
    return body[:limit] if limit else body


def _attachments(msg):
    return [_dec(p.get_filename()) for p in msg.walk() if p.get_filename()]


def _accounts(account):
    accounts = CURRENT.get()
    if not accounts:
        raise RuntimeError("Nessuna casella collegata: ricollega il connettore.")
    if account:
        key = account.strip().lower()
        if key not in accounts:
            raise RuntimeError(f"Casella non collegata: {account}. Disponibili: {', '.join(accounts)}")
        return [key]
    return list(accounts)


class Box:
    def __init__(self, account):
        self.user = account

    def __enter__(self):
        pwd = CURRENT.get().get(self.user)
        if not pwd:
            raise RuntimeError(f"Casella {self.user} non collegata.")
        self.c = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=45)
        self.c.login(self.user, pwd)
        return self.c

    def __exit__(self, *a):
        try:
            self.c.logout()
        except Exception:
            pass


def _select(c, folder):
    typ, _ = c.select('"%s"' % (folder or "INBOX").replace('"', ""), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"Cartella non trovata: {folder}")


def _since(days):
    return (datetime.now() - timedelta(days=max(int(days), 1))).strftime("%d-%b-%Y")


def _summaries(c, ids, limit, account):
    out = []
    for uid in list(reversed(ids))[:limit]:
        typ, data = c.uid("fetch", uid, "(FLAGS BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            continue
        flags = data[0][0].decode(errors="replace")
        msg = email.message_from_bytes(data[0][1])
        m = re.search(r"FLAGS \(([^)]*)\)", flags)
        labels = [f for f in (m.group(1).split() if m else []) if not f.startswith("\\")]
        out.append({
            "account": account,
            "labels": labels,
            "id": uid.decode() if isinstance(uid, bytes) else str(uid),
            "date": msg.get("Date", ""),
            "from": _dec(msg.get("From")),
            "to": _dec(msg.get("To")),
            "reply_to": _dec(msg.get("Reply-To")),
            "subject": _dec(msg.get("Subject")),
            "unread": "\\Seen" not in flags,
            "preview": _text(msg, 600),
            "attachments": _attachments(msg),
        })
    return out


def _clamp(v, default, hi=50):
    try:
        return max(1, min(int(v), hi))
    except (TypeError, ValueError):
        return default


def _sortkey(m):
    try:
        return email.utils.parsedate_to_datetime(m["date"]).timestamp()
    except Exception:
        return 0


def _each(a, fn):
    """Esegue fn su ogni casella richiesta e unisce i risultati, segnalando le caselle in errore."""
    results, errors = [], []
    for acc in _accounts(a.get("account")):
        try:
            with Box(acc) as c:
                results += fn(c, acc)
        except Exception as e:
            errors.append(f"{acc}: {e}")
    if errors and not results:
        raise RuntimeError("; ".join(errors))
    results.sort(key=_sortkey, reverse=True)
    out = {"emails": results[:_clamp(a.get("limit"), 20)]}
    if errors:
        out["errors"] = errors
    return out


def list_emails(a):
    def run(c, acc):
        _select(c, a.get("folder"))
        crit = ["SINCE", _since(a.get("days", 2))] + (["UNSEEN"] if a.get("unread_only") else [])
        if a.get("label"):
            crit += ["KEYWORD", _kw(a["label"])]
        typ, data = c.uid("search", None, *crit)
        res = _summaries(c, data[0].split() if data and data[0] else [], _clamp(a.get("limit"), 20), acc)
        return [r for r in res if not r["labels"]] if a.get("unlabeled_only") else res
    return _each(a, run)


def search_emails(a):
    def run(c, acc):
        _select(c, a.get("folder"))
        crit = ["SINCE", _since(a.get("days", 30))]
        if a.get("sender"):
            crit += ["FROM", '"%s"' % a["sender"].replace('"', "")]
        text = a.get("text") or ""
        if text and not text.isascii():
            c.literal = text.encode("utf-8")
            typ, data = c.uid("search", "CHARSET", "UTF-8", *crit, "TEXT")
        else:
            phrase = '"%s"' % text.replace('"', "").replace("\\", "") if text else None
            typ, data = c.uid("search", None, *(crit + (["TEXT", phrase] if text else [])))
        return _summaries(c, data[0].split() if data and data[0] else [], _clamp(a.get("limit"), 20), acc)
    return _each(a, run)


def read_email(a):
    acc = _accounts(a.get("account"))[0]
    with Box(acc) as c:
        _select(c, a.get("folder"))
        typ, data = c.uid("fetch", str(a["id"]), "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError("Email non trovata")
        msg = email.message_from_bytes(data[0][1])
        return {
            "account": acc, "id": str(a["id"]), "date": msg.get("Date", ""), "from": _dec(msg.get("From")),
            "to": _dec(msg.get("To")), "cc": _dec(msg.get("Cc")), "reply_to": _dec(msg.get("Reply-To")),
            "subject": _dec(msg.get("Subject")), "body": _text(msg, 20000), "attachments": _attachments(msg),
        }


def list_folders(a):
    if a.get("labels"):
        return list_labels(a)
    out = {}
    for acc in _accounts(a.get("account")):
        try:
            with Box(acc) as c:
                typ, data = c.list()
                out[acc] = [re.sub(r'^.*"[./]" ', "", d.decode(errors="replace")).strip('"') for d in data or []]
        except Exception as e:
            out[acc] = f"Errore: {e}"
    return out


def _special(c, flag, names):
    """Trova una cartella speciale (\\Trash, \\Sent) o per nome."""
    typ, data = c.list()
    folders = []
    for d in data or []:
        line = d.decode(errors="replace")
        m = re.match(r'\((?P<f>[^)]*)\) "(?P<sep>[^"]*)" (?P<n>.+)$', line)
        if not m:
            continue
        name = m.group("n").strip('"')
        folders.append(name)
        if flag.lower() in m.group("f").lower():
            return name
    for n in names:
        for f in folders:
            if f.lower() == n.lower() or f.lower().endswith("." + n.lower()) or f.lower().endswith("/" + n.lower()):
                return f
    return None


def _ids(a):
    ids = a.get("ids") or ([a["id"]] if a.get("id") else [])
    if not ids:
        raise RuntimeError("Nessun id indicato")
    return ",".join(str(x) for x in ids)


def _rw(c, folder):
    typ, _ = c.select('"%s"' % (folder or "INBOX").replace('"', ""))
    if typ != "OK":
        raise RuntimeError(f"Cartella non trovata: {folder}")


def _expunge(c, uids):
    # Con UIDPLUS cancella solo i messaggi indicati, non altri già segnati come eliminati da Mail
    if "UIDPLUS" in getattr(c, "capabilities", ()):
        c.uid("EXPUNGE", uids)
    else:
        c.expunge()


def _move(c, uids, dest):
    caps = c.capabilities if hasattr(c, "capabilities") else ()
    if "MOVE" in caps:
        typ, data = c.uid("MOVE", uids, '"%s"' % dest)
        if typ == "OK":
            return
    typ, data = c.uid("COPY", uids, '"%s"' % dest)
    if typ != "OK":
        raise RuntimeError(f"Spostamento non riuscito verso {dest}: {data}")
    c.uid("STORE", uids, "+FLAGS.SILENT", "(\\Deleted)")
    _expunge(c, uids)


def move_email(a):
    acc = _accounts(a.get("account"))[0]
    with Box(acc) as c:
        _rw(c, a.get("folder"))
        _move(c, _ids(a), a["to_folder"])
        return {"account": acc, "moved": a.get("ids"), "to": a["to_folder"]}


def delete_email(a):
    acc = _accounts(a.get("account"))[0]
    with Box(acc) as c:
        _rw(c, a.get("folder"))
        uids = _ids(a)
        trash = _special(c, "\\Trash", ["Trash", "Cestino", "Deleted", "Deleted Items", "Posta eliminata"])
        if a.get("permanent") or not trash or (a.get("folder") or "INBOX").lower() == (trash or "").lower():
            c.uid("STORE", uids, "+FLAGS.SILENT", "(\\Deleted)")
            _expunge(c, uids)
            return {"account": acc, "deleted_permanently": a.get("ids")}
        _move(c, uids, trash)
        return {"account": acc, "moved_to_trash": a.get("ids"), "trash": trash}


def mark_email(a):
    acc = _accounts(a.get("account"))[0]
    with Box(acc) as c:
        _rw(c, a.get("folder"))
        uids = _ids(a)
        if "read" in a:
            c.uid("STORE", uids, ("+" if a["read"] else "-") + "FLAGS.SILENT", "(\\Seen)")
        if "flagged" in a:
            c.uid("STORE", uids, ("+" if a["flagged"] else "-") + "FLAGS.SILENT", "(\\Flagged)")
        # Etichette anche da mark_email, per i client che hanno ancora l'elenco strumenti vecchio
        add = [_kw(x) for x in a.get("add_labels") or a.get("add") or []]
        rem = [_kw(x) for x in a.get("remove_labels") or a.get("remove") or []]
        if add:
            c.uid("STORE", uids, "+FLAGS.SILENT", "(" + " ".join(add) + ")")
        if rem:
            c.uid("STORE", uids, "-FLAGS.SILENT", "(" + " ".join(rem) + ")")
        return {"account": acc, "updated": a.get("ids"), "labels_added": add, "labels_removed": rem}


def _kw(label):
    """Le keyword IMAP non ammettono spazi e caratteri speciali."""
    k = re.sub(r"[^\w$.\-]", "_", str(label).strip())
    if not k:
        raise RuntimeError("Etichetta vuota")
    return k


def list_labels(a):
    out = {}
    for acc in _accounts(a.get("account")):
        try:
            with Box(acc) as c:
                typ, _ = c.select('"%s"' % (a.get("folder") or "INBOX").replace('"', ""), readonly=True)
                perm = []
                for line in (c.untagged_responses.get("OK") or []) + (c.untagged_responses.get("FLAGS") or []):
                    txt = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
                    perm += [f for f in re.findall(r"[^\s()\[\]]+", txt) if not f.startswith("\\") and f not in ("PERMANENTFLAGS", "FLAGS")]
                typ, data = c.uid("search", None, "SINCE", _since(a.get("days", 365)))
                ids = data[0].split() if data and data[0] else []
                counts = {}
                for i in range(0, len(ids), 500):
                    typ, fl = c.uid("fetch", b",".join(ids[i:i + 500]), "(FLAGS)")
                    for item in fl or []:
                        txt = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
                        m = re.search(r"FLAGS \(([^)]*)\)", txt)
                        for f in (m.group(1).split() if m else []):
                            if not f.startswith("\\"):
                                counts[f] = counts.get(f, 0) + 1
                out[acc] = {"labels_in_use": dict(sorted(counts.items(), key=lambda x: -x[1])),
                            "defined": sorted(set(perm)), "emails_checked": len(ids)}
        except Exception as e:
            out[acc] = f"Errore: {e}"
    return out


def label_email(a):
    acc = _accounts(a.get("account"))[0]
    with Box(acc) as c:
        _rw(c, a.get("folder"))
        uids = _ids(a)
        add = [_kw(x) for x in a.get("add") or []]
        rem = [_kw(x) for x in a.get("remove") or []]
        if add:
            c.uid("STORE", uids, "+FLAGS.SILENT", "(" + " ".join(add) + ")")
        if rem:
            c.uid("STORE", uids, "-FLAGS.SILENT", "(" + " ".join(rem) + ")")
        return {"account": acc, "ids": a.get("ids"), "added": add, "removed": rem}


def _addrs(v):
    if not v:
        return []
    return [x for x in (v if isinstance(v, list) else [v]) if x]


def _save_sent(acc, msg, answered=None):
    """Salva una copia negli Inviati (Aruba non lo fa da solo) e, per le risposte, segna l'originale come risposto."""
    try:
        with Box(acc) as c:
            sent = _special(c, "\\Sent", ["Sent", "Inviati", "Posta inviata", "Sent Items", "Sent Messages"])
            if sent:
                c.append('"%s"' % sent, "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if answered:
                c.select('"%s"' % (answered[0] or "INBOX"))
                c.uid("STORE", str(answered[1]), "+FLAGS.SILENT", "(\\Answered)")
            return sent
    except Exception:
        return None


def _from(acc):
    name = (SIGNATURES.get(acc) or {}).get("name")
    return email.utils.formataddr((name, acc)) if name else acc


def _sign(acc, body, a):
    sig = (SIGNATURES.get(acc) or {}).get("signature")
    if not sig or a.get("signature") is False:
        return body
    return body.rstrip() + "\n\n" + sig.strip("\n") + "\n"


def _smtp(acc, msg, rcpt):
    with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=25) as s:
        s.login(acc, CURRENT.get().get(acc))
        s.send_message(msg, from_addr=acc, to_addrs=rcpt)


def send_email(a):
    acc = _accounts(a.get("account"))[0]
    msg = EmailMessage()
    msg["From"] = _from(acc)
    msg["To"] = ", ".join(_addrs(a.get("to")))
    if _addrs(a.get("cc")):
        msg["Cc"] = ", ".join(_addrs(a.get("cc")))
    msg["Subject"] = a.get("subject", "")
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=acc.split("@")[-1])
    msg.set_content(_sign(acc, a.get("body", ""), a))
    rcpt = _addrs(a.get("to")) + _addrs(a.get("cc")) + _addrs(a.get("bcc"))
    if not rcpt:
        raise RuntimeError("Nessun destinatario")
    _smtp(acc, msg, rcpt)
    return {"account": acc, "sent_to": rcpt, "subject": msg["Subject"], "saved_in": _save_sent(acc, msg)}


def reply_email(a):
    acc = _accounts(a.get("account"))[0]
    with Box(acc) as c:
        _select(c, a.get("folder"))
        typ, data = c.uid("fetch", str(a["id"]), "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError("Email originale non trovata")
        orig = email.message_from_bytes(data[0][1])
    to = [orig.get("Reply-To") or orig.get("From")]
    cc = []
    if a.get("reply_all"):
        others = email.utils.getaddresses([orig.get("To", ""), orig.get("Cc", "")])
        cc = [addr for _, addr in others if addr and addr.lower() != acc]
    subj = _dec(orig.get("Subject"))
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    quoted = "\n".join("> " + line for line in _text(orig, 8000).splitlines())
    body = _sign(acc, a.get("body", ""), a) + f"\nIl {orig.get('Date', '')}, {_dec(orig.get('From'))} ha scritto:\n" + quoted
    msg = EmailMessage()
    msg["From"] = _from(acc)
    msg["To"] = ", ".join(_dec(x) for x in to if x)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subj
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=acc.split("@")[-1])
    if orig.get("Message-ID"):
        msg["In-Reply-To"] = orig["Message-ID"]
        msg["References"] = ((orig.get("References") or "") + " " + orig["Message-ID"]).strip()
    msg.set_content(body)
    rcpt = [addr for _, addr in email.utils.getaddresses([msg["To"]] + ([msg["Cc"]] if cc else [])) if addr]
    _smtp(acc, msg, rcpt)
    saved = _save_sent(acc, msg, answered=(a.get("folder"), a["id"]))
    return {"account": acc, "replied_to": rcpt, "subject": subj, "saved_in": saved}


HANDLERS = {"list_emails": list_emails, "search_emails": search_emails, "read_email": read_email, "list_folders": list_folders,
            "send_email": send_email, "reply_email": reply_email, "move_email": move_email,
            "delete_email": delete_email, "mark_email": mark_email,
            "list_labels": list_labels, "label_email": label_email}


def rpc(req):
    method, rid = req.get("method"), req.get("id")
    if rid is None:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req.get("params", {}).get("protocolVersion", PROTOCOL),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "aruba-studio", "version": "3.3.0"},
            "instructions": "Caselle email Aruba collegate: " + ", ".join(CURRENT.get()) + ". Prima di inviare, rispondere o eliminare chiedi conferma all'utente.",
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = req.get("params", {})
        fn = HANDLERS.get(p.get("name"))
        if not fn:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Strumento sconosciuto"}}
        try:
            res = fn(p.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {"isError": True, "content": [{"type": "text", "text": f"Errore: {e}"}]}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Metodo non supportato"}}


def check_login(user, password):
    """Verifica le credenziali Aruba con un login IMAP. Restituisce None se ok, altrimenti il messaggio d'errore."""
    try:
        c = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=45)
        try:
            c.login(user, password)
        finally:
            try:
                c.logout()
            except Exception:
                pass
        return None
    except imaplib.IMAP4.error:
        return "email o password non corretti"
    except Exception as e:
        return f"server Aruba non raggiungibile ({e})"
