//! Fastboot protocol over an abstract bulk transport (port of `fastboot_usb.py`).

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use crate::image::{MAX_IMAGE, is_hex_of_len, py_int16, validate_image};
use crate::{Error, Result};

/// Measured `getvar:product` of this I14.
pub const PRODUCT: &str = "MAGC6735_65U_1_M0";
/// The only variables `--query` may read.
pub const QUERIES: [&str; 6] = [
    "product",
    "unlocked",
    "secure",
    "max-download-size",
    "partition-size:boot",
    "partition-size:recovery",
];

pub const VENDOR_ID: u16 = 0x0bb4;
pub const PRODUCT_ID: u16 = 0x0c01;
pub const ENDPOINT_OUT: u8 = 0x01;
pub const ENDPOINT_IN: u8 = 0x81;
pub const WRITE_TIMEOUT_MS: u32 = 3000;
pub const READ_SIZE: usize = 512;
pub const CHUNK: usize = 16384;
const MAX_INFO: usize = 256;
const RESPONSE_BUDGET: Duration = Duration::from_secs(10);

/// Errors a transport may report. All but `Interrupted` become a concise
/// `FastbootError` ("... failed; stopped without retry"), like any Python
/// exception raised by `device.write`/`device.read`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransportError {
    Timeout,
    Io(String),
    /// Equivalent of a `KeyboardInterrupt` escaping the USB call.
    Interrupted,
}

/// Bulk transport to one claimed Fastboot interface (pyusb `device` subset).
pub trait Transport {
    /// Bulk OUT; returns the number of bytes written.
    fn write(
        &mut self,
        endpoint: u8,
        data: &[u8],
        timeout_ms: u32,
    ) -> Result<usize, TransportError>;
    /// Bulk IN of up to `size` bytes.
    fn read(
        &mut self,
        endpoint: u8,
        size: usize,
        timeout_ms: u32,
    ) -> Result<Vec<u8>, TransportError>;
    /// Physical port path (pyusb `device.port_numbers`), `None` if unknown.
    fn port_numbers(&self) -> Option<Vec<u8>>;
    /// Release the interface and device (pyusb `dispose_resources`).
    fn close(&mut self) -> Result<(), TransportError>;
}

/// One endpoint descriptor as pyusb exposes it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct EndpointInfo {
    pub address: u8,
    pub attributes: u8,
    pub max_packet_size: u16,
}

/// One interface (alternate setting) descriptor of the active configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InterfaceInfo {
    pub number: u8,
    pub alternate_setting: u8,
    pub class: u8,
    pub subclass: u8,
    pub protocol: u8,
    pub endpoints: Vec<EndpointInfo>,
}

impl InterfaceInfo {
    /// The measured I14 Fastboot interface.
    pub fn expected() -> Self {
        InterfaceInfo {
            number: 0,
            alternate_setting: 0,
            class: 255,
            subclass: 66,
            protocol: 3,
            endpoints: vec![
                EndpointInfo {
                    address: ENDPOINT_OUT,
                    attributes: 2,
                    max_packet_size: 512,
                },
                EndpointInfo {
                    address: ENDPOINT_IN,
                    attributes: 2,
                    max_packet_size: 512,
                },
            ],
        }
    }
}

/// A USB device found by VID/PID, before any interface is claimed.
pub trait Candidate {
    fn vendor_product(&self) -> (u16, u16);
    /// Every interface descriptor (all alternate settings) of the active
    /// configuration, like iterating pyusb's `get_active_configuration()`.
    fn active_interfaces(&mut self) -> Result<Vec<InterfaceInfo>>;
}

/// Require one candidate and the measured active Fastboot interface.
pub fn select_device<C: Candidate>(devices: Vec<C>) -> Result<C> {
    if devices.len() != 1 {
        return Err(Error::fastboot("expected exactly one matching USB device"));
    }
    let Some(mut device) = devices.into_iter().next() else {
        return Err(Error::fastboot("expected exactly one matching USB device"));
    };
    if device.vendor_product() != (VENDOR_ID, PRODUCT_ID) {
        return Err(Error::fastboot("USB VID/PID mismatch"));
    }
    let interfaces = device.active_interfaces()?;
    if interfaces.len() != 1 {
        return Err(Error::fastboot("unexpected active USB interface layout"));
    }
    let interface = &interfaces[0];
    let signature = (
        interface.number,
        interface.alternate_setting,
        interface.class,
        interface.subclass,
        interface.protocol,
    );
    let mut endpoints = interface.endpoints.clone();
    endpoints.sort();
    let expected = InterfaceInfo::expected();
    if signature != (0, 0, 255, 66, 3) || endpoints != expected.endpoints {
        return Err(Error::fastboot("Fastboot interface or endpoint mismatch"));
    }
    Ok(device)
}

/// Fastboot client; every action re-checks the product first.
pub struct Fastboot<T: Transport> {
    pub device: T,
    interrupt: Option<Arc<AtomicBool>>,
    response_budget: Duration,
}

impl<T: Transport> Fastboot<T> {
    pub fn new(device: T) -> Self {
        Fastboot {
            device,
            interrupt: None,
            response_budget: RESPONSE_BUDGET,
        }
    }

    /// Treat a set flag (e.g. from a SIGINT handler) like a Python
    /// `KeyboardInterrupt` at the next USB call boundary.
    pub fn with_interrupt(mut self, flag: Arc<AtomicBool>) -> Self {
        self.interrupt = Some(flag);
        self
    }

    /// Override the 10 s response deadline (tests only need this).
    pub fn with_response_budget(mut self, budget: Duration) -> Self {
        self.response_budget = budget;
        self
    }

    pub fn close(&mut self) -> Result<(), TransportError> {
        self.device.close()
    }

    fn check_interrupt(&self) -> Result<()> {
        match &self.interrupt {
            Some(flag) if flag.load(Ordering::SeqCst) => Err(Error::Interrupted),
            _ => Ok(()),
        }
    }

    fn write(&mut self, data: &[u8]) -> Result<()> {
        self.check_interrupt()?;
        let result = self.device.write(ENDPOINT_OUT, data, WRITE_TIMEOUT_MS);
        self.check_interrupt()?;
        match result {
            Err(TransportError::Interrupted) => Err(Error::Interrupted),
            Err(_) => Err(Error::fastboot("USB write failed; stopped without retry")),
            Ok(written) if written != data.len() => {
                Err(Error::fastboot("short USB write; stopped without retry"))
            }
            Ok(_) => Ok(()),
        }
    }

    /// Wait for the final status, skipping at most 255 INFO packets.
    pub fn response(&mut self, expected: &[u8; 4]) -> Result<Vec<u8>> {
        let deadline = Instant::now() + self.response_budget;
        for _ in 0..MAX_INFO {
            // Python: int((deadline - time.monotonic()) * 1000)
            let remaining = deadline
                .checked_duration_since(Instant::now())
                .map_or(0, |left| left.as_millis());
            if remaining == 0 {
                return Err(Error::fastboot("Fastboot response deadline exceeded"));
            }
            let timeout = u32::try_from(remaining.min(3000)).unwrap_or(3000);
            self.check_interrupt()?;
            let result = self.device.read(ENDPOINT_IN, READ_SIZE, timeout);
            self.check_interrupt()?;
            let packet = match result {
                Ok(packet) => packet,
                Err(TransportError::Interrupted) => return Err(Error::Interrupted),
                Err(_) => return Err(Error::fastboot("USB read failed; stopped without retry")),
            };
            if !(4..=64).contains(&packet.len()) {
                return Err(Error::fastboot("short or oversized Fastboot response"));
            }
            let (status, payload) = packet.split_at(4);
            if status == b"INFO" {
                continue;
            }
            if status == b"FAIL" {
                return Err(Error::fastboot("device rejected command"));
            }
            if status != expected {
                return Err(Error::fastboot("unexpected Fastboot response status"));
            }
            return Ok(payload.to_vec());
        }
        Err(Error::fastboot("too many Fastboot INFO responses"))
    }

    pub fn command(&mut self, command: &str, expected: &[u8; 4]) -> Result<Vec<u8>> {
        self.write(command.as_bytes())?;
        self.response(expected)
    }

    pub fn getvar(&mut self, name: &str) -> Result<String> {
        let payload = self.command(&format!("getvar:{name}"), b"OKAY")?;
        if !payload.is_ascii() {
            return Err(Error::fastboot("non-ASCII Fastboot variable"));
        }
        String::from_utf8(payload).map_err(|_| Error::fastboot("non-ASCII Fastboot variable"))
    }

    pub fn guard(&mut self) -> Result<()> {
        if self.getvar("product")? != PRODUCT {
            return Err(Error::fastboot("product mismatch; action stopped"));
        }
        Ok(())
    }

    pub fn query(&mut self, name: &str) -> Result<String> {
        if !QUERIES.contains(&name) {
            return Err(Error::fastboot("query is not allowed"));
        }
        self.guard()?;
        if name == "product" {
            Ok(PRODUCT.to_owned())
        } else {
            self.getvar(name)
        }
    }

    pub fn reboot(&mut self) -> Result<()> {
        self.guard()?;
        self.command("reboot", b"OKAY")?;
        Ok(())
    }

    /// RAM boot (`fastboot boot`); never writes a partition.
    pub fn boot(&mut self, data: &[u8], sha256: Option<&str>) -> Result<()> {
        validate_image(data, sha256)?;
        self.guard()?;
        if self.getvar("unlocked")? != "yes" {
            return Err(Error::fastboot("temporary boot requires unlocked=yes"));
        }
        // As in Python, an unparsable partition size stops before max-download-size.
        let invalid = || Error::fastboot("invalid boot partition or download size");
        let partition = py_int16(&self.getvar("partition-size:boot")?).ok_or_else(invalid)?;
        let maximum = py_int16(&self.getvar("max-download-size")?).ok_or_else(invalid)?;
        if partition != MAX_IMAGE as i128 || maximum < data.len() as i128 {
            return Err(Error::fastboot(
                "boot partition or maximum download size mismatch",
            ));
        }
        self.download(data)?;
        self.command("boot", b"OKAY")?;
        Ok(())
    }

    pub fn download(&mut self, data: &[u8]) -> Result<()> {
        let accepted = self.command(&format!("download:{:08x}", data.len()), b"DATA")?;
        let length_ok = is_hex_of_len(&accepted, 8)
            && std::str::from_utf8(&accepted)
                .ok()
                .and_then(|text| usize::from_str_radix(text, 16).ok())
                == Some(data.len());
        if !length_ok {
            return Err(Error::fastboot(
                "device accepted an incorrect download length",
            ));
        }
        for chunk in data.chunks(CHUNK) {
            self.write(chunk)?;
        }
        self.response(b"OKAY")?;
        Ok(())
    }
}
