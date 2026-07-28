const COMMANDS: &[&str] = &["capture", "save", "open_downloads", "open_external"];

fn main() {
    tauri_plugin::Builder::new(COMMANDS)
        .android_path("android")
        .build();
}
