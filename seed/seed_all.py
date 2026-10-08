"""
ETL - LOAD. Extracts + transforms the Jolpica dump, then bulk-loads all three databases.
Run from the project root:   .venv/bin/python -m seed.seed_all
Safe to re-run: every store is cleared and rebuilt.
"""
import sqlite3
import time
from pathlib import Path

from neo4j import GraphDatabase
from pymongo import ASCENDING, MongoClient

import config
from seed.cars_2024 import CARS
from seed.etl_jolpica import extract_transform

BATCH = 5000


def chunks(items, size=BATCH):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def load_sqlite(d):
    Path(config.SQLITE_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.SQLITE_PATH)
    con.executescript("""
        DROP TABLE IF EXISTS results;          DROP TABLE IF EXISTS driver_standings;
        DROP TABLE IF EXISTS team_standings;   DROP TABLE IF EXISTS races;
        DROP TABLE IF EXISTS seasons;          DROP TABLE IF EXISTS drivers;
        DROP TABLE IF EXISTS teams;            DROP TABLE IF EXISTS circuits;
        CREATE TABLE drivers  (driver_id TEXT PRIMARY KEY, code TEXT, full_name TEXT NOT NULL,
                               search_name TEXT, nationality TEXT, date_of_birth TEXT, number INTEGER);
        CREATE TABLE teams    (team_id TEXT PRIMARY KEY, name TEXT NOT NULL, search_name TEXT, nationality TEXT);
        CREATE TABLE circuits (circuit_id TEXT PRIMARY KEY, name TEXT NOT NULL, locality TEXT, country TEXT,
                               latitude REAL, longitude REAL, search_name TEXT);
        CREATE TABLE seasons  (year INTEGER PRIMARY KEY, complete INTEGER NOT NULL);
        CREATE TABLE races    (race_id INTEGER PRIMARY KEY, season INTEGER REFERENCES seasons(year),
                               round INTEGER, name TEXT, date TEXT,
                               circuit_id TEXT REFERENCES circuits(circuit_id));
        CREATE TABLE results  (race_id INTEGER REFERENCES races(race_id), session_type TEXT,
                               driver_id TEXT REFERENCES drivers(driver_id), team_id TEXT REFERENCES teams(team_id),
                               grid INTEGER, position INTEGER, points REAL, laps INTEGER, status TEXT,
                               fastest_lap_rank INTEGER);
        CREATE TABLE driver_standings (season INTEGER, driver_id TEXT REFERENCES drivers(driver_id),
                               position INTEGER, points REAL, wins INTEGER, PRIMARY KEY (season, driver_id));
        CREATE TABLE team_standings   (season INTEGER, team_id TEXT REFERENCES teams(team_id),
                               position INTEGER, points REAL, wins INTEGER, PRIMARY KEY (season, team_id));
    """)

    def insert(table, records):
        cols = list(records[0])
        con.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [tuple(r[c] for c in cols) for r in records])

    with con:  # one transaction for the whole load
        for table in ("drivers", "teams", "circuits", "seasons", "races", "results",
                      "driver_standings", "team_standings"):
            insert(table, d[table])
        con.executescript("""
            CREATE INDEX idx_results_driver ON results(driver_id, session_type);
            CREATE INDEX idx_results_team   ON results(team_id, session_type);
            CREATE INDEX idx_results_race   ON results(race_id);
            CREATE INDEX idx_races_circuit  ON races(circuit_id);
        """)
    total = sum(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
                ("drivers", "teams", "circuits", "seasons", "races", "results", "driver_standings", "team_standings"))
    con.close()
    return f"{total:,} rows in 8 tables"


def load_mongo(d):
    client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=3000)
    db = client[config.MONGO_DB]
    db.cars.drop()
    db.cars.insert_many([dict(c) for c in CARS])
    db.cars.create_index([("team_id", ASCENDING), ("season", ASCENDING)], unique=True)

    db.session_laps.drop()
    for batch in chunks(d["lap_docs"], 2000):
        db.session_laps.insert_many(batch, ordered=False)
    db.session_laps.create_index("driver_id")
    db.session_laps.create_index("team_id")
    db.session_laps.create_index([("circuit_id", ASCENDING), ("session_type", ASCENDING),
                                  ("fastest_lap_ms", ASCENDING)])
    docs = db.session_laps.count_documents({})
    laps = next(db.session_laps.aggregate([{"$group": {"_id": None, "n": {"$sum": "$lap_count"}}}]))["n"]
    client.close()
    return f"{docs:,} session documents embedding {laps:,} laps + {len(CARS)} car specs"


def load_neo4j(d):
    driver = GraphDatabase.driver(config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD))
    with driver.session() as s:
        s.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS")
        for label, key in (("Driver", "driver_id"), ("Team", "team_id"), ("Circuit", "circuit_id"), ("Engine", "name")):
            s.run(f"CREATE CONSTRAINT {label.lower()}_key IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

        for batch in chunks(d["drivers"]):
            s.run("UNWIND $rows AS r CREATE (:Driver {driver_id: r.driver_id, name: r.full_name, nationality: r.nationality})",
                  rows=batch)
        s.run("UNWIND $rows AS r CREATE (:Team {team_id: r.team_id, name: r.name})", rows=d["teams"])
        s.run("UNWIND $rows AS r CREATE (:Circuit {circuit_id: r.circuit_id, name: r.name, country: r.country})",
              rows=d["circuits"])

        for batch in chunks(d["drove_for"]):
            s.run("""UNWIND $rows AS r
                     MATCH (dr:Driver {driver_id: r.driver}), (t:Team {team_id: r.team})
                     CREATE (dr)-[:DROVE_FOR {seasons: r.seasons, first: r.seasons[0], last: r.seasons[-1]}]->(t)""",
                  rows=batch)
        for batch in chunks(d["wins"]):
            s.run("""UNWIND $rows AS r
                     MATCH (dr:Driver {driver_id: r.driver}), (c:Circuit {circuit_id: r.circuit})
                     CREATE (dr)-[:WON_AT {season: r.season, round: r.round, race: r.race, team_id: r.team}]->(c)""",
                  rows=batch)

        # Teammates: two drivers with a DROVE_FOR to the same team in at least one shared season
        s.run("""MATCH (a:Driver)-[ra:DROVE_FOR]->(:Team)<-[rb:DROVE_FOR]-(b:Driver)
                 WHERE a.driver_id < b.driver_id
                 WITH a, b, [y IN ra.seasons WHERE y IN rb.seasons] AS shared
                 WHERE size(shared) > 0
                 WITH a, b, reduce(all = [], l IN collect(shared) | all + l) AS seasons
                 CREATE (a)-[:TEAMMATE_OF {seasons: seasons}]->(b)""")

        # Engine suppliers are only known for the 2024 cars (from the MongoDB car specs)
        s.run("""UNWIND $rows AS r
                 MATCH (t:Team {team_id: r.team})
                 MERGE (e:Engine {name: r.engine})
                 CREATE (t)-[:POWERED_BY {season: r.season}]->(e)""",
              rows=[{"team": c["team_id"], "engine": c["power_unit"]["supplier"], "season": c["season"]} for c in CARS])

        nodes = s.run("MATCH (n) RETURN count(n) AS n").single()["n"]
        rels = s.run("MATCH ()-[r]->() RETURN count(r) AS n").single()["n"]
    driver.close()
    return f"{nodes:,} nodes, {rels:,} relationships"


def step(label, fn, *args):
    start = time.perf_counter()
    result = fn(*args)
    print(f"{label:<28} {result if isinstance(result, str) else '':<60} {time.perf_counter() - start:6.1f}s")
    return result


if __name__ == "__main__":
    data = step("extract + transform (CSV)", extract_transform)
    print(f"{'':<28} read {data['lap_rows_read']:,} lap rows, {len(data['results']):,} race results")
    step("load SQLite (RDBMS)", load_sqlite, data)
    step("load MongoDB", load_mongo, data)
    step("load Neo4j", load_neo4j, data)
    print("All three sources loaded.")
