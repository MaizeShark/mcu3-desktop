//! tesla-touch: turns mouse input on an X display (e.g. Xvfb + x11vnc) into a virtual
//! multitouch screen for QtCar's EvDevTouchDriver.
//!
//!   left button  = one finger (tap / drag)
//!   wheel        = two-finger pinch zoom around the pointer
//!   right button = long press
//!
//! Input is captured on the X display itself, so any VNC client on any host OS works.
//! With --from, a real touchscreen is passed through instead (mapped to a letterboxed UI).

mod cursor;
mod passthrough;
mod record;
mod touch;

use std::ffi::CString;
use std::fs;
use std::io::{self, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::time::{Duration, Instant};
use touch::TouchScreen;
use x11rb::connection::Connection;

pub enum Msg {
    Motion { x: i32, y: i32 },
    Button { button: u8, pressed: bool, x: i32, y: i32 },
    Quit,
}

const USAGE: &str = "\
Usage: tesla-touch [options]

  --display DPY     X display to take mouse input from          [default: :1]
  --size WxH        touch range                                 [default: X screen size]
  --bind PATH       bind-mount the event node to PATH (e.g. <chroot>/dev/input/touch),
                    unmounted again on exit. Needs root.
  --name NAME       input device name                           [default: cyttsp6_mt]
  --long-press MS   duration of a right-click long press        [default: 800]
  --from DEV        pass a real touchscreen through instead of the X mouse (e.g.
                    /dev/input/event5). It's grabbed, so the desktop doesn't see its touches.
  --panel WxH       with --from: the panel's pixel size. The UI (--size, default 1920x1200)
                    is shown scaled to fit with black bars; touches are mapped to it.
  --no-cursor       don't force a visible arrow cursor
  -v, --verbose     log every touch frame
  -h, --help
";

struct Args {
    display: String,
    size: Option<(i32, i32)>,
    bind: Option<PathBuf>,
    name: String,
    long_press: Duration,
    cursor: bool,
    verbose: bool,
    from: Option<PathBuf>,
    panel: Option<(i32, i32)>,
}

fn parse_size(v: &str, opt: &str) -> Result<(i32, i32), String> {
    let bad = || format!("{opt} must look like 1920x1200");
    let (w, h) = v.split_once('x').ok_or_else(bad)?;
    Ok((w.parse().map_err(|_| bad())?, h.parse().map_err(|_| bad())?))
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args {
        display: ":1".into(),
        size: None,
        bind: None,
        name: "cyttsp6_mt".into(),
        long_press: Duration::from_millis(800),
        cursor: true,
        verbose: false,
        from: None,
        panel: None,
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut value = || it.next().ok_or(format!("{arg} needs a value"));
        match arg.as_str() {
            "--display" => a.display = value()?,
            "--size" => {
                let v = value()?;
                let (w, h) = v.split_once('x').ok_or("--size must look like 1920x1200")?;
                let parse = |s: &str| s.parse::<i32>().map_err(|_| format!("bad --size {v}"));
                a.size = Some((parse(w)?, parse(h)?));
            }
            "--bind" => a.bind = Some(value()?.into()),
            "--name" => a.name = value()?,
            "--long-press" => {
                let ms = value()?.parse().map_err(|_| "bad --long-press")?;
                a.long_press = Duration::from_millis(ms);
            }
            "--from" => a.from = Some(value()?.into()),
            "--panel" => a.panel = Some(parse_size(&value()?, "--panel")?),
            "--no-cursor" => a.cursor = false,
            "-v" | "--verbose" => a.verbose = true,
            "-h" | "--help" => {
                print!("{USAGE}");
                std::process::exit(0);
            }
            _ => return Err(format!("unknown option {arg}\n\n{USAGE}")),
        }
    }
    Ok(a)
}

fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    };
    if let Err(e) = run(args) {
        eprintln!("tesla-touch: {e}");
        std::process::exit(1);
    }
}

fn run(args: Args) -> Result<(), Box<dyn std::error::Error>> {
    let (width, height) = match (args.size, &args.from) {
        (Some(size), _) => size,
        (None, Some(_)) => (1920, 1200),
        (None, None) => screen_size(&args.display)?,
    };
    // --from: open (and grab) the real touchscreen before creating ours
    let source = match &args.from {
        Some(path) => Some(passthrough::open(path).map_err(|e| format!("{}: {e}", path.display()))?),
        None => None,
    };

    let mut ts = TouchScreen::new(&args.name, width, height)?;
    let node = ts.node.clone();
    // udev sets up the new node asynchronously (owner, mode); let it finish first, otherwise
    // it resets our chmod right after.
    let udev_db = wait_for_udev(&node);
    // QtCar runs as user "tesla" inside the chroot and must be able to open the node.
    let _ = fs::set_permissions(&node, fs::Permissions::from_mode(0o666));
    let mode = fs::metadata(&node).map(|m| m.permissions().mode() & 0o777).unwrap_or(0);
    if mode != 0o666 {
        eprintln!(
            "tesla-touch: warning: {} has mode {mode:o}, QtCar (user tesla) can't open it. Run as root.",
            node.display()
        );
    }
    if let Some(db) = udev_db {
        warn_if_host_uses_device(&db);
    }

    let bound = match &args.bind {
        Some(target) => {
            bind_mount(&node, target)?;
            Some(target.clone())
        }
        None => None,
    };

    if let Some((dev, name)) = source {
        let panel = args.panel.unwrap_or((width, height));
        let lb = passthrough::Letterbox::fit(width, height, panel.0, panel.1);
        let (tx, rx) = mpsc::channel();
        {
            let tx = tx.clone();
            ctrlc::set_handler(move || {
                let _ = tx.send(passthrough::Event::Quit);
            })?;
        }
        std::thread::spawn(move || passthrough::run(dev, panel, lb, tx));
        println!(
            "ready: {} ({width}x{height}) from {} \"{name}\", panel {}x{}",
            node.display(),
            args.from.as_ref().unwrap().display(),
            panel.0,
            panel.1
        );
        io::stdout().flush()?;
        let mut result = Ok(());
        loop {
            match rx.recv() {
                Ok(passthrough::Event::Frame(frame)) => {
                    if args.verbose {
                        eprintln!("frame {frame:?}");
                    }
                    ts.set(&frame)?;
                }
                Ok(passthrough::Event::Error(e)) => {
                    result = Err(e);
                    break;
                }
                Ok(passthrough::Event::Quit) | Err(_) => break,
            }
        }
        let _ = ts.release_all();
        if let Some(target) = bound {
            unmount(&target);
        }
        eprintln!("tesla-touch: stopped");
        if let Err(e) = result {
            return Err(e.into());
        }
        std::process::exit(0);
    }

    let (tx, rx) = mpsc::channel();
    {
        let tx = tx.clone();
        ctrlc::set_handler(move || {
            let _ = tx.send(Msg::Quit);
        })?;
    }
    record::spawn(&args.display, tx)?;
    if args.cursor
        && let Err(e) = cursor::spawn(&args.display, args.verbose) {
            eprintln!("tesla-touch: warning: cursor fix disabled: {e}");
        }

    println!("ready: {} ({width}x{height}) on display {}", node.display(), args.display);
    io::stdout().flush()?;

    let mut g = Gestures::new(width, height, args.long_press);
    loop {
        let timeout = if g.animating() { Duration::from_millis(5) } else { Duration::from_secs(3600) };
        match rx.recv_timeout(timeout) {
            Ok(Msg::Quit) | Err(RecvTimeoutError::Disconnected) => break,
            Ok(msg) => g.handle(msg),
            Err(RecvTimeoutError::Timeout) => {}
        }
        g.tick();
        for frame in g.take_frames() {
            if args.verbose {
                eprintln!("frame {frame:?}");
            }
            ts.set(&frame)?;
        }
    }

    let _ = ts.release_all();
    if let Some(target) = bound {
        unmount(&target);
    }
    eprintln!("tesla-touch: stopped");
    // the RECORD thread is blocked on the X connection; don't wait for it
    std::process::exit(0);
}

fn screen_size(display: &str) -> Result<(i32, i32), Box<dyn std::error::Error>> {
    // Xvfb may still be starting up
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match x11rb::connect(Some(display)) {
            Ok((conn, screen)) => {
                let s = &conn.setup().roots[screen];
                return Ok((s.width_in_pixels as i32, s.height_in_pixels as i32));
            }
            Err(e) if Instant::now() > deadline => {
                return Err(format!("can't connect to X display {display}: {e}").into());
            }
            Err(_) => std::thread::sleep(Duration::from_millis(200)),
        }
    }
}

// ---------------------------------------------------------------------------
// Gestures: turns pointer events into touch frames (desired finger positions).

type Frame = Vec<Option<(i32, i32)>>;

enum Mode {
    Idle,
    /// left button held: one finger follows the pointer
    Finger,
    /// right click: one finger held still until `until`
    LongPress { until: Instant },
    Pinch(Pinch),
}

/// Two fingers centred on `center`, spread horizontally; `sep` eases towards `target`.
struct Pinch {
    center: (f64, f64),
    sep: f64,
    target: f64,
    last_step: Instant,
    settled_at: Option<Instant>,
}

const PINCH_STEP: f64 = 1.25; // finger distance factor per wheel notch
const PINCH_START: f64 = 240.0;
const PINCH_MIN: f64 = 80.0;
const PINCH_MAX: f64 = 720.0;
const PINCH_FRAME: Duration = Duration::from_millis(8);
const PINCH_LINGER: Duration = Duration::from_millis(150); // keep fingers down for the next notch

struct Gestures {
    width: f64,
    long_press: Duration,
    pointer: (i32, i32),
    mode: Mode,
    frames: Vec<Frame>,
}

impl Gestures {
    fn new(width: i32, _height: i32, long_press: Duration) -> Self {
        Self { width: width as f64, long_press, pointer: (0, 0), mode: Mode::Idle, frames: Vec::new() }
    }

    fn animating(&self) -> bool {
        matches!(self.mode, Mode::LongPress { .. } | Mode::Pinch(_))
    }

    fn take_frames(&mut self) -> Vec<Frame> {
        std::mem::take(&mut self.frames)
    }

    /// Lift all fingers and go idle.
    fn lift(&mut self) {
        if !matches!(self.mode, Mode::Idle) {
            self.mode = Mode::Idle;
            self.frames.push(vec![]);
        }
    }

    fn handle(&mut self, msg: Msg) {
        let (button, pressed, x, y) = match msg {
            Msg::Motion { x, y } => {
                self.pointer = (x, y);
                if matches!(self.mode, Mode::Finger) {
                    self.frames.push(vec![Some((x, y))]);
                }
                return;
            }
            Msg::Button { button, pressed, x, y } => (button, pressed, x, y),
            Msg::Quit => return,
        };
        self.pointer = (x, y);
        match (button, pressed) {
            (1, true) => {
                self.lift(); // ends a running pinch / long press first
                self.mode = Mode::Finger;
                self.frames.push(vec![Some((x, y))]);
            }
            (1, false) if matches!(self.mode, Mode::Finger) => self.lift(),
            (3, true) if matches!(self.mode, Mode::Idle) => {
                self.mode = Mode::LongPress { until: Instant::now() + self.long_press };
                self.frames.push(vec![Some((x, y))]);
            }
            (4, true) | (5, true) if !matches!(self.mode, Mode::Finger) => self.wheel(button == 4),
            _ => {}
        }
    }

    fn wheel(&mut self, zoom_in: bool) {
        let factor = if zoom_in { PINCH_STEP } else { 1.0 / PINCH_STEP };
        if let Mode::Pinch(p) = &mut self.mode {
            let target = p.target * factor;
            if (PINCH_MIN..=PINCH_MAX).contains(&target) {
                p.target = target;
                p.settled_at = None;
                return;
            }
        }
        // no pinch running, or the fingers ran out of room: start a fresh one
        self.lift();
        let half = (PINCH_MAX / 2.0 + 10.0).min(self.width / 2.0);
        let center = ((self.pointer.0 as f64).clamp(half, self.width - half), self.pointer.1 as f64);
        self.mode = Mode::Pinch(Pinch {
            center,
            sep: PINCH_START,
            target: PINCH_START * factor,
            last_step: Instant::now(),
            settled_at: None,
        });
        self.push_pinch_frame();
    }

    fn push_pinch_frame(&mut self) {
        if let Mode::Pinch(p) = &self.mode {
            let (cx, cy) = p.center;
            let d = p.sep / 2.0;
            let y = cy.round() as i32;
            self.frames.push(vec![Some(((cx - d).round() as i32, y)), Some(((cx + d).round() as i32, y))]);
        }
    }

    fn tick(&mut self) {
        let now = Instant::now();
        match &mut self.mode {
            Mode::LongPress { until } if now >= *until => self.lift(),
            Mode::Pinch(p) if now.duration_since(p.last_step) >= PINCH_FRAME => {
                p.last_step = now;
                let delta = p.target - p.sep;
                if delta.abs() > 1.0 {
                    // ease towards the target, at least 4 px per frame
                    p.sep += (delta * 0.3).abs().clamp(4.0f64.min(delta.abs()), delta.abs()).copysign(delta);
                    self.push_pinch_frame();
                } else if now.duration_since(*p.settled_at.get_or_insert(now)) >= PINCH_LINGER {
                    self.lift();
                }
            }
            _ => {}
        }
    }
}

// ---------------------------------------------------------------------------

/// Bind-mount the event node into the chroot, so QtCar sees it at a fixed path without
/// exposing all of the host's /dev/input.
fn bind_mount(node: &Path, target: &Path) -> io::Result<()> {
    if !target.exists() {
        fs::File::create(target)?;
    }
    let src = CString::new(node.as_os_str().as_bytes())?;
    let dst = CString::new(target.as_os_str().as_bytes())?;
    let rc = unsafe { libc::mount(src.as_ptr(), dst.as_ptr(), std::ptr::null(), libc::MS_BIND, std::ptr::null()) };
    if rc != 0 {
        let e = io::Error::last_os_error();
        return Err(io::Error::new(e.kind(), format!("bind mount {} -> {}: {e}", node.display(), target.display())));
    }
    Ok(())
}

fn unmount(target: &Path) {
    if let Ok(dst) = CString::new(target.as_os_str().as_bytes()) {
        unsafe { libc::umount2(dst.as_ptr(), libc::MNT_DETACH) };
    }
}

/// Wait (up to 3 s) until udev has processed the device node; returns its udev database entry.
fn wait_for_udev(node: &Path) -> Option<String> {
    use std::os::unix::fs::MetadataExt;
    let rdev = fs::metadata(node).ok()?.rdev();
    let db_path = format!("/run/udev/data/c{}:{}", libc::major(rdev), libc::minor(rdev));
    if !Path::new("/run/udev/data").is_dir() {
        return None; // no udev (container etc.), nothing will touch the node
    }
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        // udev writes the database entry after it has set owner and mode
        if let Ok(db) = fs::read_to_string(&db_path) {
            return Some(db);
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    None
}

/// The host's own input stack (libinput on Wayland and Xorg) would otherwise treat the
/// virtual touchscreen as a real one, so touches meant for QtCar also hit the host desktop.
fn warn_if_host_uses_device(udev_db: &str) {
    let ignored = udev_db.lines().any(|l| l == "E:LIBINPUT_IGNORE_DEVICE=1");
    let is_input = udev_db.lines().any(|l| l == "E:ID_INPUT=1");
    if is_input && !ignored {
        eprintln!(
            "tesla-touch: warning: the host desktop may also react to these touches.\n\
             \x20            Install 99-tesla-touch.rules (see README) to hide the device from it."
        );
    }
}
