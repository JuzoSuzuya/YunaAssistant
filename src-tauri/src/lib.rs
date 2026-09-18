use std::process::{Child, Command};
use std::sync::Mutex;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, RunEvent};

/// Fixed deployment root — the binary is launched from autostart with an
/// arbitrary cwd, so the stack script path must be absolute.
const PROJECT_ROOT: &str = "/mnt/disk03/YunaAI/07_projects/Yuna-by-Hoshi";

/// Holds the spawned `start_stack.sh` child so it can be reaped on exit.
struct StackChild(Mutex<Option<Child>>);

fn spawn_stack() -> std::io::Result<Child> {
    Command::new("bash")
        .arg(format!("{PROJECT_ROOT}/scripts/start_stack.sh"))
        .spawn()
}

fn restart_stack(app: &tauri::AppHandle) {
    let state = app.state::<StackChild>();
    let mut guard = state.0.lock().unwrap();
    if let Some(mut child) = guard.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    match spawn_stack() {
        Ok(child) => {
            *guard = Some(child);
            println!("yuna-desktop: stack restarted");
        }
        Err(e) => eprintln!("yuna-desktop: start_stack.sh spawn failed: {e}"),
    }
}

fn toggle_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        if window.is_visible().unwrap_or(false) {
            let _ = window.hide();
        } else {
            let _ = window.show();
            let _ = window.set_focus();
        }
    }
}

fn kill_stack_child(app: &tauri::AppHandle) {
    let state = app.state::<StackChild>();
    let mut guard = state.0.lock().unwrap();
    if let Some(mut child) = guard.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            // Ensure the stack (ollama → bridge → voice) is up.
            let child = spawn_stack().ok();
            app.manage(StackChild(Mutex::new(child)));

            // Tray: Show/Hide, Restart stack, Quit.
            let show_hide = MenuItem::with_id(app, "show_hide", "Show/Hide", true, None::<&str>)?;
            let restart = MenuItem::with_id(app, "restart", "Restart stack", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_hide, &restart, &quit])?;

            let mut builder = TrayIconBuilder::with_id("yuna-tray")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show_hide" => toggle_window(app),
                    "restart" => restart_stack(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                });

            if let Some(icon) = app.default_window_icon() {
                builder = builder.icon(icon.clone());
            }
            builder.build(app)?;

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                kill_stack_child(app_handle);
            }
        });
}
