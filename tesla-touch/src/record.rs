//! Watches all pointer input on an X display with the RECORD extension.
//! Unlike polling XQueryPointer this sees every event, including the XTEST
//! events x11vnc injects for the VNC client, and costs nothing while idle.

use crate::Msg;
use std::error::Error;
use std::sync::mpsc::Sender;
use std::thread;
use x11rb::connection::{Connection, RequestConnection};
use x11rb::protocol::record::{self, ConnectionExt as _};
use x11rb::protocol::xproto;

const CATEGORY_FROM_SERVER: u8 = 0;

pub fn spawn(display: &str, tx: Sender<Msg>) -> Result<(), Box<dyn Error>> {
    // RECORD needs two connections: one to control the context, one that receives the data.
    let (ctrl, _) = x11rb::connect(Some(display))?;
    let (data, _) = x11rb::connect(Some(display))?;
    if ctrl.extension_information(record::X11_EXTENSION_NAME)?.is_none() {
        return Err("X server has no RECORD extension (start Xvfb with +extension RECORD)".into());
    }
    ctrl.record_query_version(1, 13)?.reply()?;

    let context = ctrl.generate_id()?;
    let range = record::Range {
        device_events: record::Range8 {
            first: xproto::BUTTON_PRESS_EVENT,
            last: xproto::MOTION_NOTIFY_EVENT,
        },
        ..Default::default()
    };
    ctrl.record_create_context(context, 0, &[record::CS::ALL_CLIENTS.into()], &[range])?
        .check()?;

    let display = display.to_owned();
    thread::spawn(move || {
        let _ctrl = ctrl; // the context lives as long as this connection
        let replies = match data.record_enable_context(context) {
            Ok(replies) => replies,
            Err(e) => {
                eprintln!("tesla-touch: RECORD enable failed: {e}");
                let _ = tx.send(Msg::Quit);
                return;
            }
        };
        for reply in replies {
            let reply = match reply {
                Ok(reply) => reply,
                Err(e) => {
                    eprintln!("tesla-touch: lost X connection: {e}");
                    break;
                }
            };
            if reply.category != CATEGORY_FROM_SERVER {
                continue;
            }
            // Device events are raw 32-byte core protocol events.
            for ev in reply.data.as_chunks::<32>().0 {
                if let Some(msg) = parse_event(ev)
                    && tx.send(msg).is_err() {
                        return;
                    }
            }
        }
        eprintln!("tesla-touch: X display {display} closed");
        let _ = tx.send(Msg::Quit);
    });
    Ok(())
}

fn parse_event(ev: &[u8]) -> Option<Msg> {
    // ButtonPress / ButtonRelease / MotionNotify share this layout:
    // type(1) detail(1) seq(2) time(4) root(4) event(4) child(4) root_x(2) root_y(2) ...
    let x = i16::from_ne_bytes([ev[20], ev[21]]) as i32;
    let y = i16::from_ne_bytes([ev[22], ev[23]]) as i32;
    match ev[0] & 0x7f {
        xproto::BUTTON_PRESS_EVENT => Some(Msg::Button { button: ev[1], pressed: true, x, y }),
        xproto::BUTTON_RELEASE_EVENT => Some(Msg::Button { button: ev[1], pressed: false, x, y }),
        xproto::MOTION_NOTIFY_EVENT => Some(Msg::Motion { x, y }),
        _ => None,
    }
}
