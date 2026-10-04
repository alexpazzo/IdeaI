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

Il progetto è **in fase di progettazione**: questo repository contiene il documento di architettura, non ancora codice.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — documento di architettura decision-complete: schema dati completo, contratti tra componenti, formule di punteggio, configurazione, deployment e roadmap a fasi con criteri di accettazione.

Quanto descritto in questa pagina è la specifica da implementare: quando il codice esisterà, l'avvio previsto sarà `docker compose up` seguito da `ideai doctor`, il comando di bootstrap che verifica PostgreSQL e il backend di inferenza, scarica i modelli mancanti e prova la pipeline end-to-end.
