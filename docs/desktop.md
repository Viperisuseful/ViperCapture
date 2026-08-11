# Desktop app

ViperCapture Desktop is beta software. It is a Tauri 2 application under
`desktop/`. The existing
web application remains under `frontend/`; neither project writes into the
other project's source or build directory.

The desktop bundle includes:

- the React interface and Tauri Rust shell;
- the FastAPI/Playwright renderer packaged with PyInstaller;
- platform-native Playwright Chromium, Firefox, and WebKit runtimes;
- a random loopback port and per-launch bearer secret;
- app-data capture storage and clean sidecar/browser shutdown.

The renderer listens only on `127.0.0.1`. Desktop API requests require the
ephemeral secret passed directly from Rust, and the webview CSP permits only
Tauri IPC and loopback HTTP.

## Prerequisites

Follow the [official Tauri 2 prerequisites](https://v2.tauri.app/start/prerequisites/)
for each operating system. Windows development requires:

- Microsoft C++ Build Tools with **Desktop development with C++**
- Microsoft Edge WebView2
- Rust using the MSVC toolchain
- Node.js and npm

Install the Python build dependencies from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r desktop\requirements.txt
```

Then prepare and launch the app:

```powershell
cd desktop
npm ci
npm run sidecar:build
npm run tauri:dev
```

`sidecar:build` downloads the current Playwright Chromium, Firefox, and WebKit builds into the
Tauri resources directory and creates the target-triple sidecar binary expected
by Tauri.

## Validation

From `desktop/`:

```powershell
npm run lint
npm run build
npm run tauri:check
python scripts/smoke_sidecar.py
npm run tauri:build
```

The sidecar smoke test starts the packaged binary, verifies authentication,
renders `https://example.com`, checks the PNG signature, requests graceful
shutdown, and fails if any step does not complete.

## Release packages

The `Desktop Release` GitHub Actions workflow builds on each native runner:

- Windows x64: NSIS setup executable and MSI
- macOS Apple Silicon: DMG
- macOS Intel: DMG
- Linux x64: Debian package with declared Chromium, Firefox, WebKit, and FFmpeg runtime dependencies

The first release artifacts are unsigned. Windows SmartScreen and macOS
Gatekeeper may warn until repository secrets for platform code-signing
certificates are configured. Linux packages are also not repository-signed.
The desktop workspace exposes every output family, browser selection, URL/HTML/
Markdown input, resizing, extraction, PDF, video, diagnostics, deterministic
rendering, slices, and the cleanup/network controls. Expert JSON overrides are
deep-merged into the generated strict request for actions, assertions, cookies,
proxies, profiles, viewport packs, certification, and newly added API fields.
MP4 is shown only when the installed or packaged FFmpeg exposes the required
libx264 encoder; WebM and GIF remain available through Playwright's runtime.
