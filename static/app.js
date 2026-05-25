/* ── Element refs ──────────────────────────────────────────── */
const form              = document.getElementById("captureForm");
const captureBtn        = document.getElementById("captureBtn");
const captureBtnLabel   = captureBtn.querySelector(".capture-label");
const downloadBtn       = document.getElementById("downloadBtn");
const openFolderBtn     = document.getElementById("openFolderBtn");
const previewImage      = document.getElementById("previewImage");
const previewPlaceholder = document.getElementById("previewPlaceholder");
const previewFname      = document.getElementById("previewFilename");
const historyList       = document.getElementById("historyList");
const filmstripHint     = document.getElementById("filmstripHint");
const filmstripSection  = document.getElementById("filmstripSection");
const openAfterDownload = document.getElementById("openAfterDownload");
const statusEl          = document.getElementById("status");

let latestBlob      = null;
let latestObjectUrl = null;
let latestFilename  = "screenshot.png";
const captureHistory = [];

/* ── Segmented quality control ──────────────────────────────── */
const sharpnessControl = document.getElementById("sharpnessControl");
const deviceScaleInput = document.getElementById("deviceScale");

sharpnessControl.querySelectorAll(".seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    sharpnessControl.querySelectorAll(".seg-btn")
      .forEach((b) => b.classList.remove("seg-active"));
    btn.classList.add("seg-active");
    deviceScaleInput.value = btn.dataset.value;
  });
});

/* ── Resolution presets ─────────────────────────────────────── */
const widthInput  = document.getElementById("width");
const heightInput = document.getElementById("height");

document.querySelectorAll(".preset-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    widthInput.value  = btn.dataset.w;
    heightInput.value = btn.dataset.h;
    document.querySelectorAll(".preset-btn")
      .forEach((b) => b.classList.remove("preset-active"));
    btn.classList.add("preset-active");
  });
});

/* ── Status helper ─────────────────────────────────────────── */
const setStatus = (message, type = "") => {
  statusEl.textContent = message;
  statusEl.className = "status-text" + (type ? ` is-${type}` : "");
};

/* ── Loading state ─────────────────────────────────────────── */
const setLoading = (loading) => {
  const panel = document.querySelector(".preview-panel");
  panel.classList.toggle("is-loading", loading);
  captureBtn.classList.toggle("is-loading", loading);
  captureBtn.disabled = loading;
  captureBtnLabel.textContent = loading ? "Taking screenshot…" : "Take Screenshot";
  if (loading) downloadBtn.disabled = true;
};

/* ── Filename helpers ──────────────────────────────────────── */
const sanitizeFilePart = (value) =>
  (value || "").toString().trim()
    .replace(/[^a-z0-9._-]+/gi, "_")
    .replace(/^_+|_+$/g, "") || "screenshot";

const formatFilename = (urlText, templateText) => {
  const now  = new Date();
  const date = now.toISOString().slice(0, 10).replace(/-/g, "");
  const time = `${String(now.getHours()).padStart(2,"0")}${String(now.getMinutes()).padStart(2,"0")}${String(now.getSeconds()).padStart(2,"0")}`;
  let host = "site";
  try { host = sanitizeFilePart(new URL(urlText).hostname || "site"); } catch {}
  const raw    = (templateText || "{host}_{date}_{time}.png").trim() || "{host}_{date}_{time}.png";
  const filled = raw.replaceAll("{host}", host).replaceAll("{date}", date).replaceAll("{time}", time);
  return `${sanitizeFilePart(filled.replace(/\.png$/i, ""))}.png`;
};

/* ── Download helper ───────────────────────────────────────── */
const triggerDownload = (blob, objectUrl, filename) => {
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
};

/* ── Server-side save ──────────────────────────────────────── */
const saveScreenshotToAppFolder = async (blob, filename) => {
  const payload = new FormData();
  payload.append("filename", filename);
  payload.append("screenshot", blob, filename);
  const res = await fetch("/save-screenshot", { method: "POST", body: payload });
  if (!res.ok) {
    let msg = `Save failed (${res.status})`;
    try { const b = await res.json(); if (b?.detail) msg = b.detail; } catch {}
    throw new Error(msg);
  }
};

/* ── Open captures folder ──────────────────────────────────── */
const openFileLocation = async () => {
  const res = await fetch("/open-captures-folder", { method: "POST" });
  if (!res.ok) throw new Error("Couldn't open the captures folder");
};

/* ── Render history filmstrip ──────────────────────────────── */
const renderHistory = () => {
  historyList.innerHTML = "";
  filmstripHint.textContent = captureHistory.length
    ? `${captureHistory.length} capture${captureHistory.length === 1 ? "" : "s"}`
    : "No captures yet";

  // Slide in filmstrip after first capture
  if (captureHistory.length > 0) {
    filmstripSection.classList.add("has-captures");
  }

  captureHistory.forEach((entry) => {
    const card = document.createElement("article");
    card.className = "history-item";
    card.setAttribute("role", "listitem");
    card.title = entry.filename;

    const thumb = document.createElement("img");
    thumb.className = "history-thumb";
    thumb.src = entry.objectUrl;
    thumb.alt = entry.filename;
    thumb.loading = "lazy";

    const dlBtn = document.createElement("button");
    dlBtn.className = "history-dl-btn";
    dlBtn.type = "button";
    dlBtn.title = `Re-download ${entry.filename}`;
    dlBtn.textContent = "⬇";
    dlBtn.addEventListener("click", () =>
      triggerDownload(entry.blob, entry.objectUrl, entry.filename)
    );

    card.append(thumb, dlBtn);
    historyList.appendChild(card);
  });
};

/* ── CAPTURE ───────────────────────────────────────────────── */
form.addEventListener("submit", async (e) => {
  e.preventDefault();

  const data             = new FormData(form);
  const url              = String(data.get("url") || "");
  const filenameTemplate = String(data.get("filename_template") || "{host}_{date}_{time}.png");
  const params           = new URLSearchParams();

  params.set("url",                 url);
  params.set("width",               String(data.get("width")               || "1920"));
  params.set("height",              String(data.get("height")              || "1080"));
  params.set("device_scale_factor", String(data.get("device_scale_factor") || "2"));
  params.set("wait",                String(data.get("wait")                || "4"));

  setLoading(true);
  setStatus("Taking screenshot…", "loading");

  try {
    const res = await fetch(`/screenshot?${params}`);

    if (!res.ok) {
      let msg = `Request failed (${res.status})`;
      try { const b = await res.json(); if (b?.detail) msg = b.detail; } catch {}
      throw new Error(msg);
    }

    latestBlob     = await res.blob();
    latestFilename = formatFilename(url, filenameTemplate);

    if (latestObjectUrl) URL.revokeObjectURL(latestObjectUrl);
    latestObjectUrl = URL.createObjectURL(latestBlob);

    previewImage.src                  = latestObjectUrl;
    previewImage.style.display        = "block";
    previewPlaceholder.style.display  = "none";
    previewFname.textContent          = latestFilename;
    previewFname.title                = latestFilename;
    downloadBtn.disabled              = false;

    captureHistory.unshift({
      blob:       latestBlob,
      objectUrl:  latestObjectUrl,
      filename:   latestFilename,
      capturedAt: new Date().toLocaleString(),
    });
    if (captureHistory.length > 20) {
      const removed = captureHistory.pop();
      if (removed?.objectUrl) URL.revokeObjectURL(removed.objectUrl);
    }

    renderHistory();
    setStatus("Screenshot captured.", "success");

  } catch (err) {
    latestBlob = null;
    setStatus(err.message || "Something went wrong — check the URL and try again", "error");
  } finally {
    setLoading(false);
  }
});

/* ── DOWNLOAD ──────────────────────────────────────────────── */
downloadBtn.addEventListener("click", async () => {
  if (!latestBlob || !latestObjectUrl) return;

  triggerDownload(latestBlob, latestObjectUrl, latestFilename);

  try {
    await saveScreenshotToAppFolder(latestBlob, latestFilename);
    if (openAfterDownload.checked) await openFileLocation();
    setStatus("Saved to captures folder.", "success");
  } catch (err) {
    setStatus(err.message || "Downloaded but couldn't save to the captures folder", "error");
  }
});

/* ── OPEN FOLDER ───────────────────────────────────────────── */
openFolderBtn.addEventListener("click", async () => {
  try {
    await openFileLocation();
    setStatus("Opened captures folder.", "success");
  } catch (err) {
    setStatus(err.message || "Couldn't open the folder", "error");
  }
});

/* ── Cleanup on unload ─────────────────────────────────────── */
window.addEventListener("beforeunload", () => {
  if (latestObjectUrl) URL.revokeObjectURL(latestObjectUrl);
  captureHistory.forEach((e) => { if (e.objectUrl) URL.revokeObjectURL(e.objectUrl); });
});
