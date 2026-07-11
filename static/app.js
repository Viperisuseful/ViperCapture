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
const captchaDialog     = document.getElementById("captchaDialog");
const captchaProvider   = document.getElementById("captchaProvider");

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

const captchaProviderNames = {
  cloudflare: "Cloudflare Turnstile",
  recaptcha: "Google reCAPTCHA",
  hcaptcha: "hCaptcha",
};

const confirmCaptchaCapture = (provider) => new Promise((resolve) => {
  captchaProvider.textContent = captchaProviderNames[provider] || "A captcha";
  captchaDialog.returnValue = "cancel";
  captchaDialog.addEventListener(
    "close",
    () => resolve(captchaDialog.returnValue === "proceed"),
    { once: true },
  );
  captchaDialog.showModal();
});

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
  const filenameTemplate = String(data.get("filename_template") || "{host}_{date}_{time}.png");
  const params           = new URLSearchParams();

  params.set("url",                 url);
  params.set("width",               String(data.get("width")               || "1920"));
  params.set("height",              String(data.get("height")              || "1080"));
  params.set("device_scale_factor", String(data.get("device_scale_factor") || "2"));
  params.set("wait",                String(data.get("wait")                || "1"));

  setLoading(true);
  setStatus("Loading and scrolling the page before capture.", "loading");

  try {
    let res = await fetch(`/screenshot?${params}`);

    if (res.status === 409) {
      let detail = null;
      try { detail = (await res.json())?.detail; } catch {}

      if (detail?.code === "captcha_detected") {
        setLoading(false);
        setStatus("Captcha detected. Waiting for your choice.", "error");

        const proceed = await confirmCaptchaCapture(detail.provider);
        if (!proceed) {
          downloadBtn.disabled = !latestBlob;
          setStatus("Capture canceled.");
          return;
        }

        params.set("proceed_on_captcha", "true");
        setLoading(true);
        setStatus("Reloading the page and capturing the result.", "loading");
        res = await fetch(`/screenshot?${params}`);
      }
    }

    if (!res.ok) {
      let msg = `Request failed (${res.status})`;
      try {
        const body = await res.json();
        if (typeof body?.detail === "string") msg = body.detail;
        else if (body?.detail?.message) msg = body.detail.message;
      } catch {}
      throw new Error(msg);
    }

    latestBlob     = await res.blob();
    latestFilename = formatFilename(url, filenameTemplate);

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
    setStatus(err.message || "Something went wrong — check the URL and try again", "error");
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
