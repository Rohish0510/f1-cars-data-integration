# F1 Cars: Heterogeneous Big Data Integration through XML

Big Data lab project. Case study: **Formula 1, 1950 to present.**
One search query is answered by three different databases (SQLite, MongoDB, Neo4j) and the answers are merged into a single, schema-validated XML document.

| Requirement | Where it is |
|---|---|
| At least 2 databases (Mongo, Neo4j) + optional RDBMS | MongoDB, Neo4j **and** SQLite: `sources/` |
| Data integration using XML | `integration/mapping.xml` (global schema), `integration/mediator.py` (merge), `integration/global_schema.xsd` (validation) |
| Explain how one query fetches from all DBs | The 4-step mediator (section 4). The UI's **Source queries** tab shows every native SQL / MongoDB / Cypher query that ran |
| Frontend + backend | `frontend/` (HTML/CSS/JS) + `app.py` (Flask) |

---

## 1. The dataset (real data, ~860k source rows)

Source: the **Jolpica F1** open database (successor of the Ergast API), free CSV dump: https://api.jolpi.ca/data/dumps/download/
18 CSV tables, 1950 to the current season, including **729,241 lap-time rows**, 51,413 session results, 27,619 race entries and 12,867 pit stops.

The 2024 car specifications (`seed/cars_2024.py`) are not in the dump; they were added by hand to show a semi-structured MongoDB collection.

## 2. Where the Vs of Big Data show up

| V | In this project |
|---|---|
| **Volume** | ~860k source rows. MongoDB holds 709,434 laps embedded in 36,799 documents; SQLite 33,246 rows; Neo4j ~10k relationships |
| **Variety** | Three data models at once: relational tables, nested documents, a property graph. This is the core of the assignment |
| **Veracity** | Accent-insensitive matching ("Perez" finds "Sergio Pérez"), incomplete seasons excluded from championship titles, XSD validation of every integrated result, provenance (`source="…"`) on every fragment |
| **Value** | Questions no single database can answer: one query returns a driver's career stats (SQL), lap and pit data (MongoDB) and career graph (Neo4j) |
| **Velocity** | Not the focus. The dump is re-downloadable and `seed_all` rebuilds all three stores in under 5 seconds, so refreshing to a newer dump is one command |

## 3. Why three databases? (each stores what it is best at)

| Database | Stores | Why this DB |
|---|---|---|
| **SQLite (RDBMS)** | `drivers`, `teams`, `circuits`, `seasons`, `races`, `results`, `driver_standings`, `team_standings` | Fixed, tabular facts with foreign keys. Career stats are computed live with `JOIN` + `GROUP BY` over ~27k results |
| **MongoDB** | `session_laps`: one document per driver per session with `laps[]` and `pit_stops[]` **embedded**; `cars`: 2024 specs | Lap data is naturally nested and large. Aggregation pipelines (`$group`, `$top`, `$median`) summarise thousands of laps per query. `cars` documents have different fields per car |
| **Neo4j** | `(:Driver)-[:DROVE_FOR {seasons}]->(:Team)`, `(:Driver)-[:TEAMMATE_OF {seasons}]-(:Driver)`, `(:Driver)-[:WON_AT {season, team_id}]->(:Circuit)`, `(:Team)-[:POWERED_BY]->(:Engine)` | Relationships. "Who were Hamilton's longest teammates?" is one hop in a graph; in SQL it is a self-join of results on team + season |

The **shared keys** `driver_id`, `team_id`, `circuit_id` (Jolpica reference ids such as `max_verstappen`, `ferrari`, `monaco`) are identical in all three databases. They are what make the merge possible.

## 4. What happens on ONE query (explain this in the viva)

```
 Browser (frontend/)                 Flask backend (app.py)
 ───────────────────                 ──────────────────────
 search "Hamilton" ── GET /api/search?q=Hamilton ──►  MEDIATOR (integration/mediator.py)
                                                      │  reads mapping.xml (global schema)
                                                      ├──► rdbms_source  ──SQL────► SQLite
                                                      ├──► mongo_source  ──MQL────► MongoDB
                                                      └──► neo4j_source  ──Cypher─► Neo4j
                                                      each WRAPPER converts its native result to XML
                                                      │
                                                      ▼  merge on the shared key, validate with XSD
 DOMParser renders  ◄────────── application/xml ──── <F1Integration> … </F1Integration>
```

`integration/mediator.py → integrate(term)`:

1. **Resolve.** The term goes to **every** source listed in `mapping.xml <resolution>`. Each one says which entities it recognises (top 6 per type):
   - SQLite: driver name / code / nationality, team name, circuit name / city / country
   - MongoDB: car name or power-unit model (`"MCL38"` → McLaren)
   - Neo4j: engine supplier (`"Honda RBPT"` → Red Bull + RB)
2. **Fetch.** For every `<part>` in `mapping.xml`, the mediator calls that source's wrapper **once for all matched ids** (`IN (...)`, `$in`, `UNWIND`). It never queries row by row.
3. **Merge.** Each wrapper has already turned its result into an XML fragment (`<Profile source="rdbms">`, `<Laps source="mongodb">`, `<Relations source="neo4j">`). The mediator places them under `<Driver id>` / `<Team id>` / `<Circuit id>` by the shared key.
4. **Validate.** The integrated document is checked against `global_schema.xsd` before it is returned.

The result is plain XML, so the **XPath** tab queries across all three databases at once.
For example, `//Driver[Profile/world_titles > 0]/Relations/teams/team` filters on an SQLite value and returns Neo4j data.

### Integration concepts you can name
- **Mediator / wrapper architecture**: one wrapper per source, one mediator on top.
- **Global-as-View (GAV)**: the global entities `Driver`, `Team`, `Circuit` are defined as views over the local sources in `mapping.xml`. The mediator has no F1- or DB-specific code; adding a fourth database means adding a `<source>` and a `<part>`.
- **Schema heterogeneity**: rows vs documents vs graphs, normalised to one XML model.
- **Entity resolution**: different sources recognise the same real-world entity differently; `matchedBy` records which source matched and how many candidates there were.
- **Provenance**: every fragment carries `source="…"`; `<Trace>` lists the exact native queries with row counts and timings.

## 5. ETL: how the data gets in (`seed/`)

| Step | File | What it does |
|---|---|---|
| Extract | `seed/etl_jolpica.py` | Streams the 18 CSV files with Python's `csv` module |
| Transform | `seed/etl_jolpica.py` | Joins session entries to drivers, teams and races; converts `00:01:16.29` lap times to milliseconds; groups 729k lap rows into per-session documents; computes final standings and which seasons are complete; derives graph edges |
| Load | `seed/seed_all.py` | SQLite: one transaction + indexes. MongoDB: `insert_many` in batches of 2,000 + indexes. Neo4j: `UNWIND` batches of 5,000 + uniqueness constraints; teammates derived in Cypher |

Typical run (all three stores rebuilt from scratch):

```
extract + transform (CSV)                                         1.9s
                             read 729,241 lap rows, 26,771 race results
load SQLite (RDBMS)          33,246 rows in 8 tables              0.1s
load MongoDB                 36,799 session documents embedding 709,434 laps + 10 car specs   0.6s
load Neo4j                   1,177 nodes, 9,946 relationships     1.2s
```

## 6. Run it

Prerequisites: Python 3.11+, MongoDB, Neo4j (on macOS: `brew install mongodb-community neo4j`).

Start the databases (set the Neo4j password once, before its first start):
```bash
brew services start mongodb-community
```
```bash
neo4j-admin dbms set-initial-password f1password
```
```bash
brew services start neo4j
```

Download the dataset (free, ~14 MB zip):
```bash
mkdir -p data/raw && curl -L -o data/raw/jolpica_dump.zip "https://api.jolpi.ca/data/dumps/download/delayed/?dump_type=csv"
```
```bash
unzip -o data/raw/jolpica_dump.zip -d data/raw/jolpica
```

Install, load, run:
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
```bash
.venv/bin/python -m seed.seed_all
```
```bash
.venv/bin/python app.py
```
Open http://localhost:5050. Neo4j Browser (to show the graph live) is at http://localhost:7474 (user `neo4j`, password `f1password`, local demo only).

Connection settings can be overridden with env vars `SQLITE_PATH`, `MONGO_URI`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` (see `config.py`).

Tests (databases loaded and running):
```bash
.venv/bin/python -m unittest -v
```

## 7. Demo script (about 6 minutes)

1. **Volume strip** at the top: 33k SQL rows, 709k laps in MongoDB, ~10k graph relationships. Open a "breakdown".
2. Search **Hamilton**. Profile (SQLite: 106 wins, 7 titles), Laps (MongoDB: laps logged, laps led, median pit stop), Relations (Neo4j: McLaren → Mercedes → Ferrari, longest teammates, wins by circuit).
3. Search **Monaco**. MongoDB finds the fastest race lap in the dataset (1:12.909, Hamilton, 2021) by aggregating every Monaco lap document; Neo4j gives the most wins there (Senna, 6).
4. Search **Ferrari**. Point at `matchedBy`: SQLite matched team names; Neo4j added Haas and Sauber (Ferrari engines, 2024); Imola appears because its official name is Autodromo *Enzo e Dino Ferrari*.
5. Search **MCL38**. Only MongoDB knows car names, yet the result has SQL stats and graph data.
6. **Source queries** tab: the real SQL, aggregation pipelines and Cypher, with row counts and milliseconds.
7. **XML document** tab: one document, `source` attribute on every part.
8. **XPath** tab: `//Driver[Profile/world_titles > 0]/Profile/full_name/text()`.
9. **Mapping** tab: the global schema that drives everything.
10. Optional, Neo4j Browser: `MATCH p=(:Driver {driver_id:'hamilton'})-[:TEAMMATE_OF]-(:Driver) RETURN p`.

## 8. Likely viva questions

- **Why not put everything in one database?** Each model fits one part of the data: relational for fixed facts and aggregates, documents for nested lap data, graph for relationships. Integration lets us keep each in its best store and still answer one query.
- **Why XML and not JSON?** XML has a schema language (XSD) for validating the integrated result and a standard query language (XPath) that works over the merged data. Both are used here.
- **How do the databases know they are talking about the same driver?** Shared reference keys (`hamilton`, `ferrari`, `monaco`) are loaded into all three by the same ETL script.
- **How does it scale?** Each source is queried once per part with all ids batched, indexes exist on every lookup key, and MongoDB does the heavy lap aggregation inside the database. A typical query runs 8 to 13 native queries in under 100 ms on a laptop.
- **Is it safe against injection?** SQL uses `?` placeholders, Cypher uses `$parameters`, and the MongoDB search escapes user text before building a regex. Tests cover this.

## 9. API (every response is XML)

| Endpoint | Returns |
|---|---|
| `GET /api/search?q=Hamilton` | integrated `<F1Integration>` document |
| `GET /api/xpath?q=Hamilton&xpath=//wins` | `<XPathResult>` |
| `GET /api/mapping` | `mapping.xml` |
| `GET /api/stats` | data volume held by each source |
| `GET /api/health` | status of each source |

## 10. Files

```
config.py                       connection settings
app.py                          Flask backend (XML API + serves frontend)
integration/mapping.xml         global schema: which source supplies which part
integration/global_schema.xsd   validates the integrated document
integration/mediator.py         resolve → fetch → merge → validate
integration/trace.py            records each native query for the UI
sources/rdbms_source.py         SQLite wrapper  (rows      → XML)
sources/mongo_source.py         MongoDB wrapper (documents → XML, aggregation pipelines)
sources/neo4j_source.py         Neo4j wrapper   (graph     → XML)
sources/xml_helpers.py          shared XML building helpers
seed/etl_jolpica.py             ETL extract + transform from the Jolpica CSV dump
seed/seed_all.py                ETL load into SQLite + MongoDB + Neo4j
seed/cars_2024.py               hand-added 2024 car specs (semi-structured)
frontend/                       index.html, style.css, app.js (parses XML with DOMParser)
test_integration.py             end-to-end tests
```

## 11. Data notes

- Data is the Jolpica dump as downloaded (free tier, 14 days behind live). The current season is incomplete, so its leader is not counted as champion.
- "points" in a profile is the sum of points scored in every Grand Prix and sprint. In early decades only a driver's best results counted toward the championship, so this can differ from official championship totals.
- Lap-by-lap timing exists from 1996 onward (a few earlier races have only a fastest-lap time); pit-stop data from 2011. Median pit stop is pit-lane time, entry to exit.
- Engine suppliers (Neo4j `POWERED_BY`) and car specs (MongoDB `cars`) cover the 2024 season only.
