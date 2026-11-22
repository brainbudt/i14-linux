//! Minimal L2CAP on the ACL links: the phone offers no channels, so it only answers the
//! signalling channel (CID 1) politely: information requests, connection requests refused with
//! "PSM not supported", echo, and a command reject for anything else. Fixed and dynamic channels
//! other than signalling are ignored. Outgoing ACL respects the controller's buffer credits.

use std::collections::{HashMap, VecDeque};

pub const SIG_CID: u16 = 0x0001;
/// Largest L2CAP frame reassembled; a peer sending more is dropped (we offer no channel that needs it).
pub const MAX_FRAME: usize = 1024;
/// BR/EDR minimum signalling MTU: our replies stay within it.
pub const SIG_MTU: usize = 48;
/// ACL packets waiting for credits; more are dropped (oldest first).
pub const MAX_QUEUE: usize = 32;

const CMD_REJECT: u8 = 0x01;
const CONN_REQ: u8 = 0x02;
const CONN_RSP: u8 = 0x03;
const CONF_REQ: u8 = 0x04;
const DISCONN_REQ: u8 = 0x06;
const ECHO_REQ: u8 = 0x08;
const ECHO_RSP: u8 = 0x09;
const INFO_REQ: u8 = 0x0A;
const INFO_RSP: u8 = 0x0B;

/// Per-handle reassembly of L2CAP frames from ACL fragments.
#[derive(Default)]
pub struct Reassembly {
    bufs: HashMap<u16, Vec<u8>>,
}

impl Reassembly {
    /// One ACL packet in; a complete frame (cid, payload) out. PB 0x02/0x00 start a frame,
    /// 0x01 continues one; a continuation without a start is dropped.
    pub fn feed(&mut self, handle: u16, pb: u8, data: &[u8]) -> Option<(u16, Vec<u8>)> {
        let buf = if pb == 0x01 {
            self.bufs.get_mut(&handle)?
        } else {
            self.bufs.insert(handle, Vec::new());
            self.bufs.get_mut(&handle).unwrap()
        };
        buf.extend_from_slice(data);
        if buf.len() > MAX_FRAME + 4 {
            self.bufs.remove(&handle);
            return None;
        }
        if buf.len() < 4 {
            return None;
        }
        let len = u16::from_le_bytes([buf[0], buf[1]]) as usize;
        if len > MAX_FRAME {
            self.bufs.remove(&handle);
            return None;
        }
        if buf.len() < 4 + len {
            return None;
        }
        let buf = self.bufs.remove(&handle).unwrap();
        let cid = u16::from_le_bytes([buf[2], buf[3]]);
        Some((cid, buf[4..4 + len].to_vec()))
    }

    pub fn drop_handle(&mut self, handle: u16) {
        self.bufs.remove(&handle);
    }

    pub fn clear(&mut self) {
        self.bufs.clear();
    }
}

fn cmd(code: u8, ident: u8, data: &[u8]) -> Vec<u8> {
    let mut v = vec![code, ident];
    v.extend_from_slice(&(data.len() as u16).to_le_bytes());
    v.extend_from_slice(data);
    v
}

fn u16_at(d: &[u8], i: usize) -> Option<u16> {
    d.get(i..i + 2).map(|b| u16::from_le_bytes([b[0], b[1]]))
}

/// The replies (each one signalling command) to one signalling C-frame.
pub fn signalling(frame: &[u8]) -> Vec<Vec<u8>> {
    let mut out = Vec::new();
    let mut i = 0;
    while i + 4 <= frame.len() {
        let (code, ident) = (frame[i], frame[i + 1]);
        let len = u16::from_le_bytes([frame[i + 2], frame[i + 3]]) as usize;
        let Some(data) = frame.get(i + 4..i + 4 + len) else {
            break; // truncated command: nothing more in this frame can be trusted
        };
        i += 4 + len;
        if ident == 0 {
            continue; // identifier 0 is invalid: silently dropped (Core Vol 3 Part A 4)
        }
        match code {
            INFO_REQ => {
                let Some(kind) = u16_at(data, 0) else {
                    out.push(cmd(CMD_REJECT, ident, &[0, 0]));
                    continue;
                };
                let mut d = kind.to_le_bytes().to_vec();
                match kind {
                    0x0002 => d.extend_from_slice(&[0, 0, 0, 0, 0, 0]), // result 0, no extended features
                    0x0003 => d.extend_from_slice(&[0, 0, 0x02, 0, 0, 0, 0, 0, 0, 0]), // signalling only
                    _ => d.extend_from_slice(&[0x01, 0x00]), // not supported
                }
                out.push(cmd(INFO_RSP, ident, &d));
            }
            CONN_REQ => {
                let Some(scid) = u16_at(data, 2) else {
                    out.push(cmd(CMD_REJECT, ident, &[0, 0]));
                    continue;
                };
                let mut d = vec![0, 0]; // no destination CID
                d.extend_from_slice(&scid.to_le_bytes());
                d.extend_from_slice(&[0x02, 0x00, 0x00, 0x00]); // PSM not supported, no status
                out.push(cmd(CONN_RSP, ident, &d));
            }
            ECHO_REQ => {
                let n = data.len().min(SIG_MTU - 4);
                out.push(cmd(ECHO_RSP, ident, &data[..n]));
            }
            CONF_REQ | DISCONN_REQ => {
                // no channel exists: "invalid CID in request" with the CIDs as given
                let mut d = vec![0x02, 0x00];
                match code {
                    CONF_REQ => {
                        d.extend_from_slice(&u16_at(data, 0).unwrap_or(0).to_le_bytes());
                        d.extend_from_slice(&[0, 0]);
                    }
                    _ => {
                        d.extend_from_slice(&u16_at(data, 0).unwrap_or(0).to_le_bytes());
                        d.extend_from_slice(&u16_at(data, 2).unwrap_or(0).to_le_bytes());
                    }
                }
                out.push(cmd(CMD_REJECT, ident, &d));
            }
            // responses (we never ask, so never answer them; a reject of a reject would loop)
            0x01 | 0x03 | 0x05 | 0x07 | 0x09 | 0x0B | 0x0D | 0x0F | 0x11 | 0x13 | 0x15 | 0x18 => {}
            _ => out.push(cmd(CMD_REJECT, ident, &[0, 0])), // command not understood
        }
    }
    out
}

/// Basic L2CAP frame: length, CID, payload.
pub fn frame(cid: u16, payload: &[u8]) -> Vec<u8> {
    let mut v = (payload.len() as u16).to_le_bytes().to_vec();
    v.extend_from_slice(&cid.to_le_bytes());
    v.extend_from_slice(payload);
    v
}

/// H4 ACL packets for one L2CAP frame: first fragment PB 0x00 (first, non-flushable), the rest
/// PB 0x01, each at most `mtu` bytes of data.
pub fn acl_packets(handle: u16, l2cap: &[u8], mtu: usize) -> Vec<Vec<u8>> {
    let mtu = mtu.max(1);
    l2cap
        .chunks(mtu)
        .enumerate()
        .map(|(i, chunk)| {
            let pb: u16 = if i == 0 { 0x00 } else { 0x01 };
            let mut v = vec![super::ACL];
            v.extend_from_slice(&((handle & 0x0fff) | (pb << 12)).to_le_bytes());
            v.extend_from_slice(&(chunk.len() as u16).to_le_bytes());
            v.extend_from_slice(chunk);
            v
        })
        .collect()
}

/// Outgoing ACL with the controller's buffer credits (Read Buffer Size, Number of Completed
/// Packets). Nothing is written without a credit.
#[derive(Default)]
pub struct AclOut {
    pub mtu: usize,
    pub credits: usize,
    pub max_credits: usize,
    pub queue: VecDeque<(u16, Vec<u8>)>,
    in_flight: HashMap<u16, usize>,
}

impl AclOut {
    pub fn set_buffers(&mut self, mtu: usize, packets: usize) {
        self.mtu = mtu;
        self.credits = packets;
        self.max_credits = packets;
        self.in_flight.clear();
    }

    pub fn push(&mut self, handle: u16, pkts: Vec<Vec<u8>>) {
        for p in pkts {
            if self.queue.len() >= MAX_QUEUE {
                self.queue.pop_front();
            }
            self.queue.push_back((handle, p));
        }
    }

    /// Packets that may be written now (each takes a credit).
    pub fn take(&mut self) -> Vec<Vec<u8>> {
        let mut out = Vec::new();
        while self.credits > 0 {
            let Some((h, p)) = self.queue.pop_front() else {
                break;
            };
            self.credits -= 1;
            *self.in_flight.entry(h).or_default() += 1;
            out.push(p);
        }
        out
    }

    /// Number of Completed Packets for one handle.
    pub fn completed(&mut self, handle: u16, n: usize) {
        let f = self.in_flight.entry(handle).or_default();
        let n = n.min(*f);
        *f -= n;
        self.credits = (self.credits + n).min(self.max_credits);
    }

    /// A link is gone: its packets in the controller are freed, its queued ones dropped.
    pub fn drop_handle(&mut self, handle: u16) {
        let n = self.in_flight.remove(&handle).unwrap_or(0);
        self.credits = (self.credits + n).min(self.max_credits);
        self.queue.retain(|(h, _)| *h != handle);
    }

    pub fn reset(&mut self) {
        *self = Self::default();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn information_request_answered() {
        let mut f = cmd(INFO_REQ, 1, &[2, 0]);
        f.extend(cmd(INFO_REQ, 2, &[3, 0]));
        f.extend(cmd(INFO_REQ, 3, &[1, 0]));
        f.extend(cmd(INFO_REQ, 4, &[])); // malformed: rejected, not a panic
        assert_eq!(
            signalling(&f),
            vec![
                cmd(INFO_RSP, 1, &[2, 0, 0, 0, 0, 0, 0, 0]),
                cmd(INFO_RSP, 2, &[3, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0]),
                cmd(INFO_RSP, 3, &[1, 0, 1, 0]),
                cmd(CMD_REJECT, 4, &[0, 0]),
            ]
        );
    }

    #[test]
    fn connection_request_refused_psm_not_supported() {
        // SDP (PSM 1) from source CID 0x0040: the remote's CID comes back, result 0x0002
        let r = signalling(&cmd(CONN_REQ, 7, &[1, 0, 0x40, 0]));
        assert_eq!(r, vec![cmd(CONN_RSP, 7, &[0, 0, 0x40, 0, 2, 0, 0, 0])]);
    }

    #[test]
    fn echo_answered() {
        assert_eq!(
            signalling(&cmd(ECHO_REQ, 9, b"hi")),
            vec![cmd(ECHO_RSP, 9, b"hi")]
        );
        let long = vec![7u8; 200];
        assert_eq!(signalling(&cmd(ECHO_REQ, 9, &long))[0].len(), SIG_MTU);
    }

    #[test]
    fn unknown_signal_rejected() {
        assert_eq!(
            signalling(&cmd(0x30, 5, &[1, 2])),
            vec![cmd(CMD_REJECT, 5, &[0, 0])]
        );
        // configuration / disconnection for a channel that does not exist: invalid CID
        assert_eq!(
            signalling(&cmd(CONF_REQ, 6, &[0x41, 0, 0, 0])),
            vec![cmd(CMD_REJECT, 6, &[2, 0, 0x41, 0, 0, 0])]
        );
        assert_eq!(
            signalling(&cmd(DISCONN_REQ, 8, &[0x41, 0, 0x40, 0])),
            vec![cmd(CMD_REJECT, 8, &[2, 0, 0x41, 0, 0x40, 0])]
        );
        // responses and a command reject are never answered (no reject loops); ident 0 dropped
        let mut f = cmd(CMD_REJECT, 5, &[0, 0]);
        f.extend(cmd(INFO_RSP, 6, &[2, 0, 0, 0]));
        f.extend(cmd(ECHO_REQ, 0, b"x"));
        assert!(signalling(&f).is_empty());
        // a truncated command ends the frame
        assert!(signalling(&[ECHO_REQ, 1, 10, 0, 1, 2]).is_empty());
        assert!(signalling(&[ECHO_REQ, 1]).is_empty());
    }

    #[test]
    fn fragmented_acl_reassembled() {
        let mut r = Reassembly::default();
        let whole = frame(SIG_CID, &cmd(ECHO_REQ, 1, b"abcdef"));
        assert_eq!(r.feed(1, 0x02, &whole[..3]), None);
        assert_eq!(
            r.feed(2, 0x01, &whole[3..]),
            None,
            "continuation without a start"
        );
        assert_eq!(r.feed(1, 0x01, &whole[3..6]), None);
        assert_eq!(
            r.feed(1, 0x01, &whole[6..]),
            Some((SIG_CID, cmd(ECHO_REQ, 1, b"abcdef")))
        );
        assert_eq!(
            r.feed(1, 0x01, &whole[6..]),
            None,
            "nothing left to continue"
        );
        // a new start discards a half frame
        assert_eq!(r.feed(1, 0x02, &whole[..5]), None);
        assert_eq!(
            r.feed(1, 0x02, &whole),
            Some((SIG_CID, cmd(ECHO_REQ, 1, b"abcdef")))
        );
        // an absurd length is dropped instead of buffered
        assert_eq!(r.feed(1, 0x02, &[0xff, 0xff, 1, 0]), None);
        assert_eq!(r.feed(1, 0x01, &[0; 10]), None);
    }

    #[test]
    fn no_send_without_credits() {
        let mut o = AclOut::default();
        o.push(1, acl_packets(1, &frame(1, &[0; 10]), 8));
        assert!(o.take().is_empty(), "no buffers read yet: nothing goes out");
        o.set_buffers(8, 1);
        o.queue.clear();
        o.push(1, acl_packets(1, &frame(1, &[0; 10]), 8)); // 14 bytes -> 2 fragments
        let first = o.take();
        assert_eq!(first.len(), 1);
        assert_eq!(&first[0][..5], &[2, 1, 0x00, 8, 0]);
        assert!(o.take().is_empty());
        o.completed(1, 1);
        let second = o.take();
        assert_eq!(&second[0][..5], &[2, 1, 0x10, 6, 0]);
        o.completed(7, 5); // a handle with nothing in flight gives no credits
        assert_eq!(o.credits, 0);
        o.drop_handle(1);
        assert_eq!(o.credits, 1);
    }
}
