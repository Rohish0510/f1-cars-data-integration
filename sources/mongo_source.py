"""
MongoDB wrapper.
Native format: BSON documents  ->  translated to XML elements.
  session_laps : one document per driver per session, laps[] and pit_stops[] embedded (~700k laps total)
  cars         : 2024 car specs, schema varies per document
Nested objects become child elements; arrays become repeated <item> elements.
"""
import atexit
import json
import re

from lxml import etree
from pymongo import MongoClient

import config
from sources.xml_helpers import leaf, lap_time, part

SOURCE = "mongodb"
_client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=2000)
atexit.register(_client.close)
_db = _client[config.MONGO_DB]


def _to_xml(parent, key, value):
    """Recursive document -> XML conversion (handles any schema, so new fields just appear)."""
    el = etree.SubElement(parent, key)
    if isinstance(value, dict):
        for k, v in value.items():
            _to_xml(el, k, v)
    elif isinstance(value, list):
        el.set("type", "array")
        for v in value:
            _to_xml(el, "item", v)
    else:
        el.text = str(value)
    return el


def _aggregate(tracer, step, collection, pipeline):
    with tracer.record(SOURCE, step, f"db.{collection}.aggregate({json.dumps(pipeline, indent=1)})") as t:
        docs = list(_db[collection].aggregate(pipeline))
        t["rows"] = len(docs)
    return docs


def health():
    return _client.admin.command("ping")["ok"] == 1


def stats():
    laps = next(_db.session_laps.aggregate([{"$group": {"_id": None, "laps": {"$sum": "$lap_count"},
                                                        "pits": {"$sum": {"$size": "$pit_stops"}}}}]))
    return {"session_laps documents": _db.session_laps.estimated_document_count(),
            "embedded laps": laps["laps"], "embedded pit stops": laps["pits"],
            "cars documents": _db.cars.estimated_document_count()}


def resolve(term, tracer, limit):
    pattern = re.compile(re.escape(term), re.IGNORECASE)  # escape: user text is not a regex
    shown = {"$or": [{"car_name": {"$regex": term, "$options": "i"}},
                     {"power_unit.model": {"$regex": term, "$options": "i"}}]}
    with tracer.record(SOURCE, "resolve cars", f"db.cars.find({json.dumps(shown)}, {{team_id: 1}})") as t:
        docs = list(_db.cars.find({"$or": [{"car_name": pattern}, {"power_unit.model": pattern}]},
                                  {"team_id": 1, "car_name": 1}).limit(limit))
        t["rows"] = len(docs)
    return [("Team", d["team_id"], f"mongodb: car {d['car_name']}") for d in docs]


# --- aggregation pipelines -------------------------------------------------------------
def _usage_pipeline(key, ids):
    """Laps, laps led and pit-stop timing for many drivers/teams in ONE pipeline."""
    return [
        {"$match": {key: {"$in": ids}}},
        {"$group": {"_id": f"${key}",
                    "sessions_with_timing": {"$sum": 1},
                    "races_with_timing": {"$sum": {"$cond": [{"$eq": ["$session_type", "R"]}, 1, 0]}},
                    "first_timed_season": {"$min": "$season"},
                    "laps_logged": {"$sum": "$lap_count"},
                    "laps_led": {"$sum": "$laps_led"},
                    "pit_stops": {"$sum": {"$size": "$pit_stops"}},
                    "pit_durations": {"$push": "$pit_stops.duration_ms"}}},
        {"$project": {"sessions_with_timing": 1, "races_with_timing": 1, "first_timed_season": 1,
                      "laps_logged": 1, "laps_led": 1, "pit_stops": 1,
                      "median_pit_ms": {"$median": {"input": {"$reduce": {
                          "input": "$pit_durations", "initialValue": [],
                          "in": {"$concatArrays": ["$$value", "$$this"]}}}, "method": "approximate"}}}},
    ]


def _circuit_pipeline(ids):
    """Fastest race lap ever recorded at each circuit + how many laps were timed there."""
    return [
        {"$match": {"circuit_id": {"$in": ids}, "session_type": "R"}},
        # $top must see timed laps first; documents without a lap time get has_time = 0
        {"$addFields": {"has_time": {"$cond": [{"$gt": ["$fastest_lap_ms", None]}, 1, 0]}}},
        {"$group": {"_id": "$circuit_id",
                    "timed_races": {"$addToSet": "$race_id"},
                    "laps_logged": {"$sum": "$lap_count"},
                    "lap_record": {"$top": {
                        "sortBy": {"has_time": -1, "fastest_lap_ms": 1},
                        "output": {"ms": "$fastest_lap_ms", "driver": "$driver_name", "team": "$team_name",
                                   "season": "$season", "lap": "$fastest_lap_no"}}}}},
    ]


def _usage_xml(element, doc):
    el = part(element, SOURCE)
    for k in ("races_with_timing", "sessions_with_timing", "first_timed_season", "laps_logged", "laps_led", "pit_stops"):
        leaf(el, k, doc[k])
    median = doc.get("median_pit_ms")
    leaf(el, "median_pit_stop", None if median is None else f"{median / 1000:.3f}s",
         note="pit-lane time, entry to exit")
    return el


def fetch(entity, element, ids, tracer):
    ids = sorted(ids)
    if element == "Car":
        # Latest car spec per team (only 2024 specs are stored)
        pipeline = [{"$match": {"team_id": {"$in": ids}}}, {"$sort": {"season": -1}},
                    {"$group": {"_id": "$team_id", "car": {"$first": "$$ROOT"}}}]
        out = {}
        for d in _aggregate(tracer, "fetch Team cars", "cars", pipeline):
            car = part("Car", SOURCE)
            for k, v in d["car"].items():
                if k not in ("_id", "team_id"):
                    _to_xml(car, k, v)
            out[d["_id"]] = car
        return out

    if entity == "Circuit":
        out = {}
        for d in _aggregate(tracer, "fetch Circuit lap record", "session_laps", _circuit_pipeline(ids)):
            el = part(element, SOURCE)
            rec = d["lap_record"]
            leaf(el, "timed_races", len(d["timed_races"]))
            leaf(el, "laps_logged", d["laps_logged"])
            leaf(el, "fastest_race_lap", lap_time(rec["ms"]), driver=rec["driver"], team=rec["team"],
                 season=rec["season"], lap=rec["lap"])
            out[d["_id"]] = el
        return out

    key = {"Driver": "driver_id", "Team": "team_id"}[entity]
    docs = _aggregate(tracer, f"fetch {entity} lap usage", "session_laps", _usage_pipeline(key, ids))
    return {d["_id"]: _usage_xml(element, d) for d in docs}
