/**
 * Ressources partagées — front-end statique.
 * Charge data.json, affiche les cartes, gère filtre et recherche.
 */

const DATA_URL = "data.json";

const dom = {
  grid: document.getElementById("grid"),
  empty: document.getElementById("empty"),
  subtitle: document.getElementById("subtitle"),
  filters: document.getElementById("filters"),
  search: document.getElementById("search"),
};

const state = {
  resources: [],
  activeCategory: "all",
};

/* ---------- Utilitaires ---------- */

function escapeHtml(value) {
  const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(value).replace(/[&<>"']/g, (c) => map[c]);
}

function formatRelativeDate(iso) {
  const d = new Date(iso);
  const diffDays = Math.floor((Date.now() - d.getTime()) / 86_400_000);
  if (diffDays === 0) return "aujourd'hui";
  if (diffDays === 1) return "hier";
  if (diffDays < 7) return `il y a ${diffDays} jours`;
  if (diffDays < 30) return `il y a ${Math.floor(diffDays / 7)} sem.`;
  return d.toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
}

function faviconUrl(domain) {
  return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=64`;
}

function pluralize(count, word) {
  return `${count} ${word}${count > 1 ? "s" : ""}`;
}

/* ---------- Rendu ---------- */

function renderCards(resources) {
  dom.grid.innerHTML = "";
  dom.empty.hidden = resources.length > 0;
  for (const r of resources) {
    dom.grid.appendChild(buildCard(r));
  }
}

function buildCard(r) {
  const card = document.createElement("a");
  card.className = "card";
  card.href = r.url;
  card.target = "_blank";
  card.rel = "noopener noreferrer";

  const noteHtml = r.note ? `<div class="note">${escapeHtml(r.note)}</div>` : "";
  const descHtml = r.description
    ? `<div class="description">${escapeHtml(r.description)}</div>`
    : "";
  const badgeHtml = r.category
    ? `<span class="badge">${escapeHtml(r.category)}</span>`
    : "";

  card.innerHTML = `
    <div class="card-head">
      <div class="card-domain">
        <img class="favicon" src="${faviconUrl(r.domain)}" alt="" loading="lazy"
             onerror="this.style.visibility='hidden'">
        <span class="domain">${escapeHtml(r.domain)}</span>
      </div>
      ${badgeHtml}
    </div>
    ${noteHtml}
    ${descHtml}
    <div class="meta">${formatRelativeDate(r.date)}</div>
  `;
  return card;
}

function renderFilters() {
  const categories = [...new Set(state.resources.map((r) => r.category).filter(Boolean))].sort();
  dom.filters.innerHTML = "";
  dom.filters.appendChild(buildChip("Tout", "all", state.resources.length));
  for (const c of categories) {
    const count = state.resources.filter((r) => r.category === c).length;
    dom.filters.appendChild(buildChip(c, c, count));
  }
}

function buildChip(label, value, count) {
  const btn = document.createElement("button");
  btn.className = "chip" + (state.activeCategory === value ? " active" : "");
  btn.textContent = `${label} · ${count}`;
  btn.addEventListener("click", () => {
    state.activeCategory = value;
    document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
    btn.classList.add("active");
    applyFilter();
  });
  return btn;
}

/* ---------- Filtre & recherche ---------- */

function applyFilter() {
  const q = dom.search.value.trim().toLowerCase();
  let list = state.resources;

  if (state.activeCategory !== "all") {
    list = list.filter((r) => r.category === state.activeCategory);
  }
  if (q) {
    list = list.filter((r) =>
      [r.note, r.description, r.category, r.domain, r.url]
        .filter(Boolean)
        .some((field) => field.toLowerCase().includes(q))
    );
  }
  renderCards(list);
}

dom.search.addEventListener("input", applyFilter);

/* ---------- Chargement des données ---------- */

function applyData(data) {
  state.resources = (data.resources ?? []).slice().sort(
    (a, b) => new Date(b.date) - new Date(a.date)
  );
  const updated = data.lastUpdated ? ` · maj ${formatRelativeDate(data.lastUpdated)}` : "";
  dom.subtitle.textContent = pluralize(state.resources.length, "ressource") + updated;
  renderFilters();
  renderCards(state.resources);
}

async function loadData() {
  try {
    const response = await fetch(DATA_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    applyData(await response.json());
  } catch (error) {
    console.error("Échec du chargement de data.json :", error);
    dom.subtitle.textContent = "Erreur de chargement";
  }
}

loadData();
