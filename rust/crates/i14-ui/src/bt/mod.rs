//! Bluetooth through MediaTek's /dev/stpbt: raw HCI in H4 framing (the stock kernel has no BlueZ).
//!
//! Opening the device powers Bluetooth on (WMT), closing powers it off. This module switches the
//! radio on/off, reads the controller's address, searches for nearby devices (classic inquiry
//! with names, and BLE advertisements), and pairs classic devices with Secure Simple Pairing
//! (`pair.rs`; link keys in `keys.rs`, polite L2CAP refusals in `l2cap.rs`, test state in
//! `state.rs`). Using devices (SDP, RFCOMM, HID, audio) and BLE pairing are not implemented.

use std::collections::{HashMap, VecDeque};
use std::fs::OpenOptions;
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::{AsRawFd, RawFd};
use std::path::PathBuf;

pub const DEV: &str = "/dev/stpbt";
mod h4;
pub mod keys;
pub mod l2cap;
pub mod pair;
pub mod state;
pub use h4::*;
pub use keys::{KeyStore, Paired};
pub use pair::{Confirm, Conn, Pairing};

/// The name other devices see, and the class of device "phone / smartphone".
pub const LOCAL_NAME: &str = "I14";
pub const CLASS_OF_DEVICE: [u8; 3] = [0x0C, 0x02, 0x5A];

pub fn addr_str(raw: &[u8]) -> String {
    raw.iter()
        .rev()
        .map(|b| format!("{b:02X}"))
        .collect::<Vec<_>>()
        .join(":")
}

/// Python-style slice `p[a:b]`: clamped, never panics.
fn sl(p: &[u8], a: usize, b: usize) -> &[u8] {
    let b = b.min(p.len());
    &p[a.min(b)..b]
}

/// Complete (0x09) or short (0x08) local name from EIR / advertising data.
pub fn eir_name(data: &[u8]) -> Option<String> {
    let (mut i, mut short) = (0usize, None);
    while i + 1 < data.len() {
        let n = data[i] as usize;
        if n == 0 || i + 1 + n > data.len() {
            break;
        }
        let (kind, value) = (data[i + 1], sl(data, i + 2, i + 1 + n));
        if kind == 0x09 {
            return Some(String::from_utf8_lossy(value).into_owned());
        }
        if kind == 0x08 {
            short = Some(String::from_utf8_lossy(value).into_owned());
        }
        i += 1 + n;
    }
    short
}

/// The opened Bluetooth device: anything readable/writable with an fd to select on.
pub trait Device: Read + Write + AsRawFd {}
impl<T: Read + Write + AsRawFd> Device for T {}

pub type Opener = Box<dyn FnMut(&str) -> io::Result<Box<dyn Device>>>;

fn open_dev(path: &str) -> io::Result<Box<dyn Device>> {
    let f = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOCTTY | libc::O_NONBLOCK)
        .open(path)?;
    Ok(Box::new(f))
}

/// time.monotonic(): CLOCK_MONOTONIC in seconds.
pub use i14_sys::monotonic;

/// `e.strerror or e`: the OS message without Rust's " (os error N)" suffix.
fn strerror(e: &io::Error) -> String {
    let s = e.to_string();
    match e.raw_os_error() {
        Some(code) => s
            .strip_suffix(&format!(" (os error {code})"))
            .unwrap_or(&s)
            .to_string(),
        None => s,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Start,
    Inquiry,
    Names,
    Le,
}

/// Who a command is for. Every queued command carries its owner, and `flush_cmds` (the only
/// way out of the queue) checks it as the command goes out: a command whose operation has ended
/// is dropped there, so nothing queued outlives what queued it. The owner also says what to do
/// with the command's completion; a result for an operation that has ended is ignored.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Owner {
    /// right whatever the state when it goes out: refusals and cancels only
    Any,
    Reset,
    /// one step of the controller set-up; the next step follows its completion. Its value is
    /// taken from the state when it goes out.
    Setup,
    /// the scan mode: written as visibility asks when it goes out, dropped when already in effect
    Scan,
    /// a positive answer to a controller event: checked again as it goes out (`vet_reply`)
    Reply,
    /// a newly paired device's name: only while its key is stored and no search runs
    PairedName,
    /// one step of a search (`search_id`): an inquiry, one name, or the LE scan
    Search(u64),
    /// pairing `id`'s Create Connection: until it has a link or accepted the device's own
    Connect(u64),
    /// pairing `id`'s Authentication Requested
    Auth(u64),
    /// a Disconnect of the connection with this serial (a handle may be reused)
    Link(u64),
    /// the user's "yes" under consent `id`: once that has ended it goes out as a "no"
    Consent(u64),
}

/// The controller set-up after a reset, one command at a time. Secure Connections is skipped when
/// the controller does not list the command; the scan mode depends on visibility.
const SETUP: [u16; 12] = [
    READ_BD_ADDR,
    SET_EVENT_MASK,
    WRITE_INQUIRY_MODE,
    READ_LOCAL_VERSION,
    READ_LOCAL_COMMANDS,
    READ_BUFFER_SIZE,
    WRITE_SIMPLE_PAIRING_MODE,
    WRITE_SC_HOST_SUPPORT,
    WRITE_LOCAL_NAME,
    WRITE_CLASS_OF_DEVICE,
    WRITE_EIR,
    WRITE_SCAN_ENABLE,
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Found {
    pub name: Option<String>,
    pub rssi: Option<i32>,
    pub kind: &'static str,
    pub psrm: Option<u8>,
}

pub struct Bluetooth {
    pub dev: String,
    pub opener: Opener,
    pub clock: fn() -> f64,
    pub device: Option<Box<dyn Device>>,
    pub packets: Packets,
    /// address -> found device, in insertion order (Python dict)
    pub devices: Vec<(String, Found)>,
    pub address: String,
    pub phase: Option<Phase>,
    pub phase_end: f64,
    pub names_todo: Vec<String>,
    pub message: String,
    pub changed: bool,
    /// sent commands still due a Command Complete / Status, oldest first: the controller answers
    /// commands of one opcode in order, so a result belongs to the oldest of its opcode
    waiting: VecDeque<(u16, Owner)>,
    /// commands waiting for a command credit (HCI flow control, Core spec Vol 4 Part E §4.4)
    cmd_queue: VecDeque<(Vec<u8>, Owner)>,
    /// the latest operation id (search step, pairing, connection, consent; see `Owner`)
    op_id: u64,
    /// the running search step's id
    search_id: u64,
    /// the device whose name the running Names step asked for: only its answer moves on
    name_asked: Option<String>,
    /// completions still due for operations that have ended: a late connection is closed, and a
    /// new pairing with that device waits until they are in (see `pair::Late`)
    abandoned: Vec<pair::Late>,
    /// commands the controller accepts now: Num_HCI_Command_Packets of the last Command
    /// Complete / Command Status, one less per command sent; independent of ACL credits
    cmd_credits: u8,
    /// set-up commands still to send after the reset
    setup: Vec<u16>,
    /// reset and set-up done: commands like INQUIRY may go
    pub ready: bool,
    /// switched on by a search, visibility or a pairing: off again once idle
    pub auto_on: bool,
    /// HCI version from Read Local Version
    pub version: Option<u8>,
    /// Simple Pairing mode accepted by the controller
    pub ssp: bool,
    sc_supported: bool,
    /// Secure Connections host support accepted
    pub sc: bool,
    scan_sent: Option<u8>,
    pub keys: KeyStore,
    pub conns: Vec<Conn>,
    /// a code on screen waiting for "Koppelen" / "Weigeren"
    pub pending: Option<Confirm>,
    pub visible_until: Option<f64>,
    pub pairing: Option<Pairing>,
    /// the device whose code the user accepted, until when: only its next link key is stored
    confirmed: Option<pair::Consent>,
    /// the screen is PIN-locked (set by the app): a pairing code cannot be accepted
    pub ui_locked: bool,
    names: HashMap<String, String>,
    remote_io: HashMap<String, u8>,
    reasm: l2cap::Reassembly,
    pub acl_out: l2cap::AclOut,
    /// a confirmation arrived: the runner switches the screen on
    pub wake: bool,
    pub state_path: Option<PathBuf>,
    last_state: String,
}

impl Default for Bluetooth {
    fn default() -> Self {
        Self::new()
    }
}

impl Bluetooth {
    /// The phone's Bluetooth: /dev/stpbt, keys in the data directory, state in /run.
    pub fn new() -> Self {
        let mut bt = Self::with_opener(DEV, Box::new(open_dev));
        bt.keys = KeyStore::new(Some(PathBuf::from(keys::KEYS_FILE)));
        if let Some(m) = bt.keys.load() {
            bt.message = m;
        }
        bt.state_path = Some(PathBuf::from(state::STATE_FILE));
        bt
    }

    /// For tests: any device, keys in memory, no state file.
    pub fn with_opener(dev: &str, opener: Opener) -> Self {
        Bluetooth {
            dev: dev.to_string(),
            opener,
            clock: monotonic,
            device: None,
            packets: Packets::new(),
            devices: Vec::new(),
            address: String::new(),
            phase: None,
            phase_end: 0.0,
            names_todo: Vec::new(),
            message: String::new(),
            changed: true,
            waiting: VecDeque::new(),
            cmd_queue: VecDeque::new(),
            op_id: 0,
            search_id: 0,
            name_asked: None,
            abandoned: Vec::new(),
            cmd_credits: 0,
            setup: Vec::new(),
            ready: false,
            auto_on: false,
            version: None,
            ssp: false,
            sc_supported: false,
            sc: false,
            scan_sent: None,
            keys: KeyStore::new(None),
            conns: Vec::new(),
            pending: None,
            visible_until: None,
            pairing: None,
            confirmed: None,
            ui_locked: false,
            names: HashMap::new(),
            remote_io: HashMap::new(),
            reasm: l2cap::Reassembly::default(),
            acl_out: l2cap::AclOut::default(),
            wake: false,
            state_path: None,
            last_state: String::new(),
        }
    }

    pub fn on(&self) -> bool {
        self.device.is_some()
    }

    pub fn fd(&self) -> Option<RawFd> {
        self.device.as_ref().map(|d| d.as_raw_fd())
    }

    pub fn fileno(&self) -> Option<RawFd> {
        self.fd()
    }

    /// Writes one H4 packet (never blocks); on a write error Bluetooth is switched off.
    fn write_raw(&mut self, buf: &[u8]) -> bool {
        let Some(dev) = self.device.as_mut() else {
            return false;
        };
        let r = loop {
            match dev.write(buf) {
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                r => break r,
            }
        };
        match r {
            Ok(_) => true,
            Err(e) => {
                self.power(false);
                self.message = format!("Bluetooth-fout: {}", strerror(&e));
                false
            }
        }
    }

    /// Queue one HCI command (never blocks): it goes out once the controller has a command
    /// credit and only while its owner is current; on a write error Bluetooth is switched off.
    fn send(&mut self, opcode: u16, params: &[u8], owner: Owner) -> bool {
        if self.device.is_none() {
            return false;
        }
        self.cmd_queue.push_back((command(opcode, params), owner));
        self.flush_cmds()
    }

    /// Writes queued commands while the controller has credits; false after a write error.
    /// The only way out of the queue: each command is checked against the state of now.
    fn flush_cmds(&mut self) -> bool {
        while self.cmd_credits > 0 {
            let Some((pkt, owner)) = self.cmd_queue.pop_front() else {
                break;
            };
            let Some(pkt) = self.vet(pkt, owner) else {
                continue; // its operation has ended
            };
            if !self.write_raw(&pkt) {
                return false;
            }
            self.cmd_credits -= 1;
            // every command, so a completion is never taken for an older one of its opcode
            self.waiting
                .push_back((u16::from_le_bytes([pkt[1], pkt[2]]), owner));
        }
        true
    }

    /// The command to write for a queued one: None when its owner has ended. An answer the
    /// controller waits for is never dropped: it becomes the refusal when what it relied on
    /// (consent, visibility, the pairing, the key) is gone or changed. A command that sets state
    /// carries the value of now, never the one of when it was queued.
    fn vet(&mut self, pkt: Vec<u8>, owner: Owner) -> Option<Vec<u8>> {
        let op = u16::from_le_bytes([pkt[1], pkt[2]]);
        match owner {
            Owner::Any => Some(pkt),
            Owner::Reply => Some(self.vet_reply(pkt)),
            Owner::Setup => {
                let params = self.setup_params(op);
                if op == WRITE_SCAN_ENABLE {
                    self.scan_sent = params.first().copied();
                }
                Some(command(op, &params))
            }
            Owner::Scan => {
                let want = self.scan_mode();
                (self.scan_sent != Some(want)).then(|| {
                    self.scan_sent = Some(want);
                    command(WRITE_SCAN_ENABLE, &[want])
                })
            }
            Owner::PairedName => {
                let addr = addr_str(sl(&pkt, 4, 10));
                (self.phase.is_none() && self.keys.get(&addr).is_some()).then_some(pkt)
            }
            Owner::Consent(id) if !self.current(owner) => Some(self.refuse_yes(id, &pkt)),
            _ => self.current(owner).then_some(pkt),
        }
    }

    /// Whether the operation that queued a command is still the one running.
    fn current(&self, owner: Owner) -> bool {
        let pairing = |id| self.pairing.as_ref().filter(|p| p.id == id);
        match owner {
            // checked (or given their value) in `vet`
            Owner::Any
            | Owner::Reset
            | Owner::Setup
            | Owner::Scan
            | Owner::Reply
            | Owner::PairedName => true,
            Owner::Search(id) => self.phase.is_some() && id == self.search_id,
            Owner::Connect(id) => pairing(id).is_some_and(|p| p.handle.is_none() && !p.incoming),
            Owner::Auth(id) => pairing(id).is_some(),
            Owner::Link(serial) => self.conns.iter().any(|c| c.serial == serial),
            Owner::Consent(id) => {
                let now = (self.clock)();
                !self.ui_locked
                    && self
                        .confirmed
                        .as_ref()
                        .is_some_and(|c| c.id == id && now <= c.until)
            }
        }
    }

    /// A command of `owner` waits for a credit and will still go out.
    fn unsent(&self, owner: Owner) -> bool {
        self.current(owner) && self.cmd_queue.iter().any(|(_, o)| *o == owner)
    }

    pub fn power(&mut self, on: bool) -> bool {
        if on && self.device.is_none() {
            match (self.opener)(&self.dev) {
                Ok(d) => self.device = Some(d),
                Err(e) => {
                    self.message = format!("Bluetooth starten mislukt: {}", strerror(&e));
                    self.changed = true;
                    return false;
                }
            }
            self.packets = Packets::new();
            self.ready = false;
            self.scan_sent = None;
            // after power-on the host may send one command: the reset (HCI §4.4)
            self.cmd_queue.clear();
            self.cmd_credits = 1;
            self.message = "Bluetooth aan".into();
            if let Some(m) = self.keys.load() {
                self.message = m;
            }
            self.send(RESET, b"", Owner::Reset);
            if self.device.is_none() {
                // the reset could not be sent: send switched it off with the reason
                self.changed = true;
                return false;
            }
        } else if !on && self.device.is_some() {
            if let Some(c) = self.pending.take() {
                // refuse before the radio goes, but only with a command credit (HCI §4.4):
                // without one the closing radio ends the pairing anyway
                if self.cmd_credits > 0 {
                    let params = keys::addr_bytes(&c.addr).unwrap_or_default();
                    self.write_raw(&command(USER_CONFIRM_NEG_REPLY, &params));
                }
            }
            self.auto_on = false;
            self.device = None; // closing the fd; close errors are ignored
            self.phase = None;
            self.waiting.clear();
            self.cmd_queue.clear();
            self.cmd_credits = 0;
            self.setup.clear();
            self.ready = false;
            self.conns.clear();
            self.pending = None;
            self.pairing = None;
            self.visible_until = None;
            self.confirmed = None;
            self.abandoned.clear();
            self.remote_io.clear();
            self.reasm.clear();
            self.acl_out.reset();
            self.message = "Bluetooth uit".into();
        }
        self.changed = true;
        true
    }

    fn complete(&mut self, op: u16, owner: Owner, status: u8, params: &[u8]) {
        match owner {
            Owner::Reset => self.after_reset(status),
            Owner::Setup => self.setup_done(op, status, params),
            Owner::Search(id) if op == INQUIRY => {
                if status != 0 && self.phase == Some(Phase::Inquiry) && id == self.search_id {
                    self.message = format!("zoeken geweigerd (status {status})");
                    self.start_le();
                }
            }
            Owner::Connect(_) | Owner::Auth(_) => self.link_command_status(owner, status),
            _ => {}
        }
    }

    fn after_reset(&mut self, status: u8) {
        if status != 0 {
            // a controller that refuses its reset is never ready: off, with the reason
            self.phase = None;
            self.power(false);
            self.message = format!("Bluetooth-reset mislukt (status {status})");
            return;
        }
        self.setup = SETUP.iter().rev().copied().collect();
        self.next_setup();
    }

    fn setup_params(&self, op: u16) -> Vec<u8> {
        match op {
            SET_EVENT_MASK => 0x3DBFF807FFFBFFFFu64.to_le_bytes().to_vec(),
            // results with RSSI and names where possible
            WRITE_INQUIRY_MODE => vec![0x02],
            WRITE_SIMPLE_PAIRING_MODE | WRITE_SC_HOST_SUPPORT => vec![0x01],
            WRITE_LOCAL_NAME => {
                let mut v = LOCAL_NAME.as_bytes().to_vec();
                v.resize(248, 0);
                v
            }
            WRITE_CLASS_OF_DEVICE => CLASS_OF_DEVICE.to_vec(),
            WRITE_EIR => {
                // FEC not required, then the complete local name
                let mut v = vec![0x00, LOCAL_NAME.len() as u8 + 1, 0x09];
                v.extend_from_slice(LOCAL_NAME.as_bytes());
                v.resize(241, 0);
                v
            }
            WRITE_SCAN_ENABLE => vec![self.scan_mode()],
            _ => Vec::new(),
        }
    }

    fn next_setup(&mut self) {
        while let Some(op) = self.setup.pop() {
            if op == WRITE_SC_HOST_SUPPORT && !(self.ssp && self.sc_supported) {
                continue;
            }
            // the value is taken when it goes out (`vet`)
            self.send(op, &self.setup_params(op), Owner::Setup);
            return;
        }
    }

    fn setup_done(&mut self, op: u16, status: u8, params: &[u8]) {
        match op {
            READ_BD_ADDR => {
                self.address = if status == 0 {
                    addr_str(sl(params, 0, 6))
                } else {
                    String::new()
                }
            }
            READ_LOCAL_VERSION => {
                self.version = (status == 0).then(|| params.first().copied()).flatten()
            }
            READ_LOCAL_COMMANDS => {
                self.sc_supported = status == 0
                    && params
                        .get(SC_HOST_SUPPORT_OCTET)
                        .is_some_and(|b| b & SC_HOST_SUPPORT_MASK != 0);
            }
            READ_BUFFER_SIZE => {
                let (mtu, n) = if status == 0 && params.len() >= 5 {
                    (
                        u16::from_le_bytes([params[0], params[1]]) as usize,
                        u16::from_le_bytes([params[3], params[4]]) as usize,
                    )
                } else {
                    (0, 0)
                };
                // a controller that will not say: the smallest legal buffer, one at a time
                if mtu == 0 || n == 0 {
                    self.acl_out.set_buffers(27, 1);
                } else {
                    self.acl_out.set_buffers(mtu, n);
                }
            }
            WRITE_SIMPLE_PAIRING_MODE => {
                self.ssp = status == 0;
                if status != 0 {
                    self.message = format!("koppelen niet ondersteund (status {status})");
                }
            }
            WRITE_SC_HOST_SUPPORT => self.sc = status == 0,
            WRITE_SCAN_ENABLE => {
                self.ready = true;
                self.apply_scan(); // visibility changed while the set-up ran
                if self.phase == Some(Phase::Start) {
                    // a search waited for the radio
                    self.inquiry();
                }
                self.start_pairing();
                return;
            }
            _ => {}
        }
        self.next_setup();
    }

    /// Search; a radio switched on just for this goes off again when the search ends. The inquiry
    /// starts only once the controller has finished its reset and set-up (one command at a time).
    pub fn search(&mut self) {
        if self.device.is_none() {
            if !self.power(true) {
                return;
            }
            self.auto_on = true;
        }
        self.devices.clear();
        self.names_todo.clear();
        self.message = "zoeken...".into();
        self.changed = true;
        if !self.ready {
            self.new_step(Phase::Start);
            self.phase_end = (self.clock)() + 5.0;
            return;
        }
        self.inquiry();
    }

    /// Starts a search step: the commands of earlier steps still waiting for a credit are no
    /// longer current (see `Owner::Search`).
    fn new_step(&mut self, phase: Phase) {
        self.phase = Some(phase);
        self.op_id += 1;
        self.search_id = self.op_id;
        self.name_asked = None;
    }

    /// Cancels the running step's inquiry or LE scan once its start has gone out; while it
    /// still waits for a credit, ending the step is enough.
    fn cancel_step(&mut self) {
        let cancel: Option<(u16, &[u8])> = match self.phase {
            Some(Phase::Inquiry) => Some((INQUIRY_CANCEL, b"")),
            Some(Phase::Le) => Some((LE_SCAN_ENABLE, b"\x00\x00")),
            _ => None,
        };
        if let Some((op, params)) = cancel.filter(|_| !self.unsent(Owner::Search(self.search_id))) {
            // Any: only stops a scan, queued before any later start; a stopped one just refuses
            self.send(op, params, Owner::Any);
        }
    }

    fn inquiry(&mut self) {
        self.names_todo.clear();
        self.new_step(Phase::Inquiry);
        self.phase_end = (self.clock)() + INQUIRY_S + 3.0;
        self.message = "zoeken...".into();
        // general inquiry access code 0x9E8B33, 8 x 1.28 s, unlimited responses
        self.send(
            INQUIRY,
            b"\x33\x8b\x9e\x08\x00",
            Owner::Search(self.search_id),
        );
        self.changed = true;
    }

    fn start_le(&mut self) {
        self.new_step(Phase::Le);
        self.phase_end = (self.clock)() + LE_S;
        let me = Owner::Search(self.search_id);
        self.send(LE_SET_EVENT_MASK, &0x1Fu64.to_le_bytes(), me);
        // active scan, 100 ms interval/window, public address, accept all
        self.send(LE_SCAN_PARAMS, &[1, 0xA0, 0x00, 0xA0, 0x00, 0, 0], me);
        self.send(LE_SCAN_ENABLE, b"\x01\x00", me);
    }

    /// A pairing needs the radio: a running search ends early (its results stay). Its commands
    /// still waiting for a credit are not sent; what already went out is cancelled.
    pub(super) fn stop_search(&mut self) {
        self.cancel_step();
        self.phase = None;
        self.names_todo.clear();
    }

    fn finish(&mut self) {
        self.cancel_step();
        self.phase = None;
        if self.auto_on && self.idle() {
            self.auto_on = false;
            self.power(false);
        }
        let n = self.devices.len();
        self.message = if n == 1 {
            format!("{n} apparaat gevonden")
        } else {
            format!("{n} apparaten gevonden")
        };
        self.changed = true;
    }

    fn next_name(&mut self) {
        if !self.names_todo.is_empty() {
            let addr = self.names_todo.remove(0);
            self.new_step(Phase::Names);
            let mut params: Vec<u8> = addr
                .split(':')
                .rev()
                .map(|h| u8::from_str_radix(h, 16).unwrap_or(0))
                .collect();
            let mode = self
                .devices
                .iter()
                .find(|(a, _)| *a == addr)
                .and_then(|(_, d)| d.psrm)
                .unwrap_or(1);
            self.phase_end = (self.clock)() + 6.0;
            params.extend_from_slice(&[mode, 0, 0, 0]);
            self.name_asked = Some(addr);
            self.send(REMOTE_NAME, &params, Owner::Search(self.search_id));
            return;
        }
        self.start_le();
    }

    fn seen(
        &mut self,
        raw: &[u8],
        rssi: Option<i32>,
        kind: &'static str,
        name: Option<String>,
        psrm: Option<u8>,
    ) {
        let addr = addr_str(raw);
        let i = match self.devices.iter().position(|(a, _)| *a == addr) {
            Some(i) => i,
            None => {
                self.devices.push((
                    addr,
                    Found {
                        name: None,
                        rssi,
                        kind,
                        psrm: None,
                    },
                ));
                self.devices.len() - 1
            }
        };
        let d = &mut self.devices[i].1;
        if rssi.is_some() {
            d.rssi = rssi;
        }
        if let Some(n) = name.filter(|n| !n.is_empty()) {
            d.name = Some(n);
        }
        if psrm.is_some() {
            d.psrm = psrm;
        }
        self.changed = true;
    }

    pub fn read(&mut self) {
        let Some(dev) = self.device.as_mut() else {
            return;
        };
        let mut buf = [0u8; 4096];
        let r = loop {
            match dev.read(&mut buf) {
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                r => break r,
            }
        };
        let n = match r {
            Ok(n) => n,
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => return,
            Err(e) => {
                self.message = format!("Bluetooth-fout: {}", strerror(&e));
                self.power(false);
                return;
            }
        };
        for packet in self.packets.feed(&buf[..n]) {
            if self.device.is_none() {
                break; // a handler switched Bluetooth off: the rest is stale
            }
            match packet {
                Pkt::Event(code, p) => self.handle(code, &p),
                Pkt::Acl { handle, pb, data } => self.acl(handle, pb, &data),
            }
        }
    }

    /// What waits for the oldest sent command with this opcode.
    fn take_waiter(&mut self, op: u16) -> Option<Owner> {
        let i = self.waiting.iter().position(|(o, _)| *o == op)?;
        self.waiting.remove(i).map(|(_, t)| t)
    }

    pub fn handle(&mut self, code: u8, p: &[u8]) {
        self.changed = true;
        if code == EV_CMD_COMPLETE && p.len() >= 3 {
            // opcode 0 (no status) only hands out credits
            self.cmd_credits = p[0];
            let op = u16::from_le_bytes([p[1], p[2]]);
            if p.len() >= 4 {
                if let Some(t) = self.take_waiter(op) {
                    self.complete(op, t, p[3], &p[4..]);
                }
            }
            self.flush_cmds();
        } else if code == EV_CMD_STATUS && p.len() >= 4 {
            self.cmd_credits = p[1];
            let op = u16::from_le_bytes([p[2], p[3]]);
            if let Some(t) = self.take_waiter(op) {
                self.complete(op, t, p[0], b"");
            }
            self.flush_cmds();
        } else if self.handle_link(code, p) {
        } else if matches!(code, EV_INQ_RESULT | EV_INQ_RSSI | EV_EXT_INQ) && !p.is_empty() {
            let n = if code == EV_EXT_INQ { 1 } else { p[0] as usize };
            let size = 14;
            for i in 0..n {
                let off = 1 + i * size;
                let rec = sl(p, off, off + size);
                if rec.len() < size {
                    break;
                }
                let rssi = if code != EV_INQ_RESULT {
                    Some(rec[13] as i8 as i32)
                } else {
                    None
                };
                let name = if code == EV_EXT_INQ {
                    eir_name(sl(p, 1 + size, p.len()))
                } else {
                    None
                };
                self.seen(&rec[..6], rssi, "klassiek", name, Some(rec[6]));
            }
        } else if code == EV_INQ_COMPLETE && self.phase == Some(Phase::Inquiry) {
            self.names_todo = self
                .devices
                .iter()
                .filter(|(_, d)| d.name.is_none())
                .map(|(a, _)| a.clone())
                .collect();
            self.next_name();
        } else if code == EV_NAME && p.len() >= 7 {
            let addr = addr_str(&p[1..7]);
            if p[0] == 0 {
                let rest = &p[7..];
                let end = rest.iter().position(|&b| b == 0).unwrap_or(rest.len());
                let name = String::from_utf8_lossy(&rest[..end]).into_owned();
                if !name.is_empty() {
                    self.names.insert(addr.clone(), name.clone());
                    if let Err(m) = self.keys.set_name(&addr, &name) {
                        self.message = m;
                    }
                }
                // a name asked for a paired device outside a search is not a search result
                if self.phase == Some(Phase::Names) || self.devices.iter().any(|(a, _)| *a == addr)
                {
                    self.seen(&p[1..7], None, "klassiek", Some(name), None);
                }
            }
            // only the running step's answer moves on; a late one only adds its name
            if self.phase == Some(Phase::Names) && self.name_asked.as_ref() == Some(&addr) {
                self.next_name();
            }
        } else if code == EV_LE && !p.is_empty() && p[0] == 0x02 {
            // LE advertising report(s)
            let (n, mut off) = (sl(p, 1, 2).first().copied().unwrap_or(0), 2usize);
            for _ in 0..n {
                if off + 9 > p.len() {
                    break;
                }
                let (raw, ln) = (&p[off + 2..off + 8], p[off + 8] as usize);
                let data = sl(p, off + 9, off + 9 + ln);
                let rssi = sl(p, off + 9 + ln, off + 10 + ln)
                    .first()
                    .copied()
                    .unwrap_or(0) as i8 as i32;
                self.seen(raw, Some(rssi), "BLE", eir_name(data), None);
                off += 10 + ln;
            }
        }
    }

    pub fn tick(&mut self) {
        self.step();
        self.publish();
    }

    /// `tick` without publishing: the runner publishes once the screen's lock state is known.
    pub fn step(&mut self) {
        if self.device.is_some() {
            let now = (self.clock)();
            self.tick_link(now);
            self.tick_search(now);
            if self.auto_on && self.device.is_some() && self.idle() {
                // switched on for a search, visibility or a pairing that has ended
                let m = std::mem::take(&mut self.message);
                self.power(false);
                self.message = m;
            }
        }
    }

    fn tick_search(&mut self, now: f64) {
        let Some(phase) = self.phase else { return };
        if now <= self.phase_end {
            return;
        }
        match phase {
            Phase::Inquiry => {
                self.cancel_step();
                self.handle(EV_INQ_COMPLETE, b"\x00");
            }
            Phase::Start => {
                self.message = "Bluetooth reageert niet".into();
                self.phase = None;
                if self.auto_on {
                    self.power(false);
                    self.message = "Bluetooth reageert niet".into();
                }
            }
            Phase::Names => self.next_name(),
            Phase::Le => self.finish(),
        }
    }

    /// Writes the state file when it changed (errors are ignored: it is only for tests).
    pub fn publish(&mut self) {
        let Some(path) = self.state_path.clone() else {
            return;
        };
        let s = state::render(self, (self.clock)());
        if s != self.last_state && keys::write_atomic_mode(&path, &s, 0o644).is_ok() {
            self.last_state = s;
        }
    }

    /// Tests elsewhere in the crate: power on and complete reset + set-up as a controller would.
    /// The fake controller advertises TEST_CREDITS command credits.
    #[cfg(test)]
    pub(crate) fn ready_for_tests(&mut self) {
        self.power(true);
        let mut ops = vec![RESET];
        ops.extend(SETUP.iter().filter(|&&o| o != WRITE_SC_HOST_SUPPORT));
        for op in ops {
            let mut p = vec![TEST_CREDITS];
            p.extend_from_slice(&op.to_le_bytes());
            p.push(0);
            match op {
                READ_BD_ADDR => p.extend_from_slice(&[1, 0x21, 3, 0x46, 0, 0]),
                READ_BUFFER_SIZE => p.extend_from_slice(&[0xFD, 0x03, 64, 8, 0, 8, 0]),
                _ => {}
            }
            self.handle(EV_CMD_COMPLETE, &p);
        }
        assert!(self.ready);
    }

    /// Devices with a name first, then by signal strength: (address, name, rssi, kind).
    pub fn listing(&self) -> Vec<(String, String, Option<i32>, &'static str)> {
        let mut items: Vec<_> = self
            .devices
            .iter()
            .map(|(a, d)| {
                (
                    a.clone(),
                    d.name.clone().unwrap_or_default(),
                    d.rssi,
                    d.kind,
                )
            })
            .collect();
        items.sort_by_key(|t| (t.1.is_empty(), -t.2.unwrap_or(-200)));
        items
    }
}

/// Num_HCI_Command_Packets the fake controller in tests advertises: enough that tests need not
/// answer every command (flow control itself is tested with 0 and 1).
#[cfg(test)]
pub(crate) const TEST_CREDITS: u8 = 16;

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixStream;

    struct T {
        b: UnixStream,
        bt: Bluetooth,
    }

    impl Drop for T {
        fn drop(&mut self) {
            self.bt.power(false);
        }
    }

    fn setup() -> T {
        let (a, b) = UnixStream::pair().unwrap();
        a.set_nonblocking(true).unwrap();
        let opener: Opener =
            Box::new(move |_p: &str| Ok(Box::new(a.try_clone()?) as Box<dyn Device>));
        T {
            b,
            bt: Bluetooth::with_opener(DEV, opener),
        }
    }

    impl T {
        fn sent(&mut self) -> Vec<u8> {
            self.b.set_nonblocking(true).unwrap();
            let mut data = Vec::new();
            let mut buf = [0u8; 4096];
            loop {
                match self.b.read(&mut buf) {
                    Ok(0) => return data,
                    Ok(n) => data.extend_from_slice(&buf[..n]),
                    Err(e) if e.kind() == io::ErrorKind::WouldBlock => return data,
                    Err(e) => panic!("{e}"),
                }
            }
        }

        fn reply(&mut self, data: &[u8]) {
            self.b.write_all(data).unwrap();
            self.bt.read();
        }

        fn complete(&mut self, opcode: u16, params: &[u8]) {
            let mut p = vec![TEST_CREDITS];
            p.extend_from_slice(&opcode.to_le_bytes());
            p.push(0);
            p.extend_from_slice(params);
            let mut pkt = vec![4, 0x0E, p.len() as u8];
            pkt.extend_from_slice(&p);
            self.reply(&pkt);
        }

        fn ready(&mut self) {
            self.bt.power(true);
            self.complete(RESET, b"");
            self.finish_setup(false);
            self.sent();
        }

        /// Completes every set-up step after the reset, in order, with plausible replies.
        fn finish_setup(&mut self, sc: bool) {
            for op in SETUP {
                if op == WRITE_SC_HOST_SUPPORT && !sc {
                    continue;
                }
                let params = setup_reply(op, sc);
                self.complete(op, &params);
            }
        }

        fn event(&mut self, code: u8, params: &[u8]) {
            let mut pkt = vec![4, code, params.len() as u8];
            pkt.extend_from_slice(params);
            self.reply(&pkt);
        }

        /// Python's `self.b.close()`: the other end is gone, writes fail.
        fn close_b(&mut self) {
            self.b.shutdown(std::net::Shutdown::Both).unwrap();
            let (_x, y) = UnixStream::pair().unwrap();
            self.b = y; // drops the original peer socket
        }
    }

    fn contains(hay: &[u8], needle: &[u8]) -> bool {
        hay.windows(needle.len()).any(|w| w == needle)
    }

    fn inquiry_cmd() -> Vec<u8> {
        command(INQUIRY, b"\x33\x8b\x9e\x08\x00")
    }

    #[test]
    fn power_on_resets_and_reads_the_address() {
        let mut t = setup();
        assert!(t.bt.power(true));
        assert_eq!(t.sent(), command(RESET, b""));
        t.complete(RESET, b"");
        assert!(t.sent().starts_with(&command(READ_BD_ADDR, b"")));
        t.complete(READ_BD_ADDR, &[6, 5, 4, 3, 2, 1]);
        assert_eq!(t.bt.address, "01:02:03:04:05:06");
    }

    #[test]
    fn search_waits_for_the_reset_when_it_switches_bluetooth_on() {
        let mut t = setup();
        t.bt.search();
        assert_eq!(t.sent(), command(RESET, b"")); // no inquiry before the reset has finished
        assert_eq!(t.bt.phase, Some(Phase::Start));
        t.complete(RESET, b"");
        assert!(!contains(&t.sent(), &inquiry_cmd()));
        t.finish_setup(false);
        assert!(t.sent().ends_with(&inquiry_cmd()));
        assert_eq!(t.bt.phase, Some(Phase::Inquiry));
    }

    #[test]
    fn a_silent_controller_ends_the_search() {
        let mut t = setup();
        t.bt.search();
        t.bt.phase_end = 0.0;
        t.bt.tick();
        assert_eq!(
            (t.bt.phase, t.bt.on(), t.bt.message.as_str()),
            (None, false, "Bluetooth reageert niet")
        );
    }

    #[test]
    fn search_classic_names_then_le() {
        let mut t = setup();
        t.ready();
        t.bt.search();
        assert!(t.sent().starts_with(&inquiry_cmd()));
        // extended inquiry result with a name, an RSSI result without one
        let mut eir = vec![7, 0x09];
        eir.extend_from_slice(b"Toets1");
        eir.extend_from_slice(&[0; 232]);
        let mut rec = vec![1, 1, 1, 1, 1, 1, 1, 0, 0x40, 0x05, 0x25, 0, 0];
        rec.push(-60i8 as u8);
        let mut pkt = vec![4, 0x2F, 255, 1];
        pkt.extend_from_slice(&rec);
        pkt.extend_from_slice(&eir);
        t.reply(&pkt);
        let mut pkt2 = vec![4, 0x22, 15, 1, 2, 2, 2, 2, 2, 2, 1, 0, 0, 0, 0, 0, 0];
        pkt2.push(-80i8 as u8);
        t.reply(&pkt2);
        t.reply(&[4, 0x01, 1, 0]); // inquiry complete: ask the second device's name
        let mut want = vec![2; 6];
        want.extend_from_slice(b"\x01\x00\x00\x00");
        assert!(t.sent().starts_with(&command(REMOTE_NAME, &want)));
        let mut name = b"Speaker".to_vec();
        name.resize(248, 0);
        let mut pkt3 = vec![4, 0x07, 255, 0, 2, 2, 2, 2, 2, 2];
        pkt3.extend_from_slice(&name);
        t.reply(&pkt3);
        assert_eq!(t.bt.phase, Some(Phase::Le));
        let mut adv = vec![0x02, 1, 0, 0, 3, 3, 3, 3, 3, 3, 5, 4, 0x09];
        adv.extend_from_slice(b"Tag");
        adv.push(-50i8 as u8);
        let mut pkt4 = vec![4, 0x3E, adv.len() as u8];
        pkt4.extend_from_slice(&adv);
        t.reply(&pkt4);
        t.bt.phase_end = 0.0;
        t.bt.tick();
        assert_eq!(t.bt.phase, None);
        t.bt.devices.push((
            "04:04:04:04:04:04".into(),
            Found {
                name: None,
                rssi: Some(-20),
                kind: "BLE",
                psrm: None,
            },
        ));
        let s =
            |a: &str, n: &str, r: i32, k: &'static str| (a.to_string(), n.to_string(), Some(r), k);
        assert_eq!(
            t.bt.listing(),
            vec![
                s("03:03:03:03:03:03", "Tag", -50, "BLE"),
                s("01:01:01:01:01:01", "Toets1", -60, "klassiek"),
                s("02:02:02:02:02:02", "Speaker", -80, "klassiek"),
                s("04:04:04:04:04:04", "", -20, "BLE"), // unnamed last
            ]
        );
        t.bt.devices.retain(|(a, _)| a != "04:04:04:04:04:04");
        assert_eq!(t.bt.message, "3 apparaten gevonden");
    }

    #[test]
    fn write_error_switches_bluetooth_off_instead_of_raising() {
        let mut t = setup();
        t.ready();
        t.close_b(); // the other end is gone: writes fail
        t.bt.search();
        assert!(!t.bt.on());
        assert!(t.bt.message.contains("Bluetooth-fout"), "{}", t.bt.message);
        t.bt.tick();
    }

    #[test]
    fn a_search_that_switched_bluetooth_on_switches_it_off_again() {
        let mut t = setup();
        t.bt.search();
        assert!(t.bt.on());
        t.complete(RESET, b"");
        t.finish_setup(false);
        t.bt.phase = Some(Phase::Le);
        t.bt.phase_end = 0.0;
        t.bt.tick();
        assert!(!t.bt.on());
        t.bt.power(true); // switched on by the user: stays on after a search
        t.bt.search();
        t.bt.phase = Some(Phase::Le);
        t.bt.phase_end = 0.0;
        t.bt.tick();
        assert!(t.bt.on());
    }

    #[test]
    fn a_failed_reset_write_is_reported_not_hidden() {
        let mut t = setup();
        t.close_b();
        assert!(!t.bt.power(true));
        t.bt.search();
        assert!(!t.bt.on());
        assert!(t.bt.message.contains("Bluetooth-fout"), "{}", t.bt.message);
        assert_eq!(t.bt.phase, None);
    }

    fn refused_reset() -> Vec<u8> {
        let mut p = vec![1];
        p.extend_from_slice(&RESET.to_le_bytes());
        p.push(3);
        p
    }

    #[test]
    fn a_refused_reset_switches_a_user_enabled_radio_off_too() {
        let mut t = setup();
        t.bt.power(true);
        t.bt.handle(0x0E, &refused_reset());
        assert!(!t.bt.on());
        assert_eq!(t.bt.message, "Bluetooth-reset mislukt (status 3)");
    }

    #[test]
    fn a_refused_reset_ends_the_waiting_search() {
        let mut t = setup();
        t.bt.search();
        t.bt.handle(0x0E, &refused_reset());
        assert_eq!((t.bt.phase, t.bt.on()), (None, false));
        assert_eq!(t.bt.message, "Bluetooth-reset mislukt (status 3)");
    }

    #[test]
    fn a_missing_device_is_shown_not_raised() {
        // from Pages.test_bluetooth_failure_is_shown_not_raised: the real opener on a missing node
        let mut bt = Bluetooth::new();
        bt.dev = "/dev/i14-test-no-stpbt".into();
        assert!(!bt.power(true));
        assert_eq!(
            bt.message,
            "Bluetooth starten mislukt: No such file or directory"
        );
    }

    #[test]
    fn packets_split_and_junk() {
        let mut p = Packets::new();
        assert_eq!(p.feed(b"\x00\x04\x0e"), vec![]);
        assert_eq!(
            p.feed(b"\x01\x05\x04\x0e\x00"),
            vec![Pkt::Event(0x0E, b"\x05".to_vec()), Pkt::Event(0x0E, vec![])]
        );
    }
    #[test]
    fn acl_payload_with_0x04_is_not_an_event() {
        let mut p = Packets::new();
        assert_eq!(
            p.feed(b"\x02\x01\x20\x07\x00\x04\x0e\x04\x01\x03\x0c\x00\x04\x0e\x00"),
            vec![
                Pkt::Acl {
                    handle: 1,
                    pb: 2,
                    data: b"\x04\x0e\x04\x01\x03\x0c\x00".to_vec()
                },
                Pkt::Event(0x0e, vec![])
            ]
        );
    }
    #[test]
    fn packets_split_acl_across_reads() {
        let mut p = Packets::new();
        assert!(p.feed(b"\x02\x01\x10\x02").is_empty());
        assert!(p.feed(b"\x00\x04").is_empty());
        assert_eq!(
            p.feed(b"\x05"),
            vec![Pkt::Acl {
                handle: 1,
                pb: 1,
                data: vec![4, 5]
            }]
        );
    }
    #[test]
    fn sco_packet_skipped() {
        let mut p = Packets::new();
        assert!(p.feed(b"\x03\x01\x00\x03\x04").is_empty());
        assert_eq!(
            p.feed(b"\x0e\x00\x04\x0e\x00"),
            vec![Pkt::Event(0x0e, vec![])]
        );
    }
    #[test]
    fn unknown_type_resyncs() {
        let mut p = Packets::new();
        assert_eq!(
            p.feed(b"\xff\x00\x04\x0e\x00"),
            vec![Pkt::Event(0x0e, vec![])]
        );
    }

    // --- set-up for pairing --------------------------------------------------------------

    fn commands_bitmap(sc: bool) -> Vec<u8> {
        let mut v = vec![0xffu8; 64];
        if !sc {
            v[SC_HOST_SUPPORT_OCTET] &= !SC_HOST_SUPPORT_MASK;
        }
        v
    }

    fn setup_reply(op: u16, sc: bool) -> Vec<u8> {
        match op {
            READ_BD_ADDR => vec![1, 0x21, 3, 0x46, 0, 0],
            READ_LOCAL_VERSION => vec![8, 0, 0, 8, 0x46, 0, 0, 0],
            READ_LOCAL_COMMANDS => commands_bitmap(sc),
            // ACL MTU 1021, SCO 64, 8 ACL buffers, 8 SCO buffers
            READ_BUFFER_SIZE => vec![0xFD, 0x03, 64, 8, 0, 8, 0],
            _ => vec![],
        }
    }

    fn setup_params(op: u16, visible: bool) -> Vec<u8> {
        match op {
            SET_EVENT_MASK => 0x3DBFF807FFFBFFFFu64.to_le_bytes().to_vec(),
            WRITE_INQUIRY_MODE => vec![2],
            WRITE_SIMPLE_PAIRING_MODE | WRITE_SC_HOST_SUPPORT => vec![1],
            WRITE_LOCAL_NAME => {
                let mut v = b"I14".to_vec();
                v.resize(248, 0);
                v
            }
            WRITE_CLASS_OF_DEVICE => vec![0x0C, 0x02, 0x5A],
            WRITE_EIR => {
                let mut v = vec![0, 4, 0x09, b'I', b'1', b'4'];
                v.resize(241, 0);
                v
            }
            WRITE_SCAN_ENABLE => vec![if visible { 3 } else { 2 }],
            _ => vec![],
        }
    }

    fn complete_status(t: &mut T, opcode: u16, status: u8, params: &[u8]) {
        let mut p = vec![TEST_CREDITS];
        p.extend_from_slice(&opcode.to_le_bytes());
        p.push(status);
        p.extend_from_slice(params);
        t.event(0x0E, &p);
    }

    #[test]
    fn after_reset_enables_ssp_name_class_and_page_scan() {
        let mut t = setup();
        t.bt.power(true);
        t.sent();
        t.complete(RESET, b"");
        let order = [
            READ_BD_ADDR,
            SET_EVENT_MASK,
            WRITE_INQUIRY_MODE,
            READ_LOCAL_VERSION,
            READ_LOCAL_COMMANDS,
            READ_BUFFER_SIZE,
            WRITE_SIMPLE_PAIRING_MODE,
            WRITE_LOCAL_NAME,
            WRITE_CLASS_OF_DEVICE,
            WRITE_EIR,
            WRITE_SCAN_ENABLE,
        ];
        for op in order {
            // exactly one command at a time, the next only after the previous completed
            assert_eq!(t.sent(), command(op, &setup_params(op, false)), "{op:04x}");
            assert!(!t.bt.ready, "ready before {op:04x}");
            t.complete(op, &setup_reply(op, false));
        }
        assert!(t.bt.ready);
        assert_eq!(t.sent(), b"");
        assert_eq!(t.bt.address, "00:00:46:03:21:01");
        assert_eq!((t.bt.version, t.bt.ssp, t.bt.sc), (Some(8), true, false));
        assert_eq!((t.bt.acl_out.mtu, t.bt.acl_out.credits), (1021, 8));
    }

    #[test]
    fn secure_connections_only_when_supported() {
        for sc in [true, false] {
            let mut t = setup();
            t.bt.power(true);
            t.complete(RESET, b"");
            for op in &SETUP[..7] {
                t.complete(*op, &setup_reply(*op, sc));
            }
            let next = t.sent();
            let name = command(WRITE_LOCAL_NAME, &setup_params(WRITE_LOCAL_NAME, false));
            if sc {
                assert!(next.ends_with(&command(WRITE_SC_HOST_SUPPORT, &[1])));
                t.complete(WRITE_SC_HOST_SUPPORT, b"");
                assert!(t.bt.sc);
                assert_eq!(t.sent(), name);
            } else {
                assert!(!contains(&next, &command(WRITE_SC_HOST_SUPPORT, &[1])));
                assert!(next.ends_with(&name));
                assert!(!t.bt.sc);
            }
        }
    }

    #[test]
    fn a_refused_ssp_mode_is_reported() {
        let mut t = setup();
        t.bt.search();
        t.complete(RESET, b"");
        for op in &SETUP[..6] {
            t.complete(*op, &setup_reply(*op, true));
        }
        complete_status(&mut t, WRITE_SIMPLE_PAIRING_MODE, 0x11, b"");
        assert_eq!(t.bt.message, "koppelen niet ondersteund (status 17)");
        assert!(!t.bt.ssp);
        // no Secure Connections without SSP; the set-up goes on and the search still runs
        assert!(!contains(&t.sent(), &command(WRITE_SC_HOST_SUPPORT, &[1])));
        for op in [
            WRITE_LOCAL_NAME,
            WRITE_CLASS_OF_DEVICE,
            WRITE_EIR,
            WRITE_SCAN_ENABLE,
        ] {
            t.complete(op, b"");
        }
        assert!(t.sent().ends_with(&inquiry_cmd()));
        assert_eq!(t.bt.phase, Some(Phase::Inquiry));
    }

    #[test]
    fn an_unknown_setup_command_does_not_stall_the_setup() {
        // a controller answering Command Status with an error for a set-up step
        let mut t = setup();
        t.bt.power(true);
        t.complete(RESET, b"");
        for op in &SETUP[..3] {
            t.complete(*op, &setup_reply(*op, false));
        }
        t.event(0x0F, &[0x01, 1, 0x01, 0x10]); // READ_LOCAL_VERSION unknown
        assert!(t.sent().ends_with(&command(READ_LOCAL_COMMANDS, b"")));
        assert_eq!(t.bt.version, None);
        t.complete(READ_LOCAL_COMMANDS, &commands_bitmap(false));
        complete_status(&mut t, READ_BUFFER_SIZE, 1, b"");
        assert_eq!((t.bt.acl_out.mtu, t.bt.acl_out.credits), (27, 1));
    }

    // --- links and pairing -----------------------------------------------------------------

    use std::cell::Cell;
    thread_local! {
        static NOW: Cell<f64> = const { Cell::new(100.0) };
    }
    fn fake_now() -> f64 {
        NOW.with(|n| n.get())
    }
    fn set_now(v: f64) {
        NOW.with(|n| n.set(v));
    }

    const MAC: &str = "02:11:22:33:44:55";
    const MAC_RAW: [u8; 6] = [0x55, 0x44, 0x33, 0x22, 0x11, 0x02];
    const OTHER_RAW: [u8; 6] = [6, 5, 4, 3, 2, 1];
    const KEY: [u8; 16] = [
        0xA1, 0xB2, 0xC3, 0xD4, 0xE5, 0xF6, 0x07, 0x18, 0x29, 0x3A, 0x4B, 0x5C, 0x6D, 0x7E, 0x8F,
        0x90,
    ];

    /// Ready, with a fake clock at 100 s.
    fn ready_t() -> T {
        set_now(100.0);
        let mut t = setup();
        t.bt.clock = fake_now;
        t.ready();
        t
    }

    fn with(raw: &[u8], tail: &[u8]) -> Vec<u8> {
        let mut v = raw.to_vec();
        v.extend_from_slice(tail);
        v
    }

    fn conn_request(t: &mut T, raw: &[u8], link: u8) {
        t.event(EV_CONN_REQUEST, &with(raw, &[0x0C, 0x01, 0x1C, link]));
    }

    fn conn_complete(t: &mut T, status: u8, handle: u16, raw: &[u8]) {
        let mut p = vec![status];
        p.extend_from_slice(&handle.to_le_bytes());
        p.extend_from_slice(raw);
        p.extend_from_slice(&[1, 0]);
        t.event(EV_CONN_COMPLETE, &p);
    }

    fn disconn_complete(t: &mut T, handle: u16) {
        let mut p = vec![0];
        p.extend_from_slice(&handle.to_le_bytes());
        p.push(0x13);
        t.event(EV_DISCONN_COMPLETE, &p);
    }

    fn user_confirm(t: &mut T, raw: &[u8], value: u32) {
        t.event(EV_USER_CONFIRM, &with(raw, &value.to_le_bytes()));
    }

    fn key_notify(t: &mut T, raw: &[u8], kind: u8) {
        let mut tail = KEY.to_vec();
        tail.push(kind);
        t.event(EV_LINK_KEY_NOTIFY, &with(raw, &tail));
    }

    fn acl(handle: u16, pb: u16, data: &[u8]) -> Vec<u8> {
        let mut v = vec![2];
        v.extend_from_slice(&(handle | (pb << 12)).to_le_bytes());
        v.extend_from_slice(&(data.len() as u16).to_le_bytes());
        v.extend_from_slice(data);
        v
    }

    #[test]
    fn connection_request_is_accepted_as_slave() {
        let mut t = ready_t();
        // not visible, unknown: refused
        conn_request(&mut t, &MAC_RAW, 1);
        assert_eq!(
            t.sent(),
            command(REJECT_CONNECTION, &with(&MAC_RAW, &[0x0F]))
        );
        // visible: accepted, staying peripheral
        t.bt.set_visible(true);
        assert_eq!(t.sent(), command(WRITE_SCAN_ENABLE, &[3]));
        conn_request(&mut t, &MAC_RAW, 1);
        assert_eq!(t.sent(), command(ACCEPT_CONNECTION, &with(&MAC_RAW, &[1])));
        // known device while not visible: accepted
        t.bt.set_visible(false);
        assert_eq!(t.sent(), command(WRITE_SCAN_ENABLE, &[2]));
        t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
        conn_request(&mut t, &MAC_RAW, 1);
        assert_eq!(t.sent(), command(ACCEPT_CONNECTION, &with(&MAC_RAW, &[1])));
    }

    #[test]
    fn sco_or_le_link_request_rejected() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        t.sent();
        for link in [0, 2] {
            conn_request(&mut t, &MAC_RAW, link);
            assert_eq!(
                t.sent(),
                command(REJECT_CONNECTION, &with(&MAC_RAW, &[0x0D]))
            );
        }
    }

    #[test]
    fn connection_complete_and_disconnect_tracked() {
        let mut t = ready_t();
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        conn_complete(&mut t, 0, 0x0C, &OTHER_RAW);
        conn_complete(&mut t, 4, 0x0D, &[9; 6]); // failed: not tracked
        let addrs: Vec<_> =
            t.bt.conns
                .iter()
                .map(|c| (c.handle, c.addr.clone()))
                .collect();
        assert_eq!(
            addrs,
            vec![(0x0B, MAC.into()), (0x0C, "01:02:03:04:05:06".into())]
        );
        disconn_complete(&mut t, 0x0B);
        assert_eq!(t.bt.conns.len(), 1);
        assert!(t.bt.message.contains("verbroken"), "{}", t.bt.message);
        t.bt.power(false);
        assert!(t.bt.conns.is_empty());
    }

    #[test]
    fn known_device_gets_its_link_key() {
        let mut t = ready_t();
        t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
        t.event(EV_LINK_KEY_REQUEST, &MAC_RAW);
        assert_eq!(t.sent(), command(LINK_KEY_REPLY, &with(&MAC_RAW, &KEY)));
    }

    #[test]
    fn unknown_device_negative_reply() {
        let mut t = ready_t();
        t.event(EV_LINK_KEY_REQUEST, &MAC_RAW);
        assert_eq!(t.sent(), command(LINK_KEY_NEG_REPLY, &MAC_RAW));
    }

    #[test]
    fn io_capability_request_replies_display_yes_no() {
        let mut t = ready_t();
        t.event(EV_IO_CAP_REQUEST, &MAC_RAW); // not visible: pairing not allowed
        assert_eq!(
            t.sent(),
            command(IO_CAP_NEG_REPLY, &with(&MAC_RAW, &[0x18]))
        );
        t.bt.set_visible(true);
        t.sent();
        t.event(EV_IO_CAP_REQUEST, &MAC_RAW);
        assert_eq!(t.sent(), command(IO_CAP_REPLY, &with(&MAC_RAW, &[1, 0, 3])));
    }

    #[test]
    fn user_confirmation_waits_for_the_user() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        t.sent();
        user_confirm(&mut t, &MAC_RAW, 42);
        assert_eq!(t.sent(), b"", "nothing is answered without the user");
        let c = t.bt.pending.clone().unwrap();
        assert_eq!(
            (c.addr.as_str(), c.code.as_str(), c.just_works),
            (MAC, "000042", false)
        );
        assert!(t.bt.wake);
        t.bt.confirm(true);
        assert_eq!(t.sent(), command(USER_CONFIRM_REPLY, &MAC_RAW));
        assert!(t.bt.pending.is_none());

        user_confirm(&mut t, &MAC_RAW, 1_234_567); // shown modulo one million
        assert_eq!(t.bt.pending.as_ref().unwrap().code, "234567");
        t.bt.confirm(false);
        assert_eq!(t.sent(), command(USER_CONFIRM_NEG_REPLY, &MAC_RAW));
        assert_eq!(t.bt.message, "koppelen geweigerd");

        user_confirm(&mut t, &MAC_RAW, 7);
        set_now(100.0 + 29.0);
        t.bt.tick();
        assert_eq!(t.sent(), b"");
        set_now(100.0 + 30.5);
        t.bt.tick();
        assert_eq!(t.sent(), command(USER_CONFIRM_NEG_REPLY, &MAC_RAW));
        assert_eq!(t.bt.message, "koppelen verlopen");
        assert!(t.bt.pending.is_none());
    }

    #[test]
    fn confirmation_only_while_visible_or_initiated_by_us() {
        let mut t = ready_t();
        user_confirm(&mut t, &MAC_RAW, 1);
        assert_eq!(t.sent(), command(USER_CONFIRM_NEG_REPLY, &MAC_RAW));
        assert!(t.bt.pending.is_none());
        // a pairing we started allows that one device only
        t.bt.pair(MAC);
        t.sent();
        user_confirm(&mut t, &OTHER_RAW, 1);
        assert_eq!(t.sent(), command(USER_CONFIRM_NEG_REPLY, &OTHER_RAW));
        user_confirm(&mut t, &MAC_RAW, 1);
        assert_eq!(t.sent(), b"");
        assert!(t.bt.pending.is_some());
    }

    #[test]
    fn one_confirmation_at_a_time() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &MAC_RAW, 1);
        t.sent();
        user_confirm(&mut t, &OTHER_RAW, 2);
        assert_eq!(t.sent(), command(USER_CONFIRM_NEG_REPLY, &OTHER_RAW));
        assert_eq!(t.bt.pending.as_ref().unwrap().addr, MAC);
    }

    #[test]
    fn just_works_is_marked_and_still_asks() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        t.event(EV_IO_CAP_RESPONSE, &with(&MAC_RAW, &[3, 0, 0]));
        user_confirm(&mut t, &MAC_RAW, 0);
        t.sent();
        assert!(t.bt.pending.as_ref().unwrap().just_works);
        assert_eq!(t.sent(), b"");
    }

    #[test]
    fn passkey_request_refused() {
        let mut t = ready_t();
        t.event(EV_USER_PASSKEY, &MAC_RAW);
        assert_eq!(t.sent(), command(USER_PASSKEY_NEG_REPLY, &MAC_RAW));
        // a passkey the other side would type closes the link instead of pairing silently
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.event(EV_PASSKEY_NOTIFY, &with(&MAC_RAW, &123456u32.to_le_bytes()));
        assert_eq!(t.sent(), command(DISCONNECT, &[0x0B, 0, 0x05]));
    }

    #[test]
    fn legacy_pin_refused() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        t.sent();
        t.event(EV_PIN_REQUEST, &MAC_RAW);
        assert_eq!(t.sent(), command(PIN_CODE_NEG_REPLY, &MAC_RAW));
        assert_eq!(t.bt.message, "oude koppelmethode niet ondersteund");
    }

    #[test]
    fn link_key_notification_is_stored() {
        let mut t = ready_t();
        let d = keys::tests_tmpdir("notify");
        t.bt.keys = KeyStore::new(Some(d.join("bt-keys")));
        t.bt.devices.push((
            MAC.into(),
            Found {
                name: Some("MacBook".into()),
                rssi: None,
                kind: "klassiek",
                psrm: Some(1),
            },
        ));
        t.bt.set_visible(true);
        // not confirmed by the user: ignored
        key_notify(&mut t, &MAC_RAW, 5);
        assert!(t.bt.keys.get(MAC).is_none());
        user_confirm(&mut t, &MAC_RAW, 1);
        t.bt.confirm(true);
        t.event(EV_SSP_COMPLETE, &with(&[0], &MAC_RAW));
        key_notify(&mut t, &MAC_RAW, 5);
        let k = t.bt.keys.get(MAC).unwrap();
        assert_eq!((k.key, k.kind, k.name.as_str()), (KEY, 5, "MacBook"));
        assert_eq!(t.bt.message, "gekoppeld met MacBook");
        use std::os::unix::fs::PermissionsExt;
        let meta = std::fs::metadata(d.join("bt-keys")).unwrap();
        assert_eq!(meta.permissions().mode() & 0o777, 0o600);
        // a changed key for a paired device replaces the old one
        let mut tail = [7u8; 16].to_vec();
        tail.push(6);
        t.event(EV_LINK_KEY_NOTIFY, &with(&MAC_RAW, &tail));
        assert_eq!(t.bt.keys.get(MAC).unwrap().key, [7; 16]);
    }

    #[test]
    fn unnamed_device_gets_its_name_asked_after_pairing() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &OTHER_RAW, 1);
        t.bt.confirm(true);
        t.sent();
        key_notify(&mut t, &OTHER_RAW, 4);
        assert_eq!(t.bt.keys.get("01:02:03:04:05:06").unwrap().name, "");
        assert_eq!(
            t.sent(),
            command(REMOTE_NAME, &with(&OTHER_RAW, &[1, 0, 0, 0]))
        );
        let mut name = b"Toetsenbord".to_vec();
        name.resize(248, 0);
        let mut p = vec![0];
        p.extend_from_slice(&OTHER_RAW);
        p.extend_from_slice(&name);
        t.event(EV_NAME, &p);
        assert_eq!(
            t.bt.keys.get("01:02:03:04:05:06").unwrap().name,
            "Toetsenbord"
        );
        assert!(t.bt.devices.is_empty(), "not a search result");
    }

    #[test]
    fn debug_keys_are_not_stored() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &MAC_RAW, 1);
        t.bt.confirm(true);
        key_notify(&mut t, &MAC_RAW, 3);
        assert!(t.bt.keys.get(MAC).is_none());
        assert_eq!(t.bt.message, "debugsleutel geweigerd");
    }

    #[test]
    fn simple_pairing_failure_is_shown() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &MAC_RAW, 1);
        t.bt.confirm(true);
        t.event(EV_SSP_COMPLETE, &with(&[0x05], &MAC_RAW));
        assert_eq!(t.bt.message, "koppelen mislukt (status 5)");
        key_notify(&mut t, &MAC_RAW, 5); // too late: the confirmation no longer counts
        assert!(t.bt.keys.get(MAC).is_none());
    }

    #[test]
    fn pair_with_creates_connection_then_authenticates() {
        let mut t = ready_t();
        t.bt.pair(MAC);
        assert_eq!(
            t.sent(),
            command(
                CREATE_CONNECTION,
                &with(&MAC_RAW, &[0x18, 0xCC, 1, 0, 0, 0, 1])
            )
        );
        t.event(0x0F, &[0, TEST_CREDITS, 0x05, 0x04]); // command status: pending
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        assert_eq!(t.sent(), command(AUTH_REQUESTED, &[0x0B, 0]));
        t.event(EV_LINK_KEY_REQUEST, &MAC_RAW);
        assert_eq!(t.sent(), command(LINK_KEY_NEG_REPLY, &MAC_RAW));
        t.event(EV_IO_CAP_REQUEST, &MAC_RAW);
        assert_eq!(t.sent(), command(IO_CAP_REPLY, &with(&MAC_RAW, &[1, 0, 3])));
        user_confirm(&mut t, &MAC_RAW, 654321);
        assert_eq!(t.bt.pending.as_ref().unwrap().code, "654321");
        t.bt.confirm(true);
        t.event(EV_SSP_COMPLETE, &with(&[0], &MAC_RAW));
        key_notify(&mut t, &MAC_RAW, 5);
        t.event(EV_AUTH_COMPLETE, &[0, 0x0B, 0]);
        assert_eq!(t.bt.message, format!("gekoppeld met {MAC}"));
        assert!(t.bt.pairing.is_none());
        assert!(t.bt.keys.get(MAC).is_some());
    }

    #[test]
    fn reconnect_uses_the_stored_key_without_asking() {
        let mut t = ready_t();
        t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW); // already connected: authenticate directly
        t.sent();
        t.bt.pair(MAC);
        assert_eq!(t.sent(), command(AUTH_REQUESTED, &[0x0B, 0]));
        t.event(EV_LINK_KEY_REQUEST, &MAC_RAW);
        assert_eq!(t.sent(), command(LINK_KEY_REPLY, &with(&MAC_RAW, &KEY)));
        t.event(EV_AUTH_COMPLETE, &[0, 0x0B, 0]);
        assert_eq!(t.bt.message, "verbonden met Mac");
        assert!(t.bt.pending.is_none());
        // a peer that lost its key
        t.bt.pair(MAC);
        t.event(EV_AUTH_COMPLETE, &[0x06, 0x0B, 0]);
        assert!(
            t.bt.message.starts_with("sleutel geweigerd door Mac"),
            "{}",
            t.bt.message
        );
    }

    #[test]
    fn failed_pairing_steps_are_reported() {
        let mut t = ready_t();
        t.bt.pair(MAC);
        t.event(0x0F, &[0x0C, TEST_CREDITS, 0x05, 0x04]); // Create Connection refused
        assert_eq!(t.bt.message, "verbinden mislukt (status 12)");
        assert!(t.bt.pairing.is_none());
        t.bt.pair(MAC);
        conn_complete(&mut t, 0x04, 0, &MAC_RAW); // page timeout
        assert_eq!(t.bt.message, "verbinden mislukt (status 4)");
        t.bt.pair(MAC);
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.event(0x0F, &[0x0C, TEST_CREDITS, 0x11, 0x04]); // Authentication Requested refused
        assert_eq!(t.bt.message, "koppelen mislukt (status 12)");
        t.bt.pair("not an address");
        assert_eq!(t.bt.message, "ongeldig adres not an address");
    }

    #[test]
    fn a_pairing_that_hangs_times_out_and_closes_the_link() {
        let mut t = ready_t();
        t.bt.pair(MAC);
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.sent();
        t.bt.pair(MAC);
        assert_eq!(t.bt.message, "er loopt al een koppeling");
        set_now(100.0 + 61.0);
        t.bt.tick();
        assert_eq!(t.sent(), command(DISCONNECT, &[0x0B, 0, 0x13]));
        assert_eq!(t.bt.message, "koppelen verlopen");
        assert!(t.bt.pairing.is_none());
    }

    #[test]
    fn a_disconnect_during_confirmation_ends_it() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        user_confirm(&mut t, &MAC_RAW, 1);
        t.sent();
        disconn_complete(&mut t, 0x0B);
        assert!(t.bt.pending.is_none());
        assert_eq!(t.sent(), b"", "no reply for a link that is gone");
        assert!(t.bt.message.contains("afgebroken"), "{}", t.bt.message);
    }

    #[test]
    fn forget_removes_key_and_disconnects() {
        let mut t = ready_t();
        t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
        t.bt.keys.put("01:02:03:04:05:06", KEY, 5, "Ander").unwrap();
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.sent();
        t.bt.forget(MAC);
        assert_eq!(t.sent(), command(DISCONNECT, &[0x0B, 0, 0x13]));
        assert!(t.bt.keys.get(MAC).is_none());
        assert!(t.bt.keys.get("01:02:03:04:05:06").is_some());
        assert_eq!(t.bt.message, "vergeten: Mac");
        // forgetting works with the radio off too
        t.bt.power(false);
        t.bt.forget("01:02:03:04:05:06");
        assert!(t.bt.keys.keys.is_empty());
    }

    #[test]
    fn visibility_lasts_two_minutes() {
        set_now(100.0);
        let mut t = setup();
        t.bt.clock = fake_now;
        // off: visibility switches the radio on and the set-up ends with inquiry + page scan
        t.bt.set_visible(true);
        assert!(t.bt.on() && t.bt.auto_on);
        t.complete(RESET, b"");
        t.finish_setup(false);
        assert!(t.sent().ends_with(&command(WRITE_SCAN_ENABLE, &[3])));
        assert!(t.bt.needs_tick());
        set_now(100.0 + 119.0);
        t.bt.tick();
        assert!(t.bt.visible());
        set_now(100.0 + 121.0);
        t.bt.tick();
        assert!(!t.bt.visible());
        // switched on only for this: off again
        assert!(!t.bt.on());
        assert_eq!(t.bt.message, "niet meer zichtbaar");
    }

    #[test]
    fn auto_off_waits_for_connections() {
        set_now(100.0);
        let mut t = setup();
        t.bt.clock = fake_now;
        t.bt.search();
        t.complete(RESET, b"");
        t.finish_setup(false);
        t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.bt.phase = Some(Phase::Le);
        t.bt.phase_end = 0.0;
        t.bt.tick();
        assert!(t.bt.on(), "a connection keeps the radio on");
        disconn_complete(&mut t, 0x0B);
        t.bt.tick();
        assert!(!t.bt.on());
    }

    #[test]
    fn pair_while_off_waits_for_the_setup() {
        set_now(100.0);
        let mut t = setup();
        t.bt.clock = fake_now;
        t.bt.pair(MAC);
        assert!(t.bt.on());
        assert_eq!(t.sent(), command(RESET, b""));
        t.complete(RESET, b"");
        t.finish_setup(false);
        assert!(t.sent().ends_with(&command(
            CREATE_CONNECTION,
            &with(&MAC_RAW, &[0x18, 0xCC, 1, 0, 0, 0, 1])
        )));
    }

    #[test]
    fn pair_ends_a_running_search() {
        let mut t = ready_t();
        t.bt.search();
        t.sent();
        t.bt.pair(MAC);
        let sent = t.sent();
        assert!(sent.starts_with(&command(INQUIRY_CANCEL, b"")));
        assert_eq!(t.bt.phase, None);
    }

    #[test]
    fn power_off_refuses_a_waiting_confirmation() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &MAC_RAW, 1);
        t.sent();
        t.bt.power(false);
        assert_eq!(t.sent(), command(USER_CONFIRM_NEG_REPLY, &MAC_RAW));
        assert!(t.bt.pending.is_none() && !t.bt.visible());
    }

    #[test]
    fn malformed_link_events_are_ignored() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        t.sent();
        for code in [
            EV_CONN_REQUEST,
            EV_CONN_COMPLETE,
            EV_DISCONN_COMPLETE,
            EV_AUTH_COMPLETE,
            EV_ENCRYPT_CHANGE,
            EV_PIN_REQUEST,
            EV_LINK_KEY_REQUEST,
            EV_LINK_KEY_NOTIFY,
            EV_IO_CAP_REQUEST,
            EV_IO_CAP_RESPONSE,
            EV_USER_CONFIRM,
            EV_USER_PASSKEY,
            EV_SSP_COMPLETE,
            EV_PASSKEY_NOTIFY,
        ] {
            t.event(code, &[1, 2, 3]);
        }
        t.event(EV_NUM_COMPLETED, &[5, 1, 0]); // claims 5 handles, has none complete
        assert_eq!(t.sent(), b"");
        assert!(t.bt.conns.is_empty() && t.bt.pending.is_none());
        assert!(t.bt.on());
    }

    // --- ACL and L2CAP through the device ---------------------------------------------------

    fn sig(code: u8, ident: u8, data: &[u8]) -> Vec<u8> {
        let mut c = vec![code, ident];
        c.extend_from_slice(&(data.len() as u16).to_le_bytes());
        c.extend_from_slice(data);
        l2cap::frame(1, &c)
    }

    #[test]
    fn acl_signalling_is_answered_between_events() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.sent();
        // SDP connection request split over two ACL fragments, an event in between, all in two reads
        let frame = sig(0x02, 3, &[1, 0, 0x40, 0]);
        let mut stream = acl(0x0B, 2, &frame[..5]);
        let mut ev = vec![4, EV_ENCRYPT_CHANGE, 4, 0, 0x0B, 0, 1];
        stream.append(&mut ev);
        let rest = acl(0x0B, 1, &frame[5..]);
        stream.extend_from_slice(&rest[..3]);
        t.reply(&stream);
        assert_eq!(t.sent(), b"", "half a packet: nothing yet");
        assert!(t.bt.conns[0].encrypted);
        t.reply(&rest[3..]);
        let want = acl(0x0B, 0, &sig(0x03, 3, &[0, 0, 0x40, 0, 2, 0, 0, 0]));
        assert_eq!(t.sent(), want);
        // ACL for a handle we do not have is ignored
        t.reply(&acl(0x0C, 2, &sig(0x08, 1, b"x")));
        assert_eq!(t.sent(), b"");
        // non-signalling channels are ignored
        t.reply(&acl(0x0B, 2, &l2cap::frame(0x40, b"data")));
        assert_eq!(t.sent(), b"");
    }

    #[test]
    fn acl_replies_wait_for_controller_buffers() {
        let mut t = ready_t();
        t.bt.acl_out.set_buffers(1021, 1);
        t.bt.set_visible(true);
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.sent();
        let mut two = sig(0x08, 1, b"a");
        two.extend(sig(0x08, 2, b"b"));
        t.reply(&acl(0x0B, 2, &two[..9]));
        t.reply(&acl(0x0B, 2, &two[9..]));
        assert_eq!(t.sent(), acl(0x0B, 0, &sig(0x09, 1, b"a")));
        t.event(EV_NUM_COMPLETED, &[1, 0x0B, 0, 1, 0]);
        assert_eq!(t.sent(), acl(0x0B, 0, &sig(0x09, 2, b"b")));
        // a disconnect frees what was in flight
        disconn_complete(&mut t, 0x0B);
        assert_eq!(t.bt.acl_out.credits, 1);
    }

    #[test]
    fn info_requests_answered_through_the_device() {
        let mut t = ready_t();
        t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.sent();
        t.reply(&acl(0x0B, 2, &sig(0x0A, 1, &[2, 0])));
        assert_eq!(
            t.sent(),
            acl(0x0B, 0, &sig(0x0B, 1, &[2, 0, 0, 0, 0, 0, 0, 0]))
        );
    }

    // --- state file ------------------------------------------------------------------------

    #[test]
    fn state_has_no_key_material() {
        let mut t = ready_t();
        let d = keys::tests_tmpdir("state");
        let path = d.join("i14-bt.json");
        t.bt.state_path = Some(path.clone());
        t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.bt.set_visible(true);
        user_confirm(&mut t, &OTHER_RAW, 999);
        t.bt.tick();
        let text = std::fs::read_to_string(&path).unwrap();
        let v: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["on"], true);
        assert_eq!(v["address"], "00:00:46:03:21:01");
        assert_eq!(v["visible"], true);
        assert_eq!(v["pending"]["addr"], "01:02:03:04:05:06");
        assert_eq!(v["pending"]["code"], "000999");
        assert_eq!(
            v["pending"]["tap"]["accept"],
            serde_json::json!([190, 1065])
        );
        assert_eq!(
            v["paired"],
            serde_json::json!([{"addr": MAC, "name": "Mac", "connected": true}])
        );
        assert_eq!(v["connected"], serde_json::json!([MAC]));
        let hexkey: String = KEY.iter().map(|b| format!("{b:02x}")).collect();
        assert!(!text.to_lowercase().contains(&hexkey), "{text}");
        assert!(!text.contains("161, 178"), "{text}");
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o644
        );
        // rewritten only when something changed
        std::fs::remove_file(&path).unwrap();
        t.bt.tick();
        assert!(!path.exists());
        t.bt.confirm(false);
        t.bt.tick();
        assert!(path.exists());
    }

    // Review findings (Sol, 2026-11-08)

    #[test]
    fn s2_forget_withdraws_consent_of_a_pairing_in_flight() {
        // re-pairing a bonded Mac: the user accepts, then forgets it before the key arrives
        let mut t = ready_t();
        t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.bt.pair(MAC);
        user_confirm(&mut t, &MAC_RAW, 1);
        t.bt.confirm(true);
        t.event(EV_SSP_COMPLETE, &with(&[0], &MAC_RAW));
        t.sent();
        t.bt.forget(MAC);
        assert_eq!(t.sent(), command(DISCONNECT, &[0x0B, 0, 0x13]));
        // queued before the Disconnection Complete: must not restore the bond
        key_notify(&mut t, &MAC_RAW, 5);
        assert!(t.bt.keys.get(MAC).is_none(), "forgotten bond came back");
        disconn_complete(&mut t, 0x0B);
        key_notify(&mut t, &MAC_RAW, 5);
        assert!(t.bt.keys.get(MAC).is_none());
    }

    #[test]
    fn s3_commands_wait_for_command_credits() {
        let mut t = ready_t();
        t.bt.search();
        assert_eq!(t.sent(), inquiry_cmd());
        // Command Status of Inquiry: room for one command
        t.event(EV_CMD_STATUS, &[0, 1, 0x01, 0x04]);
        t.bt.pair(MAC);
        assert_eq!(
            t.sent(),
            command(INQUIRY_CANCEL, b""),
            "Create Connection must wait"
        );
        assert!(t.bt.pairing.is_some());
        // Inquiry Cancel completes with one credit: now Create Connection goes
        t.event(EV_CMD_COMPLETE, &[1, 0x02, 0x04, 0]);
        let create = command(
            CREATE_CONNECTION,
            &with(&MAC_RAW, &[0x18, 0xCC, 1, 0, 0, 0, 1]),
        );
        assert_eq!(t.sent(), create);
        // zero credits advertised: nothing is sent, not even an answer
        t.event(EV_CMD_STATUS, &[0, 0, 0x05, 0x04]);
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.event(EV_LINK_KEY_REQUEST, &MAC_RAW);
        assert_eq!(t.sent(), b"", "sent without a command credit");
        // a credit-only Command Complete (opcode 0) releases them one at a time, in order
        t.event(EV_CMD_COMPLETE, &[1, 0, 0]);
        assert_eq!(t.sent(), command(AUTH_REQUESTED, &[0x0B, 0]));
        t.event(EV_CMD_STATUS, &[0, 1, 0x11, 0x04]);
        assert_eq!(t.sent(), command(LINK_KEY_NEG_REPLY, &MAC_RAW));
        // ACL credits are separate: Number Of Completed Packets gives no command credit
        t.event(EV_LINK_KEY_REQUEST, &MAC_RAW);
        t.event(EV_NUM_COMPLETED, &[1, 0x0B, 0, 1, 0]);
        assert_eq!(t.sent(), b"");
    }

    #[test]
    fn s4_one_pairing_holds_the_phone_until_its_key_arrives() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &MAC_RAW, 1);
        t.bt.confirm(true);
        t.sent();
        // a second peer while the Mac's key is still due: refused, no dialog
        user_confirm(&mut t, &OTHER_RAW, 2);
        assert_eq!(t.sent(), command(USER_CONFIRM_NEG_REPLY, &OTHER_RAW));
        assert!(t.bt.pending.is_none());
        // a key of another device does not use up the Mac's consent
        key_notify(&mut t, &OTHER_RAW, 5);
        assert!(t.bt.keys.get("01:02:03:04:05:06").is_none());
        key_notify(&mut t, &MAC_RAW, 5);
        assert!(t.bt.keys.get(MAC).is_some(), "the Mac's key was not stored");
        // the Mac is done: the next device may pair
        user_confirm(&mut t, &OTHER_RAW, 3);
        assert!(t.bt.pending.is_some());
        t.bt.confirm(false);
        // a consent whose key never comes stops holding the phone
        user_confirm(&mut t, &OTHER_RAW, 4);
        t.bt.confirm(true);
        set_now(100.0 + pair::PAIR_S + 1.0);
        t.bt.tick();
        key_notify(&mut t, &OTHER_RAW, 5);
        assert!(
            t.bt.keys.get("01:02:03:04:05:06").is_none(),
            "late key stored"
        );
    }

    #[test]
    fn s5_a_changed_combination_key_keeps_the_security_type() {
        let mut t = ready_t();
        t.bt.keys.put(MAC, KEY, 8, "Mac").unwrap(); // authenticated P-256
        let mut tail = [7u8; 16].to_vec();
        tail.push(0x06); // Changed Combination Key
        t.event(EV_LINK_KEY_NOTIFY, &with(&MAC_RAW, &tail));
        let k = t.bt.keys.get(MAC).unwrap();
        assert_eq!((k.key, k.kind), ([7; 16], 8));
    }

    #[test]
    fn s1_a_locked_screen_cannot_accept_a_code() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &MAC_RAW, 1);
        t.sent();
        t.bt.ui_locked = true;
        t.bt.confirm(true);
        assert_eq!(t.sent(), b"", "accepted while locked");
        assert!(
            t.bt.pending.is_some(),
            "still waiting for an unlocked answer"
        );
        t.bt.confirm(false); // refusing always works
        assert_eq!(t.sent(), command(USER_CONFIRM_NEG_REPLY, &MAC_RAW));
    }

    // Review round 2 (Sol, 2026-11-08)

    /// The controller advertises zero command credits.
    fn no_credits(t: &mut T) {
        t.event(EV_CMD_STATUS, &[0, 0, 0, 0]);
    }

    #[test]
    fn r2_s3_power_off_sends_no_refusal_without_a_credit() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &MAC_RAW, 1);
        no_credits(&mut t);
        t.sent();
        t.bt.power(false);
        assert_eq!(t.sent(), b"", "sent without a command credit");
        assert!(!t.bt.on() && t.bt.pending.is_none());
    }

    #[test]
    fn r2_s4_an_accepted_code_blocks_a_new_pairing_until_its_key_arrives() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &MAC_RAW, 1);
        t.bt.confirm(true);
        t.sent();
        t.bt.pair("01:02:03:04:05:06");
        assert_eq!(
            t.sent(),
            b"",
            "Create Connection before the Mac's key arrived"
        );
        assert!(t.bt.pairing.is_none());
        assert_eq!(t.bt.message, "er loopt al een koppeling");
    }

    #[test]
    fn r2_s6_queued_commands_die_with_their_operation() {
        // a forgotten device's queued key reply goes out as a refusal, without the key
        let mut t = ready_t();
        t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        no_credits(&mut t);
        t.event(EV_LINK_KEY_REQUEST, &MAC_RAW);
        t.bt.forget(MAC);
        assert_eq!(t.sent(), b"");
        t.event(EV_CMD_COMPLETE, &[TEST_CREDITS, 0, 0]);
        let mut want = command(LINK_KEY_NEG_REPLY, &MAC_RAW);
        want.extend_from_slice(&command(DISCONNECT, &[0x0B, 0, 0x13]));
        assert_eq!(t.sent(), want);
        // an expired pairing's queued Create Connection is never sent, and nothing waits for it
        let mut t = ready_t();
        no_credits(&mut t);
        t.bt.pair(MAC);
        set_now(100.0 + pair::PAIR_S + 1.0);
        t.bt.tick();
        assert!(t.bt.pairing.is_none());
        t.event(EV_CMD_COMPLETE, &[TEST_CREDITS, 0, 0]);
        assert_eq!(t.sent(), b"", "expired pairing still connects");
        assert!(!t.bt.waiting.iter().any(|(o, _)| *o == CREATE_CONNECTION));
    }

    // Review round 3 (Sol, 2026-11-08)

    const OTHER: &str = "01:02:03:04:05:06";

    #[test]
    fn r3_s8_a_stale_command_status_does_not_end_the_next_pairing() {
        let mut t = ready_t();
        t.bt.pair(MAC);
        t.sent(); // the Mac's Create Connection is out
        t.bt.forget(MAC);
        no_credits(&mut t);
        t.bt.pair(OTHER);
        assert_eq!(t.sent(), b"");
        // the Mac's Create Connection fails (page timeout), with one credit
        t.event(EV_CMD_STATUS, &[0x04, 1, 0x05, 0x04]);
        assert_eq!(t.bt.pairing.as_ref().map(|p| p.addr.as_str()), Some(OTHER));
        let create = command(
            CREATE_CONNECTION,
            &with(&OTHER_RAW, &[0x18, 0xCC, 1, 0, 0, 0, 1]),
        );
        assert_eq!(
            t.sent(),
            create,
            "the new pairing's Create Connection vanished"
        );
        // its own failure still ends it
        t.event(EV_CMD_STATUS, &[0x04, 1, 0x05, 0x04]);
        assert!(t.bt.pairing.is_none());
    }

    #[test]
    fn r3_s9_an_expired_consent_is_not_sent_as_a_yes() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &MAC_RAW, 1);
        no_credits(&mut t);
        t.sent();
        t.bt.confirm(true);
        assert_eq!(t.sent(), b"");
        set_now(100.0 + pair::PAIR_S + 1.0);
        t.bt.tick();
        t.bt.pair(OTHER);
        assert!(t.bt.pairing.is_some());
        t.event(EV_CMD_COMPLETE, &[1, 0, 0]);
        assert_eq!(t.sent(), command(USER_CONFIRM_NEG_REPLY, &MAC_RAW));
    }

    #[test]
    fn r3_a_stale_inquiry_refusal_starts_no_scan() {
        let mut t = ready_t();
        t.bt.search();
        t.sent();
        t.bt.pair(MAC); // ends the search
        t.sent();
        t.event(EV_CMD_STATUS, &[0x0C, TEST_CREDITS, 0x01, 0x04]); // the old inquiry refused
        assert_eq!(t.bt.phase, None, "a scan started during the pairing");
        assert!(!contains(&t.sent(), &command(LE_SCAN_ENABLE, b"\x01\x00")));
        // a new search: the first inquiry's refusal is not the new one's
        t.bt.search();
        t.sent();
        t.bt.stop_search();
        t.bt.search();
        t.sent();
        t.event(EV_CMD_STATUS, &[0x0C, TEST_CREDITS, 0x01, 0x04]);
        assert_eq!(t.bt.phase, Some(Phase::Inquiry));
        t.event(EV_CMD_STATUS, &[0x0C, TEST_CREDITS, 0x01, 0x04]);
        assert_eq!(t.bt.phase, Some(Phase::Le));
    }

    #[test]
    fn r3_an_abandoned_pairings_late_connection_is_closed() {
        let mut t = ready_t();
        t.bt.pair(MAC);
        t.sent(); // Create Connection is out
        t.bt.forget(MAC);
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        assert_eq!(t.sent(), command(DISCONNECT, &[0x0B, 0, 0x13]));
        // an expired pairing likewise
        let mut t = ready_t();
        t.bt.pair(MAC);
        t.sent();
        set_now(100.0 + pair::PAIR_S + 1.0);
        t.bt.tick();
        conn_complete(&mut t, 0, 0x0C, &MAC_RAW);
        assert_eq!(t.sent(), command(DISCONNECT, &[0x0C, 0, 0x13]));
        // only once: a later connection from it is an ordinary one
        disconn_complete(&mut t, 0x0C);
        t.bt.set_visible(true);
        t.sent();
        conn_complete(&mut t, 0, 0x0D, &MAC_RAW);
        assert_eq!(t.sent(), b"");
        // a pairing whose Create Connection was refused, or whose connection failed, leaves
        // nothing to close: the device's next connection is an ordinary one
        for refused in [true, false] {
            let mut t = ready_t();
            t.bt.pair(MAC);
            t.sent();
            if refused {
                t.event(EV_CMD_STATUS, &[0x0C, TEST_CREDITS, 0x05, 0x04]);
            } else {
                conn_complete(&mut t, 0x04, 0, &MAC_RAW);
            }
            assert!(t.bt.pairing.is_none() && t.bt.abandoned.is_empty());
            t.bt.set_visible(true);
            t.sent();
            conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
            assert_eq!(t.sent(), b"");
        }
    }

    // Review round 4 (Sol, 2026-11-08)

    fn create_cmd(raw: &[u8]) -> Vec<u8> {
        command(CREATE_CONNECTION, &with(raw, &[0x18, 0xCC, 1, 0, 0, 0, 1]))
    }

    #[test]
    fn r4_s9_a_yes_that_expired_while_queued_goes_out_as_no() {
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &MAC_RAW, 1);
        no_credits(&mut t);
        t.sent();
        t.bt.confirm(true); // t = 100: consent until 160
        set_now(100.0 + pair::PAIR_S + 0.1);
        t.event(EV_CMD_COMPLETE, &[1, 0, 0]); // the credit arrives before the tick
        t.bt.step();
        assert_eq!(
            t.sent(),
            command(USER_CONFIRM_NEG_REPLY, &MAC_RAW),
            "expired yes sent"
        );
    }

    #[test]
    fn r4_every_queued_positive_reply_is_checked_when_sent() {
        // a yes accepted unlocked, sent after the screen locked
        let mut t = ready_t();
        t.bt.set_visible(true);
        user_confirm(&mut t, &MAC_RAW, 1);
        no_credits(&mut t);
        t.sent();
        t.bt.confirm(true);
        t.bt.ui_locked = true;
        t.event(EV_CMD_COMPLETE, &[1, 0, 0]);
        assert_eq!(t.sent(), command(USER_CONFIRM_NEG_REPLY, &MAC_RAW));
        assert!(
            t.bt.confirmed.is_none(),
            "a refused yes still holds the phone"
        );
        // a connection accepted while visible, sent after the visibility ended
        let mut t = ready_t();
        t.bt.set_visible(true);
        no_credits(&mut t);
        t.sent();
        conn_request(&mut t, &MAC_RAW, 1);
        t.event(EV_IO_CAP_REQUEST, &MAC_RAW);
        set_now(100.0 + pair::VISIBLE_S + 1.0);
        t.event(EV_CMD_COMPLETE, &[2, 0, 0]);
        let mut want = command(REJECT_CONNECTION, &with(&MAC_RAW, &[0x0F]));
        want.extend_from_slice(&command(IO_CAP_NEG_REPLY, &with(&MAC_RAW, &[0x18])));
        assert_eq!(t.sent(), want);
        // a key reply sends the key stored when it goes out, or a refusal when there is none
        let mut t = ready_t();
        t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
        no_credits(&mut t);
        t.event(EV_LINK_KEY_REQUEST, &MAC_RAW);
        t.bt.keys.put(MAC, [9; 16], 5, "Mac").unwrap();
        t.event(EV_CMD_COMPLETE, &[1, 0, 0]);
        assert_eq!(t.sent(), command(LINK_KEY_REPLY, &with(&MAC_RAW, &[9; 16])));
        t.event(EV_LINK_KEY_REQUEST, &MAC_RAW); // no credit left: queued
        t.bt.keys.remove(MAC).unwrap();
        t.event(EV_CMD_COMPLETE, &[1, 0, 0]);
        assert_eq!(t.sent(), command(LINK_KEY_NEG_REPLY, &MAC_RAW));
    }

    #[test]
    fn r4_s10_a_stale_connection_complete_does_not_end_a_retry() {
        let mut t = ready_t();
        t.bt.pair(MAC);
        assert_eq!(t.sent(), create_cmd(&MAC_RAW));
        t.event(EV_CMD_STATUS, &[0, 0, 0x05, 0x04]); // accepted, zero credits left
        t.bt.forget(MAC);
        t.bt.pair(MAC); // retry while the first Connection Complete is still due
        let id = t.bt.pairing.as_ref().unwrap().id;
        conn_complete(&mut t, 0x04, 0, &MAC_RAW); // the first attempt's page timeout
        assert_eq!(
            t.bt.pairing.as_ref().map(|p| p.id),
            Some(id),
            "retry ended by a stale result"
        );
        t.event(EV_CMD_COMPLETE, &[1, 0, 0]);
        assert_eq!(
            t.sent(),
            create_cmd(&MAC_RAW),
            "the retry's Create Connection vanished"
        );
        // its own failure still ends it
        conn_complete(&mut t, 0x04, 0, &MAC_RAW);
        assert!(t.bt.pairing.is_none());
    }

    #[test]
    fn r4_a_retry_waits_for_the_old_link_to_close() {
        // the first attempt's connection succeeds late: it is closed, then the retry connects
        let mut t = ready_t();
        t.bt.pair(MAC);
        t.sent();
        t.bt.forget(MAC);
        t.bt.pair(MAC);
        assert_eq!(
            t.sent(),
            b"",
            "retry sent while the old Connection Complete is due"
        );
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        assert_eq!(
            t.sent(),
            command(DISCONNECT, &[0x0B, 0, 0x13]),
            "no Authentication on a closing link"
        );
        assert!(t.bt.pairing.as_ref().is_some_and(|p| !p.started));
        disconn_complete(&mut t, 0x0B);
        assert_eq!(t.sent(), create_cmd(&MAC_RAW));
        // a timed-out pairing's Authentication Complete: the retry waits for the link to close
        let mut t = ready_t();
        t.bt.pair(MAC);
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        t.sent();
        no_credits(&mut t);
        set_now(100.0 + pair::PAIR_S + 1.0);
        t.bt.tick(); // its Disconnect waits for a credit
        t.bt.pair(MAC);
        let id = t.bt.pairing.as_ref().unwrap().id;
        t.event(EV_AUTH_COMPLETE, &[0x05, 0x0B, 0]); // the old pairing's
        assert_eq!(
            t.bt.pairing.as_ref().map(|p| p.id),
            Some(id),
            "retry ended by a stale result"
        );
        disconn_complete(&mut t, 0x0B);
        assert_eq!(
            t.bt.pairing.as_ref().map(|p| p.id),
            Some(id),
            "retry ended by the old link"
        );
        t.event(EV_CMD_COMPLETE, &[2, 0, 0]);
        assert_eq!(
            t.sent(),
            create_cmd(&MAC_RAW),
            "a queued Disconnect outlived its link"
        );
        // an old completion that never comes holds a retry only for a bounded time
        let mut t = ready_t();
        t.bt.pair(MAC);
        t.sent();
        t.bt.forget(MAC);
        t.bt.pair(MAC);
        set_now(100.0 + pair::LATE_S + 1.0);
        t.bt.tick();
        assert_eq!(t.sent(), create_cmd(&MAC_RAW));
    }

    #[test]
    fn r5_s12_accepting_the_device_withdraws_our_unsent_create_connection() {
        // the real order: Accept goes out and completes before the Connection Complete
        let mut t = ready_t();
        no_credits(&mut t);
        t.bt.pair(MAC); // Create Connection waits for a credit
        conn_request(&mut t, &MAC_RAW, 1); // the Mac connects to us first
        t.event(EV_CMD_COMPLETE, &[1, 0, 0]);
        let accept = command(ACCEPT_CONNECTION, &with(&MAC_RAW, &[1]));
        assert_eq!(
            t.sent(),
            accept,
            "Create Connection sent for a link being accepted"
        );
        let op = ACCEPT_CONNECTION.to_le_bytes();
        t.event(EV_CMD_STATUS, &[0, 1, op[0], op[1]]);
        assert_eq!(t.sent(), b"", "Create Connection sent after the Accept");
        assert!(t.bt.pairing.is_some());
        conn_complete(&mut t, 0, 0x0B, &MAC_RAW);
        assert_eq!(t.sent(), command(AUTH_REQUESTED, &[0x0B, 0]));
        assert_eq!(t.bt.pairing.as_ref().and_then(|p| p.handle), Some(0x0B));
    }

    #[test]
    fn r5_s11_a_search_stopped_after_a_phase_timeout_sends_nothing_it_queued() {
        let mut t = ready_t();
        no_credits(&mut t);
        t.bt.search(); // the Inquiry waits for a credit
        set_now(100.0 + INQUIRY_S + 4.0);
        t.bt.tick(); // the inquiry times out: no names to ask, on to LE
        assert_eq!(t.bt.phase, Some(Phase::Le));
        t.bt.pair(MAC);
        t.event(EV_CMD_COMPLETE, &[TEST_CREDITS, 0, 0]);
        assert_eq!(
            t.sent(),
            create_cmd(&MAC_RAW),
            "the stopped search still ran"
        );
    }

    #[test]
    fn r4_a_stopped_search_sends_none_of_its_queued_commands() {
        for phase in [Phase::Inquiry, Phase::Names, Phase::Le] {
            let mut t = ready_t();
            t.bt.search();
            t.sent();
            if phase != Phase::Inquiry {
                // an RSSI result without a name, then the inquiry ends
                let mut pkt = vec![4, 0x22, 15, 1, 2, 2, 2, 2, 2, 2, 1, 0, 0, 0, 0, 0, 0];
                pkt.push(-80i8 as u8);
                t.reply(&pkt);
                if phase == Phase::Le {
                    t.reply(&[4, 0x01, 1, 0]);
                    t.bt.phase_end = 0.0; // the name never comes
                    t.sent();
                }
            }
            if phase == Phase::Inquiry {
                t.bt.stop_search();
                t.sent();
            }
            no_credits(&mut t);
            match phase {
                Phase::Inquiry => t.bt.search(), // its INQUIRY waits for a credit
                Phase::Names => t.reply(&[4, 0x01, 1, 0]),
                _ => t.bt.tick(),
            }
            assert_eq!((t.sent(), t.bt.phase), (vec![], Some(phase)));
            t.bt.pair(MAC);
            t.event(EV_CMD_COMPLETE, &[TEST_CREDITS, 0, 0]);
            assert_eq!(
                t.sent(),
                create_cmd(&MAC_RAW),
                "{phase:?}: a stopped search still sent"
            );
        }
    }

    // Review round 5 (Sol, 2026-11-08): nothing queued outlives its owner

    /// The opcodes of the HCI commands in `out`, in order.
    fn opcodes(out: &[u8]) -> Vec<u16> {
        let (mut ops, mut i) = (Vec::new(), 0);
        while i + 4 <= out.len() && out[i] == 1 {
            ops.push(u16::from_le_bytes([out[i + 1], out[i + 2]]));
            i += 4 + out[i + 3] as usize;
        }
        ops
    }

    /// An inquiry result: one classic device without a name, every address byte `raw`.
    fn found_unnamed(t: &mut T, raw: u8) {
        let mut pkt = vec![
            4, 0x22, 15, 1, raw, raw, raw, raw, raw, raw, 1, 0, 0, 0, 0, 0, 0,
        ];
        pkt.push(-80i8 as u8);
        t.reply(&pkt);
    }

    fn name_cmd(raw: u8) -> Vec<u8> {
        command(REMOTE_NAME, &[raw, raw, raw, raw, raw, raw, 1, 0, 0, 0])
    }

    struct Case {
        name: &'static str,
        /// the command kind queued without a credit
        kind: u16,
        queue: fn(&mut T),
        /// ends the command's owner
        end: fn(&mut T),
        /// sent once credits return: nothing, or the refusal an answer becomes
        want: fn() -> Vec<u8>,
    }

    /// Every command kind with an owner or a value taken from the state at send time. The rest
    /// always go out: the reset and set-up, cancels and refusals.
    const OWNED: &[Case] = &[
        Case {
            name: "inquiry, search stopped",
            kind: INQUIRY,
            queue: |t| {
                no_credits(t);
                t.bt.search();
            },
            end: |t| t.bt.stop_search(),
            want: Vec::new,
        },
        Case {
            name: "remote name, search stopped",
            kind: REMOTE_NAME,
            queue: |t| {
                t.bt.search();
                t.sent();
                found_unnamed(t, 2);
                no_credits(t);
                t.event(EV_INQ_COMPLETE, &[0]);
            },
            end: |t| t.bt.stop_search(),
            want: Vec::new,
        },
        Case {
            name: "remote name, its step timed out",
            kind: REMOTE_NAME,
            queue: |t| {
                t.bt.search();
                t.sent();
                found_unnamed(t, 2);
                found_unnamed(t, 7);
                no_credits(t);
                t.event(EV_INQ_COMPLETE, &[0]);
            },
            end: |t| {
                set_now(100.0 + 7.0); // the first name never came: on to the next
                t.bt.tick();
            },
            want: || name_cmd(7),
        },
        Case {
            name: "LE scan, search stopped",
            kind: LE_SCAN_ENABLE,
            queue: |t| {
                t.bt.search();
                t.sent();
                no_credits(t);
                t.event(EV_INQ_COMPLETE, &[0]); // nothing found: on to LE
            },
            end: |t| t.bt.stop_search(),
            want: Vec::new,
        },
        Case {
            name: "create connection, pairing forgotten",
            kind: CREATE_CONNECTION,
            queue: |t| {
                no_credits(t);
                t.bt.pair(MAC);
            },
            end: |t| t.bt.forget(MAC),
            want: Vec::new,
        },
        Case {
            name: "create connection, the device connects to us",
            kind: CREATE_CONNECTION,
            queue: |t| {
                no_credits(t);
                t.bt.pair(MAC);
            },
            end: |t| conn_request(t, &MAC_RAW, 1),
            want: || command(ACCEPT_CONNECTION, &with(&MAC_RAW, &[1])),
        },
        Case {
            name: "authentication, link closed",
            kind: AUTH_REQUESTED,
            queue: |t| {
                t.bt.pair(MAC);
                t.sent();
                no_credits(t);
                conn_complete(t, 0, 0x0B, &MAC_RAW);
            },
            end: |t| disconn_complete(t, 0x0B),
            want: Vec::new,
        },
        Case {
            name: "disconnect, link closed and its handle reused",
            kind: DISCONNECT,
            queue: |t| {
                t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
                conn_complete(t, 0, 0x0B, &MAC_RAW);
                t.sent();
                no_credits(t);
                t.bt.forget(MAC);
            },
            end: |t| {
                disconn_complete(t, 0x0B);
                conn_complete(t, 0, 0x0B, &MAC_RAW);
            },
            want: Vec::new,
        },
        Case {
            name: "yes, consent withdrawn",
            kind: USER_CONFIRM_REPLY,
            queue: |t| {
                t.bt.set_visible(true);
                user_confirm(t, &MAC_RAW, 1);
                t.sent();
                no_credits(t);
                t.bt.confirm(true);
            },
            end: |t| t.event(EV_SSP_COMPLETE, &with(&[5], &MAC_RAW)),
            want: || command(USER_CONFIRM_NEG_REPLY, &MAC_RAW),
        },
        Case {
            name: "accept connection, visibility ended",
            kind: ACCEPT_CONNECTION,
            queue: |t| {
                t.bt.set_visible(true);
                t.sent();
                no_credits(t);
                conn_request(t, &MAC_RAW, 1);
            },
            end: |t| t.bt.set_visible(false),
            want: || {
                let mut v = command(REJECT_CONNECTION, &with(&MAC_RAW, &[0x0F]));
                v.extend_from_slice(&command(WRITE_SCAN_ENABLE, &[2]));
                v
            },
        },
        Case {
            name: "IO capability, visibility ended",
            kind: IO_CAP_REPLY,
            queue: |t| {
                t.bt.set_visible(true);
                t.sent();
                no_credits(t);
                t.event(EV_IO_CAP_REQUEST, &MAC_RAW);
            },
            end: |t| t.bt.set_visible(false),
            want: || {
                let mut v = command(IO_CAP_NEG_REPLY, &with(&MAC_RAW, &[0x18]));
                v.extend_from_slice(&command(WRITE_SCAN_ENABLE, &[2]));
                v
            },
        },
        Case {
            name: "scan mode, visibility ended",
            kind: WRITE_SCAN_ENABLE,
            queue: |t| {
                no_credits(t);
                t.bt.set_visible(true);
            },
            end: |t| t.bt.set_visible(false),
            want: Vec::new,
        },
        Case {
            name: "paired device's name, key forgotten",
            kind: REMOTE_NAME,
            queue: |t| {
                t.bt.set_visible(true);
                user_confirm(t, &OTHER_RAW, 1);
                t.bt.confirm(true);
                t.sent();
                no_credits(t);
                key_notify(t, &OTHER_RAW, 4);
            },
            end: |t| t.bt.forget(OTHER),
            want: Vec::new,
        },
        Case {
            name: "link key, key forgotten",
            kind: LINK_KEY_REPLY,
            queue: |t| {
                t.bt.keys.put(MAC, KEY, 5, "Mac").unwrap();
                no_credits(t);
                t.event(EV_LINK_KEY_REQUEST, &MAC_RAW);
            },
            end: |t| t.bt.forget(MAC),
            want: || command(LINK_KEY_NEG_REPLY, &MAC_RAW),
        },
    ];

    #[test]
    fn r5_no_queued_command_outlives_its_owner() {
        let restore = |t: &mut T| t.event(EV_CMD_COMPLETE, &[TEST_CREDITS, 0, 0]);
        for case in OWNED {
            let start = |c: &Case| {
                let mut t = ready_t();
                (c.queue)(&mut t);
                assert_eq!(t.sent(), b"", "{}: sent without a credit", c.name);
                t
            };
            // control: with its owner current, the command does go out
            let mut t = start(case);
            restore(&mut t);
            assert!(
                opcodes(&t.sent()).contains(&case.kind),
                "{}: never queued",
                case.name
            );
            // its owner ended while it waited: not sent, or sent as the refusal
            let mut t = start(case);
            (case.end)(&mut t);
            assert_eq!(t.sent(), b"", "{}: sent without a credit", case.name);
            restore(&mut t);
            assert_eq!(t.sent(), (case.want)(), "{}", case.name);
        }
    }

    // Review round 6 (Sol, 2026-11-08)

    #[test]
    fn r6_s13_a_stale_scan_mode_is_never_written() {
        let mut t = ready_t();
        no_credits(&mut t);
        t.bt.set_visible(true);
        t.bt.set_visible(false);
        t.event(EV_CMD_COMPLETE, &[1, 0, 0]);
        assert!(!t.bt.visible());
        assert_eq!(t.sent(), b"", "discoverable after visibility ended");
        // the next change still goes out, with the value of now
        t.bt.set_visible(true);
        assert_eq!(t.sent(), command(WRITE_SCAN_ENABLE, &[3]));
    }

    #[test]
    fn r6_s14_a_late_name_does_not_skip_the_next_request() {
        let mut t = ready_t();
        t.bt.search();
        t.sent();
        for raw in [2, 7, 9] {
            found_unnamed(&mut t, raw);
        }
        t.event(EV_INQ_COMPLETE, &[0]);
        assert_eq!(t.sent(), name_cmd(2));
        no_credits(&mut t);
        set_now(100.0 + 7.0); // the first name never came: the second waits for a credit
        t.bt.tick();
        let mut p = vec![0, 2, 2, 2, 2, 2, 2];
        p.extend_from_slice(b"Laat\0");
        t.event(EV_NAME, &p); // the first name after all
        t.event(EV_CMD_COMPLETE, &[TEST_CREDITS, 0, 0]);
        assert_eq!(t.sent(), name_cmd(7), "the second name request was skipped");
        assert_eq!(
            t.bt.name_of("02:02:02:02:02:02"),
            "Laat",
            "the late name is kept"
        );
        let mut p = vec![0, 7, 7, 7, 7, 7, 7];
        p.extend_from_slice(b"Twee\0");
        t.event(EV_NAME, &p);
        assert_eq!(t.sent(), name_cmd(9));
    }
}
