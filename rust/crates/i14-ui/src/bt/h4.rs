pub const CMD: u8 = 0x01;
pub const EVT: u8 = 0x04;
// opcodes (OGF << 10 | OCF)
pub const RESET: u16 = 0x0C03;
pub const SET_EVENT_MASK: u16 = 0x0C01;
pub const WRITE_INQUIRY_MODE: u16 = 0x0C45;
pub const READ_BD_ADDR: u16 = 0x1009;
pub const INQUIRY: u16 = 0x0401;
pub const INQUIRY_CANCEL: u16 = 0x0402;
pub const REMOTE_NAME: u16 = 0x0419;
pub const LE_SET_EVENT_MASK: u16 = 0x2001;
pub const LE_SCAN_PARAMS: u16 = 0x200B;
pub const LE_SCAN_ENABLE: u16 = 0x200C;
// controller set-up for pairing
pub const READ_LOCAL_VERSION: u16 = 0x1001;
pub const READ_LOCAL_COMMANDS: u16 = 0x1002;
pub const READ_BUFFER_SIZE: u16 = 0x1005;
pub const WRITE_SIMPLE_PAIRING_MODE: u16 = 0x0C56;
pub const WRITE_SC_HOST_SUPPORT: u16 = 0x0C7A;
pub const WRITE_LOCAL_NAME: u16 = 0x0C13;
pub const WRITE_CLASS_OF_DEVICE: u16 = 0x0C24;
pub const WRITE_EIR: u16 = 0x0C52;
pub const WRITE_SCAN_ENABLE: u16 = 0x0C1A;
// links and pairing
pub const CREATE_CONNECTION: u16 = 0x0405;
pub const DISCONNECT: u16 = 0x0406;
pub const ACCEPT_CONNECTION: u16 = 0x0409;
pub const REJECT_CONNECTION: u16 = 0x040A;
pub const LINK_KEY_REPLY: u16 = 0x040B;
pub const LINK_KEY_NEG_REPLY: u16 = 0x040C;
pub const PIN_CODE_NEG_REPLY: u16 = 0x040E;
pub const AUTH_REQUESTED: u16 = 0x0411;
pub const IO_CAP_REPLY: u16 = 0x042B;
pub const USER_CONFIRM_REPLY: u16 = 0x042C;
pub const USER_CONFIRM_NEG_REPLY: u16 = 0x042D;
pub const USER_PASSKEY_NEG_REPLY: u16 = 0x042F;
pub const IO_CAP_NEG_REPLY: u16 = 0x0434;
/// Supported-commands bitmap (Core spec Vol 4 Part E 6.27): "Write Secure Connections Host
/// Support" is octet 32 bit 3 (bit number 259 in BlueZ monitor/packet.c; Reset = 47 = octet 5 bit 7).
pub const SC_HOST_SUPPORT_OCTET: usize = 32;
pub const SC_HOST_SUPPORT_MASK: u8 = 0x08;
pub const EV_INQ_COMPLETE: u8 = 0x01;
pub const EV_INQ_RESULT: u8 = 0x02;
pub const EV_CMD_COMPLETE: u8 = 0x0E;
pub const EV_CMD_STATUS: u8 = 0x0F;
pub const EV_NAME: u8 = 0x07;
pub const EV_INQ_RSSI: u8 = 0x22;
pub const EV_EXT_INQ: u8 = 0x2F;
pub const EV_LE: u8 = 0x3E;
pub const EV_CONN_COMPLETE: u8 = 0x03;
pub const EV_CONN_REQUEST: u8 = 0x04;
pub const EV_DISCONN_COMPLETE: u8 = 0x05;
pub const EV_AUTH_COMPLETE: u8 = 0x06;
pub const EV_ENCRYPT_CHANGE: u8 = 0x08;
pub const EV_NUM_COMPLETED: u8 = 0x13;
pub const EV_PIN_REQUEST: u8 = 0x16;
pub const EV_LINK_KEY_REQUEST: u8 = 0x17;
pub const EV_LINK_KEY_NOTIFY: u8 = 0x18;
pub const EV_IO_CAP_REQUEST: u8 = 0x31;
pub const EV_IO_CAP_RESPONSE: u8 = 0x32;
pub const EV_USER_CONFIRM: u8 = 0x33;
pub const EV_USER_PASSKEY: u8 = 0x34;
pub const EV_SSP_COMPLETE: u8 = 0x36;
pub const EV_PASSKEY_NOTIFY: u8 = 0x3B;
pub const ACL: u8 = 0x02;
pub const INQUIRY_S: f64 = 10.24;
pub const LE_S: f64 = 6.0;

pub fn command(opcode: u16, params: &[u8]) -> Vec<u8> {
    let mut v = vec![CMD];
    v.extend_from_slice(&opcode.to_le_bytes());
    v.push(params.len() as u8);
    v.extend_from_slice(params);
    v
}

#[derive(Debug, PartialEq, Eq)]
pub enum Pkt {
    Event(u8, Vec<u8>),
    Acl { handle: u16, pb: u8, data: Vec<u8> },
}

#[derive(Default)]
pub struct Packets {
    pub buf: Vec<u8>,
}
impl Packets {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn feed(&mut self, data: &[u8]) -> Vec<Pkt> {
        self.buf.extend_from_slice(data);
        let mut out = Vec::new();
        while !self.buf.is_empty() {
            let (header, len) = match self.buf[0] {
                4 if self.buf.len() >= 3 => (3, self.buf[2] as usize),
                2 if self.buf.len() >= 5 => {
                    (5, u16::from_le_bytes([self.buf[3], self.buf[4]]) as usize)
                }
                3 if self.buf.len() >= 4 => (4, self.buf[3] as usize),
                2..=4 => break,
                _ => {
                    self.buf.remove(0);
                    continue;
                }
            };
            if self.buf.len() < header + len {
                break;
            }
            match self.buf[0] {
                4 => out.push(Pkt::Event(
                    self.buf[1],
                    self.buf[header..header + len].to_vec(),
                )),
                2 => {
                    let flags = u16::from_le_bytes([self.buf[1], self.buf[2]]);
                    out.push(Pkt::Acl {
                        handle: flags & 0x0fff,
                        pb: ((flags >> 12) & 3) as u8,
                        data: self.buf[header..header + len].to_vec(),
                    });
                }
                _ => {}
            }
            self.buf.drain(..header + len);
        }
        out
    }
}
