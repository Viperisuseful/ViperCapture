use rand::{distr::Alphanumeric, Rng};
use serde::Serialize;
use std::{net::TcpListener, path::PathBuf, sync::Mutex};
use tauri::{AppHandle, Manager, RunEvent, State};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct BackendConfig {
    base_url: String,
    token: String,
}

#[derive(Default)]
struct BackendState {
    config: Mutex<Option<BackendConfig>>,
    child: Mutex<Option<CommandChild>>,
}

#[tauri::command]
fn backend_config(state: State<'_, BackendState>) -> Result<BackendConfig, String> {
    state
        .config
        .lock()
        .map_err(|_| "Renderer state is unavailable".to_string())?
        .clone()
        .ok_or_else(|| "Renderer is still starting".to_string())
}

#[tauri::command]
fn open_external(destination: String) -> Result<(), String> {
    let url = match destination.as_str() {
        "github" => "https://github.com/Viperisuseful/ViperCapture",
        "cloud" => "https://capture.viperisuseful.cc",
        _ => return Err("That external destination is not allowed".to_string()),
    };

    open::that(url).map_err(|error| format!("Could not open the default browser: {error}"))
}

#[tauri::command]
fn open_downloads(app: AppHandle) -> Result<(), String> {
    let downloads = app
        .path()
        .download_dir()
        .map_err(|error| format!("Could not locate the Downloads folder: {error}"))?;
    open::that(downloads).map_err(|error| format!("Could not open the Downloads folder: {error}"))
}

fn reserve_loopback_port() -> Result<u16, String> {
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|listener| listener.local_addr())
        .map(|address| address.port())
        .map_err(|error| format!("Could not reserve a renderer port: {error}"))
}

fn playwright_dir(app: &tauri::App) -> Result<PathBuf, String> {
    let bundled = app
        .path()
        .resource_dir()
        .map_err(|error| format!("Could not locate bundled resources: {error}"))?
        .join("playwright");
    if bundled.exists() {
        return Ok(bundled);
    }

    Ok(PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("resources")
        .join("playwright"))
}

fn stop_backend(app: &tauri::AppHandle) {
    let state = app.state::<BackendState>();
    if let Ok(mut guard) = state.child.lock() {
        if let Some(child) = guard.take() {
            let _ = child.kill();
        }
    };
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .manage(BackendState::default())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let port = reserve_loopback_port()?;
            let token: String = rand::rng()
                .sample_iter(&Alphanumeric)
                .take(64)
                .map(char::from)
                .collect();
            let captures_dir = app
                .path()
                .app_data_dir()
                .map_err(|error| format!("Could not locate app data: {error}"))?
                .join("captures");
            std::fs::create_dir_all(&captures_dir)?;
            let browser_dir = playwright_dir(app)?;

            let (mut events, child) = app
                .shell()
                .sidecar("vipercapture-sidecar")?
                .env("VIPERCAPTURE_PORT", port.to_string())
                .env("VIPERCAPTURE_DESKTOP_TOKEN", &token)
                .env("VIPERCAPTURE_PARENT_PID", std::process::id().to_string())
                .env("VIPERCAPTURE_CAPTURES_DIR", captures_dir.as_os_str())
                .env("PLAYWRIGHT_BROWSERS_PATH", browser_dir.as_os_str())
                .spawn()?;

            let config = BackendConfig {
                base_url: format!("http://127.0.0.1:{port}"),
                token,
            };
            let state = app.state::<BackendState>();
            *state
                .config
                .lock()
                .map_err(|_| "Renderer state is unavailable")? = Some(config);
            *state
                .child
                .lock()
                .map_err(|_| "Renderer process state is unavailable")? = Some(child);

            tauri::async_runtime::spawn(async move {
                while let Some(event) = events.recv().await {
                    match event {
                        CommandEvent::Stdout(line) => {
                            log::info!("renderer: {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Stderr(line) => {
                            log::warn!("renderer: {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Error(error) => {
                            log::error!("renderer process error: {error}");
                        }
                        CommandEvent::Terminated(payload) => {
                            log::info!("renderer exited: {:?}", payload.code);
                        }
                        _ => {}
                    }
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_config,
            open_external,
            open_downloads
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|handle, event| {
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            stop_backend(handle);
        }
    });
}
