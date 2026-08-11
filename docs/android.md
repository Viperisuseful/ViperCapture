# Android app

ViperCapture Android is beta software. It is part of the isolated Tauri 2
project under `desktop/`. It does not change the hosted or self-hosted web
application.

Android cannot run the packaged Python/Playwright desktop sidecar. The app
therefore uses a private Tauri mobile plugin with an offscreen Android WebView.
Captures stay on the device until the user presses **Download**, which writes
the image to `Downloads/ViperCapture` through Android MediaStore.

## Requirements

- Android 10 or newer
- JDK 21
- Android SDK Platform 36 and Build Tools 36
- Android NDK `27.1.12297006`
- Rust Android targets:

```powershell
rustup target add `
  aarch64-linux-android `
  armv7-linux-androideabi `
  i686-linux-android `
  x86_64-linux-android
```

Set `JAVA_HOME`, `ANDROID_HOME`, and `NDK_HOME` as described in the
[official Tauri prerequisites](https://v2.tauri.app/start/prerequisites/).

## Build

From `desktop/`:

```powershell
npm ci
npm run tauri -- android init --ci
npm run tauri -- android build --apk --debug
```

Windows must have Developer Mode enabled because the Tauri CLI links compiled
Rust libraries into Gradle's ABI directories. CI builds on Linux and does not
need that Windows setting.

For signed release packages:

```powershell
npm run tauri -- android build --apk --aab --ci
```

The Gradle project reads signing values from the ignored file
`desktop/src-tauri/gen/android/keystore.properties`.

## Android renderer support

The Android interface exposes:

- PNG, JPEG, and WebP output
- full-page or viewport capture
- viewport width, height, and output density
- lazy-content scrolling
- fixed post-load delay and timeout
- JPEG/WebP quality and PNG/WebP transparency
- previews, capture history, and MediaStore downloads

CSS selector capture, custom request headers, and selector/text wait conditions
remain desktop-only and are hidden on Android. Android WebView rendering can
also differ from desktop Chromium for browser-specific layout or font behavior.

## Release automation

The `Android Release` GitHub Actions workflow builds and verifies a signed,
universal APK and AAB, writes SHA-256 checksums, and publishes them for tags
matching `android-v*`.

It requires these repository secrets:

- `ANDROID_KEYSTORE_BASE64`
- `ANDROID_KEY_ALIAS`
- `ANDROID_KEY_PASSWORD`
- `ANDROID_STORE_PASSWORD`
