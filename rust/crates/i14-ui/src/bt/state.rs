//! `/run/i14-bt.json`: what the Bluetooth side is doing, for tests over ssh. No key material:
//! only addresses, names, the 6-digit code on screen and where its buttons are.

use serde_json::{json, Value};

use super::Bluetooth;

pub const STATE_FILE: &str = "/run/i14-bt.json";

/// The confirmation dialog's buttons (x, y, w, h) on the 720x1600 screen; drawn by the app,
/// published here so a test can tap them.
pub const ACCEPT_RECT: (i32, i32, i32, i32) = (40, 1000, 300, 130);
pub const REJECT_RECT: (i32, i32, i32, i32) = (380, 1000, 300, 130);

fn center(r: (i32, i32, i32, i32)) -> Value {
    json!([r.0 + r.2 / 2, r.1 + r.3 / 2])
}

pub fn render(bt: &Bluetooth, now: f64) -> String {
    let pending = bt.pending.as_ref().map(|c| {
        json!({
            "addr": c.addr,
            "name": c.name,
            "code": c.code,
            "just_works": c.just_works,
            "seconds_left": (c.until - now).max(0.0).ceil() as i64,
            // PIN-locked: the buttons are not there (taps reach the PIN pad), unlock first
            "locked": bt.ui_locked,
            "tap": {"accept": center(ACCEPT_RECT), "reject": center(REJECT_RECT)},
        })
    });
    let paired: Vec<Value> = bt
        .paired()
        .into_iter()
        .map(|(addr, name, connected)| json!({"addr": addr, "name": name, "connected": connected}))
        .collect();
    let connected: Vec<&str> = bt.conns.iter().map(|c| c.addr.as_str()).collect();
    let v = json!({
        "on": bt.on(),
        "ready": bt.ready,
        "address": bt.address,
        "hci_version": bt.version,
        "ssp": bt.ssp,
        "secure_connections": bt.sc,
        "visible": bt.visible(),
        "visible_seconds_left": bt.visible_until.map(|u| (u - now).max(0.0).ceil() as i64),
        "pairing": bt.pairing.as_ref().map(|p| p.addr.clone()),
        "pending": pending,
        "paired": paired,
        "connected": connected,
        "message": bt.message,
    });
    let mut s = serde_json::to_string(&v).unwrap_or_default();
    s.push('\n');
    s
}
