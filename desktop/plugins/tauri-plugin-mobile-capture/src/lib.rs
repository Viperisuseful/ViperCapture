use tauri::{
    plugin::{Builder, TauriPlugin},
    Runtime,
};

pub fn init<R: Runtime>() -> TauriPlugin<R> {
    Builder::new("mobile-capture")
        .setup(|_app, _api| {
            #[cfg(target_os = "android")]
            _api.register_android_plugin(
                "cc.viperisuseful.vipercapture.mobilecapture",
                "MobileCapturePlugin",
            )?;
            Ok(())
        })
        .build()
}
