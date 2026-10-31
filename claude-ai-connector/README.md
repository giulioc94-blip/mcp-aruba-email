# Connettore Aruba per claude.ai (ArubaStudio)

Server MCP remoto per le caselle Aruba dello Studio Curcio Terremoto, pensato per claude.ai: niente computer acceso, accesso con login OAuth. Gira come funzione Python su Vercel (progetto `aruba-mcp-studio`, regione fra1).

## Come funziona

- Su claude.ai si aggiunge il connettore con l'URL `https://aruba-mcp-studio.vercel.app/mcp`.
- claude.ai apre `/authorize`, una pagina in italiano dove si inseriscono email e password delle caselle (fino a tre). Le password vengono provate con un login IMAP e poi cifrate (AES-GCM) dentro il token che claude.ai conserva. Il server non salva niente.
- Endpoint: `/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`, `/register` (registrazione dinamica), `/authorize`, `/token` (PKCE S256, refresh di un anno), `/mcp` (JSON-RPC).
- IMAP `imaps.aruba.it:993`, SMTP `smtps.aruba.it:465`. Le email inviate si salvano negli Inviati; la lettura non segna come letto.

## Strumenti (versione 3.3.0)

`list_emails` (anche per etichetta o solo senza etichette), `search_emails`, `read_email`, `list_folders` (con `labels: true` elenca le etichette), `send_email`, `reply_email`, `move_email`, `delete_email` (nel Cestino, o definitivo), `mark_email` (letto, contrassegnato, `add_labels` e `remove_labels`), `list_labels`, `label_email`.

Le etichette della webmail Aruba sono keyword IMAP `x-tag-<nome>`.

## Variabili d'ambiente su Vercel

| Variabile | Cosa |
|---|---|
| `SECRET_KEY` | chiave per cifrare codici e token. Se cambia, il connettore va ricollegato |
| `SIGNATURES` | JSON `{casella: {name, signature}}` con nome mittente e firma di ogni casella |

Mai mettere password o chiavi nel repository.

## Pubblicare una modifica

Dalla cartella: `vercel deploy --prod`, oppure con lo strumento Vercel `create_deployment` passando i file. Le chat di claude.ai già aperte vedono l'elenco strumenti di quando sono partite: i parametri nuovi passano comunque, gli strumenti nuovi compaiono nelle chat nuove.
