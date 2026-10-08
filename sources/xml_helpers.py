"""Small helpers every wrapper uses to turn native results into XML elements."""
from lxml import etree


def part(tag, source):
    """The root of one fetched fragment, stamped with its provenance."""
    return etree.Element(tag, source=source)


def leaf(parent, tag, value, **attrs):
    el = etree.SubElement(parent, tag, {k: str(v) for k, v in attrs.items() if v is not None})
    el.text = "" if value is None else str(value)
    return el


def array(parent, tag, item_tag, records, text_key):
    """[{text_key: 'Ferrari', 'wins': 5}, ...] -> <tag type="array"><item_tag wins="5">Ferrari</item_tag>..."""
    el = etree.SubElement(parent, tag, type="array")
    for r in records:
        attrs = {k: str(v) for k, v in r.items() if k != text_key and v is not None}
        etree.SubElement(el, item_tag, attrs).text = str(r[text_key])
    return el


def lap_time(ms):
    """76290 -> '1:16.290'"""
    if ms is None:
        return None
    minutes, rest = divmod(ms, 60000)
    return f"{minutes}:{rest / 1000:06.3f}"
