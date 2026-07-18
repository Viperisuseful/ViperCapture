/* ── Theme ─────────────────────────────────────────────────── */
const themeToggle = document.getElementById("themeToggle");
const themeColor = document.getElementById("themeColor");

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function applyTheme(theme, persist = true) {
  document.documentElement.dataset.theme = theme;
  themeColor?.setAttribute("content", theme === "dark" ? "#111318" : "#ffffff");
  themeToggle?.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} theme`);
  if (persist) {
    try { localStorage.setItem("theme", theme); } catch (error) {}
  }
}

applyTheme(currentTheme(), false);

themeToggle?.addEventListener("click", () => {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
});

window.addEventListener("storage", (event) => {
  if (event.key === "theme" && ["light", "dark"].includes(event.newValue)) {
    applyTheme(event.newValue, false);
  }
});

/* ── Element refs ──────────────────────────────────────────── */
const form              = document.getElementById("captureForm");
const captureBtn        = document.getElementById("captureBtn");
const captureBtnLabel   = captureBtn.querySelector(".capture-label");
const downloadBtn       = document.getElementById("downloadBtn");
const openFolderBtn     = document.getElementById("openFolderBtn");
const previewPanel      = document.getElementById("previewPanel");
const previewImage      = document.getElementById("previewImage");
const previewPlaceholder = document.getElementById("previewPlaceholder");
const previewFname      = document.getElementById("previewFilename");
const historyList       = document.getElementById("historyList");
const filmstripHint     = document.getElementById("filmstripHint");
const filmstripSection  = document.getElementById("filmstripSection");
const openAfterDownload = document.getElementById("openAfterDownload");
const statusEl          = document.getElementById("status");
const densityHint       = document.getElementById("densityHint");
const outputFormat      = document.getElementById("outputFormat");
const previewFormat     = document.getElementById("previewFormat");
const downloadLabel     = document.getElementById("downloadLabel");
const filenameTemplate  = document.getElementById("filenameTemplate");
const captureMode       = document.getElementById("captureMode");
const selectorField     = document.getElementById("selectorField");
const selectorInput     = document.getElementById("selector");
const imageQuality      = document.getElementById("imageQuality");
const transparentBackground = document.getElementById("transparentBackground");

let maxScreenshotPixels = 50_000_000;

const appConfig = fetch("/app-config")
  .then((res) => {
    if (!res.ok) throw new Error("Config unavailable");
    return res.json();
  })
  .catch(() => ({ server_saves: false }));

appConfig.then(({ server_saves: serverSaves, max_screenshot_pixels: maxPixels }) => {
  const parsedLimit = Number(maxPixels);
  if (Number.isFinite(parsedLimit) && parsedLimit > 0) maxScreenshotPixels = parsedLimit;
  document.documentElement.dataset.mode = serverSaves ? "local" : "hosted";
  openFolderBtn.hidden = !serverSaves;
  openAfterDownload.closest(".toggle-standalone").hidden = !serverSaves;
  syncScaleAvailability();
});

let latestBlob      = null;
let latestObjectUrl = null;
let latestFilename  = "screenshot.png";
const captureHistory = [];

/* ── Segmented quality control ──────────────────────────────── */
const sharpnessControl = document.getElementById("sharpnessControl");
const deviceScaleInput = document.getElementById("deviceScale");
const scaleButtons = [...sharpnessControl.querySelectorAll(".seg-btn")];

const selectScale = (selectedButton) => {
  scaleButtons.forEach((button) => {
    const active = button === selectedButton;
    button.classList.toggle("seg-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  deviceScaleInput.value = selectedButton.dataset.value;
};

scaleButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    selectScale(btn);
  });
});

/* ── Resolution presets ─────────────────────────────────────── */
const widthInput  = document.getElementById("width");
const heightInput = document.getElementById("height");
const presetButtons = [...document.querySelectorAll(".preset-btn")];

const syncActivePreset = () => {
  presetButtons.forEach((btn) => {
    const active = btn.dataset.w === widthInput.value && btn.dataset.h === heightInput.value;
    btn.classList.toggle("preset-active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
};

const syncScaleAvailability = () => {
  const basePixels = Number(widthInput.value) * Number(heightInput.value);
  let hasUnavailableOption = false;

  scaleButtons.forEach((button) => {
    const scale = Number(button.dataset.value);
    const available = Number.isFinite(basePixels)
      && basePixels > 0
      && basePixels * scale * scale <= maxScreenshotPixels;
    button.disabled = !available;
    button.title = available
      ? ""
      : `Exceeds this server's ${Math.floor(maxScreenshotPixels / 1_000_000)} MP output limit`;
    hasUnavailableOption ||= !available;
  });

  const activeButton = scaleButtons.find((button) => button.classList.contains("seg-active"));
  if (activeButton?.disabled) {
    const fallback = [...scaleButtons].reverse().find((button) => !button.disabled);
    if (fallback) selectScale(fallback);
  }

  densityHint.textContent = hasUnavailableOption
    ? "Higher density is limited at this size"
    : "2× works for most captures";
};

presetButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    widthInput.value  = btn.dataset.w;
    heightInput.value = btn.dataset.h;
    syncActivePreset();
    syncScaleAvailability();
  });
});

widthInput.addEventListener("input", () => {
  syncActivePreset();
  syncScaleAvailability();
});
heightInput.addEventListener("input", () => {
  syncActivePreset();
  syncScaleAvailability();
});

syncScaleAvailability();

/* ── Status helper ─────────────────────────────────────────── */
const setStatus = (message, type = "") => {
  statusEl.textContent = message;
  statusEl.className = "status-text" + (type ? ` is-${type}` : "");
};

/* ── Loading state ─────────────────────────────────────────── */
const setLoading = (loading) => {
  previewPanel.classList.toggle("is-loading", loading);
  captureBtn.classList.toggle("is-loading", loading);
  captureBtn.setAttribute("aria-busy", String(loading));
  captureBtn.disabled = loading;
  captureBtnLabel.textContent = loading ? "Loading page" : "Capture page";
  if (loading) downloadBtn.disabled = true;
};

/* ── Filename helpers ──────────────────────────────────────── */
const sanitizeFilePart = (value) =>
  (value || "").toString().trim()
    .replace(/[^a-z0-9._-]+/gi, "_")
    .replace(/^_+|_+$/g, "") || "screenshot";

const formatFilename = (urlText, templateText, imageFormat) => {
  const now  = new Date();
  const date = now.toISOString().slice(0, 10).replace(/-/g, "");
  const time = `${String(now.getHours()).padStart(2,"0")}${String(now.getMinutes()).padStart(2,"0")}${String(now.getSeconds()).padStart(2,"0")}`;
  let host = "site";
  try { host = sanitizeFilePart(new URL(urlText).hostname || "site"); } catch {}
  const extension = imageFormat === "jpeg" ? "jpg" : imageFormat;
  const raw    = (templateText || `{host}_{date}_{time}.${extension}`).trim() || `{host}_{date}_{time}.${extension}`;
  const filled = raw.replaceAll("{host}", host).replaceAll("{date}", date).replaceAll("{time}", time);
  return `${sanitizeFilePart(filled.replace(/\.(png|jpe?g|webp)$/i, ""))}.${extension}`;
};

const syncOutputFormat = () => {
  const selected = outputFormat.value;
  const label = selected === "jpeg" ? "JPEG" : selected.toUpperCase();
  const extension = selected === "jpeg" ? "jpg" : selected;
  const area = captureMode.value === "full_page" ? "full page" : captureMode.value;
  previewFormat.textContent = `${label} · ${area}`;
  downloadLabel.textContent = `Download ${label}`;
  filenameTemplate.value = filenameTemplate.value.replace(/\.(png|jpe?g|webp)$/i, `.${extension}`);
  imageQuality.disabled = selected === "png";
  transparentBackground.disabled = selected === "jpeg";
  if (selected === "jpeg") transparentBackground.checked = false;
};

outputFormat.addEventListener("change", syncOutputFormat);
captureMode.addEventListener("change", () => {
  selectorField.hidden = captureMode.value !== "selector";
  selectorInput.required = captureMode.value === "selector";
  syncOutputFormat();
});
syncOutputFormat();

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
  const res = await fetch("/open-downloads-folder", { method: "POST" });
  if (!res.ok) throw new Error("Couldn't open the Downloads folder");
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
    dlBtn.textContent = "Save";
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
  const filenamePattern  = String(data.get("filename_template") || "{host}_{date}_{time}.png");
  const imageFormat      = String(data.get("format") || "png");
  let headers = {};
  const rawHeaders = String(data.get("headers") || "").trim();
  if (rawHeaders) {
    try {
      headers = JSON.parse(rawHeaders);
      if (!headers || Array.isArray(headers) || typeof headers !== "object") throw new Error();
    } catch {
      setStatus("Headers must be a JSON object of string names and values.", "error");
      return;
    }
  }
  const mode = String(data.get("capture_mode") || "full_page");
  const waitSelector = String(data.get("wait_selector") || "").trim();
  const waitText = String(data.get("wait_text") || "").trim();
  const payload = {
    url,
    output: imageFormat,
    viewport: {
      width: Number(data.get("width") || 1920),
      height: Number(data.get("height") || 1080),
      device_scale_factor: Number(data.get("device_scale_factor") || 2),
    },
    full_page: mode === "full_page",
    image: {
      transparent_background: Boolean(data.get("transparent_background")),
    },
    headers,
    wait_for: {
      event: String(data.get("wait_event") || "load"),
      delay_ms: Math.round(Number(data.get("wait") || 0) * 1000),
      timeout_ms: Number(data.get("wait_timeout") || 15000),
    },
  };
  if (mode === "selector") payload.selector = String(data.get("selector") || "").trim();
  if (["jpeg", "webp"].includes(imageFormat)) payload.image.quality = Number(data.get("quality") || 90);
  if (waitSelector) payload.wait_for.selector = waitSelector;
  if (waitText) payload.wait_for.text = waitText;

  setLoading(true);
  setStatus("Loading and scrolling the page before capture.", "loading");

  try {
    const res = await fetch("/v1/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      let msg = `Request failed (${res.status})`;
      try {
        const body = await res.json();
        if (body?.error?.message) msg = body.error.message;
        if (body?.error?.request_id) msg += ` Request ID: ${body.error.request_id}`;
      } catch {}
      throw new Error(msg);
    }

    latestBlob     = await res.blob();
    latestFilename = formatFilename(url, filenamePattern, imageFormat);

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
    });
    if (captureHistory.length > 20) {
      const removed = captureHistory.pop();
      if (removed?.objectUrl) URL.revokeObjectURL(removed.objectUrl);
    }

    renderHistory();
    setStatus("Capture ready to download.", "success");

    if (window.matchMedia("(max-width: 61.25rem)").matches) {
      previewPanel.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "start",
      });
    }

  } catch (err) {
    downloadBtn.disabled = !latestBlob;
    setStatus(err.message || "Something went wrong. Check the URL and try again.", "error");
  } finally {
    setLoading(false);
  }
});

/* ── DOWNLOAD ──────────────────────────────────────────────── */
downloadBtn.addEventListener("click", async () => {
  if (!latestBlob || !latestObjectUrl) return;

  triggerDownload(latestBlob, latestObjectUrl, latestFilename);

  const { server_saves: serverSaves } = await appConfig;
  if (!serverSaves) {
    setStatus("Downloaded to your device.", "success");
    return;
  }

  try {
    await saveScreenshotToAppFolder(latestBlob, latestFilename);
    if (openAfterDownload.checked) await openFileLocation();
    setStatus("Downloaded. A local copy was saved in captures.", "success");
  } catch (err) {
    setStatus(err.message || "Downloaded, but the local copy could not be saved", "error");
  }
});

/* ── OPEN FOLDER ───────────────────────────────────────────── */
openFolderBtn.addEventListener("click", async () => {
  try {
    await openFileLocation();
    setStatus("Opened Downloads folder.", "success");
  } catch (err) {
    setStatus(err.message || "Couldn't open the Downloads folder", "error");
  }
});

/* ── Cleanup on unload ─────────────────────────────────────── */
window.addEventListener("beforeunload", () => {
  captureHistory.forEach((e) => { if (e.objectUrl) URL.revokeObjectURL(e.objectUrl); });
});
