// Frontend: calls the backend, receives XML (not JSON), parses it with DOMParser and renders it.
// The renderer is generic: it walks whatever parts the XML contains, so new fields in any DB just appear.
const $ = (sel) => document.querySelector(sel);
const SOURCE_LABEL = { rdbms: "SQLite (RDBMS)", mongodb: "MongoDB", neo4j: "Neo4j" };
const PART_TITLE = { Profile: "Profile & stats", Car: "Car spec", Laps: "Lap & pit-stop data", Relations: "Relationships" };
const EXAMPLES = ["Hamilton", "Ferrari", "Monaco", "Senna", "MCL38", "Honda", "Brazilian", "Silverstone", "Perez"];
const XPATH_EXAMPLES = [
  "//Driver/Profile/full_name/text()",
  "//Driver[Profile/world_titles > 0]/Profile/full_name/text()",
  "sum(//Driver/Profile/wins)",
  "//Circuit/Laps/fastest_race_lap/@driver",
  "//Team/Relations/top_winners/driver[1]/text()",
  "count(//Trace/SourceQuery[@source='mongodb'])",
];
const HIDDEN = new Set(["full_name", "name", "car_name"]); // already shown in the card title
let currentQuery = "";

// h("div", {class: "x"}, child, "text") -> element. textContent only: data is never parsed as HTML.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  for (const c of children.flat()) if (c != null) el.append(c instanceof Node ? c : String(c));
  return el;
}
const badge = (src) => h("span", { class: `src ${src}` }, src);
const text = (node, sel) => node?.querySelector(sel)?.textContent ?? "";
const label = (tag) => tag.replace(/_/g, " ");
// Thousands separators for counts, never for years ("2024", "first_season")
const fmtNum = (s, key) => (/^\d{4,}(\.\d+)?$/.test(s) && !/season|year/.test(key) ? Number(s).toLocaleString("en-US") : s);

async function fetchXml(url) {
  const res = await fetch(url);
  const doc = new DOMParser().parseFromString(await res.text(), "application/xml");
  if (!res.ok) throw new Error(doc.documentElement.textContent || res.statusText);
  return doc;
}

// Pretty XML with light syntax highlighting. Escape first, so data can never become markup;
// then one pass per tag, so inserted spans are never re-matched.
function highlightXml(xmlText) {
  const esc = xmlText.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc
    .replace(/(&lt;!--[\s\S]*?--&gt;)/g, '<span class="c">$1</span>')
    .replace(/(&lt;\/?)([\w:.-]+)((?:\s+[\w:-]+="[^"]*")*)/g, (_, open, name, attrs) =>
      `${open}<span class="t">${name}</span>` +
      attrs.replace(/([\w:-]+)=("[^"]*")/g, '<span class="a">$1</span>=<span class="v">$2</span>'));
}
const serialize = (doc) => new XMLSerializer().serializeToString(doc);

// Attributes -> readable suffix: first/last -> "2007-2012", seasons -> "6 seasons", wins -> "9 wins"
function describeAttrs(el, skip = []) {
  const a = Object.fromEntries([...el.attributes].map((x) => [x.name, x.value]));
  const bits = [];
  if (a.first && a.last) bits.push(a.first === a.last ? a.first : `${a.first}-${a.last}`);
  for (const [k, v] of Object.entries(a)) {
    if (["first", "last", "id", "type", "note", ...skip].includes(k)) continue;
    if (["seasons", "wins", "starts"].includes(k)) bits.push(`${v} ${v === "1" ? k.slice(0, -1) : k}`);
    else if (k === "lap") bits.push(`lap ${v}`);
    else bits.push(v);
  }
  return bits.length ? ` (${bits.join(", ")})` : "";
}

function valueOf(child) {
  if (child.getAttribute("type") === "array") {
    const list = [...child.children].map((i) =>
      i.children.length ? [...i.children].map((c) => `${label(c.tagName)}: ${c.textContent}`).join(", ")
        : i.textContent + describeAttrs(i));
    return list.join(" · ") || "none";
  }
  if (child.children.length) return [...child.children].map((c) => `${label(c.tagName)}: ${c.textContent}`).join(" · ");
  return (fmtNum(child.textContent, child.tagName) || "n/a") + describeAttrs(child);
}

function partView(partEl) {
  const src = partEl.getAttribute("source");
  const dl = h("dl", { class: "kv" });
  for (const child of partEl.children) {
    if (HIDDEN.has(child.tagName)) continue;
    const season = child.getAttribute("season");
    const key = label(child.tagName) + (season && child.getAttribute("type") === "array" ? ` ${season}` : "");
    dl.append(h("dt", {}, key), h("dd", {}, valueOf(child)));
  }
  const title = partEl.tagName === "Car" ? `Car: ${text(partEl, "car_name")}` : PART_TITLE[partEl.tagName];
  return h("div", { class: `part ${src}` }, h("h3", {}, title, badge(src)), dl);
}

function subtitle(type, p) {
  if (type === "Driver") return `${text(p, "nationality")} · ${text(p, "first_season")}-${text(p, "last_season")}`;
  if (type === "Team") return `${text(p, "nationality")} · ${text(p, "first_season")}-${text(p, "last_season")}`;
  return `${text(p, "locality")}, ${text(p, "country")}`;
}

function renderEntity(el) {
  const type = el.tagName, profile = el.querySelector(":scope > Profile");
  const name = text(profile, "full_name") || text(profile, "name");
  return h("article", { class: "entity" },
    h("header", { class: "entity-head" },
      h("div", {}, h("span", { class: "kind" }, type), h("h2", {}, name),
        h("span", { class: "sub" }, subtitle(type, profile))),
      h("span", { class: "matched" }, `matched by ${el.getAttribute("matchedBy")}`)),
    h("div", { class: "parts" }, [...el.children].map(partView)));
}

function renderPipeline(doc) {
  const q = [...doc.querySelectorAll("Trace > SourceQuery")];
  const summary = (list) => Object.entries(list.reduce((acc, e) => {
    const s = e.getAttribute("source"); acc[s] = (acc[s] || 0) + 1; return acc;
  }, {})).map(([s, n]) => `${SOURCE_LABEL[s]} ×${n}`).join(", ") || "none";
  const total = q.reduce((s, e) => s + Number(e.getAttribute("ms")), 0).toFixed(1);
  const n = doc.documentElement.getAttribute("entities");
  const steps = [
    ["1 Resolve", summary(q.filter((e) => e.getAttribute("step").startsWith("resolve")))],
    ["2 Fetch", summary(q.filter((e) => e.getAttribute("step").startsWith("fetch")))],
    ["3 Merge (XML)", `${n} ${n === "1" ? "entity" : "entities"} joined on driver_id / team_id / circuit_id`],
    ["4 Validate", `XSD valid · ${q.length} native queries · ${total} ms`],
  ];
  $("#pipeline").replaceChildren(...steps.map(([b, s]) => h("div", { class: "step" }, h("b", {}, b), h("span", {}, s))));
}

function renderTrace(doc) {
  $("#trace").replaceChildren(...[...doc.querySelectorAll("Trace > SourceQuery")].map((e) => {
    const src = e.getAttribute("source");
    return h("div", { class: `trace-item ${src}` },
      h("header", {}, badge(src), h("strong", {}, e.getAttribute("step")),
        h("span", { class: "meta" }, `${e.getAttribute("rows")} rows · ${e.getAttribute("ms")} ms`)),
      h("pre", {}, e.textContent));
  }));
}

async function runSearch(q) {
  currentQuery = q;
  const btn = $("#search-form button");
  btn.setAttribute("aria-busy", "true");
  btn.textContent = "Querying 3 databases…";
  try {
    const doc = await fetchXml(`/api/search?q=${encodeURIComponent(q)}`);
    const entities = [...doc.querySelectorAll("F1Integration > :not(Trace) > *")];
    $("#view").replaceChildren(...(entities.length ? entities.map(renderEntity)
      : [h("p", { class: "empty" }, `No source recognised "${q}". Try a driver, team, circuit, car or engine.`)]));
    $("#xml").innerHTML = highlightXml(serialize(doc));
    renderPipeline(doc);
    renderTrace(doc);
    $("#pipeline").hidden = $("#tabs").hidden = false;
  } catch (err) {
    $("#view").replaceChildren(h("p", { class: "empty" }, `Error: ${err.message}`));
  } finally {
    btn.removeAttribute("aria-busy");
    btn.textContent = "Run integrated query";
  }
}

async function runXpath(expr) {
  if (!currentQuery) { $("#xpath-out").textContent = "Run a search first: XPath is evaluated over its integrated document."; return; }
  try {
    const doc = await fetchXml(`/api/xpath?q=${encodeURIComponent(currentQuery)}&xpath=${encodeURIComponent(expr)}`);
    $("#xpath-out").innerHTML = highlightXml(serialize(doc));
  } catch (err) {
    $("#xpath-out").textContent = err.message;
  }
}

function selectTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".panel").forEach((p) => { p.hidden = p.dataset.panel !== name; });
}

async function loadHealth() {
  try {
    const doc = await fetchXml("/api/health");
    $("#health").replaceChildren(...[...doc.querySelectorAll("source")].map((s) =>
      h("li", {}, h("span", { class: `dot${s.getAttribute("up") === "true" ? " up" : ""}` }),
        `${SOURCE_LABEL[s.getAttribute("id")]}: ${s.getAttribute("up") === "true" ? "up" : "down"}`)));
  } catch { /* header status is informational only */ }
}

// One headline per source + a collapsible per-table breakdown
function statHeadline(id, counts) {
  const n = (v) => v.toLocaleString("en-US");
  const sum = (pred) => Object.entries(counts).filter(([k]) => pred(k)).reduce((s, [, v]) => s + v, 0);
  if (id === "rdbms") return `${n(sum(() => true))} rows in ${Object.keys(counts).length} tables`;
  if (id === "mongodb") return `${n(counts["embedded laps"])} laps in ${n(counts["session_laps documents"])} documents`;
  return `${n(sum((k) => k.endsWith("nodes")))} nodes · ${n(sum((k) => k.endsWith("relationships")))} relationships`;
}

async function loadStats() {
  try {
    const doc = await fetchXml("/api/stats");
    $("#stats").replaceChildren(...[...doc.querySelectorAll("source")].map((s) => {
      const id = s.getAttribute("id");
      const counts = Object.fromEntries([...s.querySelectorAll("count")].map((c) => [c.getAttribute("name"), Number(c.textContent)]));
      return h("div", { class: `stat ${id}` },
        h("div", { class: "stat-head" }, badge(id), h("strong", {}, statHeadline(id, counts))),
        h("details", {}, h("summary", {}, "breakdown"),
          h("dl", {}, Object.entries(counts).flatMap(([k, v]) =>
            [h("dt", {}, k), h("dd", {}, v.toLocaleString("en-US"))]))));
    }));
  } catch { /* informational only */ }
}

function chips(container, list, onPick) {
  container.replaceChildren(...list.map((v) => {
    const b = h("button", { class: "chip", type: "button" }, v);
    b.addEventListener("click", () => onPick(v));
    return b;
  }));
}

$("#search-form").addEventListener("submit", (e) => { e.preventDefault(); runSearch($("#q").value.trim()); });
$("#xpath-form").addEventListener("submit", (e) => { e.preventDefault(); runXpath($("#xp").value.trim()); });
$("#tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) selectTab(e.target.dataset.tab); });
chips($("#examples"), EXAMPLES, (v) => { $("#q").value = v; runSearch(v); });
chips($("#xpath-examples"), XPATH_EXAMPLES, (v) => { $("#xp").value = v; runXpath(v); });
fetchXml("/api/mapping").then((d) => { $("#mapping").innerHTML = highlightXml(serialize(d)); }).catch(() => {});
loadHealth();
loadStats();
