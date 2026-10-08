"""
Backend (Flask). Every API response is XML - the integration format - not JSON.

  GET /api/search?q=Ferrari                 -> integrated F1Integration document
  GET /api/xpath?q=Ferrari&xpath=//car_name -> XPath evaluated over that document
  GET /api/mapping                          -> the global-schema mapping (mapping.xml)
  GET /api/stats                            -> data volume held by each source
  GET /api/health                           -> status of each source
"""
from flask import Flask, Response, request, send_from_directory
from lxml import etree

import config
from integration import mediator

app = Flask(__name__, static_folder="frontend", static_url_path="")
MAX_TERM, MAX_XPATH = 60, 300


def xml_response(el, status=200):
    body = etree.tostring(el, pretty_print=True, xml_declaration=True, encoding="UTF-8")
    return Response(body, status=status, mimetype="application/xml")


def error(message, status=400):
    el = etree.Element("Error")
    el.text = message
    return xml_response(el, status)


def read_term():
    term = (request.args.get("q") or "").strip()
    if not term or len(term) > MAX_TERM:
        return None
    return term


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/search")
def search():
    term = read_term()
    if term is None:
        return error(f"q is required (1-{MAX_TERM} characters)")
    return xml_response(mediator.integrate(term))


@app.get("/api/xpath")
def xpath():
    term, expression = read_term(), (request.args.get("xpath") or "").strip()
    if term is None or not expression or len(expression) > MAX_XPATH:
        return error("q and xpath are required")
    doc = mediator.integrate(term)
    try:
        results = mediator.run_xpath(doc, expression)
    except etree.XPathError as exc:
        return error(f"Invalid XPath: {exc}")
    root = etree.Element("XPathResult", expression=expression, count=str(len(results)))
    for r in results:
        etree.SubElement(root, "match").text = r
    return xml_response(root)


@app.get("/api/mapping")
def mapping():
    return xml_response(etree.parse(str(config.MAPPING_XML)).getroot())


@app.get("/api/stats")
def stats():
    root = etree.Element("DatasetStats")
    for sid, counts in mediator.dataset_stats().items():
        src = etree.SubElement(root, "source", id=sid)
        for name, n in counts.items():
            etree.SubElement(src, "count", name=name).text = str(n)
    return xml_response(root)


@app.get("/api/health")
def health():
    root = etree.Element("Health")
    for sid, wrapper in mediator.WRAPPERS.items():
        try:
            ok = wrapper.health()
        except Exception as exc:  # report, don't crash: one source down shouldn't hide the others
            ok, detail = False, str(exc).splitlines()[0][:120]
        else:
            detail = "connected"
        etree.SubElement(root, "source", id=sid, up=str(ok).lower()).text = detail
    return xml_response(root)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
