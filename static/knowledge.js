// Knowledge base admin: list / upload / delete documents and test retrieval.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = async (path, opts = {}) => {
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data;
  };

  async function load() {
    const d = await api("/api/knowledge");
    $("backend").innerHTML = `Search: <span class="badge">keywords (BM25)</span> + ` +
      (d.embedding_model ? `<span class="badge ok">vectors · ${esc(d.embedding_model)} · ${esc(d.vector_backend)}</span>`
                         : `<span class="badge warn">no embedding model (keyword search only)</span>`);
    $("docs").innerHTML = d.documents.map(doc => `<tr>
        <td><b>${esc(doc.source)}</b></td>
        <td>${doc.origin === "upload" ? "uploaded" : "knowledge/ folder"}</td>
        <td>${doc.chunks}</td>
        <td>${doc.embedded ? '<span class="badge ok">keywords + vectors</span>' : '<span class="badge">keywords</span>'}</td>
        <td>${doc.origin === "upload" ? `<button data-del="${esc(doc.source)}">Delete</button>` : ""}</td>
      </tr>`).join("") || `<tr><td colspan="5" class="muted">No documents yet.</td></tr>`;
    document.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
      if (!confirm(`Delete ${b.dataset.del}?`)) return;
      await api(`/api/knowledge/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
      load();
    });
  }

  $("upload").onsubmit = async (e) => {
    e.preventDefault();
    const file = $("file").files[0];
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    $("uploadMsg").textContent = "Indexing…";
    try {
      const r = await api("/api/knowledge", { method: "POST", body });
      $("uploadMsg").textContent = `Added ${r.source} (${r.chunks} chunks).`;
      $("file").value = "";
      load();
    } catch (ex) { $("uploadMsg").textContent = ex.message; }
  };

  $("searchForm").onsubmit = async (e) => {
    e.preventDefault();
    const r = await api(`/api/knowledge/search?q=${encodeURIComponent($("q").value)}&k=3`);
    $("results").innerHTML = r.results.map((h, i) => `<div class="hit">
        <div class="small"><b>${i + 1}. ${esc(h.citation)}</b>
          <span class="badge">keyword ${h.keyword}</span>
          ${h.similarity === null ? "" : `<span class="badge">similarity ${h.similarity}</span>`}</div>
        <div class="small muted">${esc(h.text)}</div></div>`).join("") || `<p class="muted">Nothing found.</p>`;
  };

  fetch("/api/public-config").then(r => r.json()).then(c => ($("bizName").textContent = c.business));
  load().catch(e => ($("docs").innerHTML = `<tr><td colspan="5">${esc(e.message)}</td></tr>`));
})();
