const form = document.getElementById("captureForm");
const statusEl = document.getElementById("status");
const captureBtn = document.getElementById("captureBtn");
const downloadBtn = document.getElementById("downloadBtn");
const openFolderBtn = document.getElementById("openFolderBtn");
const previewImage = document.getElementById("previewImage");
const placeholder = document.getElementById("placeholder");
const historyList = document.getElementById("historyList");
const historyEmpty = document.getElementById("historyEmpty");
const openAfterDownload = document.getElementById("openAfterDownload");

let latestBlob = null;
let latestObjectUrl = null;
let latestFilename = "screenshot.png";
const captureHistory = [];

const setStatus = (message, isError = false) => {
    statusEl.textContent = message;
    statusEl.classList.toggle("error", isError);
};

const sanitizeFilePart = (value) => (value || "")
    .toString()
    .trim()
    .replace(/[^a-z0-9._-]+/gi, "_")
    .replace(/^_+|_+$/g, "") || "screenshot";

const formatFilename = (urlText, templateText) => {
    const now = new Date();
    const date = now.toISOString().slice(0, 10).replace(/-/g, "");
    const time = `${String(now.getHours()).padStart(2, "0")}${String(now.getMinutes()).padStart(2, "0")}${String(now.getSeconds()).padStart(2, "0")}`;

    let host = "site";
    try {
        host = sanitizeFilePart(new URL(urlText).hostname || "site");
    } catch {
        host = "site";
    }

    const rawTemplate = (templateText || "{host}_{date}_{time}.png").trim() || "{host}_{date}_{time}.png";
    const filled = rawTemplate
        .replaceAll("{host}", host)
        .replaceAll("{date}", date)
        .replaceAll("{time}", time);

    const safeName = sanitizeFilePart(filled.replace(/\.png$/i, ""));
    return `${safeName}.png`;
};

const triggerDownload = (blob, objectUrl, filename) => {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
};

const saveScreenshotToAppFolder = async (blob, filename) => {
    const payload = new FormData();
    payload.append("filename", filename);
    payload.append("screenshot", blob, filename);

    const response = await fetch("/save-screenshot", {
        method: "POST",
        body: payload,
    });

    if (!response.ok) {
        let message = `Save failed (${response.status})`;
        try {
            const body = await response.json();
            if (body && body.detail) {
                message = body.detail;
            }
        } catch {
            // ignore parse failure
        }
        throw new Error(message);
    }
};

const openFileLocation = async () => {
    const response = await fetch("/open-captures-folder", {
        method: "POST",
    });
    if (!response.ok) {
        throw new Error("Failed to open file location");
    }
};

const renderHistory = () => {
    historyList.innerHTML = "";
    historyEmpty.style.display = captureHistory.length ? "none" : "block";

    captureHistory.forEach((entry) => {
        const card = document.createElement("article");
        card.className = "history-item";

        const thumb = document.createElement("img");
        thumb.className = "history-thumb";
        thumb.src = entry.objectUrl;
        thumb.alt = entry.filename;

        const meta = document.createElement("div");
        meta.className = "history-meta";

        const name = document.createElement("p");
        name.className = "history-name";
        name.title = entry.filename;
        name.textContent = entry.filename;

        const time = document.createElement("p");
        time.className = "history-time";
        time.textContent = entry.capturedAt;

        const actions = document.createElement("div");
        actions.className = "history-actions";

        const downloadAgain = document.createElement("button");
        downloadAgain.className = "secondary";
        downloadAgain.type = "button";
        downloadAgain.textContent = "Re-download";
        downloadAgain.addEventListener("click", () => {
            triggerDownload(entry.blob, entry.objectUrl, entry.filename);
        });

        actions.appendChild(downloadAgain);
        meta.appendChild(name);
        meta.appendChild(time);
        meta.appendChild(actions);
        card.appendChild(thumb);
        card.appendChild(meta);
        historyList.appendChild(card);
    });
};

form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const data = new FormData(form);
    const params = new URLSearchParams();
    const url = String(data.get("url") || "");
    const filenameTemplate = String(data.get("filename_template") || "{host}_{date}_{time}.png");

    params.set("url", url);
    params.set("width", String(data.get("width") || "1920"));
    params.set("height", String(data.get("height") || "1080"));
    params.set("device_scale_factor", String(data.get("device_scale_factor") || "2"));
    params.set("wait", String(data.get("wait") || "4"));
    params.set("dark_mode", data.get("dark_mode") ? "true" : "false");

    const authUsername = String(data.get("auth_username") || "").trim();
    const authPassword = String(data.get("auth_password") || "").trim();
    if (authUsername) {
        params.set("auth_username", authUsername);
    }
    if (authPassword) {
        params.set("auth_password", authPassword);
    }

    captureBtn.disabled = true;
    downloadBtn.disabled = true;
    setStatus("Capturing screenshot...");

    try {
        const response = await fetch(`/screenshot?${params.toString()}`);
        if (!response.ok) {
            let message = `Request failed (${response.status})`;
            try {
                const body = await response.json();
                if (body && body.detail) {
                    message = body.detail;
                }
            } catch {
                // ignore parse failure
            }
            throw new Error(message);
        }

        latestBlob = await response.blob();
        latestFilename = formatFilename(url, filenameTemplate);

        latestObjectUrl = URL.createObjectURL(latestBlob);
        previewImage.src = latestObjectUrl;
        previewImage.style.display = "block";
        placeholder.style.display = "none";
        downloadBtn.disabled = false;

        captureHistory.unshift({
            blob: latestBlob,
            objectUrl: latestObjectUrl,
            filename: latestFilename,
            capturedAt: new Date().toLocaleString(),
        });
        if (captureHistory.length > 20) {
            const removed = captureHistory.pop();
            if (removed && removed.objectUrl) {
                URL.revokeObjectURL(removed.objectUrl);
            }
        }
        renderHistory();
        setStatus("Screenshot captured successfully.");
    } catch (error) {
        latestBlob = null;
        setStatus(error.message || "Failed to capture screenshot", true);
    } finally {
        captureBtn.disabled = false;
    }
});

downloadBtn.addEventListener("click", async () => {
    if (!latestBlob || !latestObjectUrl) {
        return;
    }

    triggerDownload(latestBlob, latestObjectUrl, latestFilename);
    try {
        await saveScreenshotToAppFolder(latestBlob, latestFilename);
        if (openAfterDownload.checked) {
            await openFileLocation();
        }
        setStatus("Downloaded and saved to app captures folder.");
    } catch (error) {
        setStatus(error.message || "Download completed but app save failed", true);
    }
});

openFolderBtn.addEventListener("click", async () => {
    try {
        await openFileLocation();
        setStatus("Opened file location.");
    } catch (error) {
        setStatus(error.message || "Failed to open file location", true);
    }
});

window.addEventListener("beforeunload", () => {
    if (latestObjectUrl) {
        URL.revokeObjectURL(latestObjectUrl);
    }
    captureHistory.forEach((entry) => {
        if (entry.objectUrl) {
            URL.revokeObjectURL(entry.objectUrl);
        }
    });
});
