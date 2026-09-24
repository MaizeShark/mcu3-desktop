//! Virtual multitouch screen via uinput (MT protocol type B).

use evdev::uinput::{VirtualDevice, VirtualDeviceBuilder};
use evdev::{
    AbsInfo, AbsoluteAxisType, AttributeSet, BusType, EventType, InputEvent, InputId, Key,
    PropType, UinputAbsSetup,
};
use std::io;
use std::path::PathBuf;

pub const MAX_SLOTS: usize = 10;

pub struct TouchScreen {
    dev: VirtualDevice,
    pub node: PathBuf,
    width: i32,
    height: i32,
    /// per slot: (tracking id, x, y)
    slots: [Option<(i32, i32, i32)>; MAX_SLOTS],
    /// Last position the kernel stored per slot, kept across lifts. The kernel drops values
    /// equal to these, and QtCar's EvDevTouchDriver only reports a new touch once it has seen
    /// both X and Y for it -- so a touch that repeats a coordinate would be lost.
    kernel_pos: [(i32, i32); MAX_SLOTS],
    next_id: i32,
}

impl TouchScreen {
    pub fn new(name: &str, width: i32, height: i32) -> io::Result<Self> {
        let mut keys = AttributeSet::<Key>::new();
        keys.insert(Key::BTN_TOUCH);
        let mut props = AttributeSet::<PropType>::new();
        props.insert(PropType::DIRECT);

        let axis = |axis, max| UinputAbsSetup::new(axis, AbsInfo::new(0, 0, max, 0, 0, 0));
        let mut dev = VirtualDeviceBuilder::new()?
            .name(name)
            // Cypress vendor id, like the real cyttsp6 controller
            .input_id(InputId::new(BusType::BUS_USB, 0x04b4, 0x0001, 1))
            .with_keys(&keys)?
            .with_properties(&props)?
            .with_absolute_axis(&axis(AbsoluteAxisType::ABS_X, width - 1))?
            .with_absolute_axis(&axis(AbsoluteAxisType::ABS_Y, height - 1))?
            .with_absolute_axis(&axis(AbsoluteAxisType::ABS_MT_SLOT, MAX_SLOTS as i32 - 1))?
            .with_absolute_axis(&axis(AbsoluteAxisType::ABS_MT_TOUCH_MAJOR, 255))?
            .with_absolute_axis(&axis(AbsoluteAxisType::ABS_MT_WIDTH_MAJOR, 255))?
            .with_absolute_axis(&axis(AbsoluteAxisType::ABS_MT_POSITION_X, width - 1))?
            .with_absolute_axis(&axis(AbsoluteAxisType::ABS_MT_POSITION_Y, height - 1))?
            .with_absolute_axis(&axis(AbsoluteAxisType::ABS_MT_TRACKING_ID, 65535))?
            .build()?;

        let node = dev
            .enumerate_dev_nodes_blocking()?
            .find_map(|p| p.ok())
            .ok_or_else(|| io::Error::other("uinput device has no /dev/input/event* node"))?;

        Ok(Self { dev, node, width, height, slots: [None; MAX_SLOTS], kernel_pos: [(0, 0); MAX_SLOTS], next_id: 0 })
    }

    /// Set the complete touch state: `contacts[i]` is the position of the finger in slot i
    /// (None = lifted, slots past the end = lifted). Emits only what changed, as one frame.
    pub fn set(&mut self, contacts: &[Option<(i32, i32)>]) -> io::Result<()> {
        let was_touching = self.slots.iter().any(Option::is_some);
        let mut events = Vec::new();
        let abs = |code: AbsoluteAxisType, value| InputEvent::new(EventType::ABSOLUTE, code.0, value);

        for slot in 0..MAX_SLOTS {
            let want = contacts.get(slot).copied().flatten().map(|(x, y)| {
                (x.clamp(0, self.width - 1), y.clamp(0, self.height - 1))
            });
            match (self.slots[slot], want) {
                (None, Some((x, y))) => {
                    // make sure X and Y both get through (see `kernel_pos`), 1 px off at most
                    let (kx, ky) = self.kernel_pos[slot];
                    let x = differ_from(x, kx, self.width);
                    let y = differ_from(y, ky, self.height);
                    self.kernel_pos[slot] = (x, y);
                    let id = self.next_id;
                    self.next_id = (self.next_id + 1) % 65535;
                    events.push(abs(AbsoluteAxisType::ABS_MT_SLOT, slot as i32));
                    events.push(abs(AbsoluteAxisType::ABS_MT_TRACKING_ID, id));
                    events.push(abs(AbsoluteAxisType::ABS_MT_POSITION_X, x));
                    events.push(abs(AbsoluteAxisType::ABS_MT_POSITION_Y, y));
                    self.slots[slot] = Some((id, x, y));
                }
                (Some((id, ox, oy)), Some((x, y))) if (ox, oy) != (x, y) => {
                    events.push(abs(AbsoluteAxisType::ABS_MT_SLOT, slot as i32));
                    if x != ox {
                        events.push(abs(AbsoluteAxisType::ABS_MT_POSITION_X, x));
                    }
                    if y != oy {
                        events.push(abs(AbsoluteAxisType::ABS_MT_POSITION_Y, y));
                    }
                    self.slots[slot] = Some((id, x, y));
                    self.kernel_pos[slot] = (x, y);
                }
                (Some(_), None) => {
                    events.push(abs(AbsoluteAxisType::ABS_MT_SLOT, slot as i32));
                    events.push(abs(AbsoluteAxisType::ABS_MT_TRACKING_ID, -1));
                    self.slots[slot] = None;
                }
                _ => {}
            }
        }

        let touching = self.slots.iter().any(Option::is_some);
        if touching != was_touching {
            events.push(InputEvent::new(EventType::KEY, Key::BTN_TOUCH.code(), touching as i32));
        }
        if events.is_empty() {
            return Ok(());
        }
        self.dev.emit(&events) // appends SYN_REPORT
    }

    pub fn release_all(&mut self) -> io::Result<()> {
        self.set(&[])
    }
}

/// `value`, or a neighbouring pixel if it equals `previous`.
fn differ_from(value: i32, previous: i32, size: i32) -> i32 {
    match value {
        v if v != previous => v,
        v if v + 1 < size => v + 1,
        v => v - 1,
    }
}
