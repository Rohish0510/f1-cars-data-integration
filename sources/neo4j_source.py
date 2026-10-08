"""
Neo4j wrapper.
Native format: graph records (nodes, relationships, collected paths)  ->  translated to XML.
Graph: (:Driver)-[:DROVE_FOR {seasons}]->(:Team), (:Driver)-[:TEAMMATE_OF {seasons}]-(:Driver),
       (:Driver)-[:WON_AT {season, race, team_id}]->(:Circuit), (:Team)-[:POWERED_BY {season}]->(:Engine)
All Cypher uses $parameters, never string concatenation of user input.
"""
import atexit

from neo4j import GraphDatabase

import config
from sources.xml_helpers import array, leaf, part

SOURCE = "neo4j"
_driver = GraphDatabase.driver(config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD))
atexit.register(_driver.close)

RESOLVE_CYPHER = """
MATCH (t:Team)-[p:POWERED_BY]->(e:Engine)
WHERE toLower(e.name) CONTAINS toLower($term)
RETURN 'Team' AS entity, t.team_id AS id, 'neo4j: powered by ' + e.name + ' (' + p.season + ')' AS reason
LIMIT $limit
"""

DRIVER_CYPHER = """
UNWIND $ids AS did
MATCH (d:Driver {driver_id: did})
CALL (d) {
  OPTIONAL MATCH (d)-[r:DROVE_FOR]->(t:Team)
  WITH t, r ORDER BY r.first
  RETURN collect(CASE WHEN t IS NULL THEN null
                 ELSE {name: t.name, first: r.first, last: r.last, seasons: size(r.seasons)} END) AS teams
}
CALL (d) {
  OPTIONAL MATCH (d)-[m:TEAMMATE_OF]-(mate:Driver)
  WITH mate, m ORDER BY size(m.seasons) DESC, mate.name LIMIT 6
  RETURN collect(CASE WHEN mate IS NULL THEN null ELSE {name: mate.name, seasons: size(m.seasons)} END) AS teammates
}
CALL (d) {
  OPTIONAL MATCH (d)-[:TEAMMATE_OF]-(mate:Driver)
  RETURN count(DISTINCT mate) AS teammate_count
}
CALL (d) {
  OPTIONAL MATCH (d)-[w:WON_AT]->(c:Circuit)
  WITH c, count(w) AS n ORDER BY n DESC, c.name LIMIT 6
  RETURN collect(CASE WHEN c IS NULL THEN null ELSE {name: c.name, wins: n} END) AS win_circuits
}
RETURN did AS id, teams, teammates, teammate_count, win_circuits
"""

TEAM_CYPHER = """
UNWIND $ids AS tid
MATCH (t:Team {team_id: tid})
CALL (t) {
  OPTIONAL MATCH (t)-[p:POWERED_BY]->(e:Engine)
  OPTIONAL MATCH (other:Team)-[:POWERED_BY]->(e) WHERE other <> t
  RETURN e.name AS engine, p.season AS engine_season, collect(DISTINCT other.name) AS engine_partners
}
CALL (t) {
  OPTIONAL MATCH (d:Driver)-[w:WON_AT {team_id: t.team_id}]->(:Circuit)
  WITH d, count(w) AS n ORDER BY n DESC, d.name LIMIT 6
  RETURN collect(CASE WHEN d IS NULL THEN null ELSE {name: d.name, wins: n} END) AS top_winners
}
CALL (t) {
  OPTIONAL MATCH (d:Driver)-[:DROVE_FOR]->(t)
  RETURN count(d) AS drivers_all_time
}
RETURN tid AS id, engine, engine_season, engine_partners, top_winners, drivers_all_time
"""

CIRCUIT_CYPHER = """
UNWIND $ids AS cid
MATCH (c:Circuit {circuit_id: cid})
CALL (c) {
  OPTIONAL MATCH (d:Driver)-[w:WON_AT]->(c)
  WITH d, count(w) AS n, max(w.season) AS latest ORDER BY n DESC, latest DESC LIMIT 5
  RETURN collect(CASE WHEN d IS NULL THEN null ELSE {name: d.name, wins: n} END) AS top_winners
}
CALL (c) {
  OPTIONAL MATCH (d:Driver)-[w:WON_AT]->(c)
  WITH d, w ORDER BY w.season DESC, w.round DESC LIMIT 1
  RETURN d.name AS latest_winner, w.season AS latest_win_season
}
RETURN cid AS id, top_winners, latest_winner, latest_win_season
"""

CYPHER = {"Driver": DRIVER_CYPHER, "Team": TEAM_CYPHER, "Circuit": CIRCUIT_CYPHER}


def health():
    _driver.verify_connectivity()
    return True


def stats():
    with _driver.session() as s:
        nodes = {r["label"]: r["n"] for r in s.run("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n")}
        rels = {r["type"]: r["n"] for r in s.run("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n")}
    return {**{f"{k} nodes": v for k, v in nodes.items()}, **{f"{k} relationships": v for k, v in rels.items()}}


def resolve(term, tracer, limit):
    with _driver.session() as s, tracer.record(SOURCE, "resolve engines", RESOLVE_CYPHER) as t:
        records = list(s.run(RESOLVE_CYPHER, term=term, limit=limit))
        t["rows"] = len(records)
    return [(r["entity"], r["id"], r["reason"]) for r in records]


def _to_xml(entity, element, r):
    el = part(element, SOURCE)
    if entity == "Driver":
        array(el, "teams", "team", r["teams"], "name")
        leaf(el, "teammate_count", r["teammate_count"])
        array(el, "longest_teammates", "driver", r["teammates"], "name")
        array(el, "wins_by_circuit", "circuit", r["win_circuits"], "name")
    elif entity == "Team":
        leaf(el, "engine", r["engine"], season=r["engine_season"])
        array(el, "engine_partners", "team", [{"name": n} for n in sorted(r["engine_partners"])], "name")
        leaf(el, "drivers_all_time", r["drivers_all_time"])
        array(el, "top_winners", "driver", r["top_winners"], "name")
    else:
        array(el, "top_winners", "driver", r["top_winners"], "name")
        leaf(el, "latest_winner", r["latest_winner"], season=r["latest_win_season"])
    return el


def fetch(entity, element, ids, tracer):
    """One UNWIND query covers every id -> {id: <Relations/>}"""
    with _driver.session() as s, tracer.record(SOURCE, f"fetch {entity} relations", CYPHER[entity]) as t:
        records = list(s.run(CYPHER[entity], ids=sorted(ids)))
        t["rows"] = len(records)
    return {r["id"]: _to_xml(entity, element, r) for r in records}
