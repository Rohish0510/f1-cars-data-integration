"""
ETL - EXTRACT + TRANSFORM.
Reads the Jolpica F1 CSV dump (1950 to present, ~860k rows) and reshapes it for each database:

  SQLite  : normalised tables (drivers, teams, circuits, seasons, races, results, standings)
  MongoDB : one document per driver per session, with that session's laps + pit stops embedded
  Neo4j   : relationship lists (DROVE_FOR seasons, WON_AT wins)

Source: https://api.jolpi.ca/data/dumps/download/  (free 14-day-delayed CSV dump)
"""
import csv
import unicodedata
from collections import defaultdict

import config

RAW = config.ROOT / "data" / "raw" / "jolpica"
RACE_TYPES = ("R", "SR")  # Grand Prix + sprint race; others are practice / qualifying


def rows(table):
    with open(RAW / f"formula_one_{table}.csv", newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


def fold(text):
    """'Sergio Pérez' -> 'sergio perez' so a search for 'Perez' matches."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def to_ms(value):
    """'00:01:16.29' -> 76290 (milliseconds); '' -> None"""
    if not value:
        return None
    h, m, s = value.split(":")
    return round((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)


def num(value, cast=int):
    return cast(value) if value not in ("", None) else None


def extract_transform():
    year = {r["id"]: int(r["year"]) for r in rows("season")}

    # ---- reference entities ----
    drivers, driver_ref = [], {}
    for r in rows("driver"):
        driver_ref[r["id"]] = r["reference"]
        full = f'{r["forename"]} {r["surname"]}'
        drivers.append({"driver_id": r["reference"], "code": r["abbreviation"] or None, "full_name": full,
                        "search_name": fold(full), "nationality": r["nationality"],
                        "date_of_birth": r["date_of_birth"] or None, "number": num(r["permanent_car_number"])})
    driver_name = {d["driver_id"]: d["full_name"] for d in drivers}

    teams, team_ref = [], {}
    for r in rows("team"):
        team_ref[r["id"]] = r["reference"]
        teams.append({"team_id": r["reference"], "name": r["name"], "search_name": fold(r["name"]),
                      "nationality": r["nationality"]})
    team_name = {t["team_id"]: t["name"] for t in teams}

    circuits, circuit_ref = [], {}
    for r in rows("circuit"):
        circuit_ref[r["id"]] = r["reference"]
        circuits.append({"circuit_id": r["reference"], "name": r["name"], "locality": r["locality"],
                         "country": r["country"], "latitude": num(r["latitude"], float),
                         "longitude": num(r["longitude"], float),
                         "search_name": fold(f'{r["name"]} {r["locality"]} {r["country"]}')})

    races = {}
    for r in rows("round"):
        if r["is_cancelled"] == "t":
            continue
        races[r["id"]] = {"race_id": int(r["id"]), "season": year[r["season_id"]], "round": int(r["number"]),
                          "name": r["name"], "date": r["date"], "circuit_id": circuit_ref[r["circuit_id"]]}

    # ---- joins: session entry -> session (type, race) and -> round entry -> team-driver (driver, team) ----
    session = {r["id"]: (r["type"], r["round_id"]) for r in rows("session") if r["is_cancelled"] != "t"}
    team_driver = {r["id"]: (driver_ref[r["driver_id"]], team_ref[r["team_id"]]) for r in rows("teamdriver")}
    round_entry = {r["id"]: team_driver[r["team_driver_id"]] for r in rows("roundentry")}

    entries, results = {}, []
    for r in rows("sessionentry"):
        if r["session_id"] not in session:
            continue
        stype, round_id = session[r["session_id"]]
        if round_id not in races:
            continue
        driver_id, team_id = round_entry[r["round_entry_id"]]
        entries[r["id"]] = (stype, round_id, driver_id, team_id)
        if stype in RACE_TYPES:
            results.append({"race_id": int(round_id), "session_type": stype, "driver_id": driver_id,
                            "team_id": team_id, "grid": num(r["grid"]), "position": num(r["position"]),
                            "points": num(r["points"], float) or 0.0, "laps": num(r["laps_completed"]),
                            "status": r["detail"] or None, "fastest_lap_rank": num(r["fastest_lap_rank"])})

    # A season is complete when every (non-cancelled) round has race results.
    raced = {str(r["race_id"]) for r in results if r["session_type"] == "R"}
    seasons = {}
    for rid, race in races.items():
        seasons.setdefault(race["season"], True)
        if rid not in raced:
            seasons[race["season"]] = False

    # ---- final championship standings per season (the row with the highest round/session number) ----
    def final_standings(table, key, refs):
        best = {}
        for r in rows(table):
            k = (int(r["year"]), refs[r[key]])
            order = (int(r["round_number"] or 0), int(r["session_number"] or 0))
            if k not in best or order > best[k][0]:
                best[k] = (order, r)
        return [{"season": s, key: ref, "position": num(r["position"]), "points": num(r["points"], float),
                 "wins": num(r["win_count"])} for (s, ref), (_, r) in best.items()]

    driver_standings = final_standings("driverchampionship", "driver_id", driver_ref)
    team_standings = final_standings("teamchampionship", "team_id", team_ref)

    # ---- MongoDB documents: laps grouped by session entry, pit stops attached ----
    laps = defaultdict(list)       # session_entry_id -> [(lap, position, ms)]
    flagged_fastest = {}           # session_entry_id -> ms (rows marking the entry's fastest lap)
    lap_number = {}                # lap row id -> lap number (pit stops point at lap rows)
    lap_count = 0
    for r in rows("lap"):
        lap_count += 1
        ms = to_ms(r["time"])
        if r["number"]:
            laps[r["session_entry_id"]].append((int(r["number"]), num(r["position"]), ms))
            lap_number[r["id"]] = int(r["number"])
        if r["is_entry_fastest_lap"] == "t" and ms:
            flagged_fastest[r["session_entry_id"]] = ms

    pits = defaultdict(list)
    for r in rows("pitstop"):
        pits[r["session_entry_id"]].append({"stop": int(r["number"]), "lap": lap_number.get(r["lap_id"]),
                                            "duration_ms": to_ms(r["duration"])})

    lap_docs = []
    for entry_id in set(laps) | set(flagged_fastest) | set(pits):
        if entry_id not in entries:
            continue
        stype, round_id, driver_id, team_id = entries[entry_id]
        race = races[round_id]
        entry_laps = sorted(laps.get(entry_id, []))
        timed = [(ms, n) for n, _, ms in entry_laps if ms]
        fastest = min(timed) if timed else (flagged_fastest.get(entry_id), None)
        lap_docs.append({
            "_id": int(entry_id), "race_id": race["race_id"], "season": race["season"], "round": race["round"],
            "race_name": race["name"], "circuit_id": race["circuit_id"], "session_type": stype,
            "driver_id": driver_id, "driver_name": driver_name[driver_id],
            "team_id": team_id, "team_name": team_name[team_id],
            "lap_count": len(entry_laps),
            "laps_led": sum(1 for _, pos, _ in entry_laps if pos == 1) if stype in RACE_TYPES else 0,
            "fastest_lap_ms": fastest[0], "fastest_lap_no": fastest[1],
            "laps": [{"lap": n, "position": pos, "time_ms": ms} for n, pos, ms in entry_laps],
            "pit_stops": sorted(pits.get(entry_id, []), key=lambda p: p["stop"]),
        })

    # ---- Neo4j relationship lists ----
    drove = defaultdict(set)  # (driver, team) -> seasons
    wins = []
    for r in results:
        race = races[str(r["race_id"])]
        drove[(r["driver_id"], r["team_id"])].add(race["season"])
        if r["session_type"] == "R" and r["position"] == 1:
            wins.append({"driver": r["driver_id"], "team": r["team_id"], "circuit": race["circuit_id"],
                         "season": race["season"], "round": race["round"], "race": race["name"]})
    drove_for = [{"driver": d, "team": t, "seasons": sorted(s)} for (d, t), s in drove.items()]

    return {
        "drivers": drivers, "teams": teams, "circuits": circuits,
        "seasons": [{"year": y, "complete": c} for y, c in sorted(seasons.items())],
        "races": list(races.values()), "results": results,
        "driver_standings": driver_standings, "team_standings": team_standings,
        "lap_docs": lap_docs, "lap_rows_read": lap_count,
        "drove_for": drove_for, "wins": wins,
    }
