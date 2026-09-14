"""The bundled frontend, embedded as Python strings.

The UI ships as string assets rather than files on disk so the wheel needs no
package-data configuration and cannot silently lose its UI at build time
(ADR-0013). It is deliberately no-build: one page, vanilla JS, no npm toolchain.
"""

from __future__ import annotations

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>clear-record</title>
<style>
  :root { color-scheme: light dark; --line: #8883; --muted: #8888; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; }
  header { padding: 14px 20px; border-bottom: 1px solid var(--line); display: flex;
           align-items: baseline; gap: 10px; }
  header h1 { font-size: 17px; margin: 0; }
  header .tag { color: var(--muted); font-size: 13px; }
  main { display: grid; grid-template-columns: 270px 1fr; min-height: calc(100vh - 51px); }
  aside { border-right: 1px solid var(--line); padding: 14px; }
  section { padding: 18px 22px; overflow: auto; }
  ul.projects { list-style: none; margin: 0 0 14px; padding: 0; }
  ul.projects li { padding: 7px 9px; border-radius: 7px; cursor: pointer;
                   display: flex; justify-content: space-between; gap: 8px; }
  ul.projects li:hover { background: #8881; }
  ul.projects li.active { background: #4c8bf533; }
  ul.projects .count { color: var(--muted); font-variant-numeric: tabular-nums; }
  h2 { font-size: 19px; margin: 0 0 3px; }
  .muted { color: var(--muted); }
  table { border-collapse: collapse; width: 100%; margin-top: 12px; }
  th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { font-weight: 600; font-size: 13px; color: var(--muted); }
  input, select, textarea, button { font: inherit; padding: 6px 8px; border-radius: 7px;
                                    border: 1px solid var(--line); background: transparent; color: inherit; }
  button { cursor: pointer; }
  button.primary { background: #4c8bf5; border-color: #4c8bf5; color: #fff; }
  form.stack { display: grid; gap: 8px; margin-top: 10px; max-width: 520px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; }
  .empty { color: var(--muted); padding: 22px 0; }
  .err { color: #d33; margin-top: 10px; }
  .badge { font-size: 12px; padding: 1px 7px; border-radius: 20px; border: 1px solid var(--line); }
</style>
</head>
<body>
<header>
  <h1>clear-record</h1><span class="tag">project console</span>
  <span class="tag" id="status" style="margin-left:auto"></span>
  <button id="quit" title="Stop the local console">Quit</button>
</header>
<main>
  <aside>
    <strong>Projects</strong>
    <ul class="projects" id="projects"></ul>
    <form id="new-project" class="stack">
      <input name="name" placeholder="New project name" required>
      <button class="primary" type="submit">Add project</button>
    </form>
  </aside>
  <section id="detail"><div class="empty">Select or create a project.</div></section>
</main>
<script>
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, options) {
  const res = await fetch(path, options && { headers: { "content-type": "application/json" }, ...options });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.status === 204 ? null : res.json();
}

let current = null;

async function loadProjects() {
  const projects = await api("/api/projects");
  $("#projects").innerHTML = projects.map((p) =>
    `<li data-slug="${esc(p.slug)}" class="${p.slug === current ? "active" : ""}">
       <span>${esc(p.name)}</span><span class="count">${p.term_count}</span></li>`).join("")
    || '<li class="muted">No projects yet.</li>';
  $("#projects").querySelectorAll("li[data-slug]").forEach((li) =>
    li.addEventListener("click", () => select(li.dataset.slug)));
}

async function select(slug) {
  current = slug;
  await loadProjects();
  const p = await api(`/api/projects/${slug}`);
  const terms = await api(`/api/projects/${slug}/glossary`);
  const rows = terms.map((t) => `
    <tr>
      <td><strong>${esc(t.term)}</strong>${t.reading ? `<div class="muted">${esc(t.reading)}</div>` : ""}</td>
      <td>${esc(t.aliases || "")}</td>
      <td>${esc(t.definition || "")}</td>
      <td><span class="badge">${esc(t.status)}</span>
          <div class="muted" style="font-size:12px">${esc(t.added_by)}</div></td>
      <td>
        <select data-id="${t.id}" class="status">
          ${["candidate", "confirmed", "retired"].map((s) =>
            `<option ${s === t.status ? "selected" : ""}>${s}</option>`).join("")}
        </select>
        <button data-del="${t.id}" title="delete">✕</button>
      </td>
    </tr>`).join("");
  $("#detail").innerHTML = `
    <h2>${esc(p.name)}</h2>
    <div class="muted">${p.notes ? esc(p.notes) : "No notes."} &middot; slug ${esc(p.slug)}</div>
    <h3 style="margin-bottom:0">Glossary</h3>
    ${terms.length ? `<table><thead><tr>
        <th>Term</th><th>Aliases</th><th>Definition</th><th>Status</th><th></th>
      </tr></thead><tbody>${rows}</tbody></table>`
      : '<div class="empty">No glossary terms yet.</div>'}
    <form id="new-term" class="stack">
      <div class="row">
        <input name="term" placeholder="Term" required>
        <input name="reading" placeholder="Reading / pronunciation">
      </div>
      <input name="aliases" placeholder="Aliases (comma separated)">
      <input name="definition" placeholder="Definition">
      <button class="primary" type="submit">Add term</button>
    </form>
    <div class="err" id="err"></div>`;

  $("#new-term").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api(`/api/projects/${slug}/glossary`, { method: "POST", body: JSON.stringify({
        term: f.get("term"), reading: f.get("reading") || null,
        aliases: f.get("aliases") || null, definition: f.get("definition") || null,
      })});
      await select(slug);
    } catch (err) { $("#err").textContent = err.message; }
  });
  $("#detail").querySelectorAll("select.status").forEach((sel) =>
    sel.addEventListener("change", async () => {
      await api(`/api/glossary/${sel.dataset.id}`, { method: "PATCH", body: JSON.stringify({ status: sel.value }) });
      await select(slug);
    }));
  $("#detail").querySelectorAll("button[data-del]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/glossary/${btn.dataset.del}`, { method: "DELETE" });
      await select(slug);
    }));
}

$("#new-project").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = new FormData(e.target).get("name");
  try {
    const p = await api("/api/projects", { method: "POST", body: JSON.stringify({ name }) });
    e.target.reset();
    await select(p.slug);
  } catch (err) { $("#status").textContent = err.message; }
});

$("#quit").addEventListener("click", async () => {
  try { await api("/api/shutdown", { method: "POST" }); } catch (e) { /* already gone */ }
  document.body.innerHTML =
    '<p style="padding:24px;font:15px system-ui">Console stopped. You can close this tab.</p>';
});

loadProjects().then(() => {
  $("#status").textContent = "ready";
}).catch((e) => { $("#status").textContent = e.message; });
</script>
</body>
</html>
"""


__all__ = ["INDEX_HTML"]
