#!/usr/bin/env python3
"""
xml_dashboard.py
=================
Turn an arbitrary XML export into a single self-contained, clickable HTML
dashboard: searchable + sortable table, category filters, a status
breakdown chart, and a detail panel that opens when you click a row.

No hard-coded schema. The script inspects the file, finds the repeating
"record" element (e.g. <shipment>, <employee>, <order> ...), and builds
columns from whatever fields those records actually contain.

Usage
-----
    python xml_dashboard.py input.xml
    python xml_dashboard.py input.xml -o dashboard.html --title "Shipments"
    python xml_dashboard.py input.xml --record shipment   # force record tag

Output
------
A single .html file with everything inlined (data, CSS, JS). Open it in
any browser -- no server, no network access required.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET


# --------------------------------------------------------------------------
# 1. Parsing: find the repeating "record" element and flatten it to rows
# --------------------------------------------------------------------------

def strip_ns(tag: str) -> str:
    """Drop XML namespace braces, e.g. '{http://x}foo' -> 'foo'."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def find_record_tag(root: ET.Element, forced: str | None = None) -> str:
    """
    Pick the element tag that best represents one "row" of data.

    A candidate "record" tag is one that appears more than once under some
    parent (i.e. it's a repeated sibling, not just a one-off field). Among
    candidates, the one with the highest *global* occurrence count wins --
    this correctly picks e.g. <employee> over an outer <department> wrapper
    even when a given department happens to contain only one employee.
    """
    if forced:
        return forced

    is_candidate: set[str] = set()
    for parent in root.iter():
        child_tags = Counter(strip_ns(c.tag) for c in parent)
        for tag, n in child_tags.items():
            if n > 1:
                is_candidate.add(tag)

    if not is_candidate:
        # No repetition anywhere -- fall back to the root's direct children,
        # or the root itself if it has no children.
        children = list(root)
        return strip_ns(children[0].tag) if children else strip_ns(root.tag)

    global_counts = Counter(strip_ns(e.tag) for e in root.iter())
    return max(is_candidate, key=lambda t: global_counts[t])


def element_to_flat_dict(el: ET.Element, prefix: str = "") -> dict:
    """
    Flatten one record element into {field_name: text_value}.
    Attributes become "tag@attr". Nested elements become "parent.child".
    Repeated nested elements become "parent.child (2)", "(3)", ...
    """
    out: dict[str, str] = {}

    for attr_name, attr_val in el.attrib.items():
        key = f"{prefix}@{attr_name}" if prefix else f"@{attr_name}"
        out[key] = attr_val

    children = list(el)
    if not children:
        text = (el.text or "").strip()
        if text or not out:
            out[prefix or strip_ns(el.tag)] = text
        return out

    seen: Counter[str] = Counter()
    for child in children:
        tag = strip_ns(child.tag)
        seen[tag] += 1
        label = tag if seen[tag] == 1 else f"{tag} ({seen[tag]})"
        key = f"{prefix}.{label}" if prefix else label
        out.update(element_to_flat_dict(child, key))

    return out


def load_records(path: Path, forced_tag: str | None) -> tuple[str, list[dict]]:
    tree = ET.parse(path)
    root = tree.getroot()
    record_tag = find_record_tag(root, forced_tag)

    records = [
        element_to_flat_dict(el)
        for el in root.iter()
        if strip_ns(el.tag) == record_tag
    ]

    if not records:
        raise SystemExit(
            f"No <{record_tag}> elements found. Use --record to name the "
            f"repeating element explicitly (e.g. --record item)."
        )

    return record_tag, records


# --------------------------------------------------------------------------
# 2. Light schema inference: field order, "categorical" fields, date fields
# --------------------------------------------------------------------------

DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")


def humanize(field: str) -> str:
    """'dispEstDelDt' -> 'Disp Est Del Dt', 'shpr.nm' -> 'Shpr Nm'."""
    field = field.replace(".", " ").replace("@", " ")
    field = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", field)
    field = re.sub(r"[_\-]+", " ", field)
    return " ".join(w[:1].upper() + w[1:] for w in field.split())


def infer_schema(records: list[dict]) -> dict:
    fields: list[str] = []
    for r in records:
        for k in r.keys():
            if k not in fields:
                fields.append(k)

    n = len(records)
    categorical, date_fields = [], []
    for f in fields:
        values = [r.get(f, "") for r in records]
        non_empty = [v for v in values if v]
        uniq = set(non_empty)
        if not non_empty:
            continue
        if len(uniq) <= max(8, n // 4) and len(uniq) < n:
            categorical.append(f)
        if sum(1 for v in non_empty if DATE_RE.match(v)) >= len(non_empty) * 0.6:
            date_fields.append(f)

    # A short id-ish field (e.g. a tracking / order number) to headline rows.
    id_field = None
    for f in fields:
        low = f.lower()
        if any(tok in low for tok in ("id", "nbr", "num", "number", "code")):
            id_field = f
            break
    if id_field is None and fields:
        id_field = fields[0]

    # The single best categorical field to summarize as the headline chart.
    status_field = None
    for f in categorical:
        if "status" in f.lower():
            status_field = f
            break
    if status_field is None and categorical:
        status_field = max(categorical, key=lambda f: len({r.get(f) for r in records if r.get(f)}))

    return {
        "fields": fields,
        "categorical": categorical,
        "date_fields": date_fields,
        "id_field": id_field,
        "status_field": status_field,
    }


# --------------------------------------------------------------------------
# 3. HTML generation
# --------------------------------------------------------------------------

PALETTE = [
    "#1F6F5C", "#B23A2E", "#C48A2E", "#2A3F73",
    "#6E4B8A", "#3E7CA6", "#8A6D3B", "#4B5D3A",
]

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
__CSS__
</style>
</head>
<body>
<div class="wrap">

  <header class="masthead">
    <div class="masthead-top">
      <span class="masthead-meta">__RECORD_TAG__ &middot; __COUNT__ records &middot; generated __GENERATED__</span>
    </div>
    <h1>__TITLE__</h1>
  </header>

  <section class="stats" id="stats"></section>

  <section class="board">
    <div class="board-controls">
      <input id="search" type="text" placeholder="Search all fields&hellip;" autocomplete="off">
      <div id="filters" class="filters"></div>
      <button id="clearFilters" class="ghost-btn" type="button">Clear filters</button>
    </div>

    <div class="table-shell">
      <table id="table">
        <thead><tr id="headRow"></tr></thead>
        <tbody id="body"></tbody>
      </table>
    </div>
    <p class="hint">Click any row for full detail &middot; click a column header to sort</p>
    <p id="emptyState" class="empty" hidden>No records match your filters.</p>
  </section>

</div>

<div id="overlay" class="overlay" hidden>
  <div class="panel">
    <button id="panelClose" class="panel-close" type="button" aria-label="Close">&times;</button>
    <h2 id="panelTitle"></h2>
    <dl id="panelBody"></dl>
  </div>
</div>

<script>
const RECORDS = __DATA_JSON__;
const SCHEMA  = __SCHEMA_JSON__;
const PALETTE = __PALETTE_JSON__;
__JS__
</script>
</body>
</html>
"""

CSS_TEMPLATE = """
:root {
  --paper: #F6F4EF;
  --paper-raised: #FCFBF8;
  --ink: #1E1B16;
  --ink-soft: #5B5647;
  --line: #D9D2C1;
  --line-strong: #B7AD94;
  --accent: #B23A2E;
  --accent-soft: #F1DCD3;
  --teal: #1F6F5C;
  --teal-soft: #DCE9E1;
  --mono: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  --sans: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #17140F;
    --paper-raised: #201C15;
    --ink: #EFE9DC;
    --ink-soft: #A79E88;
    --line: #3A3527;
    --line-strong: #524A36;
    --accent: #E0776A;
    --accent-soft: #3A241F;
    --teal: #5FB39C;
    --teal-soft: #1C2E28;
  }
}
:root[data-theme="dark"] {
  --paper: #17140F;
  --paper-raised: #201C15;
  --ink: #EFE9DC;
  --ink-soft: #A79E88;
  --line: #3A3527;
  --line-strong: #524A36;
  --accent: #E0776A;
  --accent-soft: #3A241F;
  --teal: #5FB39C;
  --teal-soft: #1C2E28;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--sans);
  line-height: 1.45;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 40px 24px 80px; }

.masthead { border-bottom: 3px double var(--line-strong); padding-bottom: 18px; margin-bottom: 28px; }
.masthead-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.stamp {
  font-family: var(--mono);
  font-weight: 600;
  font-size: 12px;
  letter-spacing: 0.16em;
  color: var(--accent);
  border: 1.5px solid var(--accent);
  border-radius: 3px;
  padding: 3px 9px;
  transform: rotate(-2deg);
  display: inline-block;
}
.masthead-meta { font-family: var(--mono); font-size: 12.5px; color: var(--ink-soft); }
.masthead h1 { font-size: 34px; font-weight: 700; margin: 0; letter-spacing: -0.01em; }

.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin-bottom: 30px; }
.stat-card {
  background: var(--paper-raised);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 16px 18px;
}
.stat-card .num { font-family: var(--mono); font-size: 28px; font-weight: 600; line-height: 1; }
.stat-card .label { font-size: 12.5px; color: var(--ink-soft); margin-top: 6px; }
.stat-card.chart .bars { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
.stat-card.chart .bar-row { display: grid; grid-template-columns: 84px 1fr 34px; align-items: center; gap: 8px; font-size: 12px; }
.stat-card.chart .bar-label { color: var(--ink-soft); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stat-card.chart .bar-track { background: var(--line); border-radius: 3px; height: 8px; overflow: hidden; }
.stat-card.chart .bar-fill { height: 100%; border-radius: 3px; }
.stat-card.chart .bar-count { font-family: var(--mono); text-align: right; color: var(--ink-soft); }

.board-controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 14px; }
#search {
  flex: 1 1 240px;
  font-family: var(--sans);
  font-size: 14px;
  padding: 9px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 5px;
  background: var(--paper-raised);
  color: var(--ink);
}
#search:focus { outline: 2px solid var(--teal); outline-offset: 1px; }
.filters { display: flex; flex-wrap: wrap; gap: 8px; }
.filters select {
  font-family: var(--sans);
  font-size: 13px;
  padding: 8px 10px;
  border: 1px solid var(--line-strong);
  border-radius: 5px;
  background: var(--paper-raised);
  color: var(--ink);
}
.ghost-btn {
  font-family: var(--sans);
  font-size: 13px;
  padding: 8px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 5px;
  background: transparent;
  color: var(--ink-soft);
  cursor: pointer;
}
.ghost-btn:hover { border-color: var(--accent); color: var(--accent); }

.table-shell { border: 1px solid var(--line); border-radius: 6px; overflow: auto; background: var(--paper-raised); }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
thead th {
  position: sticky; top: 0;
  background: var(--paper-raised);
  text-align: left;
  padding: 10px 14px;
  font-weight: 600;
  border-bottom: 1.5px solid var(--line-strong);
  cursor: pointer;
  white-space: nowrap;
  user-select: none;
}
thead th::after { content: ''; opacity: 0.4; margin-left: 4px; font-size: 11px; }
thead th[data-dir="asc"]::after { content: '\\2191'; opacity: 1; color: var(--teal); }
thead th[data-dir="desc"]::after { content: '\\2193'; opacity: 1; color: var(--teal); }
tbody tr { border-bottom: 1px solid var(--line); cursor: pointer; }
tbody tr:hover { background: var(--teal-soft); }
tbody td { padding: 9px 14px; white-space: nowrap; }
tbody td.id-cell { font-family: var(--mono); }
tbody td.id-cell a {
  color: var(--ink);
  text-decoration: none;
  border-bottom: 1px dotted var(--line-strong);
}
tbody td.id-cell a:hover { color: var(--teal); border-bottom-color: var(--teal); }
.tag {
  display: inline-block;
  font-size: 12px;
  padding: 2px 9px;
  border-radius: 20px;
  border: 1px solid transparent;
}
.hint { font-size: 12px; color: var(--ink-soft); margin: 10px 2px 0; }
.empty { text-align: center; color: var(--ink-soft); padding: 30px; }

.overlay {
  position: fixed; inset: 0; background: rgba(20,16,10,0.45);
  display: flex; align-items: flex-start; justify-content: center;
  padding: 6vh 20px; overflow-y: auto; z-index: 10;
}
.overlay[hidden] { display: none; }
.panel {
  background: var(--paper-raised);
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  max-width: 560px; width: 100%;
  padding: 28px 30px 24px;
  position: relative;
  box-shadow: 0 18px 50px rgba(0,0,0,0.25);
}
.panel-close {
  position: absolute; top: 14px; right: 16px;
  background: none; border: none; font-size: 22px; line-height: 1;
  color: var(--ink-soft); cursor: pointer;
}
.panel-close:hover { color: var(--accent); }
.panel h2 { font-family: var(--mono); font-size: 19px; margin: 0 0 18px; padding-right: 20px; }
.panel dl { margin: 0; display: grid; grid-template-columns: minmax(120px, 40%) 1fr; row-gap: 10px; column-gap: 14px; }
.panel dt { font-size: 12px; color: var(--ink-soft); text-transform: none; align-self: start; padding-top: 2px; }
.panel dd { margin: 0; font-size: 14px; word-break: break-word; }

@media (max-width: 640px) {
  .masthead h1 { font-size: 26px; }
  .panel dl { grid-template-columns: 1fr; row-gap: 4px; }
  .panel dt { padding-top: 8px; }
}
"""

JS_TEMPLATE = """
const fields = SCHEMA.fields;
const categorical = SCHEMA.categorical;
const idField = SCHEMA.id_field;
const statusField = SCHEMA.status_field;
// Show a sensible subset of columns in the table; everything is visible in the detail panel.
const preferredCols = [idField, statusField, ...fields].filter((f, i, a) => f && a.indexOf(f) === i).slice(0, 6);

let sortField = null, sortDir = 1;
const activeFilters = {};
const statusColorMap = {};   // status value -> hex color, shared between chart + table tags

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// Turn '#1F6F5C' + 'cc' into '#1F6F5Ccc' for an alpha-tinted background/border.
function hexAlpha(hex, alphaHex) { return hex + alphaHex; }

function buildStats() {
  const el = document.getElementById('stats');
  const cards = [];
  cards.push(`<div class="stat-card"><div class="num">${RECORDS.length}</div><div class="label">Total records</div></div>`);

  const extraCategorical = categorical.filter(f => f !== statusField).slice(0, 1)[0];
  [statusField, extraCategorical].filter(Boolean).forEach((field, idx) => {
    const counts = {};
    RECORDS.forEach(r => { const v = r[field] || '—'; counts[v] = (counts[v] || 0) + 1; });
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 6);
    const max = entries[0] ? entries[0][1] : 1;
    const bars = entries.map(([label, count], i) => {
      const color = PALETTE[i % PALETTE.length];
      if (field === statusField) statusColorMap[label] = color;
      return `
      <div class="bar-row">
        <div class="bar-label" title="${esc(label)}">${esc(label)}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${(count/max*100).toFixed(0)}%; background:${color}"></div></div>
        <div class="bar-count">${count}</div>
      </div>`;
    }).join('');
    cards.push(`<div class="stat-card chart"><div class="label">${esc(humanize(field))} breakdown</div><div class="bars">${bars}</div></div>`);
  });

  el.innerHTML = cards.join('');
}

function humanize(field) {
  return field.replace(/[._@]/g, ' ').replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .split(' ').filter(Boolean).map(w => w[0].toUpperCase() + w.slice(1)).join(' ');
}

function buildFilters() {
  const el = document.getElementById('filters');
  el.innerHTML = categorical.slice(0, 4).map(field => {
    const values = [...new Set(RECORDS.map(r => r[field]).filter(Boolean))].sort();
    const opts = values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
    return `<select data-field="${esc(field)}"><option value="">${esc(humanize(field))}: All</option>${opts}</select>`;
  }).join('');
  el.querySelectorAll('select').forEach(sel => {
    sel.addEventListener('change', () => {
      if (sel.value) activeFilters[sel.dataset.field] = sel.value;
      else delete activeFilters[sel.dataset.field];
      render();
    });
  });
}

function buildHead() {
  const row = document.getElementById('headRow');
  row.innerHTML = preferredCols.map(f => `<th data-field="${esc(f)}">${esc(humanize(f))}</th>`).join('');
  row.querySelectorAll('th').forEach(th => {
    th.addEventListener('click', () => {
      const f = th.dataset.field;
      if (sortField === f) sortDir *= -1; else { sortField = f; sortDir = 1; }
      row.querySelectorAll('th').forEach(t => t.removeAttribute('data-dir'));
      th.setAttribute('data-dir', sortDir === 1 ? 'asc' : 'desc');
      render();
    });
  });
}

function matches(r) {
  for (const [field, val] of Object.entries(activeFilters)) {
    if ((r[field] || '') !== val) return false;
  }
  const q = document.getElementById('search').value.trim().toLowerCase();
  if (!q) return true;
  return fields.some(f => String(r[f] || '').toLowerCase().includes(q));
}

function render() {
  let rows = RECORDS.filter(matches);
  if (sortField) {
    rows = rows.slice().sort((a, b) => {
      const av = a[sortField] || '', bv = b[sortField] || '';
      const an = parseFloat(av), bn = parseFloat(bv);
      let cmp;
      if (!isNaN(an) && !isNaN(bn) && String(an) === av.trim() && String(bn) === bv.trim()) cmp = an - bn;
      else cmp = av.localeCompare(bv);
      return cmp * sortDir;
    });
  }

  const body = document.getElementById('body');
  document.getElementById('emptyState').hidden = rows.length > 0;
  body.innerHTML = rows.map((r, i) => {
    const idx = RECORDS.indexOf(r);
    return '<tr data-idx="' + idx + '">' + preferredCols.map(f => {
      const v = r[f] || '—';
      if (f === statusField) {
        const color = statusColorMap[v] || '#6B6656';
        const style = `background:${hexAlpha(color, '1F')}; color:${color}; border-color:${hexAlpha(color, '55')}`;
        return `<td><span class="tag" style="${style}">${esc(v)}</span></td>`;
      }
      if (f === idField) {
        if (!r[f]) return `<td class="id-cell">—</td>`;
        const url = 'https://www.google.com/search?q=' + encodeURIComponent(v);
        return `<td class="id-cell"><a href="${esc(url)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">${esc(v)}</a></td>`;
      }
      return `<td>${esc(v)}</td>`;
    }).join('') + '</tr>';
  }).join('');

  body.querySelectorAll('tr').forEach(tr => {
    tr.addEventListener('click', () => openPanel(RECORDS[+tr.dataset.idx]));
  });
}

function openPanel(record) {
  document.getElementById('panelTitle').textContent = record[idField] || 'Record detail';
  document.getElementById('panelBody').innerHTML = fields.map(f => `
    <dt>${esc(humanize(f))}</dt><dd>${esc(record[f] || '—')}</dd>
  `).join('');
  document.getElementById('overlay').hidden = false;
}
document.getElementById('panelClose').addEventListener('click', () => document.getElementById('overlay').hidden = true);
document.getElementById('overlay').addEventListener('click', e => { if (e.target.id === 'overlay') e.target.hidden = true; });
document.addEventListener('keydown', e => { if (e.key === 'Escape') document.getElementById('overlay').hidden = true; });

document.getElementById('search').addEventListener('input', render);
document.getElementById('clearFilters').addEventListener('click', () => {
  Object.keys(activeFilters).forEach(k => delete activeFilters[k]);
  document.querySelectorAll('.filters select').forEach(s => s.value = '');
  document.getElementById('search').value = '';
  render();
});

buildStats();
buildFilters();
buildHead();
render();
"""


def build_html(record_tag: str, records: list[dict], schema: dict, title: str) -> str:
    from datetime import datetime

    page = PAGE_TEMPLATE
    page = page.replace("__TITLE__", html.escape(title))
    page = page.replace("__RECORD_TAG__", html.escape(record_tag))
    page = page.replace("__COUNT__", str(len(records)))
    page = page.replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
    page = page.replace("__CSS__", CSS_TEMPLATE)
    page = page.replace("__JS__", JS_TEMPLATE)
    page = page.replace("__DATA_JSON__", json.dumps(records))
    page = page.replace("__SCHEMA_JSON__", json.dumps(schema))
    page = page.replace("__PALETTE_JSON__", json.dumps(PALETTE))
    return page


# --------------------------------------------------------------------------
# 4. CLI
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Turn an XML export into a clickable HTML dashboard.")
    ap.add_argument("xml_file", type=Path, help="Path to the input XML file")
    ap.add_argument("-o", "--output", type=Path, default=None, help="Output HTML path (default: <input>_dashboard.html)")
    ap.add_argument("--title", default=None, help="Dashboard title (default: derived from the file name)")
    ap.add_argument("--record", default=None, help="Force the repeating record tag name (skip auto-detection)")
    args = ap.parse_args()

    if not args.xml_file.exists():
        sys.exit(f"File not found: {args.xml_file}")

    record_tag, records = load_records(args.xml_file, args.record)
    schema = infer_schema(records)

    title = args.title or humanize(args.xml_file.stem)
    out_path = args.output or args.xml_file.with_name(f"{args.xml_file.stem}_dashboard.html")

    out_path.write_text(build_html(record_tag, records, schema, title), encoding="utf-8")

    print(f"Parsed {len(records)} <{record_tag}> record(s) with {len(schema['fields'])} field(s).")
    print(f"Dashboard written to: {out_path}")


if __name__ == "__main__":
    main()
