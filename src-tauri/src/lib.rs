//! YouTube AI Agent Pro — native macOS shell (Tauri 2, Apple Silicon).
//!
//! * Starts the bundled local engine (`Resources/engine/yta-engine`, a packaged Python/FastAPI service with
//!   its own runtime — nothing for the user to install) on a free 127.0.0.1 port with a per-launch token.
//! * Menu bar: Show Dashboard · Pause Agent · Resume Agent · Stop Agent · View Jobs · Quit Application.
//!   Closing the window keeps approved background jobs running; the Dock icon reopens it.
//! * In-app updates use the official signed updater (tauri-plugin-updater). Before installing, the window
//!   asks the engine to checkpoint jobs and back up the database; `prepare_restart` then stops the engine
//!   cleanly and the app relaunches into the new version. User data lives in Application Support and the
//!   Keychain — never inside the replaceable .app bundle.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};

struct Engine {
    port: u16,
    token: String,
    child: Mutex<Option<Child>>,
    log_path: PathBuf,
    start_error: Mutex<Option<String>>,
}

#[derive(Serialize)]
struct EngineInfo {
    url: String,
    token: String,
    app_version: String,
}

fn random_token() -> String {
    let mut buf = [0u8; 32];
    getrandom::getrandom(&mut buf).expect("OS random generator unavailable");
    buf.iter().map(|b| format!("{:02x}", b)).collect()
}

fn free_port() -> u16 {
    TcpListener::bind(("127.0.0.1", 0)).and_then(|l| l.local_addr()).map(|a| a.port()).unwrap_or(47821)
}

/// Tiny HTTP/1.0 client for the local engine only (no extra dependency).
fn engine_request(port: u16, method: &str, path: &str, token: Option<&str>) -> Option<u16> {
    let mut s = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(800)).ok()?;
    let _ = s.set_read_timeout(Some(Duration::from_secs(20)));
    let auth = token.map(|t| format!("X-YTA-Token: {t}\r\n")).unwrap_or_default();
    let req = format!("{method} {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n{auth}Content-Length: 0\r\n\r\n");
    s.write_all(req.as_bytes()).ok()?;
    let mut head = [0u8; 16];
    let n = s.read(&mut head).ok()?;
    if n < 12 {
        return None;
    }
    std::str::from_utf8(&head[9..12]).ok()?.parse().ok()
}

fn engine_healthy(port: u16) -> bool {
    engine_request(port, "GET", "/api/health", None) == Some(200)
}

fn engine_exe(app: &AppHandle) -> Result<PathBuf, String> {
    let res = app.path().resource_dir().map_err(|e| e.to_string())?;
    let candidates = [res.join("engine").join("yta-engine"), res.join("yta-engine")];
    candidates.iter().find(|p| p.exists()).cloned().ok_or_else(|| format!("engine not found in {}", res.display()))
}

fn start_engine(app: &AppHandle, engine: &Engine) -> Result<Child, String> {
    let exe = engine_exe(app)?;
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?; // ~/Library/Application Support/<id>
    std::fs::create_dir_all(&data_dir).map_err(|e| e.to_string())?;
    let log = std::fs::File::create(&engine.log_path).map_err(|e| e.to_string())?;
    let log_err = log.try_clone().map_err(|e| e.to_string())?;
    let bin = exe.parent().map(|p| p.join("bin")).unwrap_or_default();
    let mut cmd = Command::new(&exe);
    cmd.env("YTA_PORT", engine.port.to_string())
        .env("YTA_API_TOKEN", &engine.token)
        .env("YTA_DATA_DIR", &data_dir)
        .env("YTA_PARENT_PID", std::process::id().to_string())
        .env("YTA_APP_VERSION", app.package_info().version.to_string())
        .env("YTA_FFMPEG_BIN", bin.join("ffmpeg"))
        .env("YTA_FFPROBE_BIN", bin.join("ffprobe"))
        .env("PYTHONIOENCODING", "utf-8")
        .current_dir(exe.parent().unwrap_or(&data_dir))
        .stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err));
    cmd.spawn().map_err(|e| format!("could not start {}: {e}", exe.display()))
}

fn log_tail(path: &PathBuf) -> String {
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(12)..].join("\n")
}

async fn sleep(d: Duration) {
    let _ = tauri::async_runtime::spawn_blocking(move || std::thread::sleep(d)).await;
}

#[tauri::command]
async fn engine_info(app: AppHandle, state: tauri::State<'_, Engine>) -> Result<EngineInfo, String> {
    if let Some(err) = state.start_error.lock().unwrap().clone() {
        return Err(err);
    }
    let port = state.port;
    let deadline = Instant::now() + Duration::from_secs(120);
    loop {
        if engine_healthy(port) {
            return Ok(EngineInfo {
                url: format!("http://127.0.0.1:{port}"),
                token: state.token.clone(),
                app_version: app.package_info().version.to_string(),
            });
        }
        if let Some(child) = state.child.lock().unwrap().as_mut() {
            if let Ok(Some(status)) = child.try_wait() {
                return Err(format!("the engine stopped ({status}). {}", log_tail(&state.log_path)));
            }
        }
        if Instant::now() > deadline {
            return Err(format!("the engine did not answer within 2 minutes. {}", log_tail(&state.log_path)));
        }
        sleep(Duration::from_millis(400)).await;
    }
}

#[tauri::command]
fn engine_log_path(state: tauri::State<'_, Engine>) -> String {
    state.log_path.display().to_string()
}

/// Called by the window right before relaunching into an installed update: stop the engine so the new
/// version starts against a cleanly closed database (its WAL is checkpointed on close).
#[tauri::command]
fn prepare_restart(app: AppHandle) {
    stop_engine(&app);
}

#[tauri::command]
fn set_close_to_tray(_enabled: bool) {
    // macOS: closing the window always keeps the menu bar app running; "Quit Application" stops it.
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn agent_action(app: &AppHandle, action: &str) {
    if let Some(engine) = app.try_state::<Engine>() {
        let port = engine.port;
        let token = engine.token.clone();
        let path = format!("/api/agents/{action}");
        let handle = app.clone();
        std::thread::spawn(move || {
            let ok = engine_request(port, "POST", &path, Some(&token)) == Some(200);
            let _ = handle.emit("agent-state-changed", ok);
        });
    }
}

fn stop_engine(app: &AppHandle) {
    if let Some(engine) = app.try_state::<Engine>() {
        if let Some(mut child) = engine.child.lock().unwrap().take() {
            // SIGTERM lets uvicorn shut down: running stage finishes its checkpoint, database closes cleanly.
            let _ = Command::new("/bin/kill").arg("-TERM").arg(child.id().to_string()).status();
            let deadline = Instant::now() + Duration::from_secs(20);
            while Instant::now() < deadline {
                if let Ok(Some(_)) = child.try_wait() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(200));
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| show_main(app)))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .invoke_handler(tauri::generate_handler![engine_info, engine_log_path, set_close_to_tray, prepare_restart])
        .setup(|app| {
            let handle = app.handle().clone();
            let log_dir = handle.path().app_log_dir()?; // ~/Library/Logs/<id>
            std::fs::create_dir_all(&log_dir)?;
            let engine = Engine {
                port: free_port(),
                token: random_token(),
                child: Mutex::new(None),
                log_path: log_dir.join("engine-console.log"),
                start_error: Mutex::new(None),
            };
            match start_engine(&handle, &engine) {
                Ok(child) => *engine.child.lock().unwrap() = Some(child),
                Err(e) => *engine.start_error.lock().unwrap() = Some(e),
            }
            app.manage(engine);

            // Menu bar item
            let show = MenuItem::with_id(app, "show", "Show Dashboard", true, None::<&str>)?;
            let pause = MenuItem::with_id(app, "pause", "Pause Agent", true, None::<&str>)?;
            let resume = MenuItem::with_id(app, "resume", "Resume Agent", true, None::<&str>)?;
            let stop = MenuItem::with_id(app, "stop", "Stop Agent", true, None::<&str>)?;
            let jobs = MenuItem::with_id(app, "jobs", "View Jobs", true, None::<&str>)?;
            let sep1 = PredefinedMenuItem::separator(app)?;
            let sep2 = PredefinedMenuItem::separator(app)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Application", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &sep1, &pause, &resume, &stop, &jobs, &sep2, &quit])?;
            let mut tray = TrayIconBuilder::with_id("menubar")
                .tooltip("YouTube AI Agent Pro")
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => show_main(app),
                    "pause" => agent_action(app, "pause"),
                    "resume" => agent_action(app, "start"),
                    "stop" => agent_action(app, "stop"),
                    "jobs" => {
                        show_main(app);
                        let _ = app.emit("navigate", "/videos/");
                    }
                    "quit" => {
                        stop_engine(app);
                        app.exit(0);
                    }
                    _ => {}
                });
            if let Ok(icon) = tauri::image::Image::from_bytes(include_bytes!("../icons/menubar.png")) {
                tray = tray.icon(icon).icon_as_template(true);
            } else if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    // Keep approved background jobs running; the menu bar item and Dock icon bring it back.
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building YouTube AI Agent Pro");

    app.run(|app, event| match event {
        RunEvent::Reopen { .. } => show_main(app),
        RunEvent::Exit => stop_engine(app),
        _ => {}
    });
}
