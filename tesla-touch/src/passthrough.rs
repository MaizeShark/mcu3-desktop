//! Passthrough of a real touchscreen (--from): the screen shows QtCar's UI letterboxed (scaled to
//! fit, black bars), so its touches are mapped from panel coordinates to UI coordinates and
//! re-emitted on the virtual device. The real device is grabbed, so the desktop underneath
//! doesn't react to the same touches.

use crate::touch::MAX_SLOTS;
use evdev::{AbsoluteAxisType, Device, InputEventKind, Key};
use std::path::Path;
use std::sync::mpsc::Sender;

/// Where the UI sits on the panel: panel pixels -> UI pixels.
#[derive(Clone, Copy, Debug)]
pub struct Letterbox {
    scale: f64,
    off_x: f64,
    off_y: f64,
}

impl Letterbox {
    /// The UI (ui_w x ui_h) scaled to fit the panel (panel_w x panel_h), centered.
    pub fn fit(ui_w: i32, ui_h: i32, panel_w: i32, panel_h: i32) -> Self {
        let scale = (panel_w as f64 / ui_w as f64).min(panel_h as f64 / ui_h as f64);
        Self {
            scale,
            off_x: (panel_w as f64 - ui_w as f64 * scale) / 2.0,
            off_y: (panel_h as f64 - ui_h as f64 * scale) / 2.0,
        }
    }

    fn to_ui(&self, px: f64, py: f64) -> (i32, i32) {
        (((px - self.off_x) / self.scale).round() as i32, ((py - self.off_y) / self.scale).round() as i32)
    }
}

/// One axis of the source device: raw value -> panel pixel.
#[derive(Clone, Copy)]
struct Axis {
    min: i32,
    max: i32,
    pixels: i32,
}

impl Axis {
    fn to_panel(&self, raw: i32) -> f64 {
        (raw - self.min) as f64 / (self.max - self.min).max(1) as f64 * self.pixels as f64
    }
}

pub enum Event {
    Frame(Vec<Option<(i32, i32)>>),
    Error(String),
    Quit,
}

/// Open and grab `path`; returns the device and a description for the log.
pub fn open(path: &Path) -> Result<(Device, String), Box<dyn std::error::Error>> {
    let mut dev = Device::open(path)?;
    dev.grab()?;
    let name = dev.name().unwrap_or("?").to_string();
    Ok((dev, name))
}

/// Read touches from `dev` forever and send them as UI-coordinate frames.
pub fn run(mut dev: Device, panel: (i32, i32), lb: Letterbox, tx: Sender<Event>) {
    let abs = match dev.get_abs_state() {
        Ok(a) => a,
        Err(e) => {
            let _ = tx.send(Event::Error(format!("can't read the device's axes: {e}")));
            return;
        }
    };
    let axes = dev.supported_absolute_axes().map(|a| a.iter().collect::<Vec<_>>()).unwrap_or_default();
    let mt = axes.contains(&AbsoluteAxisType::ABS_MT_POSITION_X);
    let (cx, cy) = if mt {
        (AbsoluteAxisType::ABS_MT_POSITION_X, AbsoluteAxisType::ABS_MT_POSITION_Y)
    } else {
        (AbsoluteAxisType::ABS_X, AbsoluteAxisType::ABS_Y)
    };
    let ax = Axis { min: abs[cx.0 as usize].minimum, max: abs[cx.0 as usize].maximum, pixels: panel.0 };
    let ay = Axis { min: abs[cy.0 as usize].minimum, max: abs[cy.0 as usize].maximum, pixels: panel.1 };

    // per slot: (active, raw x, raw y)
    let mut slots = [(false, 0i32, 0i32); MAX_SLOTS];
    let mut slot = 0usize;
    loop {
        let events = match dev.fetch_events() {
            Ok(ev) => ev.collect::<Vec<_>>(),
            Err(e) => {
                let _ = tx.send(Event::Error(format!("reading the touchscreen failed: {e}")));
                return;
            }
        };
        for ev in events {
            match ev.kind() {
                InputEventKind::AbsAxis(a) if mt => match a {
                    AbsoluteAxisType::ABS_MT_SLOT => slot = (ev.value().max(0) as usize).min(MAX_SLOTS - 1),
                    AbsoluteAxisType::ABS_MT_TRACKING_ID => slots[slot].0 = ev.value() >= 0,
                    AbsoluteAxisType::ABS_MT_POSITION_X => slots[slot].1 = ev.value(),
                    AbsoluteAxisType::ABS_MT_POSITION_Y => slots[slot].2 = ev.value(),
                    _ => {}
                },
                InputEventKind::AbsAxis(AbsoluteAxisType::ABS_X) if !mt => slots[0].1 = ev.value(),
                InputEventKind::AbsAxis(AbsoluteAxisType::ABS_Y) if !mt => slots[0].2 = ev.value(),
                InputEventKind::Key(Key::BTN_TOUCH) if !mt => slots[0].0 = ev.value() != 0,
                InputEventKind::Synchronization(_) if ev.code() == 0 => {
                    let frame = slots
                        .iter()
                        .map(|&(on, x, y)| on.then(|| lb.to_ui(ax.to_panel(x), ay.to_panel(y))))
                        .collect();
                    if tx.send(Event::Frame(frame)).is_err() {
                        return;
                    }
                }
                _ => {}
            }
        }
    }
}
