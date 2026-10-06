const state = {
  items: [],
  destinations: [
    "https://dev.azure.com/PG-PSDC/TestProject2_Link1",
    "https://dev.azure.com/PG-PSDC/TestProject3_Link2",
    "https://dev.azure.com/PG-PSDC/TestProject3_Link3"
  ]
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[character]));
const actionClass = action => String(action).toLowerCase().includes("update") ? "update" : "create";

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast${error ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.add("hidden"), 4500);
}

function renderDestinations() {
  $("#destinationList").innerHTML = state.destinations.map((url, index) => `
    <div class="destination-row">
      <span class="destination-number">${index + 1}</span>
      <input type="url" value="${escapeHtml(url)}" aria-label="Destination project ${index + 1}" data-destination="${index}">
      <button class="remove-destination" type="button" data-remove="${index}" aria-label="Remove destination ${index + 1}">×</button>
    </div>`).join("");
  $("#destinationMetric").textContent = state.destinations.length;
}

function renderItems(filter = "") {
  const term = filter.trim().toLowerCase();
  const rows = state.items.filter(item => `${item.id} ${item.type} ${item.title}`.toLowerCase().includes(term));
  $("#itemRows").innerHTML = rows.map(item => `
    <tr>
      <td><input class="table-check" type="checkbox" data-item="${item.id}" ${item.selected ? "checked" : ""} aria-label="Select ${escapeHtml(item.title)}"></td>
      <td><strong>${item.id}</strong></td>
      <td><span class="type-chip ${item.type.toLowerCase().replace(" ", "-")}">${escapeHtml(item.type)}</span></td>
      <td>${escapeHtml(item.title)}</td>
      <td class="state-muted">${escapeHtml(item.state)}</td>
      <td>${item.children}</td>
    </tr>`).join("");
  updateMetrics();
}

function updateMetrics() {
  $("#itemMetric").textContent = state.items.filter(item => item.selected).length;
  try { const parts = new URL($("#source").value).pathname.split("/").filter(Boolean); $("#sourceMetric").textContent = parts.at(-1) || "Source"; } catch { $("#sourceMetric").textContent = "Source"; }
}

function currentConfig() {
  const selectedItems = state.items.filter(item => item.selected);
  return {
    source: $("#source").value.trim(),
    destinations: state.destinations.filter(Boolean),
    selectedIds: selectedItems.map(item => item.id),
    selectedItems: selectedItems.map(item => ({id: item.id, title: item.title, type: item.type, rev: item.rev})),
    fields: $$("#fieldList input:checked").map(input => input.value),
    preserveRelationships: $("#relations").checked,
    liveWrites: $("#liveWrites").checked
  };
}

async function post(url, payload = {}) {
  const response = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || "The request could not be completed.");
  return data;
}

function showPreview(data) {
  const s = data.summary;
  $("#resultTitle").textContent = "Preview complete";
  $("#resultMessage").textContent = data.message;
  $("#resultGrid").innerHTML = [
    ["Work items", s.items, "Selected"], ["Destinations", s.destinations, "Configured"],
    ["Creates", s.creates, "Expected"], ["Updates", s.updates, "Expected"], ["Hierarchy links", s.relationships, "Not enabled"]
  ].map(([label,value,note]) => `<div class="result-stat"><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`).join("");
  $("#changeRows").innerHTML = (data.changes || []).map(change => `
    <tr><td>${change.sourceId}</td><td>${escapeHtml(change.title)}</td><td>${escapeHtml(change.type)}</td><td>${escapeHtml(change.destination)}</td><td>${change.destinationId || "—"}</td><td><span class="action-chip ${actionClass(change.action)}">${escapeHtml(change.action)}</span></td><td>${escapeHtml(change.status || "Planned")}${change.error ? `<br><small class="error-text">${escapeHtml(change.error)}</small>` : ""}</td></tr>`).join("");
  $("#changeDetails").classList.toggle("hidden", !(data.changes || []).length);
  $("#results").classList.remove("hidden");
  $("#results").scrollIntoView({behavior: "smooth", block: "center"});
}

async function init() {
  renderDestinations();
  renderItems();
  const response = await fetch("/api/credential-status");
  const credential = await response.json();
  if (credential.stored) {
    $("#rememberPat").checked = true;
    $("#credentialStatus").textContent = "Saved PAT available in Windows Credential Manager";
    $("#pat").placeholder = "Saved credential available";
  }
}

$("#destinationList").addEventListener("input", event => {
  if (event.target.dataset.destination !== undefined) state.destinations[Number(event.target.dataset.destination)] = event.target.value.trim();
  updateMetrics();
});
$("#destinationList").addEventListener("click", event => {
  if (event.target.dataset.remove !== undefined) {
    if (state.destinations.length === 1) return showToast("At least one destination is required.", true);
    state.destinations.splice(Number(event.target.dataset.remove), 1); renderDestinations();
  }
});
$("#addDestination").addEventListener("click", () => { state.destinations.push(""); renderDestinations(); $("#destinationList input:last-of-type").focus(); });
$("#source").addEventListener("input", updateMetrics);
$("#itemRows").addEventListener("change", event => {
  const item = state.items.find(entry => entry.id === Number(event.target.dataset.item));
  if (item) item.selected = event.target.checked;
  updateMetrics();
});
$("#searchItems").addEventListener("input", event => renderItems(event.target.value));
$("#selectAll").addEventListener("click", () => {
  const allSelected = state.items.every(item => item.selected);
  state.items.forEach(item => item.selected = !allSelected);
  $("#selectAll").textContent = allSelected ? "Select all" : "Clear all";
  renderItems($("#searchItems").value);
});
$("#togglePat").addEventListener("click", () => {
  const input = $("#pat"); input.type = input.type === "password" ? "text" : "password";
  $("#togglePat").textContent = input.type === "password" ? "Show" : "Hide";
});
$("#validateButton").addEventListener("click", async () => {
  const button = $("#validateButton"); button.disabled = true; button.textContent = "Validating…";
  try {
    const data = await post("/api/connect", {pat: $("#pat").value, source: $("#source").value, destinations: state.destinations, rememberPat: $("#rememberPat").checked, useStoredPat: $("#rememberPat").checked});
    $("#pat").value = "";
    $("#source").value = data.projects[0].url;
    state.destinations = data.projects.slice(1).map(project => project.url);
    renderDestinations(); updateMetrics();
    $("#connectionStatus").textContent = `${data.projects.length} projects connected`;
    $("#connectionStatus").classList.add("ready"); showToast(data.message);
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Validate"; }
});
$("#loadItems").addEventListener("click", async () => {
  const button = $("#loadItems"); button.disabled = true; button.textContent = "Loading…";
  try {
    const data = await post("/api/work-items", {source: $("#source").value, marker: $("#faMarker").value});
    state.items = data.items; renderItems();
    $("#scopeStatus").textContent = `${data.items.length} live items`;
    $("#scopeStatus").classList.add("ready"); showToast(data.message);
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Load items"; }
});
$("#disconnectButton").addEventListener("click", async () => {
  const data = await post("/api/disconnect", {}); $("#pat").value = "";
  $("#connectionStatus").textContent = "Not validated"; $("#connectionStatus").classList.remove("ready"); showToast(data.message);
});
$("#deleteStoredPat").addEventListener("click", async () => {
  const data = await post("/api/disconnect", {deleteStoredPat: true});
  $("#rememberPat").checked = false; $("#pat").value = ""; $("#pat").placeholder = "Paste PAT or use saved credential";
  $("#credentialStatus").textContent = "Uses Windows Credential Manager, never app files";
  $("#connectionStatus").textContent = "Not validated"; $("#connectionStatus").classList.remove("ready"); showToast(data.message);
});
$("#previewButton").addEventListener("click", async () => {
  try { showPreview(await post("/api/preview", currentConfig())); } catch (error) { showToast(error.message, true); }
});
$("#liveWrites").addEventListener("change", event => {
  const enabled = event.target.checked;
  $("#runButton").disabled = !enabled;
});
$("#runButton").addEventListener("click", async () => {
  if (!$("#liveWrites").checked) return showToast("Enable live writes before synchronizing.", true);
  if (!window.confirm("Create or update the selected work items in every destination project? Destination states will not be changed.")) return;
  const button = $("#runButton"); button.disabled = true; button.textContent = "Synchronizing…";
  try {
    const payload = currentConfig(); payload.confirmation = "SYNC";
    const data = await post("/api/sync", payload);
    const totals = data.entries.reduce((sum,row) => ({created:sum.created+row.created,updated:sum.updated+row.updated,failed:sum.failed+row.failed}), {created:0,updated:0,failed:0});
    $("#resultTitle").textContent = "Synchronization complete"; $("#resultMessage").textContent = `${data.runId} · ${data.message}`;
    $("#resultGrid").innerHTML = [
      ["Destinations", data.entries.length, "Processed"], ["Created", totals.created, "Azure items"], ["Updated", totals.updated, "Azure items"],
      ["Failed", totals.failed, "Items"], ["Duration", data.duration, "Total"]
    ].map(([label,value,note]) => `<div class="result-stat"><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`).join("");
    $("#changeRows").innerHTML = (data.results || []).map(change => `
      <tr><td>${change.sourceId}</td><td>${escapeHtml(change.title)}</td><td>${escapeHtml(change.type)}</td><td>${escapeHtml(change.destination)}</td><td>${change.destinationId || "—"}</td><td><span class="action-chip ${actionClass(change.action)}">${escapeHtml(change.action)}</span></td><td>${escapeHtml(change.status)}${change.error ? `<br><small class="error-text">${escapeHtml(change.error)}</small>` : ""}</td></tr>`).join("");
    $("#changeDetails").classList.remove("hidden");
    $("#results").classList.remove("hidden"); $("#results").scrollIntoView({behavior:"smooth",block:"center"});
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = !$("#liveWrites").checked; button.innerHTML = "Synchronize now <span>→</span>"; }
});
$("#closeResults").addEventListener("click", () => $("#results").classList.add("hidden"));
$$('.nav-item').forEach(button => button.addEventListener("click", () => {
  $$('.nav-item').forEach(item => item.classList.remove("active")); button.classList.add("active");
  document.getElementById(button.dataset.target).scrollIntoView({behavior:"smooth"});
}));
$("#helpButton").addEventListener("click", () => $("#helpDialog").showModal());
$(".dialog-close").addEventListener("click", () => $("#helpDialog").close());

init().catch(() => showToast("The app could not finish loading.", true));
