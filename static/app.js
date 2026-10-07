const state = {
  items: [],
  destinations: [""],
  matchRows: []
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[character]));
const actionClass = action => {
  const value = String(action).toLowerCase();
  if (value.includes("up to date")) return "current";
  if (value.includes("change") || value.includes("update")) return "update";
  return "create";
};

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
      <input type="url" value="${escapeHtml(url)}" placeholder="https://dev.azure.com/organization/project" aria-label="Destination project ${index + 1}" data-destination="${index}">
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
  try { const parts = new URL($("#source").value).pathname.split("/").filter(Boolean); $("#sourceMetric").textContent = parts.at(-1) || "Not configured"; } catch { $("#sourceMetric").textContent = "Not configured"; }
}

function updateTypeSpecificFields() {
  const list = $("#typeFieldList");
  if (!list) return;
  const marker = $("#faMarker").value;
  const selectedType = marker.startsWith("type:") ? marker.slice(5) : "";
  let visible = 0;
  list.querySelectorAll("[data-types]").forEach(label => {
    const show = label.dataset.types === selectedType;
    label.classList.toggle("hidden", !show);
    if (show) visible += 1;
  });
  $("#typeFieldHint").textContent = visible
    ? `${selectedType} fields are shown for review and remain disabled in this prototype.`
    : "Choose a work-item type in Selection to see its additional fields.";
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

async function remove(url) {
  const response = await fetch(url, {method: "DELETE"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || "The request could not be completed.");
  return data;
}

function showScheduleStatus(data) {
  const status = $("#scheduleStatus");
  if (!data.enabled) {
    status.textContent = "Not scheduled";
    status.classList.remove("ready");
    return;
  }
  if (data.time) $("#scheduleTime").value = data.time;
  status.textContent = `Daily at ${data.time} · ${data.selectedItems ?? "saved"} items · ${data.destinations ?? "saved"} destinations`;
  status.classList.add("ready");
}

async function loadSourceItems() {
  $("#scopeStatus").textContent = "Loading…";
  const data = await post("/api/work-items", {source: $("#source").value, marker: $("#faMarker").value});
  state.items = data.items;
  renderItems();
  $("#scopeStatus").textContent = `${data.items.length} items`;
  $("#scopeStatus").classList.add("ready");
  return data;
}

function showPreview(data) {
  const s = data.summary;
  $("#resultTitle").textContent = "Preview complete";
  $("#resultMessage").textContent = data.message;
  $("#resultGrid").innerHTML = [
    ["Work items", s.items, "Selected"], ["Destinations", s.destinations, "Configured"],
    ["Creates", s.creates, "Expected"], ["Changes", s.updates, "To synchronize"], ["Up to date", s.upToDate || 0, "No action"]
  ].map(([label,value,note]) => `<div class="result-stat"><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`).join("");
  $("#changeRows").innerHTML = (data.changes || []).map(change => `
    <tr><td>${change.sourceId}</td><td>${escapeHtml(change.title)}</td><td>${escapeHtml(change.type)}</td><td>${escapeHtml(change.destination)}</td><td>${change.destinationId || "—"}</td><td><span class="action-chip ${actionClass(change.action)}">${escapeHtml(change.action)}</span></td><td>${escapeHtml(change.status || "Planned")}${change.error ? `<br><small class="error-text">${escapeHtml(change.error)}</small>` : ""}</td></tr>`).join("");
  $("#changeDetails").classList.toggle("hidden", !(data.changes || []).length);
  $("#results").classList.remove("hidden");
  $("#results").scrollIntoView({behavior: "smooth", block: "center"});
}

function renderMatches(rows) {
  state.matchRows = rows;
  $("#matchRows").innerHTML = rows.map((row, index) => {
    const source = `<strong>${row.sourceId} · ${escapeHtml(row.title)}</strong><small>${escapeHtml(row.type)}</small>`;
    if (row.mappedDestinationId) return `<tr><td>${source}</td><td>${escapeHtml(row.destination)}</td><td><strong>${row.mappedDestinationId}</strong><small>Already linked</small></td><td>Up to date or changes to sync</td><td></td></tr>`;
    if (!row.candidates.length) return `<tr><td>${source}</td><td>${escapeHtml(row.destination)}</td><td class="match-empty">No exact match</td><td>To create</td><td></td></tr>`;
    const options = row.candidates.map(candidate => `<option value="${candidate.id}">${candidate.id} · ${escapeHtml(candidate.title)} · ${escapeHtml(candidate.state)}</option>`).join("");
    return `<tr><td>${source}</td><td>${escapeHtml(row.destination)}</td><td><select data-candidate="${index}" aria-label="Existing match for source ${row.sourceId}">${options}</select></td><td><select data-mode="${index}" aria-label="Link mode for source ${row.sourceId}"><option value="sync">Link and sync</option><option value="aligned">Link only</option></select></td><td><input type="checkbox" data-link="${index}" checked aria-label="Link source ${row.sourceId}"></td></tr>`;
  }).join("");
}

async function init() {
  renderDestinations();
  renderItems();
  updateTypeSpecificFields();
  const response = await fetch("/api/credential-status");
  const credential = await response.json();
  if (credential.stored) {
    $("#rememberPat").checked = true;
    $("#pat").placeholder = "Saved credential available";
  }
  const scheduleResponse = await fetch("/api/schedule");
  showScheduleStatus(await scheduleResponse.json());
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
$("#faMarker").addEventListener("change", async () => {
  updateTypeSpecificFields();
  try { const data = await loadSourceItems(); showToast(data.message); }
  catch (error) { showToast(error.message, true); }
});
$("#selectAll").addEventListener("click", () => {
  const allSelected = state.items.every(item => item.selected);
  state.items.forEach(item => item.selected = !allSelected);
  $("#selectAll").textContent = allSelected ? "Select all" : "Clear all";
  renderItems($("#searchItems").value);
});
$("#matchExisting").addEventListener("click", async () => {
  const button = $("#matchExisting"); button.disabled = true; button.textContent = "Searching…";
  try {
    const data = await post("/api/matches", currentConfig());
    renderMatches(data.rows);
    $("#matchDialog").showModal();
    showToast(data.message);
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Match existing"; }
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
    $("#connectionStatus").classList.add("ready");
    const items = await loadSourceItems();
    showToast(`${data.message} ${items.message}`);
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Validate"; }
});
$("#disconnectButton").addEventListener("click", async () => {
  const data = await post("/api/disconnect", {}); $("#pat").value = "";
  $("#connectionStatus").textContent = "Not validated"; $("#connectionStatus").classList.remove("ready"); showToast(data.message);
});
$("#deleteStoredPat").addEventListener("click", async () => {
  const data = await post("/api/disconnect", {deleteStoredPat: true});
  $("#rememberPat").checked = false; $("#pat").value = ""; $("#pat").placeholder = "Paste PAT or use saved credential";
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
    const totals = data.entries.reduce((sum,row) => ({created:sum.created+row.created,updated:sum.updated+row.updated,skipped:sum.skipped+row.skipped,failed:sum.failed+row.failed,attachmentsAdded:sum.attachmentsAdded+(row.attachmentsAdded||0),attachmentsRemoved:sum.attachmentsRemoved+(row.attachmentsRemoved||0)}), {created:0,updated:0,skipped:0,failed:0,attachmentsAdded:0,attachmentsRemoved:0});
    $("#resultTitle").textContent = "Synchronization complete"; $("#resultMessage").textContent = `${data.runId} · ${data.message}`;
    $("#resultGrid").innerHTML = [
      ["Destinations", data.entries.length, "Processed"], ["Created", totals.created, "Azure items"], ["Updated", totals.updated, "Azure items"],
      ["Up to date", totals.skipped, "Skipped"], ["Attachments", totals.attachmentsAdded, `${totals.attachmentsRemoved} removed`], ["Failed", totals.failed, "Items"]
    ].map(([label,value,note]) => `<div class="result-stat"><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`).join("");
    $("#changeRows").innerHTML = (data.results || []).map(change => `
      <tr><td>${change.sourceId}</td><td>${escapeHtml(change.title)}</td><td>${escapeHtml(change.type)}</td><td>${escapeHtml(change.destination)}</td><td>${change.destinationId || "—"}</td><td><span class="action-chip ${actionClass(change.action)}">${escapeHtml(change.action)}</span></td><td>${escapeHtml(change.status)}${change.error ? `<br><small class="error-text">${escapeHtml(change.error)}</small>` : ""}</td></tr>`).join("");
    $("#changeDetails").classList.remove("hidden");
    $("#results").classList.remove("hidden"); $("#results").scrollIntoView({behavior:"smooth",block:"center"});
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = !$("#liveWrites").checked; button.textContent = "Synchronize"; }
});
$("#exportLog").addEventListener("click", async () => {
  const button = $("#exportLog"); button.disabled = true; button.textContent = "Exporting…";
  try {
    const response = await fetch("/api/export/latest");
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.message || "The synchronization log could not be exported.");
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/i);
    const blobUrl = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = blobUrl;
    link.download = match ? match[1] : "SyncWorkTrack-latest.csv";
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    showToast("The most recent synchronization log was exported.");
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Export log"; }
});
$("#saveSchedule").addEventListener("click", async () => {
  const button = $("#saveSchedule"); button.disabled = true;
  try {
    const payload = currentConfig(); payload.scheduleTime = $("#scheduleTime").value;
    const data = await post("/api/schedule", payload);
    showScheduleStatus({...data, selectedItems: payload.selectedIds.length, destinations: payload.destinations.length});
    showToast(data.message);
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; }
});
$("#removeSchedule").addEventListener("click", async () => {
  try { const data = await remove("/api/schedule"); showScheduleStatus(data); showToast(data.message); }
  catch (error) { showToast(error.message, true); }
});
$("#saveMatches").addEventListener("click", async () => {
  const choices = $$('[data-link]:checked').map(input => {
    const index = Number(input.dataset.link); const row = state.matchRows[index];
    return {
      sourceId: row.sourceId,
      destinationUrl: row.destinationUrl,
      destinationId: Number($(`[data-candidate="${index}"]`).value),
      mode: $(`[data-mode="${index}"]`).value,
    };
  });
  try {
    const data = await post("/api/mappings/link", {source: $("#source").value, choices});
    $("#matchDialog").close(); showToast(data.message);
  } catch (error) { showToast(error.message, true); }
});
$("#closeMatches").addEventListener("click", () => $("#matchDialog").close());
$("#cancelMatches").addEventListener("click", () => $("#matchDialog").close());
$("#closeResults").addEventListener("click", () => $("#results").classList.add("hidden"));
init().catch(() => showToast("The app could not finish loading.", true));
