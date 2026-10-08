"""Records every native query sent to a source, so the UI can show what really ran where."""
import time
from contextlib import contextmanager


class Tracer:
    def __init__(self):
        self.entries = []  # (source, step, query_text, rows, ms)

    @contextmanager
    def record(self, source, step, query_text):
        entry = {"source": source, "step": step, "query": query_text.strip(), "rows": 0}
        start = time.perf_counter()
        try:
            yield entry  # caller sets entry["rows"]
        finally:
            entry["ms"] = round((time.perf_counter() - start) * 1000, 2)
            self.entries.append(entry)
