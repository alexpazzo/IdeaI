# IdeaI

Trasforma conversazioni pubbliche — oggi i subreddit, domani anche altri forum e siti — in un catalogo di idee imprenditoriali valutate su fattibilità, senso economico e primi abbozzi di soluzione, ordinate per attrattività e sorvegliate nel tempo.

## Cosa fa

1. **Raccoglie** post e commenti da più subreddit (app OAuth Reddit per il flusso continuo, Pullpush per il backfill storico) tramite adapter di sorgente intercambiabili: aggiungere una sorgente non tocca il resto del sistema.
2. **Filtra** il contenuto in arrivo con un modello leggero locale: capisce se è davvero un'idea o un bisogno, e verifica che non sia già catalogato con tre cancelli di deduplicazione (chiave esatta, similarità vettoriale, conferma del modello).
3. **Analizza** ogni idea con un modello più forte e sostituibile — locale o via API esterne OpenAI-compatibili come DeepSeek — producendo problema, cliente, alternative esistenti, MVP, monetizzazione, rischi e citazioni a supporto.
4. **Mostra** le idee più interessanti in una dashboard, con punteggio deterministico spiegato (fattibilità, economia, competizione), evidenze originali e storico delle valutazioni.
5. **Sorveglia** le idee migliori: quando il thread di origine produce commenti nuovi, la valutazione viene aggiornata con una nuova revisione invece di essere ricreata.

## Come è fatto

- **Ingestion → Triaging → Analisi → Scoring → Dashboard**, disaccoppiati da una coda su PostgreSQL (`FOR UPDATE SKIP LOCKED`, nessun broker esterno): i worker si scalano replicando i container.
- **Python** (FastAPI, SQLAlchemy/Alembic, Pydantic) + **PostgreSQL 17 con pgvector** per dati e ricerca semantica.
- **Dashboard** Next.js, che parla solo con l'API e mai direttamente con il database.
- **Inferenza locale astratta**: qualunque endpoint OpenAI-compatibile (Ollama in locale, DeepSeek o altri in cloud). L'hardware non è cablato nel codice: si sceglie un profilo (`cpu`, `gpu-8gb`, `gpu-16gb+`, `cloud`).
- **Docker Compose** per stato e applicazione, con l'inferenza nativa sull'host di default e un profilo Compose per chi preferisce tenerla nei container.

## Stato

Il progetto è **implementato**: Fase 1 (MVP end-to-end) e Fase 2 (catalogazione continua e watch) del documento di architettura.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — documento di architettura decision-complete: schema dati completo, contratti tra componenti, formule di punteggio, configurazione, deployment e roadmap a fasi con criteri di accettazione.
- `src/ideai/` — ingestion OAuth Reddit + backfill Pullpush, coda su PostgreSQL, triage con i cancelli G0/G1/G2, clustering, analisi con revisioni, scoring deterministico, watch adattivo, retention/reembed.
- `src/ideai/api/` — API FastAPI; `dashboard/` — dashboard Next.js.
- La Fase 3 (secondo adapter Hacker News/RSS/Discourse, notifiche, manifest Kubernetes) **non** è implementata: `sources.kind` accetta i sei valori del documento ma solo `reddit` e `reddit_pullpush` hanno un adapter registrato. Abilitare una sorgente diversa produce un errore esplicito all'avvio di worker e doctor.

### Avvio

```bash
cp .env.example .env
# poi valorizza almeno: IDEAI_API_KEY, IDEAI_SALT e (per lo scraping) le tre IDEAI_REDDIT_*
openssl rand -hex 32   # valore per IDEAI_API_KEY
openssl rand -hex 16   # valore per IDEAI_SALT

# inferenza locale (profilo gpu-8gb): Ollama sull'host, fuori dai container
ollama pull qwen3:8b && ollama pull bge-m3:567m

docker compose up -d
docker compose run --rm bootstrap   # esegue `ideai doctor`: sei passi, exit 0 se tutto a posto
```

Poi la dashboard su <http://127.0.0.1:3000> e l'API su <http://127.0.0.1:8000/api/v1/healthz> (`/metrics` e tutti gli altri endpoint richiedono `Authorization: Bearer $IDEAI_API_KEY`).

Aggiungi un subreddit da sorvegliare:

```bash
psql "$IDEAI_DATABASE_URL" -c "INSERT INTO sources(kind,name,config) VALUES ('reddit','reddit','{\"engagement_saturation\":5000,\"cost_per_call_usd\":0.0}')"
psql "$IDEAI_DATABASE_URL" -c "INSERT INTO source_targets(source_id,target_ref,target_kind,poll_interval_s) SELECT id,'r/ItaliaPersonalFinance','subreddit',300 FROM sources WHERE name='reddit'"
```

### Note operative

- **`host.docker.internal` su Linux**: il Compose mappa già `host.docker.internal:host-gateway` per i servizi che chiamano il backend di inferenza. Serve però che Ollama ascolti **anche** sull'interfaccia di rete, non solo su `127.0.0.1`: di default ascolta su `127.0.0.1:11434` e i container non lo raggiungono. Aggiungi al servizio:
  ```ini
  # /etc/systemd/system/ollama.service.d/override.conf
  [Service]
  Environment="OLLAMA_HOST=0.0.0.0:11434"
  ```
  poi `sudo systemctl daemon-reload && sudo systemctl restart ollama` (equivalentemente `Environment=` nell'unità o `OLLAMA_HOST=0.0.0.0:11434 ollama serve`). Verifica con `ss -ltn | grep 11434`: deve comparire `0.0.0.0:11434`. In alternativa abilita il profilo `local-llm` e punta `base_url` a `http://ollama:11434`, così l'inferenza resta dentro la rete Compose.
  Attenzione: il servizio `ollama` del profilo pubblica `127.0.0.1:11434`, la stessa porta di Ollama nativo — abilitare il profilo **mentre** l'inferenza sull'host è attiva fallisce con `address already in use`. Ferma l'uno o l'altro (o togli la pubblicazione della porta se non ti serve dall'host).
- **Modelli locali**: il documento indica `local/minicpm4:8b-q4_K_M`, che però non esiste nel registry Ollama (404). Il default è quindi `local/qwen3:8b` per triage e analista e `local/bge-m3:567m` (1024 dimensioni) per gli embedding. Sono tre variabili: `IDEAI_TRIAGE_MODEL`, `IDEAI_ANALYST_MODEL`, `IDEAI_EMBED_MODEL`.
- **Backend `local`**: usa l'endpoint **nativo** Ollama (`ollama_native`) perché invia lo schema JSON come `format`, ottenendo il decoding vincolato da grammatica. `openai_compat` e `anthropic` restano implementati per ogni altro backend.
- **Hardware**: `IDEAI_HARDWARE_PROFILE` (`cpu`, `gpu-8gb`, `gpu-16gb+`, `cloud`) cambia solo il default di `IDEAI_ANALYST_MODEL`; il profilo `cpu` e `cloud` puntano a `deepseek/deepseek-chat`, quindi serve `DEEPSEEK_API_KEY`.
- **Sviluppo**: `scripts/dev_seed.py` popola il catalogo da fixture registrate (utile quando la rete verso le sorgenti non risponde). Non è un percorso di produzione.
- **Verifica**: `uv run pytest -q` avvia da sé il container `pgvector/pgvector:pg17` di prova sulla porta 55432. Se Docker non è disponibile i test di integrazione falliscono con un messaggio esplicito, non vengono saltati.

## Uso dell'API Reddit

Questa sezione documenta in modo puntuale cosa il software chiede a Reddit, in coerenza con la *Responsible Builder Policy* (trasparenza, limiti rispettati, nessuna rivendita).

- **Modalità di accesso**: OAuth `client_credentials` (application-only), **solo lettura**, una sola app su un solo account. Nessun contesto utente, nessun token di refresh.
- **Endpoint usati**: `GET /r/<subreddit>/new` (con `hot` come fallback) per il flusso continuo; `GET /api/info?id=<fullname>` per aggiornare `score`/`num_comments` di un thread specifico; `GET /comments/<id>` con gestione dei `more` per l'albero dei commenti dei soli thread sorvegliati.
- **Paginazione**: una pagina per ciclo, `IDEAI_SCRAPE_PAGE_SIZE` (default 100). Un ciclo non tenta mai di svuotare un subreddit.
- **Limiti**: tetto dichiarato `IDEAI_REDDIT_RPM` (default 100 richieste/minuto). La fonte di verità è l'header `X-Ratelimit-Remaining`: sotto il 10% del tetto il client dorme fino a `reset_timestamp + 1`. Nessun client multiplo, nessuna aggirazione dei limiti.
- **Volume indicativo**: con 1 subreddit e polling ogni 5 minuti ≈ 288 richieste/giorno; i thread sorvegliati sono riletti con intervallo minimo di 15 minuti, che raddoppia fino a 24 ore quando non ci sono novità e si azzera quando ce ne sono.
- **Nessuna scrittura**: il software non chiama alcun endpoint di `submit`, `comment`, `vote`, `save` o messaggistica. Non pubblica, non vota, non invia nulla; non ha alcuna superficie di interazione con gli utenti Reddit.
- **Dati personali**: l'autore è salvato **solo** come `sha256(username + IDEAI_SALT)`; lo username non è mai persistito in chiaro. Le citazioni salvate sono troncate a `IDEAI_QUOTE_MAX_CHARS` (default 280 caratteri).
- **Retention**: il payload grezzo (`items.raw`) viene azzerato al termine dell'analisi; gli item non catalogati (`rejected`) vengono cancellati dopo `IDEAI_RETENTION_DAYS` (default 30) dal job `maintenance.retention`.
- **Nessun addestramento**: il contenuto pubblico non è usato per addestrare, fare fine-tuning o distillare modelli. Viene passato a un modello **eseguito in locale** (Ollama) per una classificazione e un riassunto una-tantum del catalogo dell'operatore.
- **Nessuna redistribuzione**: il catalogo è a uso personale e non commerciale; il contenuto grezzo non viene rivenduto né ripubblicato.
- **Controllo immediato**: impostare `enabled = false` su `sources` o `source_targets` ferma ogni chiamata successiva verso Reddit senza toccare il codice.
