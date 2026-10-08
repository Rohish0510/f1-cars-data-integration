"""
THE MEDIATOR  -  one query in, one integrated XML document out.

  1. RESOLVE  : send the search term to every source; each says which Drivers/Teams/Circuits it recognises
  2. FETCH    : for every <part> in mapping.xml, batch-fetch from that part's source (XML fragments)
  3. MERGE    : stitch fragments into <Driver>/<Team>/<Circuit> elements by the shared key
  4. VALIDATE : check the integrated document against global_schema.xsd

Nothing here is specific to F1 or to a database: entities, parts and sources all come from mapping.xml.
"""
from datetime import datetime, timezone

from lxml import etree

import config
from integration.trace import Tracer
from sources import mongo_source, neo4j_source, rdbms_source

# source id in mapping.xml -> wrapper module (each has resolve() and fetch())
WRAPPERS = {"rdbms": rdbms_source, "mongodb": mongo_source, "neo4j": neo4j_source}

MAPPING = etree.parse(str(config.MAPPING_XML))
SCHEMA = etree.XMLSchema(etree.parse(str(config.GLOBAL_XSD)))
LIMIT = int(MAPPING.xpath("string(/globalSchema/resolution/@limit-per-entity)"))


def integrate(term):
    tracer = Tracer()

    # 1. RESOLVE - ask every source in mapping.xml's <resolution> block
    matches = {}  # entity -> {id: [reasons]}  (dict keeps the sources' ranking order)
    for source in MAPPING.xpath("/globalSchema/resolution/resolver/@source"):
        for entity, key, reason in WRAPPERS[source].resolve(term, tracer, LIMIT):
            matches.setdefault(entity, {}).setdefault(key, []).append(reason)

    root = etree.Element("F1Integration", query=term,
                         generatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         entities=str(sum(len(v) for v in matches.values())))

    for entity_def in MAPPING.xpath("/globalSchema/entity"):
        entity = entity_def.get("name")
        found = matches.get(entity)
        if not found:
            continue

        # 2. FETCH - each part from its own source, one batched call per part
        ids = set(found)
        fetched = [(p.get("element"), WRAPPERS[p.get("source")].fetch(entity, p.get("element"), ids, tracer))
                   for p in entity_def.findall("part")]

        # 3. MERGE - join the fragments on the shared key
        group = etree.SubElement(root, entity + "s")
        for key, reasons in found.items():
            el = etree.SubElement(group, entity, id=key, matchedBy="; ".join(reasons))
            for _element, by_id in fetched:
                if key in by_id:
                    el.append(by_id[key])

    trace_el = etree.SubElement(root, "Trace")
    for e in tracer.entries:
        q = etree.SubElement(trace_el, "SourceQuery", source=e["source"], step=e["step"],
                             rows=str(e["rows"]), ms=str(e["ms"]))
        q.text = e["query"]

    # 4. VALIDATE
    SCHEMA.assertValid(root)
    return root


def run_xpath(doc, expression):
    """Evaluate a user XPath over the integrated document -> list of result strings."""
    result = doc.xpath(expression)
    if not isinstance(result, list):  # count(), sum(), boolean ... return a scalar
        return [str(result)]
    return [etree.tostring(r, pretty_print=True, encoding="unicode").strip()
            if isinstance(r, etree._Element) else str(r) for r in result]


def dataset_stats():
    """Row/document/node counts per source - shows the data volume behind each query."""
    return {sid: w.stats() for sid, w in WRAPPERS.items()}
