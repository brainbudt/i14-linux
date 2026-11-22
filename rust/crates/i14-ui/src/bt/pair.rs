//! Links and classic Secure Simple Pairing. For BR/EDR the controller does all cryptography; the
//! host answers the HCI events, asks the user to compare the 6-digit code, and stores the link key.
//!
//! Rules: a connection is accepted only while the phone is visible, from a paired device, or from
//! the device the user is pairing with. Pairing (IO capability, confirmation) only happens while
//! visible or for a pairing the user started; nothing is ever confirmed without the user.

use super::keys::addr_bytes;
use super::l2cap;
use super::*;

pub const VISIBLE_S: f64 = 120.0;
pub const CONFIRM_S: f64 = 30.0;
pub const PAIR_S: f64 = 60.0;
/// How long a retry waits for an ended operation's completion: longer than the longest page
/// timeout (40.96 s) and the LMP response timeout (30 s).
pub const LATE_S: f64 = 45.0;

/// IO capability DisplayYesNo, no OOB data, MITM protection + dedicated bonding.
const IO_DISPLAY_YES_NO: u8 = 0x01;
const AUTH_MITM_DEDICATED: u8 = 0x03;
/// Reject Connection Request reasons.
const REJ_LIMITED_RESOURCES: u8 = 0x0D;
const REJ_BAD_ADDR: u8 = 0x0F;
/// IO Capability Request Negative Reply reason "Pairing Not Allowed".
const PAIRING_NOT_ALLOWED: u8 = 0x18;
const DISC_USER_ENDED: u8 = 0x13;
const DISC_AUTH_FAILURE: u8 = 0x05;
const KEY_DEBUG: u8 = 0x03;
/// Changed Combination Key: a new key value, the security of the old key (Core §7.7.24)
const KEY_CHANGED: u8 = 0x06;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Conn {
    pub handle: u16,
    pub addr: String,
    pub encrypted: bool,
    /// unique per connection: a command for this link is not sent to a reused handle
    pub serial: u64,
}

/// A numeric comparison waiting for the user.
#[derive(Clone, Debug, PartialEq)]
pub struct Confirm {
    pub addr: String,
    pub name: String,
    pub code: String,
    /// the other side has no display: there is no code to compare
    pub just_works: bool,
    pub until: f64,
}

/// A pairing (or reconnection) the user started from the phone.
#[derive(Clone, Debug, PartialEq)]
pub struct Pairing {
    /// the operation id its commands carry (a result for an earlier pairing is ignored)
    pub id: u64,
    pub addr: String,
    pub handle: Option<u16>,
    pub until: f64,
    /// Create Connection / Authentication Requested sent
    pub started: bool,
    /// a new link key was stored during this pairing
    pub new_key: bool,
    /// the device is connecting to us: our own Create Connection is no longer needed
    pub incoming: bool,
}

/// The user accepted the code of `addr`; its link key may be stored until `until`.
#[derive(Clone, Debug, PartialEq)]
pub struct Consent {
    pub id: u64,
    pub addr: String,
    pub until: f64,
}

/// A completion still due for an operation that has ended: the Command Status of Create
/// Connection / Authentication Requested / Disconnect comes first, its event later (Core Vol 4
/// Part E §7.1.5, §7.1.6, §7.1.15). Until it arrives (or `LATE_S` passes) a new pairing with that
/// device waits, so the old result is never taken for the new pairing's.
#[derive(Clone, Debug, PartialEq)]
pub struct Late {
    /// the operation that sent it (0: a Disconnect of no pairing)
    pub id: u64,
    pub addr: String,
    /// EV_CONN_COMPLETE, EV_AUTH_COMPLETE or EV_DISCONN_COMPLETE
    pub event: u8,
    pub until: f64,
}

fn u16le(p: &[u8], i: usize) -> u16 {
    u16::from_le_bytes([p[i], p[i + 1]])
}

fn addr_param(addr: &str) -> Vec<u8> {
    addr_bytes(addr).unwrap_or_default().to_vec()
}

impl Bluetooth {
    pub fn visible(&self) -> bool {
        self.visible_until.is_some()
    }

    /// Paired devices: (address, name, connected).
    pub fn paired(&self) -> Vec<(String, String, bool)> {
        self.keys
            .keys
            .iter()
            .map(|k| {
                let connected = self.conns.iter().any(|c| c.addr == k.addr);
                (k.addr.clone(), self.name_of(&k.addr), connected)
            })
            .collect()
    }

    /// The best known name, else the address.
    pub fn name_of(&self, addr: &str) -> String {
        self.keys
            .get(addr)
            .map(|k| k.name.clone())
            .filter(|n| !n.is_empty())
            .or_else(|| self.names.get(addr).cloned())
            .or_else(|| {
                self.devices
                    .iter()
                    .find(|(a, _)| a == addr)
                    .and_then(|(_, d)| d.name.clone())
            })
            .filter(|n| !n.is_empty())
            .unwrap_or_else(|| addr.to_string())
    }

    fn conn_by_handle(&self, handle: u16) -> Option<&Conn> {
        self.conns.iter().find(|c| c.handle == handle)
    }

    fn conn_by_addr(&self, addr: &str) -> Option<&Conn> {
        self.conns.iter().find(|c| c.addr == addr)
    }

    /// Pairing events are only answered while visible or for the pairing the user started.
    fn pairing_allowed(&self, addr: &str) -> bool {
        self.visible() || self.pairing.as_ref().is_some_and(|p| p.addr == addr)
    }

    /// The user accepted this device's code and its link key has not arrived yet.
    fn consented(&self, addr: &str) -> bool {
        self.confirmed.as_ref().is_some_and(|c| c.addr == addr)
    }

    /// Withdraws the user's consent for `addr` (its key will no longer be stored). A "yes" still
    /// waiting for a command credit then goes out as a refusal (see `refuse_yes`).
    fn drop_consent(&mut self, addr: &str) {
        if self.consented(addr) {
            self.confirmed = None;
        }
    }

    /// The user's "yes" under consent `id` that has ended (expired, withdrawn, or the screen
    /// locked) while it waited for a credit: the controller still expects an answer, a "no".
    pub(super) fn refuse_yes(&mut self, id: u64, pkt: &[u8]) -> Vec<u8> {
        if self.confirmed.as_ref().is_some_and(|c| c.id == id) {
            self.confirmed = None;
        }
        self.message = "koppelen verlopen".into();
        self.changed = true;
        command(USER_CONFIRM_NEG_REPLY, &pkt[4..10])
    }

    /// Other answers (`Owner::Reply`) are checked again as they go out: the visibility, pairing or
    /// key they relied on may have ended or changed. The controller still expects an answer, so it
    /// becomes the refusal, and a link key answer carries the key stored now (or none).
    pub(super) fn vet_reply(&mut self, pkt: Vec<u8>) -> Vec<u8> {
        if pkt.len() < 10 {
            return pkt;
        }
        let raw = &pkt[4..10];
        let addr = addr_str(raw);
        let now = (self.clock)();
        let visible = self.visible_until.is_some_and(|u| now <= u);
        let ours = self
            .pairing
            .as_ref()
            .is_some_and(|p| p.addr == addr && now <= p.until);
        match u16le(&pkt, 1) {
            LINK_KEY_REPLY | LINK_KEY_NEG_REPLY => match self.keys.get(&addr) {
                Some(k) => command(LINK_KEY_REPLY, &[raw, &k.key[..]].concat()),
                None => command(LINK_KEY_NEG_REPLY, raw),
            },
            ACCEPT_CONNECTION if !(visible || ours || self.keys.get(&addr).is_some()) => {
                command(REJECT_CONNECTION, &[raw, &[REJ_BAD_ADDR]].concat())
            }
            IO_CAP_REPLY if !(visible || ours) => {
                command(IO_CAP_NEG_REPLY, &[raw, &[PAIRING_NOT_ALLOWED]].concat())
            }
            _ => pkt,
        }
    }

    /// Closes link `handle` to `addr`; a pairing with that device waits for it to be gone.
    fn disconnect(&mut self, handle: u16, addr: &str, reason: u8, id: u64) {
        let Some(serial) = self.conn_by_handle(handle).map(|c| c.serial) else {
            return;
        };
        let params = [&handle.to_le_bytes()[..], &[reason]].concat();
        self.send(DISCONNECT, &params, Owner::Link(serial));
        self.abandoned.push(Late {
            id,
            addr: addr.to_string(),
            event: EV_DISCONN_COMPLETE,
            until: (self.clock)() + LATE_S,
        });
    }

    /// Another device's pairing holds the phone, from the code on screen until its key is
    /// stored or the pairing fails: one pairing at a time.
    fn pairing_busy(&self, addr: &str) -> bool {
        self.pending.as_ref().is_some_and(|c| c.addr != addr)
            || self.confirmed.as_ref().is_some_and(|c| c.addr != addr)
            || self.pairing.as_ref().is_some_and(|p| p.addr != addr)
    }

    /// Nothing that needs the radio: an automatic switch-on may end.
    pub fn idle(&self) -> bool {
        self.conns.is_empty()
            && self.pending.is_none()
            && self.pairing.is_none()
            && self.visible_until.is_none()
            && self.phase.is_none()
    }

    /// Something needs the clock soon (the runner then wakes at least once a second).
    pub fn needs_tick(&self) -> bool {
        self.phase.is_some()
            || self.pending.is_some()
            || self.pairing.is_some()
            || self.visible()
            || (self.auto_on && self.on())
    }

    /// Visible for two minutes (inquiry + page scan); off again returns to page scan only.
    pub fn set_visible(&mut self, on: bool) {
        self.changed = true;
        if on {
            if self.device.is_none() {
                if !self.power(true) {
                    return;
                }
                self.auto_on = true;
            }
            self.visible_until = Some((self.clock)() + VISIBLE_S);
            self.message = "zichtbaar als I14 (2 min)".into();
        } else if self.visible_until.take().is_some() {
            self.message = "niet meer zichtbaar".into();
        }
        self.apply_scan();
    }

    /// Inquiry + page scan while visible, else page scan only.
    pub(super) fn scan_mode(&self) -> u8 {
        if self.visible() {
            0x03
        } else {
            0x02
        }
    }

    /// Writes the scan mode the state asks for, once ready and only when it changes. One write
    /// waits at a time; its value is taken when it goes out (`vet`), so a stale one never is.
    pub(super) fn apply_scan(&mut self) {
        let queued = self.cmd_queue.iter().any(|(_, o)| *o == Owner::Scan);
        if self.ready && !queued && self.scan_sent != Some(self.scan_mode()) {
            self.send(WRITE_SCAN_ENABLE, &[self.scan_mode()], Owner::Scan);
        }
    }

    /// Pairs with (or, when a key is stored, reconnects to) a classic device: Create Connection,
    /// then Authentication Requested. One pairing at a time.
    pub fn pair(&mut self, addr: &str) {
        self.changed = true;
        let Some(addr) = super::keys::norm_addr(addr) else {
            self.message = format!("ongeldig adres {addr}");
            return;
        };
        // an accepted code whose key has not arrived still holds the phone
        if self.pairing.is_some() || self.pending.is_some() || self.confirmed.is_some() {
            self.message = "er loopt al een koppeling".into();
            return;
        }
        if self.device.is_none() {
            if !self.power(true) {
                return;
            }
            self.auto_on = true;
        }
        self.stop_search();
        self.op_id += 1;
        self.pairing = Some(Pairing {
            id: self.op_id,
            addr: addr.clone(),
            handle: None,
            until: (self.clock)() + PAIR_S,
            started: false,
            new_key: false,
            incoming: false,
        });
        self.message = format!("verbinden met {}...", self.name_of(&addr));
        if self.ready {
            self.start_pairing();
        }
    }

    /// Sends the first command of a waiting pairing (also called once the controller is ready).
    /// A pairing waits while an earlier operation with the same device still has a completion due.
    pub(super) fn start_pairing(&mut self) {
        let now = (self.clock)();
        let held = |p: &Pairing| self.abandoned.iter().any(|l| l.addr == p.addr);
        if !self.ready
            || !self
                .pairing
                .as_ref()
                .is_some_and(|p| !p.started && !held(p))
        {
            return;
        }
        let Some(p) = self.pairing.as_mut() else {
            return;
        };
        p.started = true;
        p.until = now + PAIR_S; // the wait does not count against the pairing
        let (id, addr) = (p.id, p.addr.clone());
        if let Some(handle) = self.conn_by_addr(&addr).map(|c| c.handle) {
            if let Some(p) = self.pairing.as_mut() {
                p.handle = Some(handle);
            }
            self.send(AUTH_REQUESTED, &handle.to_le_bytes(), Owner::Auth(id));
            return;
        }
        let psrm = self
            .devices
            .iter()
            .find(|(a, _)| *a == addr)
            .and_then(|(_, d)| d.psrm)
            .unwrap_or(1);
        let mut params = addr_param(&addr);
        // DM1/DH1/DM3/DH3/DM5/DH5, scan repetition mode, reserved, no clock offset, role switch ok
        params.extend_from_slice(&[0x18, 0xCC, psrm, 0x00, 0x00, 0x00, 0x01]);
        self.send(CREATE_CONNECTION, &params, Owner::Connect(id));
    }

    fn end_pairing(&mut self, message: String) {
        self.abandon();
        self.message = message;
        self.changed = true;
    }

    /// Ends the pairing (returns its id, 0 for none). Its Create Connection / Authentication
    /// Requested still waiting for a credit is then not sent; one already sent leaves its
    /// completion due (see `Late`): a late connection is closed, and a retry waits for it.
    fn abandon(&mut self) -> u64 {
        let Some(p) = self.pairing.clone() else {
            return 0;
        };
        let (owner, event) = match p.handle {
            None => (Owner::Connect(p.id), EV_CONN_COMPLETE),
            Some(_) => (Owner::Auth(p.id), EV_AUTH_COMPLETE),
        };
        if p.started && !self.unsent(owner) {
            self.abandoned.push(Late {
                id: p.id,
                addr: p.addr.clone(),
                event,
                until: (self.clock)() + LATE_S,
            });
        }
        self.pairing = None;
        p.id
    }

    /// The user's answer to the code on screen. While the screen is PIN-locked only "no" counts:
    /// a locked phone never completes a pairing.
    pub fn confirm(&mut self, accept: bool) {
        if accept && self.ui_locked {
            return;
        }
        let Some(c) = self.pending.take() else {
            return;
        };
        self.changed = true;
        if accept {
            self.op_id += 1;
            let until = (self.clock)() + PAIR_S;
            self.confirmed = Some(Consent {
                id: self.op_id,
                addr: c.addr.clone(),
                until,
            });
            self.send(
                USER_CONFIRM_REPLY,
                &addr_param(&c.addr),
                Owner::Consent(self.op_id),
            );
            self.message = format!("koppelen met {}...", c.name);
        } else {
            // Any: a refusal is right whatever happens meanwhile
            self.send(USER_CONFIRM_NEG_REPLY, &addr_param(&c.addr), Owner::Any);
            self.message = "koppelen geweigerd".into();
        }
    }

    /// Removes the stored key and closes the link to that device.
    pub fn forget(&mut self, addr: &str) {
        self.changed = true;
        let name = self.name_of(addr);
        if let Err(m) = self.keys.remove(addr) {
            self.message = m;
            return;
        }
        if self.pending.as_ref().is_some_and(|c| c.addr == addr) {
            self.confirm(false);
        }
        // a pairing in flight is cancelled: its key must not bring the bond back
        self.drop_consent(addr);
        // no new connection for it; a queued key reply or "yes" goes out as a refusal (`vet`)
        let ours = self.pairing.as_ref().is_some_and(|p| p.addr == addr);
        let id = if ours { self.abandon() } else { 0 };
        if let Some(h) = self.conn_by_addr(addr).map(|c| c.handle) {
            self.disconnect(h, addr, DISC_USER_ENDED, id);
        }
        self.message = format!("vergeten: {name}");
    }

    /// Timeouts of the confirmation, the visibility and a pairing.
    pub(super) fn tick_link(&mut self, now: f64) {
        if self.pending.as_ref().is_some_and(|c| now > c.until) {
            self.confirm(false);
            self.message = "koppelen verlopen".into();
        }
        if self.visible_until.is_some_and(|u| now > u) {
            self.set_visible(false);
        }
        if self.confirmed.as_ref().is_some_and(|c| now > c.until) {
            self.confirmed = None; // no key came: the pairing no longer holds the phone
        }
        if let Some(p) = self.pairing.clone().filter(|p| now > p.until) {
            if let Some(h) = p.handle {
                self.disconnect(h, &p.addr, DISC_USER_ENDED, p.id);
            }
            self.end_pairing("koppelen verlopen".into());
        }
        // a completion that never came holds a retry only so long
        self.abandoned.retain(|l| now <= l.until);
        self.start_pairing();
    }

    /// Command Status of Create Connection / Authentication Requested; only the pairing that sent
    /// the command ends on its failure.
    pub(super) fn link_command_status(&mut self, owner: Owner, status: u8) {
        let (Owner::Connect(id) | Owner::Auth(id)) = owner else {
            return;
        };
        if status == 0 {
            return;
        }
        // (a pairing whose device connected to us meanwhile keeps that link)
        if self.current(owner) {
            let m = if owner == Owner::Connect(id) {
                "verbinden"
            } else {
                "koppelen"
            };
            self.end_pairing(format!("{m} mislukt (status {status})"));
        }
        // a refused command brings no completion
        self.abandoned
            .retain(|l| !(l.id == id && matches!(l.event, EV_CONN_COMPLETE | EV_AUTH_COMPLETE)));
        self.start_pairing();
    }

    /// One ACL packet: reassembled, and the signalling channel answered.
    pub(super) fn acl(&mut self, handle: u16, pb: u8, data: &[u8]) {
        if self.conn_by_handle(handle).is_none() {
            return; // no such link (or it just closed): nothing to answer
        }
        let Some((cid, payload)) = self.reasm.feed(handle, pb, data) else {
            return;
        };
        if cid != l2cap::SIG_CID {
            return;
        }
        for reply in l2cap::signalling(&payload) {
            let pkts = l2cap::acl_packets(
                handle,
                &l2cap::frame(l2cap::SIG_CID, &reply),
                self.acl_out.mtu,
            );
            self.acl_out.push(handle, pkts);
        }
        self.flush_acl();
    }

    pub(super) fn flush_acl(&mut self) {
        for pkt in self.acl_out.take() {
            if !self.write_raw(&pkt) {
                return;
            }
        }
    }

    /// Link and pairing events; false when `code` is not one of them (or too short to use).
    pub(super) fn handle_link(&mut self, code: u8, p: &[u8]) -> bool {
        match code {
            EV_CONN_REQUEST if p.len() >= 10 => {
                let addr = addr_str(&p[..6]);
                if p[9] != 0x01 {
                    // SCO/eSCO: no audio here
                    let mut params = p[..6].to_vec();
                    params.push(REJ_LIMITED_RESOURCES);
                    self.send(REJECT_CONNECTION, &params, Owner::Any); // Any: a refusal
                } else if self.visible()
                    || self.keys.get(&addr).is_some()
                    || self.pairing.as_ref().is_some_and(|x| x.addr == addr)
                {
                    if let Some(x) = self
                        .pairing
                        .as_mut()
                        .filter(|x| x.addr == addr && x.handle.is_none())
                    {
                        x.incoming = true; // our unsent Create Connection is no longer current
                    }
                    let mut params = p[..6].to_vec();
                    params.push(0x01); // remain peripheral
                    self.send(ACCEPT_CONNECTION, &params, Owner::Reply);
                } else {
                    let mut params = p[..6].to_vec();
                    params.push(REJ_BAD_ADDR);
                    self.send(REJECT_CONNECTION, &params, Owner::Any); // Any: a refusal
                }
            }
            EV_CONN_COMPLETE if p.len() >= 11 => {
                let (status, handle, addr) = (p[0], u16le(p, 1) & 0x0fff, addr_str(&p[3..9]));
                let abandoned = self
                    .abandoned
                    .iter()
                    .any(|l| l.addr == addr && l.event == EV_CONN_COMPLETE);
                // the pairing that is connecting: its Create Connection out, or the device
                // connecting to us first
                let ours = !abandoned
                    && self
                        .pairing
                        .as_ref()
                        .is_some_and(|x| x.addr == addr && x.started && x.handle.is_none());
                let id = self.pairing.as_ref().map_or(0, |x| x.id);
                if status == 0 && p[9] == 0x01 {
                    self.conns.retain(|c| c.handle != handle);
                    self.op_id += 1;
                    self.conns.push(Conn {
                        handle,
                        addr: addr.clone(),
                        encrypted: p[10] != 0,
                        serial: self.op_id,
                    });
                    if ours {
                        // the link exists: an unsent Create Connection is no longer current
                        if let Some(x) = self.pairing.as_mut() {
                            x.handle = Some(handle);
                        }
                        self.send(AUTH_REQUESTED, &handle.to_le_bytes(), Owner::Auth(id));
                    } else if abandoned {
                        // the pairing that asked for this link has ended
                        self.disconnect(handle, &addr, DISC_USER_ENDED, 0);
                    } else {
                        self.message = format!("verbonden met {}", self.name_of(&addr));
                    }
                } else if ours && status != 0 && !self.unsent(Owner::Connect(id)) {
                    // (with our Create Connection unsent, the failure is of the device's attempt)
                    self.end_pairing(format!("verbinden mislukt (status {status})"));
                }
                // its connection came (or failed): nothing more to close later
                self.abandoned
                    .retain(|l| !(l.addr == addr && l.event == EV_CONN_COMPLETE));
                self.start_pairing();
            }
            EV_DISCONN_COMPLETE if p.len() >= 4 => {
                if p[0] == 0 {
                    let handle = u16le(p, 1) & 0x0fff;
                    self.reasm.drop_handle(handle);
                    self.acl_out.drop_handle(handle);
                    if let Some(i) = self.conns.iter().position(|c| c.handle == handle) {
                        let c = self.conns.remove(i);
                        let name = self.name_of(&c.addr);
                        let pending = self.pending.take_if(|x| x.addr == c.addr).is_some();
                        // only the pairing using this link ends: a retry waiting for it goes on
                        if self
                            .pairing
                            .as_ref()
                            .is_some_and(|x| x.handle == Some(handle))
                        {
                            self.end_pairing(format!("koppelen met {name} afgebroken"));
                        } else if pending {
                            self.message = format!("koppelen met {name} afgebroken");
                        } else {
                            self.message = format!("verbinding met {name} verbroken");
                        }
                        self.drop_consent(&c.addr);
                        // the link is gone: nothing more comes for it
                        self.abandoned.retain(|l| l.addr != c.addr);
                        self.flush_acl();
                        self.start_pairing();
                    }
                }
            }
            EV_AUTH_COMPLETE if p.len() >= 3 => {
                let (status, handle) = (p[0], u16le(p, 1) & 0x0fff);
                let addr = self.conn_by_handle(handle).map(|c| c.addr.clone());
                let late = self
                    .abandoned
                    .iter()
                    .position(|l| Some(&l.addr) == addr.as_ref() && l.event == EV_AUTH_COMPLETE);
                if let Some(i) = late {
                    // the result of a pairing that has ended
                    self.abandoned.remove(i);
                    self.start_pairing();
                } else if let Some(x) = self.pairing.clone().filter(|x| x.handle == Some(handle)) {
                    let name = self.name_of(&x.addr);
                    let m = match status {
                        0 if x.new_key => format!("gekoppeld met {name}"),
                        0 => format!("verbonden met {name}"),
                        0x06 if self.keys.get(&x.addr).is_some() => format!(
                            "sleutel geweigerd door {name} (status 6): vergeet en koppel opnieuw"
                        ),
                        s => format!("koppelen mislukt (status {s})"),
                    };
                    self.end_pairing(m);
                    // this was its completion: nothing more is due
                    self.abandoned
                        .retain(|l| !(l.id == x.id && l.event == EV_AUTH_COMPLETE));
                }
            }
            EV_ENCRYPT_CHANGE if p.len() >= 4 => {
                let handle = u16le(p, 1) & 0x0fff;
                if p[0] == 0 {
                    if let Some(c) = self.conns.iter_mut().find(|c| c.handle == handle) {
                        c.encrypted = p[3] != 0;
                    }
                }
            }
            EV_NUM_COMPLETED if !p.is_empty() => {
                let n = p[0] as usize;
                for i in 0..n {
                    let off = 1 + i * 4;
                    if off + 4 > p.len() {
                        break;
                    }
                    self.acl_out
                        .completed(u16le(p, off) & 0x0fff, u16le(p, off + 2) as usize);
                }
                self.flush_acl();
            }
            EV_PIN_REQUEST if p.len() >= 6 => {
                self.send(PIN_CODE_NEG_REPLY, &p[..6], Owner::Any); // Any: a refusal
                self.message = "oude koppelmethode niet ondersteund".into();
            }
            EV_LINK_KEY_REQUEST if p.len() >= 6 => {
                // the key (or none) is the one stored when the answer goes out (`vet_reply`)
                self.send(LINK_KEY_NEG_REPLY, &p[..6], Owner::Reply);
            }
            EV_LINK_KEY_NOTIFY if p.len() >= 23 => {
                let addr = addr_str(&p[..6]);
                let mut key = [0u8; 16];
                key.copy_from_slice(&p[6..22]);
                let known = self.keys.get(&addr).map(|k| k.kind);
                let kind = match (p[22], known) {
                    (KEY_CHANGED, Some(old)) => old,
                    (k, _) => k,
                };
                if !self.consented(&addr) && known.is_none() {
                    // never confirmed by the user: not stored
                    self.message =
                        format!("onverwachte sleutel van {} genegeerd", self.name_of(&addr));
                } else if kind == KEY_DEBUG {
                    self.message = "debugsleutel geweigerd".into();
                } else {
                    let name = self.name_of(&addr);
                    let store_name = if name == addr { "" } else { name.as_str() };
                    match self.keys.put(&addr, key, kind, store_name) {
                        Ok(()) => {
                            self.message = format!("gekoppeld met {name}");
                            if let Some(x) = self.pairing.as_mut().filter(|x| x.addr == addr) {
                                x.new_key = true;
                            }
                            if store_name.is_empty() && self.phase.is_none() {
                                let mut params = p[..6].to_vec();
                                params.extend_from_slice(&[0x01, 0, 0, 0]);
                                self.send(REMOTE_NAME, &params, Owner::PairedName);
                            }
                        }
                        Err(m) => self.message = m,
                    }
                }
                self.drop_consent(&addr); // consent is used up only by its own device
            }
            EV_IO_CAP_REQUEST if p.len() >= 6 => {
                let addr = addr_str(&p[..6]);
                let mut params = p[..6].to_vec();
                if self.pairing_allowed(&addr) {
                    params.extend_from_slice(&[IO_DISPLAY_YES_NO, 0x00, AUTH_MITM_DEDICATED]);
                    self.send(IO_CAP_REPLY, &params, Owner::Reply);
                } else {
                    params.push(PAIRING_NOT_ALLOWED);
                    self.send(IO_CAP_NEG_REPLY, &params, Owner::Any); // Any: a refusal
                    self.message = format!(
                        "koppelverzoek van {} geweigerd (niet zichtbaar)",
                        self.name_of(&addr)
                    );
                }
            }
            EV_IO_CAP_RESPONSE if p.len() >= 9 => {
                self.remote_io.insert(addr_str(&p[..6]), p[6]);
            }
            EV_USER_CONFIRM if p.len() >= 10 => {
                let addr = addr_str(&p[..6]);
                if !self.pairing_allowed(&addr) || self.pairing_busy(&addr) {
                    self.send(USER_CONFIRM_NEG_REPLY, &p[..6], Owner::Any); // Any: a refusal
                } else {
                    let value = u32::from_le_bytes([p[6], p[7], p[8], p[9]]) % 1_000_000;
                    let name = self.name_of(&addr);
                    self.message = format!("koppelverzoek van {name}");
                    self.pending = Some(Confirm {
                        just_works: self.remote_io.get(&addr) == Some(&0x03),
                        addr,
                        name,
                        code: format!("{value:06}"),
                        until: (self.clock)() + CONFIRM_S,
                    });
                    self.wake = true;
                }
            }
            EV_USER_PASSKEY if p.len() >= 6 => {
                self.send(USER_PASSKEY_NEG_REPLY, &p[..6], Owner::Any); // Any: a refusal
                self.message = "koppelen met code intypen niet ondersteund".into();
            }
            EV_PASSKEY_NOTIFY if p.len() >= 10 => {
                // the other side would type a code: there is no "Ja" on the phone for that,
                // so the link is closed instead of pairing without the user
                let addr = addr_str(&p[..6]);
                if let Some(h) = self.conn_by_addr(&addr).map(|c| c.handle) {
                    self.disconnect(h, &addr, DISC_AUTH_FAILURE, 0);
                }
                self.message = "koppelen met toetsenbordcode niet ondersteund".into();
            }
            EV_SSP_COMPLETE if p.len() >= 7 => {
                let addr = addr_str(&p[1..7]);
                if p[0] != 0 {
                    if self.pending.as_ref().is_some_and(|c| c.addr == addr) {
                        self.pending = None;
                    }
                    self.drop_consent(&addr);
                    self.message = format!("koppelen mislukt (status {})", p[0]);
                }
            }
            _ => return false,
        }
        true
    }
}
