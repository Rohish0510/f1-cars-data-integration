"""
RDBMS wrapper (SQLite).
Native format: relational rows  ->  translated to XML elements.
All queries use ? placeholders (prepared statements), never string formatting of user input.
Career statistics are computed live with JOIN + GROUP BY over ~27k race results.
"""
import sqlite3
from contextlib import contextmanager

import config
from seed.etl_jolpica import fold
from sources.xml_helpers import array, leaf, part

SOURCE = "rdbms"


@contextmanager
def _connect():
    """Read-only connection, always closed (sqlite3's own `with` only commits, it doesn't close)."""
    con = sqlite3.connect(f"file:{config.SQLITE_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def _in(ids):
    return ",".join("?" * len(ids))


def _query(tracer, step, sql, params):
    with _connect() as con, tracer.record(SOURCE, step, sql) as t:
        rows = con.execute(sql, params).fetchall()
        t["rows"] = len(rows)
    return rows


def health():
    with _connect() as con:
        return con.execute("SELECT count(*) FROM results").fetchone()[0] > 0


def stats():
    tables = ("drivers", "teams", "circuits", "seasons", "races", "results", "driver_standings", "team_standings")
    with _connect() as con:
        return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


# ---------------------------------------------------------------- resolve
RESOLVE_DRIVERS = """
SELECT d.driver_id, COUNT(*) OVER () AS total
FROM drivers d
LEFT JOIN (SELECT driver_id, SUM(position = 1) AS wins, COUNT(*) AS starts
           FROM results WHERE session_type = 'R' GROUP BY driver_id) s USING (driver_id)
WHERE d.search_name LIKE ? OR d.code = ? OR d.driver_id = ? OR lower(d.nationality) LIKE ?
-- exact code/id first; IS is NULL-safe (pre-2000s drivers have no code, and NULL would sort last)
ORDER BY (d.code IS ? OR d.driver_id IS ?) DESC, COALESCE(s.wins, 0) DESC, COALESCE(s.starts, 0) DESC
LIMIT ?"""

RESOLVE_TEAMS = """
SELECT t.team_id, COUNT(*) OVER () AS total
FROM teams t
LEFT JOIN (SELECT team_id, SUM(position = 1) AS wins, COUNT(*) AS starts
           FROM results WHERE session_type = 'R' GROUP BY team_id) s USING (team_id)
WHERE t.search_name LIKE ? OR t.team_id = ?
ORDER BY (t.team_id = ?) DESC, COALESCE(s.wins, 0) DESC, COALESCE(s.starts, 0) DESC
LIMIT ?"""

RESOLVE_CIRCUITS = """
SELECT c.circuit_id, COUNT(*) OVER () AS total
FROM circuits c LEFT JOIN (SELECT circuit_id, COUNT(*) AS races FROM races GROUP BY circuit_id) r USING (circuit_id)
WHERE c.search_name LIKE ? OR c.circuit_id = ?
ORDER BY (c.circuit_id = ?) DESC, COALESCE(r.races, 0) DESC
LIMIT ?"""


def resolve(term, tracer, limit):
    like, key, code = f"%{fold(term)}%", term.lower().replace(" ", "_"), term.upper()
    hits = []
    for entity, sql, params, reason in (
        ("Driver", RESOLVE_DRIVERS, (like, code, key, like, code, key, limit), "rdbms: driver name/code/nationality"),
        ("Team", RESOLVE_TEAMS, (like, key, key, limit), "rdbms: team name"),
        ("Circuit", RESOLVE_CIRCUITS, (like, key, key, limit), "rdbms: circuit name/city/country"),
    ):
        rows = _query(tracer, f"resolve {entity.lower()}s", sql, params)
        total = rows[0]["total"] if rows else 0
        hits += [(entity, r[0], f"{reason} (top {len(rows)} of {total})") for r in rows]
    return hits


# ---------------------------------------------------------------- fetch
DRIVER_PROFILE = """
SELECT d.driver_id, d.full_name, d.code, d.number, d.nationality, d.date_of_birth,
       MIN(ra.season) AS first_season, MAX(ra.season) AS last_season,
       SUM(r.session_type = 'R') AS race_starts,
       SUM(r.session_type = 'R' AND r.position = 1) AS wins,
       SUM(r.session_type = 'R' AND r.position <= 3) AS podiums,
       SUM(r.session_type = 'R' AND r.grid = 1) AS pole_starts,
       SUM(r.session_type = 'R' AND r.fastest_lap_rank = 1) AS fastest_laps,
       SUM(r.session_type = 'SR' AND r.position = 1) AS sprint_wins,
       ROUND(SUM(r.points), 1) AS points,
       (SELECT COUNT(*) FROM driver_standings s JOIN seasons se ON se.year = s.season
         WHERE s.driver_id = d.driver_id AND s.position = 1 AND se.complete) AS world_titles
FROM drivers d
LEFT JOIN results r ON r.driver_id = d.driver_id
LEFT JOIN races ra ON ra.race_id = r.race_id
WHERE d.driver_id IN ({ids})
GROUP BY d.driver_id"""

TEAM_PROFILE = """
SELECT t.team_id, t.name, t.nationality,
       MIN(ra.season) AS first_season, MAX(ra.season) AS last_season,
       COUNT(DISTINCT CASE WHEN r.session_type = 'R' THEN r.race_id END) AS races_entered,
       SUM(r.session_type = 'R' AND r.position = 1) AS wins,
       SUM(r.session_type = 'R' AND r.position <= 3) AS podiums,
       ROUND(SUM(r.points), 1) AS points,
       (SELECT COUNT(*) FROM team_standings s JOIN seasons se ON se.year = s.season
         WHERE s.team_id = t.team_id AND s.position = 1 AND se.complete) AS constructor_titles
FROM teams t
LEFT JOIN results r ON r.team_id = t.team_id
LEFT JOIN races ra ON ra.race_id = r.race_id
WHERE t.team_id IN ({ids})
GROUP BY t.team_id"""

# Drivers who started a race for each team in that team's most recent season
TEAM_LINEUP = """
WITH latest AS (SELECT r.team_id, MAX(ra.season) AS season FROM results r JOIN races ra USING (race_id)
                WHERE r.team_id IN ({ids}) GROUP BY r.team_id)
SELECT l.team_id, l.season, d.driver_id, d.full_name, COUNT(*) AS starts
FROM latest l
JOIN races ra ON ra.season = l.season
JOIN results r ON r.race_id = ra.race_id AND r.team_id = l.team_id AND r.session_type = 'R'
JOIN drivers d ON d.driver_id = r.driver_id
GROUP BY l.team_id, d.driver_id
ORDER BY starts DESC"""

CIRCUIT_PROFILE = """
SELECT c.circuit_id, c.name, c.locality, c.country, c.latitude, c.longitude,
       COUNT(ra.race_id) AS races_hosted, MIN(ra.season) AS first_season, MAX(ra.season) AS last_season,
       (SELECT name FROM races WHERE circuit_id = c.circuit_id ORDER BY date DESC LIMIT 1) AS latest_event
FROM circuits c LEFT JOIN races ra ON ra.circuit_id = c.circuit_id
WHERE c.circuit_id IN ({ids})
GROUP BY c.circuit_id"""

PROFILE_SQL = {"Driver": DRIVER_PROFILE, "Team": TEAM_PROFILE, "Circuit": CIRCUIT_PROFILE}
KEY = {"Driver": "driver_id", "Team": "team_id", "Circuit": "circuit_id"}


def fetch(entity, element, ids, tracer):
    """Batch-fetch the Profile part for many ids in ONE query -> {id: <Profile/>}"""
    ids = sorted(ids)
    rows = _query(tracer, f"fetch {entity} profiles", PROFILE_SQL[entity].format(ids=_in(ids)), ids)
    out = {}
    for r in rows:
        el = part(element, SOURCE)
        for col in r.keys():
            if col != KEY[entity]:
                leaf(el, col, r[col])
        out[r[KEY[entity]]] = el

    if entity == "Team" and out:
        lineups = {}
        for r in _query(tracer, "fetch Team latest line-up", TEAM_LINEUP.format(ids=_in(ids)), ids):
            lineups.setdefault((r["team_id"], r["season"]), []).append(
                {"name": r["full_name"], "id": r["driver_id"], "starts": r["starts"]})
        for (team_id, season), drivers in lineups.items():
            array(out[team_id], "latest_lineup", "driver", drivers, "name").set("season", str(season))
    return out
