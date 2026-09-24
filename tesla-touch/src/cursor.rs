//! Keeps a visible arrow cursor on the display.
//!
//! QtCar hides the mouse cursor (it's a touch UI), so the VNC client shows nothing.
//! Instead of drawing a fake cursor into the framebuffer, set a real arrow cursor on
//! every window whenever the displayed cursor becomes blank. x11vnc then sends it to
//! the client as a normal cursor shape, drawn locally without lag.

use std::error::Error;
use std::thread;
use x11rb::connection::{Connection, RequestConnection};
use x11rb::protocol::xfixes::{self, ConnectionExt as _};
use x11rb::protocol::xproto::{ChangeWindowAttributesAux, ConnectionExt as _, Cursor, Window};
use x11rb::protocol::Event;

const XC_LEFT_PTR: u16 = 68; // from X11/cursorfont.h

pub fn spawn(display: &str, verbose: bool) -> Result<(), Box<dyn Error>> {
    let (conn, screen) = x11rb::connect(Some(display))?;
    let root = conn.setup().roots[screen].root;
    if conn.extension_information(xfixes::X11_EXTENSION_NAME)?.is_none() {
        return Err("X server has no XFIXES extension".into());
    }
    conn.xfixes_query_version(5, 0)?.reply()?;

    let font = conn.generate_id()?;
    conn.open_font(font, b"cursor")?;
    let cursor = conn.generate_id()?;
    conn.create_glyph_cursor(cursor, font, font, XC_LEFT_PTR, XC_LEFT_PTR + 1, 0, 0, 0, 0xffff, 0xffff, 0xffff)?;
    conn.close_font(font)?;
    conn.xfixes_select_cursor_input(root, xfixes::CursorNotifyMask::DISPLAY_CURSOR)?;
    apply(&conn, root, cursor)?;

    thread::spawn(move || {
        loop {
            match conn.wait_for_event() {
                Ok(Event::XfixesCursorNotify(_)) => {
                    if is_blank(&conn).unwrap_or(false) {
                        if verbose {
                            eprintln!("tesla-touch: cursor went blank, restoring arrow");
                        }
                        let _ = apply(&conn, root, cursor);
                    }
                }
                Ok(_) => {} // errors from windows that vanished meanwhile etc.
                Err(_) => return,
            }
        }
    });
    Ok(())
}

fn is_blank(conn: &impl Connection) -> Result<bool, Box<dyn Error>> {
    let img = conn.xfixes_get_cursor_image()?.reply()?;
    Ok(img.cursor_image.iter().all(|px| px >> 24 == 0))
}

/// Set the arrow on the root window and every window below it.
fn apply(conn: &impl Connection, root: Window, cursor: Cursor) -> Result<(), Box<dyn Error>> {
    let attrs = ChangeWindowAttributesAux::new().cursor(cursor);
    let mut todo = vec![root];
    while let Some(w) = todo.pop() {
        conn.change_window_attributes(w, &attrs)?.ignore_error();
        if let Ok(tree) = conn.query_tree(w)?.reply() {
            todo.extend(tree.children);
        }
    }
    conn.flush()?;
    Ok(())
}
