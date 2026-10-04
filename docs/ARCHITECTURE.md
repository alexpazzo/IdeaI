# IdeaI — Documento di architettura

**Versione**: 1.1 — **Stato**: specifica eseguibile; questo documento descrive il sistema, non contiene codice.
**Natura del repository**: `IdeaI` non contiene codice applicativo (solo `LICENSE`, `README.md` e `docs/`, un commit iniziale). Non esistono componenti riusabili: tutte le scelte tecniche sono fissate qui.

### Come leggere questo documento

Il documento è *decision-complete*: chi lo implementa non deve scegliere nomi di tabelle, colonne, topic di coda, variabili d'ambiente, formati di risposta dei modelli o formule di punteggio. Le decisioni sono dichiarate dove servono e consolidate in due appendici normative:

- **Appendice A** — contratto di configurazione: ogni variabile `IDEAI_*`, con tipo, default e obbligatorietà. **Appendice B** — schema dati completo: DDL normativo di tutte le tabelle, viste e indici.

Le sezioni 1–3 definiscono il modello concettuale e i contratti tra componenti; le sezioni 4–8 le pipeline; le sezioni 9–10 la superficie utente e il deployment; la sezione 11 l'analisi dei vincoli posti, la roadmap a fasi e l'operatività. Identificatori, nomi di tabelle, colonne, chiavi JSON, tag di modello e topic di coda sono in inglese (stabilità di schema); la prosa, i commenti e i campi testuali destinati all'utente sono in italiano.

---

## 1. Visione, obiettivi e modello concettuale

### 1.1 Obiettivo

IdeaI trasforma conversazioni pubbliche in un catalogo di **idee imprenditoriali** valutate su fattibilità, senso economico e primi abbozzi di soluzione, ordinate per attrattività e sorvegliate nel tempo: quando il thread da cui è nata un'idea produce nuova informazione, la valutazione viene aggiornata invece di essere ricreata.

Il sistema è **local-first**: su un singolo host, con uno stato principale (PostgreSQL + pgvector) e un'API OpenAI-compatibile per l'inferenza. È **scale-out-ready**: i worker sono replicabili senza coordinamento aggiuntivo perché la coda è nel database e il claim dei job è atomico.

### 1.2 Requisiti funzionali e loro mappatura

| ID | Requisito | Componente che lo realizza | Sezione |
|---|---|---|---|
| RF-1 | Scraping di post e commenti da più subreddit | Ingestion (`SourceAdapter` Reddit + Pullpush) | §5 |
| RF-2 | Persistenza temporanea dei contenuti grezzi | tabella `items` + job `maintenance.retention` | §4 |
| RF-3 | Modello leggero locale che verifica se un contenuto è già catalogato | Triage + cancelli G0/G1/G2 | §7, §8 |
| RF-4 | Modello più forte e sostituibile che analizza l'idea | Analysis dietro `LLMProvider` | §7 |
| RF-5 | Analisi salvate nel DB e mostrate in una dashboard | `analyses` + API FastAPI + dashboard Next.js | §4, §9 |
| RF-6 | Sorveglianza di post e commenti delle idee migliori con aggiornamento della valutazione | Watch + `watchlist` + `idea_updates` + revisioni di analisi | §8 |

### 1.3 Glossario canonico

| Termine | Definizione operativa |
|---|---|
| **Sorgente** (*Source*) | Sistema esterno da cui si leggono contenuti. Riga in `sources`; implementata da un `SourceAdapter`. |
| **Target** | Unità di lettura dentro una sorgente: un subreddit, un feed RSS, un thread specifico. Riga in `source_targets`; porta il cursore di avanzamento. |
| **Item** | Contenuto normalizzato elementare: un post o un commento. Riga in `items`; ha `kind ∈ {post, comment}`. |
| **Idea** (*Idea cluster*) | Aggregato di item che descrivono lo stesso bisogno/opportunità. Riga in `idea_clusters`, collegamento in `idea_items`. |
| **Analisi** | Valutazione strutturata di un'idea, prodotta dall'agente analista. Riga in `analyses`; identificata da `(idea_id, revision)`. |
| **Revisione** | Nuova analisi dell'idea in un istante successivo; non sovrascrive mai la precedente, la marca con `superseded_by`. |
| **Watch** | Sorveglianza attiva di un'idea: polling del thread di origine per intercettare nuova informazione. Riga in `watchlist`. |
| **Opportunity Score** | Punteggio 0–100 deterministico che ordina il catalogo (formula in §8.4). |

### 1.4 Flusso end-to-end

```mermaid
flowchart LR
    S["Sorgenti<br/>Reddit OAuth, Pullpush,<br/>adapter futuri"] --> ING["Ingestion<br/>SourceAdapter + normalizzazione"]
    ING --> ITEMS[("items<br/>staging")]
    ITEMS --> TRI["Triage<br/>modello leggero + embedding"]
    TRI -->|nuovo| IDEA[("idea_clusters")]
    TRI -->|duplicato| LINK["collegamento<br/>a idea esistente"]
    IDEA --> ANA["Analyze<br/>agente analista"]
    ANA --> AN[("analyses<br/>revisioni")]
    AN --> SCORE["Scoring<br/>opportunity_score"]
    SCORE --> DASH["Dashboard<br/>+ API"]
    SCORE -->|score >= soglia| WATCH["Watch<br/>polling del thread"]
    WATCH -->|nuovi item| ITEMS
    LINK --> SCORE
```

### 1.5 Decisioni portanti di questa sezione

1. **Tre pipeline disaccoppiate.** Ingest, catalogazione (triage + clustering) e analisi comunicano *solo* attraverso la tabella `jobs` e le tabelle di dominio. Non esistono chiamate dirette ingest→analisi: ogni confine è un job durevole, quindi un arresto del processo di analisi non blocca lo scraping e viceversa.
2. **Idempotenza end-to-end.** Ogni handler di job è rieseguibile senza effetti duplicati: le chiavi naturali sono `(source_id, external_id)` per gli item e `(idea_id, revision)` per le analisi; l'inserimento di un item già noto è un upsert `ON CONFLICT (source_id, external_id) DO UPDATE` **deterministico** (§5.2, §8.2): metriche sempre, `body` e `content_hash` solo se il `content_hash` è cambiato. Applicare due volte lo stesso payload produce lo stesso stato, quindi la proprietà resta valida. La consegna dei job è *at-least-once* (§6.3), quindi questa proprietà è un requisito, non un'ottimizzazione.
3. **L'intelligenza è sostituibile in due ruoli distinti e indipendenti**: `triage` (leggero, locale, alto volume) e `analyst` (avanzato, anche cloud, basso volume). Nessun ruolo è cablato nel codice di pipeline: entrambi sono risolti a runtime dal gateway LLM (§7).
4. **Lingua dei dati.** I contenuti sorgente restano nella lingua originale. Le analisi sono prodotte in italiano (`analyses.lang = "it"`); i campi strutturati (enum, tag, metriche, nomi di colonna) restano in inglese per stabilità di schema e per non invalidare gli embedding al cambio di lingua dell'interfaccia.

---

## 2. Architettura logica: componenti e responsabilità

### 2.1 Diagramma dei componenti

```mermaid
flowchart TB
    subgraph EST["Sistemi esterni"]
        REDDIT["Reddit API (OAuth)"]
        PULL["Pullpush (backfill)"]
        FUTURE["Adapter futuri<br/>Hacker News, RSS, Discourse, GitHub"]
        CLOUD["Backend LLM esterni<br/>DeepSeek, vLLM, Anthropic"]
    end

    subgraph CORE["IdeaI"]
        ING["Ingestion"]
        TRI["Triage"]
        ANA["Analysis"]
        SCO["Scoring"]
        WAT["Watch"]
        API["API FastAPI"]
        DASH["Dashboard Next.js"]
        GW["LLM Gateway"]
        JR["Job Runtime"]
    end

    subgraph STATE["Stato"]
        PG[("PostgreSQL 17<br/>+ pgvector")]
        LLM["Backend inferenza locale<br/>endpoint OpenAI-compatibile"]
        MODELS["Filesystem modelli<br/>volume Ollama"]
    end

    REDDIT --> ING
    PULL --> ING
    FUTURE --> ING
    ING --> PG
    TRI --> PG
    ANA --> PG
    SCO --> PG
    WAT --> PG
    API --> PG
    DASH --> API
    TRI --> GW
    ANA --> GW
    ING --> GW
    GW --> LLM
    GW --> CLOUD
    LLM --> MODELS
    JR --> ING
    JR --> TRI
    JR --> ANA
    JR --> SCO
    JR --> WAT
    JR --> PG
```

### 2.2 Tabella dei componenti

| Componente | Responsabilità in una riga | Topic consumati | Tabelle scritte | Dipendenze esterne |
|---|---|---|---|---|
| **Ingestion** | Legge le sorgenti abilitate, normalizza e persiste item grezzi | `pipeline.scrape` | `items`, `source_targets`, `rate_limits`, `source_calls_daily` | API della sorgente |
| **Triage** | Calcola l'embedding dell'item, applica il cancello G1, classifica con il modello leggero e deduplica | `pipeline.triage`, `pipeline.cluster` | `items`, `embeddings`, `idea_clusters`, `idea_items` | LLM Gateway (ruolo `triage`) |
| **Analysis** | Produce l'analisi strutturata di un'idea e ne apre una nuova revisione | `pipeline.analyze` | `analyses`, `idea_clusters`, `idea_updates`, `items` (`raw`, `state`), `embeddings` | LLM Gateway (ruolo `analyst`) |
| **Scoring** | Calcola `opportunity_score` in modo deterministico e aggiorna il ranking | `pipeline.score` | `idea_clusters`, `analyses`, `watchlist` | nessuna |
| **Watch** | Sorveglia i thread delle idee promettenti e accoda le ri-analisi | `pipeline.watch` | `watchlist`, `idea_updates`, `items`, `idea_items` | API della sorgente |
| **API** | Espone in sola lettura/scrittura controllata catalogo, sorgenti e coda | — (HTTP) | `settings_overrides`, `watchlist`, `jobs`, `source_targets`, `idea_clusters`, `idea_items`, `items`, `embeddings`, `idea_updates` | — |
| **Dashboard** | Interfaccia umana: catalogo, dettaglio, sorgenti, operatività | — (HTTP → API) | nessuna (mai accesso diretto al DB) | API |
| **LLM Gateway** | Unico punto che conosce provider, prompt, schema JSON, retry e costi | — (libreria interna) | `llm_calls`, `llm_cache` | Backend di inferenza |
| **Job Runtime** | Claim, lease, retry, dead letter, scheduler, rate limiting, budget | tutti | `jobs`, `rate_limits`, `llm_calls` | PostgreSQL (advisory lock) |

**Stato persistente**: PostgreSQL 17 + pgvector (volume `pgdata`) contiene tutto il dominio — 15 tabelle, 3 viste — ed è ricostruibile solo riscrapando, quindi è l'unico backup obbligatorio. Il backend di inferenza (processo sull'host o container) e il filesystem dei modelli (volume `ollama`, pesi riscaricabili con `ollama pull`) non contengono dati di dominio.

### 2.3 Confini

- **LLM Gateway** — unico componente che conosce provider, prompt, schemi JSON, retry e contabilizzazione. Nessun altro modulo importa SDK di provider né costruisce richieste HTTP verso un modello.
- **Job Runtime** — unico componente che tocca `jobs`: claim dei job, lease con heartbeat, retry con backoff, dead letter, scheduler con lock consultivo, rate limiting verso l'esterno, budget di spesa.
- **Backend inferenza** — qualunque endpoint che esponga `/v1/chat/completions` e `/v1/embeddings` (Ollama, vLLM, llama.cpp server, LM Studio, DeepSeek) oppure l'endpoint nativo Ollama. Il dominio non importa mai SDK specifici.
- **Isolamento tra componenti** — i componenti non si conoscono tra loro: condividono esclusivamente le porte della §3 e il database. La Dashboard non parla mai con il database; l'API non esegue lavoro asincrono (accoda job).

---

## 3. Porte e contratti

Le porte vivono in `src/ideai/ports/` e sono `typing.Protocol`: contratti strutturali, verificabili dai type checker senza ereditarietà. Le firme seguenti sono vincolanti e vanno implementate verbatim.

### 3.1 Porta sorgenti

```python
# src/ideai/ports/sources.py
class SourceAdapter(Protocol):
    kind: str                      # "reddit", "reddit_pullpush", "hackernews", "rss", "discourse"
    def capabilities(self) -> SourceCapabilities: ...
        # has_comments, supports_incremental, supports_backfill, supports_search
    def rate_policy(self) -> RatePolicy: ...
        # requests_per_minute, burst, cost_per_call_usd, min_interval_ms
    async def fetch_new(self, target: SourceTarget, cursor: Cursor | None, limit: int) -> FetchPage: ...
    async def fetch_thread(self, thread_ref: ExternalRef, since: datetime | None) -> FetchPage: ...
    def normalize(self, payload: dict) -> NormalizedItem: ...
```

Il metodo `normalize` è **sincrono e puro**: riceve il payload grezzo dell'API e restituisce la forma canonica. È l'unico punto in cui si conosce la forma dei dati della sorgente; da lì in poi il sistema parla solo di `NormalizedItem`.

### 3.2 Porta LLM

```python
# src/ideai/ports/llm.py
class LLMProvider(Protocol):
    name: str
    async def complete_json(self, *, model: str, system: str, user: str, schema: dict,
                            max_tokens: int, temperature: float) -> LLMResult: ...
    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]: ...
```

`complete_json` non restituisce testo libero: restituisce un oggetto già validato contro `schema` (§7.3). Una risposta non conforme non esce dalla porta.

### 3.3 Porta coda

```python
# src/ideai/ports/queue.py
class Queue(Protocol):
    async def enqueue(self, topic: str, payload: dict, *, dedup_key: str | None = None,
                      priority: int = 100, run_after: datetime | None = None) -> int: ...
    async def claim(self, topics: list[str], worker_id: str, lease_s: int) -> Job | None: ...
    async def heartbeat(self, job_id: int) -> None: ...
    async def complete(self, job_id: int) -> None: ...
    async def fail(self, job_id: int, error: str, *, retryable: bool = True) -> None: ...   # retryable=False → dead immediato
```

`dedup_key` può essere assente; se presente rende l'enqueue idempotente finché esiste un job pendente o in esecuzione con la stessa chiave (§6.2).

### 3.4 Strutture dati condivise

```python
# src/ideai/domain.py
Cursor = dict[str, Any]          # alias, non un dataclass: forma nota solo all'adapter che lo produce

@dataclass(frozen=True)
class ExternalRef:
    source_kind: str            # "reddit", "reddit_pullpush", ...
    external_id: str            # fullname "t3_abc" / "t1_xyz", o id equivalente

@dataclass(frozen=True)
class SourceTarget:
    id: int
    source_id: int
    target_ref: str             # "r/ItaliaPersonalFinance", feed URL, thread fullname
    target_kind: str            # "subreddit" | "thread" | "feed" | "query"
    cursor: Cursor | None

@dataclass(frozen=True)
class SourceCapabilities:
    has_comments: bool
    supports_incremental: bool
    supports_backfill: bool
    supports_search: bool

@dataclass(frozen=True)
class RatePolicy:
    requests_per_minute: int
    burst: int
    cost_per_call_usd: float
    min_interval_ms: int

@dataclass(frozen=True)
class NormalizedItem:
    source_kind: str            # "reddit", "reddit_pullpush", ...
    external_id: str            # id nella sorgente (es. fullname "t3_abc")
    kind: str                   # "post" | "comment"
    thread_external_id: str     # post radice del thread (per un post: se stesso)
    parent_external_id: str | None
    title: str | None           # NULL per i commenti
    body: str
    author_hash: str | None     # sha256(autore + IDEAI_SALT), NULL se [deleted]
    url: str | None
    score: int | None
    num_comments: int | None
    lang: str | None
    created_at: datetime
    edited_at: datetime | None
    raw: dict                   # payload grezzo, azzerato dopo l'analisi
    content_hash: str           # sha256(body normalizzato)

@dataclass(frozen=True)
class FetchPage:
    items: list[NormalizedItem]
    next_cursor: Cursor | None
    exhausted: bool

@dataclass(frozen=True)
class LLMResult:
    content: dict               # già validato contro lo schema del ruolo
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    cache_hit: bool

@dataclass(frozen=True)
class Job:
    id: int
    topic: str
    payload: dict
    state: str
    attempts: int
    max_attempts: int
    locked_by: str | None
    run_after: datetime
```

`Cursor` è opaco per il core: è un dizionario serializzabile in JSON, la cui forma è nota solo all'adapter che lo produce. Per Reddit contiene `{"last_fullname": ..., "last_created_utc": ...}` (§5.4).

### 3.5 Specifica dei modelli

Un modello si indica sempre con il formato **`<backend>/<model>`**:

- `local/minicpm4:8b-q4_K_M` — backend locale, tag Ollama.
- `deepseek/deepseek-chat` — backend OpenAI-compatibile esterno.

La risoluzione avviene tramite `resolve_model_spec(spec, backends)`: il backend deve esistere in `IDEAI_LLM_BACKENDS`, il modello deve essere dichiarato o risolvibile; **backend sconosciuto o modello mancante producono un errore esplicito all'avvio**, mai un fallback silenzioso. Le due variabili che usano questo formato sono `IDEAI_TRIAGE_MODEL` e `IDEAI_ANALYST_MODEL`; i modelli di embedding usano lo stesso formato tramite `IDEAI_EMBED_MODEL`.

---

## 4. Modello dati (PostgreSQL 17 + pgvector)

### 4.1 Principi

- **15 tabelle, 3 viste.** Il DDL normativo completo è nell'Appendice B; questa sezione ne fissa semantica e vincoli.
- **Enum come `CHECK` + `StrEnum` Python**: niente tipi `ENUM` nativi, così le migrazioni non richiedono `ALTER TYPE` e l'aggiunta di un valore è una modifica di vincolo.
- **Chiavi**: `id bigint GENERATED ALWAYS AS IDENTITY` dove serve una surrogate; chiavi naturali uniche dove esiste un'identità esterna (`(source_id, external_id)`, `(idea_id, revision)`, `cache_key`).
- **Tempi**: sempre `timestamptz` in UTC.
- **Payload grezzo**: `items.raw jsonb` esiste per riprocessare un item se un prompt migliora, e viene **azzerato dopo l'analisi** dell'idea (§4.5).
- **Nessuna cancellazione implicita**: gli item non vengono mai cancellati dall'analisi; la sola cancellazione di massa è `maintenance.retention`.

### 4.2 Le 15 tabelle

**`sources`** — registro delle sorgenti. `id, kind, name, config jsonb, enabled, created_at`; `unique(kind, name)`; `kind` ∈ {`reddit`, `reddit_pullpush`, `hackernews`, `rss`, `discourse`, `github_issues`}. `config` contiene i parametri che non appartengono al codice: `cost_per_call_usd` (0 per Pullpush, 0,00024 USD per Reddit in regime commerciale), `rpm`, `engagement_saturation` (costante di saturazione dello scoring, default 5000, §8.4), `user_agent`.

**`source_targets`** — unità di lettura. `id, source_id, target_ref, target_kind, cursor jsonb, last_polled_at, next_poll_at, poll_interval_s, enabled, error_count, last_error`; `unique(source_id, target_ref)`; `target_kind` ∈ {`subreddit`, `thread`, `feed`, `query`}; `poll_interval_s` default `IDEAI_DEFAULT_POLL_INTERVAL_S` (300); indice parziale su `(next_poll_at) WHERE enabled`, quello che usa lo scheduler a ogni tick.

**`items`** — contenuto normalizzato elementare. Colonne: `id, source_id, external_id, kind, thread_external_id, parent_external_id, title, body, author_hash, url, score, num_comments, lang, created_at, edited_at, fetched_at, content_hash, raw, state, category, triage_confidence, tags, idea_id, attempts, reject_reason`; `unique(source_id, external_id)`; indici su `(state)`, `(thread_external_id)`, `(idea_id)` e GIN su `tags`. `kind` ∈ {`post`, `comment`}; `state` ∈ {`new`, `triaged`, `rejected`, `candidate`, `analyzed`, `archived`} (macchina a stati in §8.1); `category` ∈ {`product_idea`, `pain_point`, `market_signal`, `question`, `announcement`, `spam`, `off_topic`} oppure `NULL` prima del triage; `reject_reason` ∈ {`exact_dup`, `not_idea`, `off_topic`, `spam`, `removed`}; `removed` è l'unico valore prodotto dall'archiviazione del corpo (`[removed]`/`[deleted]`), non esiste un valore per la retention perché le righe scadute vengono cancellate e il motivo non è mai osservabile. `content_hash` = `sha256` del corpo normalizzato (spazi collassati, minuscole): è la base del cancello G0 (duplicati esatti, §8.2).

**`idea_clusters`** — l'idea. `id, title, canonical_summary, status, opportunity_score, score_version, first_seen_at, last_activity_at, item_count, source_kinds`; `status` ∈ {`new`, `analyzed`, `watching`, `archived`, `rejected`} (macchina a stati in §8.1bis); indice su `(opportunity_score desc)`. `item_count` e `source_kinds` sono denormalizzati e ricalcolati dal job `pipeline.score`: la dashboard ordina senza aggregazioni costose.

**`idea_items`** — collegamento item↔idea. `idea_id, item_id, role, similarity, added_at`; PK `(idea_id, item_id)`; `role` ∈ {`seed` (item che ha generato l'idea), `evidence` (item collegato da un cancello semantico, §8.2), `comment` (commento del thread promosso a evidenza), `update` (item arrivato dopo l'analisi, dal watch)}.

**`analyses`** — valutazione strutturata, immutabile. `id, idea_id, revision, lang, model_spec, provider, prompt_version, schema_version, payload jsonb, feasibility_score, economics_score, competition_score, verdict, confidence, opportunity_score, tokens_in, tokens_out, cost_usd, latency_ms, created_at, superseded_by`; `unique(idea_id, revision)`. Regola: **il payload di un'analisi non viene mai aggiornato**; una ri-analisi produce `revision = max(revision) + 1` e valorizza `superseded_by` sulla riga precedente. Le colonne scalari sono copie estratte dal payload per ordinamento e filtraggio.

**`embeddings`** — vettori per deduplicazione e ricerca semantica. `id, owner_kind, owner_id, model, dim, vec vector(1024)`; `owner_kind` ∈ {`item`, `idea`}; `unique(owner_kind, owner_id, model)`; indice **HNSW** `embeddings_idea_vec_idx` su `vec vector_cosine_ops` (`m = 16, ef_construction = 64`) **parziale su `owner_kind = 'idea'`**: solo le idee sono oggetto di ricerca per similarità (§8.2), gli item no.

**`jobs`** — coda durevole. `id, topic, dedup_key, payload jsonb, state, priority, attempts, max_attempts, run_after, locked_by, locked_at, heartbeat_at, last_error, created_at, finished_at`; `state` ∈ {`pending`, `running`, `done`, `dead`}; unicità parziale su `dedup_key WHERE state IN ('pending','running')`; indice parziale `(topic, priority, run_after) WHERE state = 'pending'`; `topic` vincolato agli otto valori di §6.3. Un errore ritentabile **non** crea uno stato intermedio: il job torna `pending` con `run_after` posticipato. `failed` non esiste perché uno stato che il claim non reclama sarebbe lavoro perso.

**`rate_limits`** — contatore di finestra per bucket. `bucket text PRIMARY KEY, window_start timestamptz, used int`; finestra di `IDEAI_RATE_LIMIT_WINDOW_S` (60) secondi; `bucket = "<source_kind>:<credential_id>"`, ad esempio `reddit:default` o `reddit_pullpush:anon` (il bucket usa `sources.kind`, quindi il backfill Pullpush ha un bucket distinto da Reddit OAuth).

**`source_calls_daily`** — contabilità giornaliera delle chiamate alle sorgenti esterne a pagamento. `day date, source_id bigint REFERENCES sources(id) ON DELETE CASCADE, calls integer, cost_usd numeric(12,6)`, PK `(day, source_id)`; aggiornata con upsert (`calls += 1`, `cost_usd += sources.config.cost_per_call_usd`) nello stesso passo del rate limit di §6.7. Il costo delle sorgenti resta separato e visibile accanto a quello LLM.

**`llm_calls`** — registro economico di ogni chiamata a un modello. `id, role, provider, model, prompt_hash, schema_version, tokens_in, tokens_out, cost_usd, latency_ms, status, error, created_at`; `role` ∈ {`triage`, `analyst`, `embed`}; `status` ∈ {`ok`, `error`, `invalid_json`}. È la fonte del budget giornaliero e del grafico costi in dashboard.

**`llm_cache`** — cache di risposte. `cache_key text PRIMARY KEY, model_spec text NOT NULL, response jsonb NOT NULL, created_at`; `cache_key = sha256(prompt_hash + schema_version + model_spec + temperature + testo normalizzato)` (§6.9). Non è una chiave naturale del dominio: è un digest che incorpora tutto ciò che rende una risposta riusabile.

**`watchlist`** — sorveglianza attiva. `idea_id PRIMARY KEY, enabled, interval_s, mode, last_checked_at, last_change_at, added_by`; `mode` ∈ {`comments_only`, `thread_full`}: `comments_only` legge **solo i nuovi commenti**, `thread_full` legge i commenti **e** rifà il refresh del post (`reddit.info`, metriche ed eventuale `edit`); `added_by` ∈ {`auto`, `manual`}; `interval_s` default 3600 e adattato come in §8.5.

**`idea_updates`** — timeline delle novità di un'idea. `id, idea_id, item_id, kind, summary, created_at`; `kind` ∈ {`new_comment`, `new_post`, `edit`, `analysis_revision`, `watch_paused`, `merged`}; `item_id` è `NULL` per `analysis_revision`, `watch_paused` e `merged`.

**`settings_overrides`** — parametri modificabili a caldo. `key text PRIMARY KEY, value jsonb, updated_at`; contiene i pesi dello scoring e le soglie che la dashboard può cambiare senza redeploy, mentre i default vivono nel codice.

### 4.3 Diagramma entità-relazione

```mermaid
erDiagram
    sources ||--o{ source_targets : "espone"
    sources ||--o{ items : "produce"
    sources ||--o{ source_calls_daily : "costo"
    idea_clusters ||--o{ idea_items : "aggrega"
    items ||--o{ idea_items : "appartiene"
    idea_clusters ||--o{ analyses : "ha revisioni"
    analyses ||--o| analyses : "superseded_by"
    idea_clusters ||--o| watchlist : "sorvegliata"
    idea_clusters ||--o{ idea_updates : "timeline"
    items ||--o{ idea_updates : "origina"
    idea_clusters ||--o{ embeddings : "vettore idea"
    items ||--o{ embeddings : "vettore item"
    jobs
    rate_limits
    llm_calls
    llm_cache
    settings_overrides
```

`embeddings` è polimorfica per progetto (`owner_kind` + `owner_id`) e non ha vincolo di chiave esterna: le relazioni nel diagramma sono logiche, non vincoli di database. Le tabelle isolate (`jobs`, `rate_limits`, `llm_calls`, `llm_cache`, `settings_overrides`) non hanno chiavi esterne verso il dominio: è ciò che permette di svuotare la coda o ruotare la cache senza toccare le idee.

### 4.4 Viste

| Vista | Contenuto | Consumatore |
|---|---|---|
| `v_idea_ranking` | idea + ultima analisi non superata + `opportunity_score` + `category`/`tags` del seed + stato del watch (`watch_enabled`, `watch_mode`, `watch_interval_s`) | `GET /ideas`, dashboard `/` |
| `v_daily_llm_cost` | somma `cost_usd`, token e chiamate per giorno e per `role` | budget, dashboard `/ops` |
| `v_queue_health` | job per `topic` e `state`, età del più vecchio `pending`, job `dead` | `GET /jobs`, `GET /stats`, dashboard `/ops` |

### 4.5 Embedding, dimensione e retention

**Decisione sull'embedding.** La colonna è `vector(1024)` fissa. Modello di default: `local/bge-m3:567m` (1024 dimensioni, multilingua — necessario perché le fonti non sono tutte in inglese e il confronto semantico avviene fra lingue). Motivo tecnico del limite: gli indici HNSW di pgvector accettano al massimo 2000 dimensioni per `vector` (4000 per `halfvec`), quindi 1024 è nell'intervallo sicuro con margine.

Conseguenze operative, da dichiarare nel codice:

- il servizio di embedding confronta la dimensione dichiarata dal modello con `IDEAI_EMBED_DIM` all'avvio e **rifiuta di partire** in caso di mismatch;
- cambiare modello o dimensione richiede: migrazione della colonna, `ideai reembed --all` (job `maintenance.reembed`, che ricalcola tutti i vettori e ricostruisce l'indice HNSW), aggiornamento di `IDEAI_EMBED_MODEL` e `IDEAI_EMBED_DIM`;
- i vettori vecchi restano leggibili durante il ricalcolo perché `embeddings.model` distingue i modelli e la query di similarità filtra sul modello corrente.

**Embedding dell'idea.** Oltre agli item, il sistema embedda l'**idea** come unità confrontabile: `owner_kind = 'idea'`, `owner_id = idea_clusters.id`, testo = `title + "\n" + canonical_summary`. Il vettore viene calcolato alla creazione dell'idea e **a ogni revisione che rigenera il sommario canonico** (§8.3), così il confronto di similarità di G1/G2 usa sempre il testo aggiornato. Gli item vengono embeddati al triage (§8.2); senza il vettore dell'idea il cancello G1 non avrebbe nulla con cui confrontarsi.

**Indice e recall.** L'indice HNSW è **parziale**: `embeddings_idea_vec_idx … WHERE owner_kind = 'idea'` (§4.2). Un indice unico su item e idee sarebbe un errore: le query dei cancelli G1/G2 e di `/ideas/search` filtrano `owner_kind = 'idea'` e `model = <corrente>`, e con HNSW un filtro applicato **dopo** lo scan riduce drasticamente il recall quando gli item superano di molto le idee. Gli item non hanno indice ANN perché nessuna query li cerca per similarità. Le stesse query impostano `SET LOCAL hnsw.iterative_scan = relaxed_order`, che richiede **pgvector ≥ 0.8.0** (dichiarato come prerequisito in §10.1 e verificato da `ideai doctor` e `/readyz`). Il parametro è per-transazione: non è una variabile d'ambiente del server.

**Retention (requisito RF-2, "salvati temporaneamente").** Il job `maintenance.retention` gira una volta al giorno; per gli item applica due regole:

1. gli item in `state = 'rejected'` più vecchi di `IDEAI_RETENTION_DAYS` (default 30) vengono cancellati insieme al loro `raw`;
2. per gli item promossi (`candidate`, `analyzed`) il campo `raw` viene azzerato al termine della prima analisi dell'idea a cui appartengono; restano i campi canonici (titolo, corpo, autore hashato, url, metriche).

---

## 5. Ingestion multi-sorgente

### 5.1 Impianto

L'ingestione è un pattern *port & adapter*: il core conosce `SourceAdapter` e `NormalizedItem`, mai Reddit. Ogni adapter si registra in un registry dichiarativo (`adapters/sources/__init__.py`), che mappa `sources.kind` → classe. Aggiungere una sorgente significa aggiungere una classe e una riga in `sources`: **zero modifiche** a pipeline, schema, scoring e dashboard.

| Capacità | Significato operativo |
|---|---|
| `has_comments` | l'adapter sa leggere i commenti di un thread (`fetch_thread`) |
| `supports_incremental` | l'adapter accetta un cursore e restituisce solo il nuovo |
| `supports_backfill` | l'adapter sa leggere contenuto storico oltre la finestra recente |
| `supports_search` | l'adapter sa cercare per query testuale |

Chi crea una riga di `watchlist` (il job `pipeline.score`, §8.5) e chi accoda il watch iniziale (il cluster, §8.2) consultano le capacità: un'idea il cui post di origine appartiene a una sorgente senza `has_comments` non entra mai in `watchlist`, e per essa il cluster accoda direttamente `pipeline.analyze` invece di `pipeline.watch`.

### 5.2 Ciclo di scrape Reddit

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Scheduler
    participant Q as jobs
    participant W as Worker ingest
    participant RL as rate_limits
    participant R as Reddit OAuth API
    participant DB as items / source_targets

    SCH->>Q: enqueue pipeline.scrape (dedup_key deterministico)
    W->>Q: claim(topics, worker_id, lease)
    Q-->>W: job pipeline.scrape
    W->>RL: advisory_xact_lock(bucket) + upsert finestra
    RL-->>W: slot disponibile o attesa
    W->>R: subreddit.new(limit=IDEAI_SCRAPE_PAGE_SIZE)
    R-->>W: listing + header X-Ratelimit-Remaining / Reset
    W->>DB: INSERT items ON CONFLICT (source_id, external_id) DO UPDATE (metriche, body se content_hash cambia)
    W->>DB: UPDATE source_targets SET cursor, last_polled_at, error_count
    W->>Q: enqueue pipeline.triage (batch)
    W->>Q: complete(job_id)
```

**Politica di upsert degli item.** L'ingest non è più un `DO NOTHING` puro: un item già noto viene aggiornato, altrimenti un contenuto modificato dopo la prima lettura resterebbe congelato e `idea_updates.kind = 'edit'` sarebbe irraggiungibile.

```sql
INSERT INTO items (…) VALUES (…)
ON CONFLICT (source_id, external_id) DO UPDATE
   SET score = EXCLUDED.score,
       num_comments = EXCLUDED.num_comments,
       edited_at = EXCLUDED.edited_at,
       fetched_at = EXCLUDED.fetched_at,
       body = CASE WHEN items.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                   THEN EXCLUDED.body ELSE items.body END,
       content_hash = CASE WHEN items.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                           THEN EXCLUDED.content_hash ELSE items.content_hash END;
```

`raw` non viene mai ripopolato se l'item è già `candidate` o `analyzed` (il payload grezzo non serve più, §4.5). Se `content_hash` cambia su un item collegato a un'idea, il job scrive una riga `idea_updates.kind = 'edit'`; se il corpo diventa `[removed]`/`[deleted]` l'item passa a `state = 'archived'` con `reject_reason = 'removed'` (l'item non viene cancellato: l'analisi lo cita).

### 5.3 Reddit primario: autenticazione, limiti, endpoint

- **Autenticazione**: app OAuth con flusso `client_credentials` (read-only, nessun contesto utente) via **Async PRAW** (`asyncpraw`: PRAW è sincrono e le porte della §3.1 sono `async`), credenziali `IDEAI_REDDIT_CLIENT_ID` e `IDEAI_REDDIT_CLIENT_SECRET`. `user_agent` = `IDEAI_REDDIT_USER_AGENT`, formato obbligatorio `ideai/<version> by /u/<owner>`.
- **Limite**: il tier gratuito OAuth concede **100 richieste/minuto** per client (10/min senza autenticazione). La fonte di verità è l'header `X-Ratelimit-Remaining`, non il contatore locale: `rate_limits` è una guardia che evita di superare il limite, non un sostituto dell'header. `IDEAI_REDDIT_RPM` (default 100) imposta il tetto dichiarato; se l'header scende sotto il 10% il worker rallenta fino al reset.
- **Uso commerciale**: se il prodotto viene venduto, Reddit richiede un contratto a pagamento, circa **0,24 USD per 1.000 chiamate**. Il costo unitario è in `sources.config.cost_per_call_usd` e confluisce nella tabella `source_calls_daily` (§7.8), aggiornata nello stesso passo del rate limit: il cambio di regime è un valore di configurazione, non una modifica di codice.
- **Endpoint usati**:
  - `subreddit.new(limit=IDEAI_SCRAPE_PAGE_SIZE)` e `subreddit.hot(limit=…)` per il going-forward (nuovo contenuto e contenuto che sta salendo);
  - `reddit.info(fullnames=[...])` per il refresh mirato di un singolo thread (score e `num_comments` aggiornati);
  - `submission.comments.replace_more(limit=0)` seguito da `submission.comments.list()` per scaricare l'albero dei commenti appiattito.
- **Paginazione**: `IDEAI_SCRAPE_PAGE_SIZE` (default 100, massimo consentito dall'API). Una pagina per ciclo di scrape, poi il cursore avanza: un ciclo non tenta mai di svuotare un subreddit.

### 5.4 Cursore e stato del target

`source_targets.cursor` per Reddit contiene:

```json
{"last_fullname": "t3_1abc2de", "last_created_utc": 1759300000}
```

`last_fullname` è il fullname `t3_*` (post) o `t1_*` (commento) dell'ultimo item visto e determina il punto di ripartenza; `last_created_utc` è la seconda fonte di verità per rilevare riordini della listing. `last_polled_at` e `next_poll_at` governano lo scheduling; `error_count` e `last_error` la salute del target (§5.7).

### 5.5 Watch di un thread

Il watch legge il thread sorvegliato e produce solo item nuovi: `reddit.info(fullnames=[thread])` per aggiornare `score` e `num_comments`, poi `fetch_thread` che scarica i commenti e li filtra localmente con `created_utc > watchlist.last_checked_at`.

L'API di Reddit non consente di filtrare i commenti per data: il filtro è locale e questo è un **costo esplicito in chiamate** (una richiesta per thread per ciclo, più le pagine di `more`), motivo per cui il watch è limitato alle idee sopra soglia e l'intervallo è adattivo (§8.5).

### 5.6 Backfill: adapter `reddit_pullpush`

Il cold-start di un subreddit nuovo richiede contenuto storico che l'API OAuth non offre in modo economico. L'adapter `reddit_pullpush` è registrato come sorgente **distinta** da `reddit` perché ha rate policy, affidabilità e resa diverse:

- endpoint: `api.pullpush.io/reddit/search/submission` e `api.pullpush.io/reddit/search/comment`;
- parametri: `before`/`after` in epoch, `size=100`, paginazione con `before` decrescente;
- nessuna autenticazione; politica di cortesia **≤1 richiesta/secondo** (`IDEAI_PULLPUSH_RPM`, default 60);
- i commenti non hanno struttura ad albero affidabile: vengono normalizzati con `parent_external_id` quando disponibile e `thread_external_id` ricavato dal link;
- la deduplicazione è garantita dalla chiave `(source_id, external_id)`: se un item è già arrivato da `reddit`, il backfill non produce duplicati semantici grazie ai cancelli G0/G1.

Il backfill è **una tantum per target**: quando `exhausted = true` il target Pullpush viene disabilitato automaticamente e il going-forward resta su OAuth.

### 5.7 Dati personali

- `author_hash = sha256(author_name + IDEAI_SALT)`; lo username non viene **mai** persistito in chiaro.
- Autori `[deleted]` → `author_hash = NULL`.
- Le citazioni salvate in `analyses.payload.evidence_quotes[].quote` sono troncate a `IDEAI_QUOTE_MAX_CHARS` (default 280): citazione breve per contesto, non riproduzione del contenuto.
- Nessuna rivendita del contenuto grezzo: `items.raw` è materiale di lavoro temporaneo, azzerato dopo l'analisi.

### 5.8 Sorgenti future: contratto, non implementazione

| `kind` | Endpoint | `has_comments` | `supports_incremental` | `supports_backfill` | Note |
|---|---|---|---|---|---|
| `reddit` | OAuth API | sì | sì | no | sorgente primaria |
| `reddit_pullpush` | `api.pullpush.io` | sì (parziale) | sì | sì | cold-start |
| `hackernews` | Algolia HN Search API | sì | sì | sì | ottimo secondo adapter |
| `rss` | feed Atom/RSS | no | no | no | blog e newsletter di settore |
| `discourse` | `/latest.json`, `/t/{id}.json` | sì | sì | sì | forum con API JSON |
| `github_issues` | REST `/repos/{r}/issues` | sì | sì | sì | segnali di bisogno tecnico |

### 5.9 Errori

- `429` o `5xx`: backoff esponenziale con jitter, `source_targets.error_count += 1`, `last_error` valorizzato.
- Dopo 10 fallimenti consecutivi il target è disabilitato automaticamente ed è visibile in `/sources` con il motivo.
- `404`/`403` su subreddit privato, inesistente o vietato: target disabilitato immediatamente, senza retry, con `last_error` esplicito.
- Successo → `error_count = 0`.

---

## 6. Coda, scheduler e rate limiting (solo PostgreSQL)

### 6.1 Nessun broker

Non esiste Redis, RabbitMQ o Celery. La coda è la tabella `jobs` e il claim è un `UPDATE` atomico con `FOR UPDATE SKIP LOCKED`: più worker possono girare in parallelo sullo stesso database senza coordinarsi. La semantica è **at-least-once**: un job può essere eseguito due volte (worker ucciso dopo l'effetto e prima di `complete`), quindi ogni handler è idempotente per contratto (§1.5).

### 6.2 Claim

```sql
UPDATE jobs
   SET state = 'running',
       locked_by = $1,
       locked_at = now(),
       heartbeat_at = now(),
       attempts = attempts + 1
 WHERE id = (
       SELECT id FROM jobs
        WHERE state = 'pending'
          AND topic = ANY($2)
          AND run_after <= now()
        ORDER BY priority, run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1)
 RETURNING *;
```

### 6.3 Topic canonici

Otto topic, stringhe esatte. Un worker si avvia con un sottoinsieme di topic (`ideai worker --topics …`), quindi l'isolamento dei carichi è una scelta di deployment.

| Topic | Prodotto da | Payload | Effetto |
|---|---|---|---|
| `pipeline.scrape` | scheduler, watch | `{target_id}` | legge una pagina dalla sorgente e persiste item |
| `pipeline.triage` | ingest, reembed | `{item_ids: [...]}` o `{batch: n}` | classifica, embedda e deduplica gli item nuovi |
| `pipeline.cluster` | triage | `{item_id}` | aggancia l'item a un'idea o ne crea una nuova |
| `pipeline.analyze` | cluster, watch, dashboard | `{idea_id, reason}` | produce una nuova revisione di analisi |
| `pipeline.score` | analyze, link di item | `{idea_id}` | ricalcola `opportunity_score` e denormalizzazioni |
| `pipeline.watch` | scheduler, cluster | `{idea_id, initial?: bool}` | verifica novità del thread sorvegliato; con `initial = true` scarica il thread intero prima della prima analisi (§8.2) |
| `maintenance.retention` | scheduler (giornaliero) | `{}` | applica la retention di §4.5, elimina le righe di `llm_cache` scadute e i job `done` più vecchi di `IDEAI_JOB_RETENTION_DAYS` |
| `maintenance.reembed` | operatore, cambio modello | `{scope: "all"\|"idea", idea_id?}` | ricalcola embedding e ricostruisce HNSW |

### 6.4 Ciclo di vita di un job

```mermaid
stateDiagram-v2
    [*] --> pending: enqueue
    pending --> running: claim
    running --> done: complete
    running --> pending: errore ritentabile o lease scaduta (attempts < max_attempts)
    running --> dead: attempts >= max_attempts o errore non ritentabile
    dead --> pending: retry manuale da /ops
    done --> [*]
```

### 6.5 Lease, retry, reaper

- **Lease**: `IDEAI_JOB_LEASE_S` (120). Il worker invia `heartbeat` ogni `IDEAI_JOB_HEARTBEAT_S` (30), aggiornando `heartbeat_at`.
- **Reaper**: un task nel processo scheduler gira ogni 60s e reclama i job `running` con `heartbeat_at` più vecchio del lease: se `attempts >= max_attempts` passano a `dead`, altrimenti tornano `pending` con `locked_by = NULL`. È la rete di sicurezza che rende accettabile at-least-once; senza la soglia sui tentativi un job che fa crashare il worker verrebbe ritentato all'infinito.
- **Retry**: `run_after = now() + min(3600, 2**attempts * 15)` secondi, con jitter ±20%. `attempts >= IDEAI_MAX_ATTEMPTS` (default 5, valore per tabella `jobs.max_attempts`) → `state = 'dead'`, visibile in `/ops` con pulsante di retry manuale. Un errore ritentabile sotto soglia **non** introduce uno stato `failed`: il job resta `pending` con `run_after` posticipato.
- **Errori non ritentabili** (payload malformato, referenza inesistente): il worker invoca `Queue.fail(..., retryable=False)` e il job passa a `dead` immediato, con `last_error` esplicito.
- **Concorrenza**: `ideai worker --topics a,b --concurrency N` esegue N task asyncio per processo (`IDEAI_WORKER_CONCURRENCY`, default 4). Scalare in orizzontale = `docker compose up --scale worker-llm=4` (o più nodi worker puntati allo stesso Postgres). Nessun coordinamento aggiuntivo.

### 6.6 Scheduler

Un processo singolo, `ideai scheduler`, che acquisisce `pg_advisory_lock(hashtext('ideai.scheduler'))` su una connessione dedicata **fuori dal pool** (un lock di sessione, non transazionale: resta acquisito per tutta la vita del processo) e, ogni `IDEAI_SCHEDULER_TICK_S` (30) secondi:

1. emette `pipeline.scrape` per ogni target con `enabled` e `next_poll_at <= now()`, calcolando `next_poll_at = now() + poll_interval_s`;
2. emette `pipeline.watch` per ogni riga di `watchlist` con `enabled` e `last_checked_at + interval_s <= now()`;
3. emette `maintenance.retention` se `SELECT max(finished_at) FROM jobs WHERE topic = 'maintenance.retention' AND state = 'done'` è `NULL` o più vecchio di 24 ore. La retention dei job conserva le righe `done` per almeno `IDEAI_JOB_RETENTION_DAYS` giorni (default 7), quindi dopo la prima esecuzione la riga esiste sempre.

**Proprietario unico di `next_poll_at`**: lo scheduler. L'adapter di ingest scrive `cursor`, `last_polled_at` e `error_count`, **mai** `next_poll_at`: in caso contrario uno scrape lento o fallito sposterebbe la scadenza mentre lo scheduler sta già calcolando il tick successivo.

`pg_advisory_lock` è di sessione: se una seconda replica dello scheduler parte, resta bloccata nell'attesa del lock (standby) e non emette nulla finché la prima non muore. Il `dedup_key` deterministico (ad esempio `scrape:<target_id>:<floor(next_poll_at/60)>`) è la seconda garanzia: un riavvio durante il tick non duplica lavoro.

### 6.7 Rate limiting

Prima di ogni chiamata esterna, il worker acquisisce lo slot con una transazione **breve**, senza mai dormire dentro la transazione:

```text
BEGIN;
SELECT pg_advisory_xact_lock(hashtext($bucket));   -- serializza i worker sullo stesso bucket
-- upsert della finestra: se window_start è scaduta azzerala, poi used += 1
COMMIT;
```

Se lo slot non è disponibile (`used >= requests_per_minute` della policy), la transazione si chiude comunque con `COMMIT` e il worker fa `asyncio.sleep` fino alla fine della finestra (`window_start + IDEAI_RATE_LIMIT_WINDOW_S`), poi ripete la sequenza. Il lock consultivo è per-bucket (`pg_advisory_xact_lock` si rilascia a fine transazione) e non blocca i worker che parlano a bucket diversi; tenerlo durante l'attesa serializzerebbe invano l'intero bucket.

Vale identicamente per Reddit OAuth (`IDEAI_REDDIT_RPM`), Reddit Pullpush (`IDEAI_PULLPUSH_RPM`) e per le API LLM esterne.

**Concorrenza verso i backend LLM.** Per ogni backend di `IDEAI_LLM_BACKENDS`, `max_concurrency` è un `asyncio.Semaphore` **per processo**: la concorrenza effettiva verso un backend è `max_concurrency × numero di processi worker-llm`, e va dimensionata di conseguenza. Se un backend dichiara anche `rpm`, le chiamate passano dallo stesso meccanismo di `rate_limits` con bucket `llm:<backend>`; `rpm: null` (default) disabilita il tetto per quel backend e lascia il solo semaforo.

### 6.8 Budget LLM

Prima di ogni chiamata a pagamento il gateway somma `llm_calls.cost_usd` del giorno corrente, definito in **UTC**: la giornata e la "mezzanotte" del rinvio sono quelle UTC, coerenti con `v_daily_llm_cost` (`date_trunc('day', created_at AT TIME ZONE 'UTC')`). Oltre `IDEAI_LLM_DAILY_BUDGET_USD` (default 5.00):

- i job `pipeline.analyze` non falliscono: vengono rimandati con `run_after` a mezzanotte UTC del giorno successivo (**non** è un errore e **non** incrementa `jobs.attempts`), e l'idea resta in `status = 'new'` (visibile in dashboard come "in attesa di analisi");
- i ruoli `triage` ed `embed` eseguiti su backend locale (`cost_usd = 0`) sono esclusi dal budget e continuano a girare.

Il controllo è *read-then-act*: con più chiamate già in volo il budget può essere superato al massimo di `max_concurrency` chiamate pagate (una per worker concorrente per backend). È un limite di spesa morbido, non un vincolo transazionale.

### 6.9 Cache

`llm_cache` è consultata prima di ogni chiamata; la chiave è `cache_key = sha256(prompt_hash + schema_version + model_spec + temperature + testo normalizzato)`, dove `prompt_hash` è lo `sha256` del file di prompt (§7.7). Il `model_spec` e la `temperature` fanno parte della chiave perché la stessa richiesta su modelli o temperature diversi non è la stessa risposta. In caso di hit, `LLMResult.cache_hit = True` e `cost_usd = 0`; la riga viene comunque registrata in `llm_calls` con costo zero, così il grafico dei consumi riflette il lavoro reale e il risparmio. Le righe più vecchie di `IDEAI_LLM_CACHE_MAX_AGE_DAYS` (default 30) vengono eliminate da `maintenance.retention`. La cache è utile soprattutto al triage, dove lo stesso commento può essere riclassificato dopo un errore.

---

## 7. Livello LLM: due ruoli, un gateway

### 7.1 Ruoli

Il gateway è l'unico componente che parla con i modelli. Espone due ruoli indipendenti, ciascuno con modello, prompt e schema propri:

| Ruolo | Variabile | Default | Volume | Dove gira | Costo |
|---|---|---|---|---|---|
| `triage` | `IDEAI_TRIAGE_MODEL` | `local/minicpm4:8b-q4_K_M` | alto (ogni item) | sempre locale | 0 |
| `analyst` | `IDEAI_ANALYST_MODEL` | `local/minicpm4:8b-q4_K_M` in Fase 1, poi `deepseek/deepseek-chat` | basso (ogni idea, per revisione) | locale o cloud | per token |
| `embed` | `IDEAI_EMBED_MODEL` | `local/bge-m3:567m` | alto (ogni item) | sempre locale | 0 |

In Fase 1 entrambi i ruoli di generazione puntano al modello locale. Spostare l'analisi su un modello più forte — locale o cloud — è un cambio di una variabile (`IDEAI_ANALYST_MODEL`) più una riga in `IDEAI_LLM_BACKENDS`. Nessuna riga di pipeline cambia.

### 7.2 Backend

`IDEAI_LLM_BACKENDS` è un oggetto JSON: nome logico → parametri di trasporto.

```json
{
  "local": {
    "type": "openai_compat",
    "base_url": "http://host.docker.internal:11434/v1",
    "api_key_env": null,
    "timeout_s": 120,
    "max_concurrency": 2,
    "rpm": null
  },
  "deepseek": {
    "type": "openai_compat",
    "base_url": "https://api.deepseek.com/v1",
    "api_key_env": "DEEPSEEK_API_KEY",
    "timeout_s": 60,
    "max_concurrency": 4,
    "rpm": null
  },
  "anthropic": {
    "type": "anthropic",
    "base_url": "https://api.anthropic.com",
    "api_key_env": "ANTHROPIC_API_KEY",
    "timeout_s": 60,
    "max_concurrency": 4,
    "rpm": null
  }
}
```

`max_concurrency` è il semaforo per processo (§6.7); `rpm`, se intero, impone anche un tetto di richieste/minuto sul bucket `llm:<backend>`; `null` disabilita il tetto e lascia il solo semaforo.

Tipi supportati:

- `openai_compat` — qualunque endpoint che implementi `/v1/chat/completions` e `/v1/embeddings`: Ollama, vLLM, llama.cpp server, LM Studio, Together, DeepSeek. DeepSeek accetta `response_format={"type": "json_object"}`; Ollama accetta `format` con lo schema JSON.
- `anthropic` — API Messages, con il tool schema dello stesso contratto.
- `ollama_native` — endpoint nativo Ollama, usato quando serve `format` con JSON Schema completo (più vincolante di `json_object`).

Le chiavi API non stanno mai nei file: `api_key_env` nomina la variabile d'ambiente che le contiene.

### 7.3 Output strutturato

Per ogni ruolo esiste uno schema JSON versionato (`prompt_version`, `schema_version`) passato al provider con il meccanismo disponibile:

1. **Validazione**: la risposta è validata con Pydantic v2 contro lo schema del ruolo.
2. **Riparazione**: se non valida, l'errore di validazione viene rispedito al modello insieme alla risposta precedente, per un massimo di **2 tentativi** di riparazione.
3. **Fallimento**: dopo il secondo tentativo fallito, `llm_calls.status = 'invalid_json'` e il job segue il percorso di retry ordinario (§6.5).

**Mai** parsing di JSON in testo libero senza validazione: un `json.loads` che riesce su un payload che non rispetta lo schema è trattato come risposta non valida.

### 7.4 Batching del triage

Gli item in stato `new` vengono accodati in batch da `IDEAI_TRIAGE_BATCH` (default 10) per chiamata. L'output contiene una lista allineata per indice (`results[i].index == i`). Se il conteggio non combacia, il batch viene ripetuto come chiamate singole: la correttezza non dipende dalla disciplina del modello nel contare.

### 7.5 Schema di triage (esatto)

```json
{
  "results": [
    {
      "index": 0,
      "confidence": 0.0,
      "category": "product_idea",
      "one_line_summary": "",
      "problem": null,
      "audience": null,
      "tags": [],
      "dup_of_idea_id": null
    }
  ]
}
```

Vincoli: `category` ∈ {`product_idea`, `pain_point`, `market_signal`, `question`, `announcement`, `spam`, `off_topic`}; `tags` con al massimo 5 elementi; `dup_of_idea_id` è un `id` di `idea_clusters` oppure `null` e viene considerato solo nel cancello G2. Lo schema **non** contiene un flag booleano che duplichi la categoria: lo stato dell'item deriva da `category` secondo la mappatura di §8.1.

### 7.6 Schema di analisi (esatto)

```json
{
  "problem": "", "target_customer": "", "current_alternatives": [], "proposed_solution": "",
  "mvp_scope": [], "differentiators": [],
  "monetization": {"model": "subscription", "price_hypothesis": "", "unit_economics_note": ""},
  "feasibility": {"score": 1, "rationale": "", "hard_blockers": [], "tech_stack_hint": []},
  "economics": {"score": 1, "tam_signal": "", "rationale": ""},
  "competition": {"score": 1, "named_players": [], "rationale": ""},
  "risks": [{"risk": "", "severity": 1, "mitigation": ""}],
  "effort": {"weeks_to_mvp": 0, "team_size": 0, "confidence": 0.0},
  "evidence_quotes": [{"item_external_id": "", "quote": ""}],
  "verdict": "promising", "confidence": 0.0, "notes": ""
}
```

Vincoli: `monetization.model` ∈ {`subscription`, `one_off`, `usage`, `marketplace`, `ads`, `unknown`}; `feasibility.score`, `economics.score`, `competition.score` e `risks[].severity` interi 1–5; `verdict` ∈ {`strong`, `promising`, `weak`, `reject`}; `confidence` ∈ 0–1; `evidence_quotes[].item_external_id` deve corrispondere a un item collegato all'idea, altrimenti la citazione viene scartata.

### 7.7 Prompt versionati

I prompt sono file, non stringhe nel codice: `src/ideai/prompts/triage_v1.md` e `src/ideai/prompts/analysis_v1.md`, ciascuno con header YAML (`version`, `schema_version`, `model_hint`, `temperature`, `max_tokens`). Lo `sha256` del file entra in `llm_calls.prompt_hash` insieme alla versione: cambiare un prompt significa creare `analysis_v2.md`, mai modificare il file esistente, così una regressione è attribuibile a una versione precisa. La selezione della versione è nel codice (`PROMPT_CURRENT = "v1"`), non in una variabile d'ambiente: è una scelta di release, non di deployment.

### 7.8 Contabilizzazione

Ogni chiamata scrive una riga in `llm_calls` con ruolo, provider, modello, hash del prompt, token e costo. Il costo è calcolato da `IDEAI_LLM_PRICES`, un oggetto JSON che mappa **`model_spec`** (`<backend>/<model>`, §3.5) → `{"in": <USD per milione di token in ingresso>, "out": <USD per milione di token in uscita>}`; i modelli locali hanno prezzo zero e i backend cloud non elencati valgono zero con un avviso a log (il dato manca, non si inventa). Esempio:

```json
{
  "deepseek/deepseek-chat": {"in": 0.27, "out": 1.10}
}
```

`v_daily_llm_cost` aggrega per giorno e ruolo; la pagina `/ops` lo mostra.

**Costo delle chiamate alla sorgente.** Le chiamate alle API esterne a pagamento (Reddit in regime commerciale) hanno la propria contabilità nella tabella `source_calls_daily`: a ogni chiamata, nello stesso passo del rate limit di §6.7, si esegue l'upsert di `(day, source_id)` con `calls += 1` e `cost_usd += sources.config.cost_per_call_usd`. `/ops` mostra il costo delle sorgenti accanto a quello LLM, così la spesa esterna non è invisibile.

---

## 8. Pipeline: stati, soglie e formule

### 8.1 Stati di un item

```mermaid
stateDiagram-v2
    [*] --> new: ingest
    new --> rejected: G0 duplicato esatto
    new --> candidate: triage G1 (sim >= IDEAI_DUP_SIM_HIGH), aggancio diretto
    new --> triaged: triage product_idea / pain_point, o market_signal con vicino
    new --> rejected: question, announcement, spam, off_topic, market_signal senza vicino
    triaged --> candidate: cluster aggancia o crea l'idea
    candidate --> analyzed: analisi completata
    candidate --> archived: contenuto rimosso
    analyzed --> archived: contenuto rimosso
    rejected --> [*]: retention
```

Significato: `new` = non ancora visto dal triage; `triaged` = classificato come contenuto che può generare o agganciare un'idea, in attesa di collocazione; `candidate` = collegato a un'idea (come seme o evidenza); `analyzed` = l'idea a cui appartiene ha almeno una revisione di analisi; `rejected` = scartato con `reject_reason`; `archived` = corpo `[removed]`/`[deleted]` o item rimosso dall'idea.

**Mappatura `category` → esito.** La categoria restituita dal triage determina da sola lo stato dell'item: un flag booleano che la duplicasse era ridondante e ambiguo per `market_signal` (che aggancia ma non genera).

| `category` | Esito | `state` / `reject_reason` |
|---|---|---|
| `product_idea` | può generare una nuova idea o agganciarsi a una esistente | `triaged` |
| `pain_point` | idem | `triaged` |
| `market_signal` | **solo aggancio**, mai seed; richiede un vicino con similarità ≥ `IDEAI_DUP_SIM_LOW` | `triaged` se il vicino esiste, altrimenti `rejected` / `not_idea` |
| `question` | fuori catalogo | `rejected` / `not_idea` |
| `announcement` | fuori catalogo | `rejected` / `not_idea` |
| `spam` | scartato | `rejected` / `spam` |
| `off_topic` | scartato | `rejected` / `off_topic` |

`pain_point` e `market_signal` restano **evidenza di domanda**: un `pain_point` può fondare un'idea (seed), un `market_signal` no. Il caso ambiguo — un `market_signal` che non trova vicini — non è più un'idea zoppa: è `rejected/not_idea`, reversibile solo da `/ops`.

### 8.1bis Stati di un'idea

`idea_clusters.status` è uno stato **dell'idea**, distinto da `items.state` (uno stato dell'item). Le due macchine non vanno confuse: "in attesa di analisi" è una proprietà dell'idea (`status = 'new'`), non dell'item.

```mermaid
stateDiagram-v2
    [*] --> new: cluster crea l'idea
    new --> analyzed: prima revisione di analisi completata
    analyzed --> watching: watch abilitato (added_by auto o manual)
    watching --> analyzed: watch disabilitato o sospeso
    analyzed --> rejected: l'ultima revisione ha verdict = reject
    watching --> rejected: l'ultima revisione ha verdict = reject
    rejected --> analyzed: una revisione successiva cambia verdetto
    new --> archived: merge o rimozione di tutti gli item
    analyzed --> archived: merge o rimozione di tutti gli item
    watching --> archived: merge o rimozione di tutti gli item
    rejected --> archived: merge o rimozione di tutti gli item
```

- `new` — creata dal clustering, nessuna analisi ancora.
- `analyzed` — esiste almeno una revisione non superata.
- `watching` — esiste una riga `watchlist` con `enabled = true`. La transizione non la decide l'idea: la decide la presenza della riga (§8.4/§8.5); quando il watch viene disabilitato o sospeso l'idea torna `analyzed`.
- `rejected` — l'**ultima** revisione ha `verdict = 'reject'` (watch disabilitato di conseguenza). È reversibile: se una revisione successiva cambia verdetto, l'idea torna `analyzed`. Nessuna idea viene cancellata automaticamente.
- `archived` — merge in un'altra idea (§9.1) o rimozione di tutti gli item collegati. Stato terminale.

### 8.2 Cancelli di de-duplicazione (requisito RF-3)

**Ordine canonico di lavorazione di un item** — questo è l'unico ordine valido; ogni altra sezione vi si riferisce:

1. **Ingest / G0.** L'item viene inserito con `ON CONFLICT (source_id, external_id) DO UPDATE` (§5.2, politica di upsert); se l'identità di provenienza esiste già o un `content_hash` è identico a quello di un item degli ultimi `IDEAI_G0_WINDOW_DAYS` giorni (default 7) → `state = 'rejected'`, `reject_reason = 'exact_dup'`. Nessun embedding, nessuna chiamata LLM.
2. **`pipeline.triage` (batch).** Calcola l'embedding dell'item. Se la similarità coseno massima con un'idea è ≥ `IDEAI_DUP_SIM_HIGH` (default 0.92) il cancello **G1** aggancia direttamente l'item all'idea (`state = 'candidate'`), senza alcuna chiamata al modello. Per tutti gli altri item il triage fa **una** chiamata batch al modello di triage (§7.4); se la similarità è in [`IDEAI_DUP_SIM_LOW`, `IDEAI_DUP_SIM_HIGH`) il prompt include i 5 vicini più prossimi e il modello può valorizzare `dup_of_idea_id`.
3. **Categoria → esito.** La categoria restituita dal modello determina lo stato secondo la tabella di §8.1.
4. **`pipeline.cluster`.** Se `dup_of_idea_id` è valorizzato (o l'item è un `market_signal` con un vicino) l'item si aggancia all'idea esistente con `role = 'evidence'` (post) o `role = 'comment'` (commento); altrimenti, solo per `product_idea`/`pain_point`, crea una nuova riga in `idea_clusters` con l'item come `role = 'seed'`. In **entrambi** i casi l'item diventa `state = 'candidate'`.

L'embedding è quindi calcolato **prima** della chiamata al modello, non dopo: il modello leggero non vede mai ciò che si può scartare con una chiave o con un confronto fra vettori.

```mermaid
flowchart TD
    A["item normalizzato"] --> G0{"G0 — (source_id, external_id) già noto<br/>o content_hash identico negli ultimi<br/>IDEAI_G0_WINDOW_DAYS giorni?"}
    G0 -->|sì| R0["rejected — exact_dup"]
    G0 -->|no| E["pipeline.triage: embedding dell'item"]
    E --> G1{"G1 — sim coseno max<br/>con idea >= IDEAI_DUP_SIM_HIGH?"}
    G1 -->|sì| L["candidate — aggancio diretto<br/>nessuna chiamata LLM"]
    G1 -->|no| B["chiamata batch al modello di triage<br/>(sim in LOW..HIGH: prompt con i 5 vicini)"]
    B --> CAT{"categoria → esito (§8.1)"}
    CAT -->|"product_idea / pain_point"| TR["triaged"]
    CAT -->|"market_signal con vicino ≥ LOW"| TR
    CAT -->|"market_signal senza vicino,<br/>question, announcement"| R1["rejected — not_idea"]
    CAT -->|"spam"| R2["rejected — spam"]
    CAT -->|"off_topic"| R3["rejected — off_topic"]
    TR --> C["pipeline.cluster"]
    C -->|"dup_of_idea_id, o market_signal"| L2["aggancia: role = evidence o comment"]
    C -->|"product_idea / pain_point senza duplicato"| N["nuova idea — role = seed"]
    L2 --> S["candidate: enqueue pipeline.score"]
    N --> Q["candidate: enqueue pipeline.analyze"]
    L --> S
```

- **G0 esatto** — identità di provenienza `(source_id, external_id)` più `content_hash` identico nella finestra `IDEAI_G0_WINDOW_DAYS`. Copre lo stesso contenuto riproposto e il cross-posting. Non è un cancello semantico e non costa nulla.
- **G1 semantico** — nearest-neighbour coseno su `embeddings(owner_kind = 'idea')`, filtrato sul modello corrente (§4.5); similarità ≥ `IDEAI_DUP_SIM_HIGH` → aggancio diretto, **senza** nuova idea e senza nuova analisi.
- **G2 conferma modello** — similarità in [`IDEAI_DUP_SIM_LOW`, `IDEAI_DUP_SIM_HIGH`) (default 0.80–0.92) → il modello di triage riceve i sommari canonici dei 5 vicini più prossimi e decide `dup_of_idea_id`. È l'unico caso in cui la decisione di deduplica è affidata al modello, ed è limitato a una fascia stretta.
- **Nessun vicino sopra soglia** → nuova riga in `idea_clusters` con l'item come `role = 'seed'`, `status = 'new'`, e accodamento di `pipeline.analyze`. Se il seed è un `post` di una sorgente con `has_comments`, il cluster accoda invece `pipeline.watch` con `{idea_id, initial: true}` (§8.5) e sarà il watch iniziale ad accodare l'analisi.

### 8.3 Analisi e revisioni (requisito RF-4)

L'analisi è un'operazione sull'**idea**, non sull'item. Input del prompt analista:

- titolo e `canonical_summary` dell'idea;
- fino a `IDEAI_ANALYSIS_MAX_EVIDENCE` (default 20) item collegati, ordinati per `score + 2 * num_comments`, con i commenti che contengono segnali di domanda ("pago per", "esiste qualcosa", "come risolvo") privilegiati;
- per le revisioni successive: il *delta* dall'ultima analisi (nuovi item, cambi di metrica) oltre al contesto precedente.

Output → nuova riga in `analyses` con `revision = max(revision) + 1`; la riga precedente riceve `superseded_by`. `idea_clusters.title` e `canonical_summary` vengono rigenerati dal modello di triage a ogni revisione, così il sommario canonico riflette l'insieme di evidenze corrente: a ogni rigenerazione si ricalcola **anche** l'embedding dell'idea (§4.5), perché è quello che G1/G2 confrontano. Al termine, `items.raw` degli item coinvolti viene azzerato (§4.5) e si accoda `pipeline.score`.

### 8.4 Scoring deterministico

Formula unica, in `src/ideai/scoring.py`, senza modello:

```text
quality    = 0.35 * (feasibility.score - 1) / 4
           + 0.35 * (economics.score - 1) / 4
           + 0.30 * (5 - competition.score) / 4

engagement = 0                                                     se seed.score IS NULL
           = min(1, log1p(seed.score) / log1p(S))                  se il seed è un commento
           = min(1, log1p(seed.score + 2 * seed.num_comments) / log1p(S))   altrimenti
             con S = sources.config.engagement_saturation (default 5000) della sorgente del seed

evidence   = min(1, len(evidence_quotes valide) / 5)
economy    = 1.0 se monetization.model != "unknown", altrimenti 0.3

opportunity_score = round(100 * (0.40 * quality
                               + 0.25 * engagement
                               + 0.20 * evidence
                               + 0.10 * economy
                               + 0.05 * confidence))
```

- `quality` ∈ 0–1: i tre giudizi hanno tutti range 1–5, quindi ciascun termine è `(score - 1)/4` (o `(5 - score)/4` per la competizione invertita); in questo modo **il minimo della formula coincide col minimo della scala** (1 → 0), invece del vecchio `(6 - c)/5` che mappava 1 su 0,2.
- `engagement` ∈ 0–1: dal post seme. `NULL` (metrica non disponibile) vale 0; se il seed è un commento si usa il solo `score` (i commenti non hanno `num_comments`); la costante di saturazione `S` è **per sorgente** (`sources.config.engagement_saturation`, default 5000) perché Reddit e Hacker News non hanno la stessa scala di punteggi.
- `evidence` ∈ 0–1: conta solo le citazioni **sopravvissute alla validazione di §7.6** (un `item_external_id` inesistente non è evidenza).
- `economy` ∈ {0.3, 1.0}: premia la presenza di un modello di ricavo identificato.
- `confidence` ∈ 0–1: fiducia dichiarata dall'analista.

Pesi e soglie vivono in `settings_overrides` (default nel codice). `score_version = "v1"` è salvato in `idea_clusters` e `analyses`: cambiare i pesi produce `v2` e permette di ricalcolare lo storico senza ambiguità.

**Quando si ricalcola**: nuova analisi; nuovo item collegato; variazione di `score`/`num_comments` del seed rilevata dal watch.

### 8.5 Watch (requisito RF-6)

Il job `pipeline.score` (§8.4) è il componente che inserisce in `watchlist` un'idea con `added_by = 'auto'` quando `opportunity_score >= IDEAI_WATCH_THRESHOLD` (default 70) **e** la sorgente del seed dichiara `has_comments` (§5.1). Non riabilita mai una riga esistente con `enabled = false`: disattivazioni manuali e sospensioni per `watch_paused` restano rispettate. La dashboard può aggiungere o rimuovere un'idea manualmente (`added_by = 'manual'`) e cambiare `mode`. Il watch **iniziale** di un'idea appena creata (`{idea_id, initial: true}`, §8.2) scarica il thread intero (`since = None`) e accoda `pipeline.analyze` con `reason = 'initial'`: è così che i commenti del thread di origine arrivano anche per le idee appena create, sotto o sopra soglia.

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Scheduler
    participant Q as jobs
    participant W as Worker watch
    participant SRC as SourceAdapter
    participant DB as items / idea_items / idea_updates
    SCH->>Q: enqueue pipeline.watch (idea_id)
    W->>Q: claim pipeline.watch
    Q-->>W: job pipeline.watch
    W->>SRC: fetch_thread(thread_ref, since = last_checked_at, o None se initial)
    SRC-->>W: nuovi commenti e metriche aggiornate
    W->>DB: INSERT items + idea_items (role=update), aggiorna metriche del seed
    W->>DB: INSERT idea_updates (new_comment / new_post / edit)
    W->>W: delta >= IDEAI_WATCH_MIN_DELTA?
    W->>Q: enqueue pipeline.analyze (reason = watch_delta)
    W->>DB: aggiorna interval_s e last_checked_at
```

Regole:

- nuovo item → riga in `items` (`state = 'candidate'`), collegamento `idea_items` con `role = 'update'` e riga in `idea_updates`;
- se `content_hash` di un item collegato cambia (refresh del post con `mode = 'thread_full'`, o rilettura da uno scrape) si scrive `idea_updates.kind = 'edit'`; se il corpo diventa `[removed]`/`[deleted]` l'item passa a `state = 'archived'` con `reject_reason = 'removed'` (§5.2);
- nuova revisione di analisi se `len(nuovi item) >= IDEAI_WATCH_MIN_DELTA` (default 3) **oppure** se `score` o `num_comments` del **seed** sono cresciuti oltre il 20% rispetto ai valori salvati in `items`; il watch aggiorna i valori del seed **dopo** il confronto (un item nuovo non ha una lettura precedente: il confronto è sempre contro lo stato persistito del seed, non contro "la lettura precedente dell'item");
- **intervallo adattivo**: `interval_s` raddoppia fino a `IDEAI_WATCH_MAX_INTERVAL_S` (default 86400) finché non arrivano novità, e torna a `IDEAI_WATCH_MIN_INTERVAL_S` (default 900) quando arrivano;
- un thread senza novità per `IDEAI_WATCH_EXPIRE_DAYS` (default 30) viene disabilitato con una riga `idea_updates.kind = 'watch_paused'`;
- **il watch non passa per il triage e non applica G0/G1/G2**: i commenti del thread si collegano direttamente all'idea sorvegliata e **non generano mai** una nuova idea. Il contenuto che il watch produce è `role = 'update'` per costruzione; se la discussione introduce un problema distinto sarà un post nuovo sullo stesso subreddit (che passa dall'ingest) a generare un'idea, non un commento del watch.

### 8.6 Fallimenti e degrado

- **Analisi impossibile** — budget esaurito: l'idea resta in `status = 'new'` e la dashboard la mostra come "in attesa di analisi". Il rinvio per budget è un `run_after`, **non** un errore: non incrementa `jobs.attempts`. Provider non raggiungibile: vale il percorso di retry di §6.5; se il job arriva a `dead`, l'idea resta `status = 'new'` e il job è ri-accodabile da `/ops`. Non viene mai scartata per un errore di infrastruttura.
- **Embedding mancante**: il cancello G1 è saltato per quell'item, che resta in coda per un secondo passaggio; il job `maintenance.reembed` ricostruisce i vettori mancanti. Nessuna perdita di contenuto.
- **Sorgente giù**: i job `pipeline.scrape` del target falliscono secondo §5.9; triage, analisi e scoring continuano a lavorare sull'arretrato.
- **Duplicato tardivo**: se due item dello stesso thread producono due idee prima che l'embedding sia disponibile, il job `pipeline.score` non le fonde automaticamente; la fusione è un'azione manuale dalla dashboard (`POST /ideas/{id}/merge`) e viene registrata in `idea_updates`.

---

## 9. API e dashboard

### 9.1 API FastAPI

Base path `/api/v1`. Autenticazione: header `Authorization: Bearer $IDEAI_API_KEY`; senza header valido la risposta è `401`, tranne che per `/healthz` e `/readyz`. Gli endpoint di scrittura sono deliberatamente pochi: l'API accoda job, non esegue lavoro lungo.

| Metodo | Path | Funzione |
|---|---|---|
| `GET` | `/ideas` | lista filtrata e ordinata del catalogo |
| `GET` | `/ideas/{id}` | dettaglio: idea, ultima analisi, storico revisioni, item collegati, timeline |
| `GET` | `/ideas/search` | ricerca ibrida (full-text + similarità vettoriale) |
| `POST` | `/ideas/{id}/watch` | abilita/disabilita il watch e ne fissa `mode` e `interval_s` |
| `POST` | `/ideas/{id}/reanalyze` | accoda `pipeline.analyze` con `reason = manual` |
| `POST` | `/ideas/{id}/merge` | fonde due idee duplicate (richiesta esplicita dell'operatore) |
| `DELETE` | `/ideas/{id}` | rimuove idea, item collegati, analisi ed embedding |
| `GET` | `/sources` | sorgenti e target con stato, cursore, errori |
| `POST` | `/sources/{id}/targets` | aggiunge un target |
| `PATCH` | `/targets/{id}` | abilita/disabilita, cambia `poll_interval_s` |
| `GET` | `/jobs` | coda per `state` e `topic`, con età del più vecchio pending |
| `POST` | `/jobs/{id}/retry` | rimette in coda un job `dead` |
| `GET` | `/stats` | KPI aggregati (idee, analisi, watch, costo LLM) |
| `GET` | `/healthz` | liveness: il processo risponde |
| `GET` | `/readyz` | readiness: Postgres, backend LLM, dimensione embedding |

Parametri di `GET /ideas`: `min_score`, `max_score`, `verdict`, `status`, `category`, `tag`, `source_kind`, `date_from`, `date_to`, `sort` ∈ {`opportunity_score`, `last_activity_at`, `first_seen_at`}, `limit` (default 50, massimo 200), `offset`. `date_from`/`date_to` filtrano `first_seen_at` (non `created_at`, che non esiste su `idea_clusters`); `category` e `tag` vengono dal seed tramite `v_idea_ranking`.

**`POST /ideas/{id}/merge`** — corpo `{"into_id": <int>}`. In **una transazione**: sposta `idea_items` sull'idea destinazione (`ON CONFLICT DO NOTHING`, la PK è `(idea_id, item_id)`), aggiorna `items.idea_id`, riassegna `idea_updates`; l'idea sorgente passa a `status = 'archived'` con il watch disabilitato; sulla destinazione scrive `idea_updates.kind = 'merged'`; infine accoda `pipeline.analyze` con `reason = 'merge'`. `into_id == id` → `422`; `into_id` inesistente → `404`. Un merge parziale non è possibile: o la transazione va a buon fine o nulla cambia.

**`DELETE /ideas/{id}`** — in una transazione cancella prima le righe `embeddings` dell'idea **e dei suoi item** (la tabella è polimorfica e non ha FK, quindi resterebbero orfane), poi gli item collegati **solo** a questa idea, quindi l'idea: `idea_items`, `analyses`, `watchlist` e `idea_updates` seguono per `ON DELETE CASCADE`. Gli item condivisi con altre idee non vengono cancellati.

Esempio di risposta di `GET /ideas?min_score=70&sort=opportunity_score&limit=2`:

```json
{
  "total": 23,
  "items": [{
    "id": 412, "title": "Fatturazione automatica per artigiani senza gestionale",
    "canonical_summary": "PMI artigiane senza software emettono fatture a mano...", "status": "watching",
    "opportunity_score": 82, "score_version": "v1", "verdict": "strong", "confidence": 0.74,
    "category": "pain_point", "tags": ["fatturazione", "pmi", "automazione"], "source_kinds": ["reddit"],
    "item_count": 37, "first_seen_at": "2026-09-12T09:14:02Z", "last_activity_at": "2026-10-02T18:41:55Z",
    "watch": {"enabled": true, "mode": "thread_full", "interval_s": 3600}
  }]
}
```

`GET /readyz` risponde `503` con l'elenco dei prerequisiti mancanti se Postgres non è raggiungibile, se il backend di inferenza non risponde a `GET /v1/models` oppure se la dimensione dichiarata da `IDEAI_EMBED_MODEL` non coincide con `IDEAI_EMBED_DIM`.

### 9.2 Dashboard Next.js

Stack: **Next.js 15 App Router + TypeScript + Tailwind + shadcn/ui + TanStack Query**. Le pagine dati usano `revalidate = 0` e fetch lato server verso `IDEAI_API_BASE_URL` (`http://api:8000` nel Compose). **Nessun accesso diretto al database dal frontend.**

| Pagina | Contenuto |
|---|---|
| `/` | KPI (idee catalogate, analizzate, watch attivi, costo LLM ultimi 7 giorni) e top 20 per `opportunity_score` con badge del verdict |
| `/ideas` | lista filtrabile e ordinabile + ricerca semantica; colonne: titolo, score, verdict, categoria, tag, sorgenti, ultima attività, stato del watch |
| `/ideas/[id]` | sommario canonico; analisi corrente con radar dei tre punteggi (fattibilità, economia, competizione); *diff* fra revisioni; evidenze con link al post originale; timeline `idea_updates`; toggle watch; pulsante di ri-analisi |
| `/sources` | target con CRUD, cursore, ultimo poll, errori, budget di rate limit consumato |
| `/ops` | coda per topic e stato, job `dead` con retry, costo LLM giornaliero per ruolo **e costo delle sorgenti** (`source_calls_daily`), ultime chiamate fallite |

Il dettaglio idea è la pagina che risponde al requisito "mostrare le idee più interessanti": il punteggio spiega sé stesso (radar + evidenze + citazioni), quindi l'utente può contestare la valutazione invece di fidarsi ciecamente.

---

## 10. Deployment locale, astrazione hardware e bootstrap

### 10.1 Compose

```mermaid
flowchart TB
    subgraph HOST["Host (Linux, macOS, Windows)"]
        subgraph COMPOSE["Docker Compose"]
            API["api<br/>127.0.0.1:8000"]
            DASH["dashboard<br/>127.0.0.1:3000"]
            W1["worker-ingest"]
            W2["worker-llm"]
            W3["worker-misc"]
            SCH["scheduler (singolo)"]
            MIG["migrate (one-shot)"]
            BOOT["bootstrap (one-shot)"]
            PG[("postgres + pgvector<br/>volume pgdata")]
        end
        subgraph NATIVE["Processi nativi (default)"]
            OLL["Ollama :11434<br/>endpoint OpenAI-compatibile"]
            VM["volume modelli"]
        end
        OLL --- VM
    end
    CLOUD["Backend LLM cloud<br/>DeepSeek, Anthropic"]
    API --> PG
    W1 --> PG
    W2 --> PG
    W3 --> PG
    SCH --> PG
    MIG --> PG
    BOOT --> PG
    API --> OLL
    W2 --> OLL
    W2 --> CLOUD
    DASH --> API
```

| Servizio | Immagine o ruolo | Note |
|---|---|---|
| `postgres` | `pgvector/pgvector:pg17` | unico stato obbligatorio; volume `pgdata`; healthcheck `pg_isready`; richiede **pgvector ≥ 0.8.0** (`hnsw.iterative_scan`, §4.5), verificato da `ideai doctor` e `/readyz` |
| `migrate` | app, one-shot `ideai db upgrade` | dipende da `postgres` healthy; rieseguibile a ogni deploy |
| `api` | app, `uvicorn` pubblicato solo su `127.0.0.1:8000` | stateless; l'accesso remoto passa da un reverse proxy autenticato (§11.3) |
| `worker-ingest` | app, `ideai worker --topics pipeline.scrape,pipeline.watch` | I/O bound, replicabile |
| `worker-llm` | app, `ideai worker --topics pipeline.triage,pipeline.cluster,pipeline.analyze,maintenance.reembed` | il collo di bottiglia: si scala per primo |
| `worker-misc` | app, `ideai worker --topics pipeline.score,maintenance.retention` | leggero |
| `scheduler` | app, `ideai scheduler` | singolo, protetto da advisory lock |
| `dashboard` | Next.js, pubblicato solo su `127.0.0.1:3000` | `IDEAI_API_BASE_URL=http://api:8000` |
| `ollama` | `ollama/ollama`, profilo Compose `local-llm` | **disattivato di default**: si abilita solo se l'inferenza non gira già sull'host |

La separazione in tre worker non è estetica: `worker-llm` ha bisogno di più CPU/GPU e di meno rete, `worker-ingest` il contrario, e in caso di saturazione si scala solo quello che serve (`docker compose up --scale worker-llm=4`).

### 10.2 Astrazione hardware (vincolo esplicito)

Il core non sa nulla di GPU, NPU o driver: parla esclusivamente con un endpoint OpenAI-compatibile. Cambiare hardware significa cambiare `IDEAI_LLM_BACKENDS`.

**Default: Ollama nativo sull'host**, raggiunto dai container via `host.docker.internal` (su Linux richiede `extra_hosts: ["host.docker.internal:host-gateway"]`, da documentare nel compose). Motivazione:

- il passthrough GPU dentro Docker è specifico per NVIDIA (`nvidia-container-toolkit`), non copre AMD né NPU, e lega i container alla versione del driver;
- su schede Pascal (GTX 1080, compute capability 6.1) l'inferenza senza bf16 e senza flash-attention è servita dal runner CUDA v12 che Ollama distribuisce ancora; tenerlo fuori dal container elimina un livello di rottura;
- chi preferisce isolare anche l'inferenza abilita il profilo `local-llm` con `deploy.resources.reservations.devices` (NVIDIA) oppure usa l'immagine ROCm documentata per AMD.

**Profili hardware** (`IDEAI_HARDWARE_PROFILE` imposta i default delle variabili non configurate; precedenza in §10.4):

| Profilo | Hardware tipico | Analyst (`IDEAI_ANALYST_MODEL`) |
|---|---|---|
| `cpu` | nessuna GPU | `deepseek/deepseek-chat` |
| `gpu-8gb` | GTX 1080 8 GB | `local/minicpm4:8b-q4_K_M` |
| `gpu-16gb+` | 16 GB+ VRAM, Apple a memoria unificata, NPU dedicata | `local/minicpm4:8b-q4_K_M` (per un analista più grande si imposta `IDEAI_ANALYST_MODEL` esplicitamente) |
| `cloud` | GPU noleggiata o solo CPU | `deepseek/deepseek-chat` |

Il profilo cambia **solo** `IDEAI_ANALYST_MODEL`: è l'unico ruolo la cui scelta dipende dall'hardware. Triage ed embedding sono identici in tutti i profili (`local/minicpm4:8b-q4_K_M` e `local/bge-m3:567m`) perché sono ruoli locali ad alto volume; un profilo che li cambiasse introdurrebbe una differenza di qualità invisibile in dashboard. Il default della tabella di Appendice A coincide col profilo `gpu-8gb`.

Un host con NPU o con memoria unificata Apple adotta il profilo `gpu-16gb+` e punta `base_url` al runtime del proprio vendor: l'architettura non cambia perché l'unico contratto è l'endpoint. **Nessun numero di token/s viene promesso**: la GTX 1080 è Pascal e le prestazioni vanno misurate in Fase 0 (§11.2).

### 10.3 Bootstrap al primo avvio

Il comando `ideai doctor` — e il servizio one-shot `bootstrap` che lo invoca nel Compose — esegue in ordine:

1. verifica che Postgres risponda e che l'estensione `pgvector` sia installata **con versione ≥ 0.8.0** (richiesta da `hnsw.iterative_scan`, §4.5);
2. verifica che il backend di inferenza risponda (`GET /api/tags` per Ollama, `GET /v1/models` per gli altri);
3. esegue `ollama pull` dei modelli mancanti indicati da `IDEAI_TRIAGE_MODEL`, `IDEAI_ANALYST_MODEL` e `IDEAI_EMBED_MODEL`, con avanzamento a schermo;
4. verifica che la dimensione reale del modello di embedding coincida con `IDEAI_EMBED_DIM`;
5. esegue un item di prova end-to-end (ingest → triage → analisi) su un contenuto fittizio e stampa un report;
6. esce con codice ≠ 0 se un prerequisito manca, con l'elenco delle alternative disponibili quando un tag non esiste.

Questo realizza il requisito "inizializzare Ollama o equivalente alla prima accensione": chi clona il repository esegue `docker compose up` e `ideai doctor`, senza dover sapere quale modello scaricare.

### 10.4 Configurazione

**Precedenza di risoluzione** (vale per ogni variabile, ed è la stessa in Appendice A):

1. **variabile d'ambiente esplicita** — vince sempre;
2. **profilo** selezionato da `IDEAI_HARDWARE_PROFILE` — riempie solo le variabili non impostate esplicitamente;
3. **default nel codice** — il valore di fallback.

`IDEAI_HARDWARE_PROFILE` non è una magia: i default elencati in Appendice A sono quelli del profilo `gpu-8gb`, che è anche il valore di default del selettore. `.env.example` elenca tutte le variabili di Appendice A con tipo, default e obbligatorietà. Nessun segreto è committato: `IDEAI_REDDIT_CLIENT_ID`, `IDEAI_REDDIT_CLIENT_SECRET`, `IDEAI_API_KEY`, `IDEAI_SALT` e le chiavi dei backend cloud vivono solo nell'ambiente.

---

## 11. Analisi dei vincoli, roadmap a fasi, operatività e legge

### 11.1 Analisi dei vincoli posti

| Vincolo dichiarato | Verdetto | Motivazione | Costo e caveat |
|---|---|---|---|
| **Tutto eseguibile localmente con Docker** | **sì per stato e applicazione, no per l'inferenza** | Compose rende riproducibili Postgres + pgvector, migrazioni, worker, API e dashboard su qualunque host; l'inferenza locale resta nativa perché il passthrough GPU in container è NVIDIA-only, fragile su Pascal e AMD, e accoppiato al driver. Il container `ollama` esiste come profilo `local-llm` per chi lo preferisce | un layer di orchestrazione locale da imparare; due modalità di inferenza da documentare |
| **Future-proof verso altre sorgenti oltre Reddit** | **sì, ed è economico ora, caro dopo** | La porta `SourceAdapter` + `NormalizedItem` canonico costano un layer di astrazione e zero overhead a runtime; retrofitarlo dopo significa riscrivere ingest e schema | la deduplicazione semantica tra sorgenti diverse richiede embedding multilingua: motivo del default `bge-m3` a 1024 dimensioni |
| **Scalare scraper, agenti di catalogazione e di analisi** | **sì, ma con un tetto che non è l'architettura** | Con `FOR UPDATE SKIP LOCKED` la coda regge migliaia di job al minuto su un singolo Postgres; i worker si scalano replicando i container o aggiungendo nodi, senza broker | i limiti reali sono il rate limit Reddit (100 richieste/minuto per client OAuth, aggirabile con più client) e la throughput o il costo del modello analista; Postgres regge bene fino a qualche decina di milioni di item, oltre servono partizionamento di `items` ed `embeddings` e solo allora un broker esterno dietro la porta `Queue` |
| **Tutto in locale, nessuna dipendenza esterna** | **sì con riserva** | Una GTX 1080 non regge un modello analista di fascia alta: il default corretto è modello leggero locale per triage ed embedding, analista sostituibile anche con backend cloud. Il sistema degrada (idea "in attesa di analisi", §8.6) invece di bloccarsi | la qualità dell'analisi locale su 8 GB è il punto debole dichiarato; mitigato dal fatto che entrambi i ruoli sono configurabili e il passaggio a cloud è una variabile |
| **Hosting non ancora scelto** | **sì, nessun impatto** | Immagini container, unico stato in Postgres, backend di inferenza indirizzabile via URL: passare a VPS, cloud o Kubernetes è un cambio di variabili e di manifest, non di codice | il volume `pgdata` va pianificato (backup, dimensione) prima di ospitare dati reali; il costo LLM va monitorato con `v_daily_llm_cost` |

### 11.2 Roadmap a fasi con criteri di accettazione osservabili

```mermaid
flowchart LR
    F0["Fase 0<br/>spike hardware e prompt"] --> F1["Fase 1<br/>MVP end-to-end"]
    F1 --> F2["Fase 2<br/>catalogazione continua e watch"]
    F2 --> F3["Fase 3<br/>espansione multi-sorgente"]
```

**Fase 0 — spike hardware e qualità dei prompt (1–2 giorni).** `docker compose up postgres`, `ideai doctor` **limitatamente ai passi 1–4** (il passo 5 esige la pipeline di Fase 1), uno script che legge un subreddit reale, persiste 100 item, ne classifica 20 con il modello di triage. Criterio di accettazione: il tag di triage (`minicpm4:8b-q4_K_M`) esiste ed è scaricabile dal registry Ollama; lo schema di triage è rispettato in ≥95% delle chiamate (percentuale letta da `llm_calls.status`); la velocità in token/s della macchina di riferimento è misurata e registrata. Nessuna decisione di modello viene presa prima di questa misura.

**Fase 1 — MVP end-to-end.** Autenticazione OAuth Reddit; schema completo con migrazioni; coda su Postgres con scheduler e reaper; triage; analisi con `analyst` puntato al modello leggero; scoring; endpoint di lettura `/ideas`, `/ideas/{id}`, `/sources`, `/stats`; pagine `/` e `/ideas/[id]`. In Fase 1 è attivo **solo G0** (duplicato esatto) e ogni item `product_idea`/`pain_point` crea una **nuova idea**: i cancelli semantici G1/G2 arrivano in Fase 2, quindi in questa fase non esiste deduplica semantica. Criterio di accettazione: da un subreddit reale compaiono almeno 5 idee con analisi completa e `opportunity_score`, visibili in dashboard, ciascuna con link alle evidenze originali.

**Fase 2 — catalogazione continua e watch.** Cancelli G0/G1/G2 attivi; watch automatico sopra soglia con intervallo adattivo; revisioni di analisi con delta; backfill Pullpush per un subreddit storico; budget LLM e pagina `/ops`. Criterio di accettazione: a un thread sorvegliato viene aggiunto un commento, e questo produce una nuova revisione di analisi con una riga `idea_updates` visibile, **senza** creare una seconda idea.

**Fase 3 — espansione.** Secondo adapter (Hacker News o un forum italiano via RSS/Discourse) senza modifiche a pipeline, schema o dashboard; sharding su più client OAuth; notifiche (Telegram o email) al superamento della soglia; manifest Kubernetes. Criterio di accettazione: la seconda sorgente produce idee nello stesso ranking, con lo stesso schema e le stesse analisi, e il codice modificato è confinato a `adapters/sources/`.

### 11.3 Operatività, sicurezza, conformità

- **Osservabilità**: log JSON strutturati (structlog) con `job_id`, `idea_id`, `source_kind`, `model_spec` su ogni riga; metriche Prometheus su `/metrics` (job per stato, lag della coda, latenza e token e costo LLM per ruolo, esiti di scrape); `/ops` è la vista minima che non richiede Grafana. `IDEAI_LOG_LEVEL` e `IDEAI_ENV` governano verbosità e formato.
- **Segreti e rete**: solo variabili d'ambiente; Postgres mai esposto su rete pubblica; `IDEAI_API_KEY` obbligatoria su ogni endpoint diverso da `/healthz` e `/readyz`. **La dashboard non ha autenticazione propria**: nel Compose `api` e `dashboard` pubblicano solo `127.0.0.1:8000` e `127.0.0.1:3000` e l'accesso remoto richiede un reverse proxy con autenticazione, **fuori dal Compose** (non è un componente di IdeaI). L'`IDEAI_API_KEY` è letta anche dal server Next.js e **mai esposta al browser**.
- **Termini di servizio Reddit**: rate limit rispettato tramite header, `user_agent` identificativo con proprietario, nessuna rivendita del contenuto grezzo, soglia di uso commerciale documentata (~0,24 USD per 1.000 chiamate) e punto di configurazione del costo in `sources.config.cost_per_call_usd`.
- **Privacy**: autori hashati con `IDEAI_SALT`, username mai persistiti, citazioni troncate a `IDEAI_QUOTE_MAX_CHARS`, retention di 30 giorni (`IDEAI_RETENTION_DAYS`) per il non catalogato, endpoint `DELETE /ideas/{id}` per le richieste di cancellazione.
- **Costi**: `IDEAI_LLM_DAILY_BUDGET_USD` è l'unico freno che non richiede presidio umano; `v_daily_llm_cost` e la pagina `/ops` rendono il consumo visibile prima che diventi un problema.

### 11.4 Strategia di verifica

Il documento è una specifica, non codice, ma fissa **come** il codice va verificato; sono test obbligatori, non opzionali:

- **(a) Golden set di triage.** `tests/fixtures/triage_golden.jsonl` contiene 200 item etichettati a mano (testo + categoria attesa). L'accuratezza di categoria è misurata e **registrata per `prompt_version`**: una modifica al prompt o al modello diventa un numero, non un'opinione.
- **(b) Test di contratto degli adapter.** Per ogni `kind` esiste `tests/fixtures/<kind>/` con payload registrati delle API reali; il test esegue `normalize` e confronta con l'item canonico atteso. Un cambio nella forma dei dati della sorgente rompe il test, non la produzione.
- **(c) Test di coda su Postgres reale.** Claim, lease, retry, reaper e dedup (`dedup_key`) si verificano contro un Postgres reale — container `pgvector/pgvector:pg17`, lo stesso dell'Appendice B — non contro un finto, perché il comportamento di `FOR UPDATE SKIP LOCKED`, degli indici parziali e dell'unicità di `dedup_key` non è simulabile. Il test include l'esecuzione **concorrente di due worker** sullo stesso database.

---

## Appendice A — Contratto di configurazione (IDEAI_*)

Tutte le variabili sono lette dall'ambiente. `.env.example` nel repository le elenca con gli stessi default. Le variabili marcate "sì" impediscono l'avvio se assenti; quelle marcate "condizionale" sono obbligatorie solo quando la condizione descritta è soddisfatta; le altre hanno un default utilizzabile senza configurazione. I default elencati sono quelli del profilo `gpu-8gb` (§10.4).

| Variabile | Tipo | Default | Obbligatoria | Descrizione |
|---|---|---|---|---|
| `IDEAI_ENV` | `dev` \| `prod` | `dev` | no | attiva/disattiva controlli di sviluppo e formato dei log |
| `IDEAI_LOG_LEVEL` | stringa | `INFO` | no | livello dei log strutturati |
| `IDEAI_DATABASE_URL` | URL | `postgresql+asyncpg://ideai:ideai@postgres:5432/ideai` | no | connessione a PostgreSQL |
| `IDEAI_API_KEY` | stringa | — | sì | bearer token accettato dall'API; letta anche dal server Next.js, **mai esposta al browser** |
| `IDEAI_API_BASE_URL` | URL | `http://api:8000` | no | base URL usata dalla dashboard per le chiamate all'API |
| `IDEAI_SALT` | stringa | — | sì | salt per `sha256` degli autori |
| `IDEAI_REDDIT_CLIENT_ID` | stringa | — | condizionale | client id dell'app OAuth Reddit; obbligatoria solo se esiste una sorgente `reddit` abilitata **e** solo nei processi che consumano `pipeline.scrape`/`pipeline.watch` (verificata all'avvio del worker, non da `api` o `worker-misc`) |
| `IDEAI_REDDIT_CLIENT_SECRET` | stringa | — | condizionale | client secret dell'app OAuth Reddit; stesse condizioni di `IDEAI_REDDIT_CLIENT_ID` |
| `IDEAI_REDDIT_USER_AGENT` | stringa | — | condizionale | formato `ideai/<versione> by /u/<owner>`; stesse condizioni di `IDEAI_REDDIT_CLIENT_ID` |
| `IDEAI_REDDIT_RPM` | intero | `100` | no | tetto di richieste/minuto verso Reddit |
| `IDEAI_PULLPUSH_RPM` | intero | `60` | no | tetto di richieste/minuto verso Pullpush |
| `IDEAI_SCRAPE_PAGE_SIZE` | intero | `100` | no | elementi per pagina di scrape |
| `IDEAI_DEFAULT_POLL_INTERVAL_S` | intero | `300` | no | intervallo di poll iniziale di un target |
| `IDEAI_RATE_LIMIT_WINDOW_S` | intero | `60` | no | ampiezza della finestra di rate limiting |
| `IDEAI_JOB_LEASE_S` | intero | `120` | no | durata del lease di un job |
| `IDEAI_JOB_HEARTBEAT_S` | intero | `30` | no | periodicità dell'heartbeat |
| `IDEAI_MAX_ATTEMPTS` | intero | `5` | no | tentativi prima dello stato `dead` |
| `IDEAI_JOB_RETENTION_DAYS` | intero | `7` | no | giorni di conservazione dei job `done` (per l'euristica dello scheduler e `/ops`) |
| `IDEAI_SCHEDULER_TICK_S` | intero | `30` | no | periodicità del tick dello scheduler |
| `IDEAI_WORKER_CONCURRENCY` | intero | `4` | no | task concorrenti per processo worker |
| `IDEAI_LLM_BACKENDS` | JSON | vedi §7.2 | no | mappa nome logico → trasporto |
| `IDEAI_LLM_PRICES` | JSON | `{}` | no | prezzi per milione di token, per modello |
| `IDEAI_LLM_DAILY_BUDGET_USD` | decimale | `5.00` | no | tetto di spesa giornaliera per i backend a pagamento |
| `IDEAI_LLM_CACHE_MAX_AGE_DAYS` | intero | `30` | no | validità delle righe di `llm_cache` |
| `IDEAI_TRIAGE_MODEL` | `<backend>/<model>` | `local/minicpm4:8b-q4_K_M` | no | modello del ruolo `triage` |
| `IDEAI_ANALYST_MODEL` | `<backend>/<model>` | `local/minicpm4:8b-q4_K_M` | no | modello del ruolo `analyst` |
| `IDEAI_EMBED_MODEL` | `<backend>/<model>` | `local/bge-m3:567m` | no | modello di embedding |
| `IDEAI_EMBED_DIM` | intero | `1024` | no | dimensione attesa dei vettori; mismatch = avvio rifiutato |
| `IDEAI_TRIAGE_BATCH` | intero | `10` | no | item per chiamata di triage |
| `IDEAI_DUP_SIM_HIGH` | decimale | `0.92` | no | soglia del cancello G1 |
| `IDEAI_DUP_SIM_LOW` | decimale | `0.80` | no | soglia inferiore del cancello G2 |
| `IDEAI_G0_WINDOW_DAYS` | intero | `7` | no | finestra di lookback del cancello G0 sui `content_hash` |
| `IDEAI_ANALYSIS_MAX_EVIDENCE` | intero | `20` | no | item di evidenza passati all'analista |
| `IDEAI_QUOTE_MAX_CHARS` | intero | `280` | no | troncamento delle citazioni |
| `IDEAI_RETENTION_DAYS` | intero | `30` | no | retention degli item non catalogati |
| `IDEAI_WATCH_THRESHOLD` | intero | `70` | no | soglia di `opportunity_score` per il watch automatico |
| `IDEAI_WATCH_MIN_DELTA` | intero | `3` | no | nuovi item che forzano una ri-analisi |
| `IDEAI_WATCH_MIN_INTERVAL_S` | intero | `900` | no | intervallo minimo di watch |
| `IDEAI_WATCH_MAX_INTERVAL_S` | intero | `86400` | no | intervallo massimo di watch |
| `IDEAI_WATCH_EXPIRE_DAYS` | intero | `30` | no | giorni senza novità prima della sospensione |
| `IDEAI_HARDWARE_PROFILE` | `cpu` \| `gpu-8gb` \| `gpu-16gb+` \| `cloud` | `gpu-8gb` | no | profilo che riempie i default non impostati |

Due variabili **non** prefissate sono nominate indirettamente da `IDEAI_LLM_BACKENDS` tramite `api_key_env`: `DEEPSEEK_API_KEY` e `ANTHROPIC_API_KEY`. Non compaiono altrove perché il gateway le legge solo quando il backend corrispondente è selezionato.

---

## Appendice B — Schema dati completo

DDL normativo: è questa la copia autorevole. §4.2 ne descrive semantica e vincoli; in caso di divergenza vale l'Appendice B.

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE sources (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('reddit','reddit_pullpush','hackernews','rss','discourse','github_issues')),
  name text NOT NULL, config jsonb NOT NULL DEFAULT '{}'::jsonb,
  enabled boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, name));

CREATE TABLE source_targets (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id bigint NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  target_ref text NOT NULL, target_kind text NOT NULL CHECK (target_kind IN ('subreddit','thread','feed','query')),
  cursor jsonb, last_polled_at timestamptz, next_poll_at timestamptz NOT NULL DEFAULT now(),
  poll_interval_s integer NOT NULL DEFAULT 300, enabled boolean NOT NULL DEFAULT true,
  error_count integer NOT NULL DEFAULT 0, last_error text, UNIQUE (source_id, target_ref));
CREATE INDEX source_targets_due_idx ON source_targets (next_poll_at) WHERE enabled;

CREATE TABLE idea_clusters (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title text NOT NULL, canonical_summary text NOT NULL,
  status text NOT NULL DEFAULT 'new' CHECK (status IN ('new','analyzed','watching','archived','rejected')),
  opportunity_score integer, score_version text NOT NULL DEFAULT 'v1',
  first_seen_at timestamptz NOT NULL DEFAULT now(), last_activity_at timestamptz NOT NULL DEFAULT now(),
  item_count integer NOT NULL DEFAULT 0, source_kinds text[] NOT NULL DEFAULT '{}');
CREATE INDEX idea_clusters_score_idx ON idea_clusters (opportunity_score DESC NULLS LAST);

CREATE TABLE items (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id bigint NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  external_id text NOT NULL, kind text NOT NULL CHECK (kind IN ('post','comment')),
  thread_external_id text, parent_external_id text, title text, body text NOT NULL,
  author_hash text, url text, score integer, num_comments integer, lang text,
  created_at timestamptz NOT NULL, edited_at timestamptz, fetched_at timestamptz NOT NULL DEFAULT now(),
  content_hash text NOT NULL, raw jsonb,
  state text NOT NULL DEFAULT 'new' CHECK (state IN ('new','triaged','rejected','candidate','analyzed','archived')),
  category text CHECK (category IN ('product_idea','pain_point','market_signal','question','announcement','spam','off_topic')),
  triage_confidence real, tags text[] NOT NULL DEFAULT '{}',
  idea_id bigint REFERENCES idea_clusters(id) ON DELETE SET NULL, attempts integer NOT NULL DEFAULT 0,
  reject_reason text CHECK (reject_reason IN ('exact_dup','not_idea','off_topic','spam','removed')),
  UNIQUE (source_id, external_id));
CREATE INDEX items_state_idx ON items (state);
CREATE INDEX items_thread_idx ON items (thread_external_id);
CREATE INDEX items_idea_idx ON items (idea_id);
CREATE INDEX items_tags_idx ON items USING gin (tags);
CREATE INDEX items_content_hash_idx ON items (content_hash);

CREATE TABLE idea_items (
  idea_id bigint NOT NULL REFERENCES idea_clusters(id) ON DELETE CASCADE,
  item_id bigint NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('seed','evidence','comment','update')),
  similarity real, added_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (idea_id, item_id));

CREATE TABLE analyses (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  idea_id bigint NOT NULL REFERENCES idea_clusters(id) ON DELETE CASCADE,
  revision integer NOT NULL, lang text NOT NULL DEFAULT 'it',
  model_spec text NOT NULL, provider text NOT NULL, prompt_version text NOT NULL, schema_version text NOT NULL,
  payload jsonb NOT NULL,
  feasibility_score smallint CHECK (feasibility_score BETWEEN 1 AND 5),
  economics_score smallint CHECK (economics_score BETWEEN 1 AND 5),
  competition_score smallint CHECK (competition_score BETWEEN 1 AND 5),
  verdict text CHECK (verdict IN ('strong','promising','weak','reject')),
  confidence real CHECK (confidence BETWEEN 0 AND 1), opportunity_score integer,
  tokens_in integer NOT NULL DEFAULT 0, tokens_out integer NOT NULL DEFAULT 0,
  cost_usd numeric(12,6) NOT NULL DEFAULT 0, latency_ms integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  superseded_by bigint REFERENCES analyses(id) ON DELETE SET NULL,
  UNIQUE (idea_id, revision));

CREATE TABLE embeddings (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_kind text NOT NULL CHECK (owner_kind IN ('item','idea')),
  owner_id bigint NOT NULL, model text NOT NULL, dim integer NOT NULL,
  vec vector(1024) NOT NULL, UNIQUE (owner_kind, owner_id, model));
CREATE INDEX embeddings_idea_vec_idx ON embeddings USING hnsw (vec vector_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE owner_kind = 'idea';

CREATE TABLE jobs (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  topic text NOT NULL CHECK (topic IN ('pipeline.scrape','pipeline.triage','pipeline.cluster','pipeline.analyze','pipeline.score','pipeline.watch','maintenance.retention','maintenance.reembed')),
  dedup_key text, payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','done','dead')),
  priority integer NOT NULL DEFAULT 100, attempts integer NOT NULL DEFAULT 0, max_attempts integer NOT NULL DEFAULT 5,
  run_after timestamptz NOT NULL DEFAULT now(), locked_by text, locked_at timestamptz, heartbeat_at timestamptz,
  last_error text, created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz);
CREATE UNIQUE INDEX jobs_dedup_idx ON jobs (dedup_key) WHERE state IN ('pending','running');
CREATE INDEX jobs_claim_idx ON jobs (topic, priority, run_after) WHERE state = 'pending';

CREATE TABLE rate_limits (
  bucket text PRIMARY KEY, window_start timestamptz NOT NULL, used integer NOT NULL DEFAULT 0);

CREATE TABLE source_calls_daily (
  day date NOT NULL,
  source_id bigint NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  calls integer NOT NULL DEFAULT 0, cost_usd numeric(12,6) NOT NULL DEFAULT 0,
  PRIMARY KEY (day, source_id));

CREATE TABLE llm_calls (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  role text NOT NULL CHECK (role IN ('triage','analyst','embed')),
  provider text NOT NULL, model text NOT NULL, prompt_hash text NOT NULL, schema_version text,
  tokens_in integer NOT NULL DEFAULT 0, tokens_out integer NOT NULL DEFAULT 0,
  cost_usd numeric(12,6) NOT NULL DEFAULT 0, latency_ms integer NOT NULL DEFAULT 0,
  status text NOT NULL CHECK (status IN ('ok','error','invalid_json')), error text,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX llm_calls_day_idx ON llm_calls (created_at, role);

CREATE TABLE llm_cache (
  cache_key text PRIMARY KEY, model_spec text NOT NULL, response jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE watchlist (
  idea_id bigint PRIMARY KEY REFERENCES idea_clusters(id) ON DELETE CASCADE,
  enabled boolean NOT NULL DEFAULT true, interval_s integer NOT NULL DEFAULT 3600,
  mode text NOT NULL DEFAULT 'thread_full' CHECK (mode IN ('comments_only','thread_full')),
  last_checked_at timestamptz, last_change_at timestamptz,
  added_by text NOT NULL DEFAULT 'auto' CHECK (added_by IN ('auto','manual')));

CREATE TABLE idea_updates (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  idea_id bigint NOT NULL REFERENCES idea_clusters(id) ON DELETE CASCADE,
  item_id bigint REFERENCES items(id) ON DELETE SET NULL,
  kind text NOT NULL CHECK (kind IN ('new_comment','new_post','edit','analysis_revision','watch_paused','merged')),
  summary text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX idea_updates_idea_idx ON idea_updates (idea_id, created_at DESC);

CREATE TABLE settings_overrides (
  key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());

CREATE VIEW v_idea_ranking AS
SELECT c.id, c.title, c.canonical_summary, c.status, c.opportunity_score, c.score_version,
       c.item_count, c.source_kinds, c.first_seen_at, c.last_activity_at,
       s.category, s.tags,
       COALESCE(w.enabled, false) AS watch_enabled, w.mode AS watch_mode, w.interval_s AS watch_interval_s,
       a.revision, a.verdict, a.confidence, a.feasibility_score, a.economics_score,
       a.competition_score, a.model_spec, a.created_at AS analyzed_at
  FROM idea_clusters c
  LEFT JOIN LATERAL (SELECT * FROM analyses x WHERE x.idea_id = c.id AND x.superseded_by IS NULL
                     ORDER BY x.revision DESC LIMIT 1) a ON true
  LEFT JOIN LATERAL (SELECT i.category, i.tags FROM idea_items ii JOIN items i ON i.id = ii.item_id
                     WHERE ii.idea_id = c.id AND ii.role = 'seed'
                     ORDER BY ii.added_at LIMIT 1) s ON true
  LEFT JOIN watchlist w ON w.idea_id = c.id;

CREATE VIEW v_daily_llm_cost AS
SELECT date_trunc('day', created_at AT TIME ZONE 'UTC') AS day, role, count(*) AS calls,
       sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out,
       sum(cost_usd) AS cost_usd, sum(latency_ms) AS latency_ms
  FROM llm_calls GROUP BY 1, 2;

CREATE VIEW v_queue_health AS
SELECT topic, state, count(*) AS jobs,
       min(run_after) FILTER (WHERE state = 'pending') AS oldest_pending,
       max(finished_at) FILTER (WHERE state = 'done') AS last_done,
       count(*) FILTER (WHERE state = 'dead') AS dead
  FROM jobs GROUP BY 1, 2;
```

Migrazioni: gestite con Alembic; la prima revisione crea esattamente quanto sopra, incluse le viste e l'indice HNSW parziale. Il ricalcolo degli embedding (`maintenance.reembed`) ricostruisce l'indice con `REINDEX INDEX embeddings_idea_vec_idx` al termine.

