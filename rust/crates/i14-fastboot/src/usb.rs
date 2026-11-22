//! Real transport on the pure-Rust `nusb` crate (replaces pyusb + libusb).
//!
//! Only [`connect`] touches USB. Order matches `fastboot_usb.connect()`:
//! enumerate VID/PID 0bb4:0c01, require exactly one, check the VID/PID, open
//! it to read the active configuration, require the measured Fastboot
//! interface, then claim interface 0.

use std::time::Duration;

use nusb::transfer::{Buffer, Bulk, Completion, In, Out, TransferError};
use nusb::{Device, DeviceInfo, Endpoint, Interface, MaybeFuture};

use crate::protocol::{
    Candidate, ENDPOINT_IN, ENDPOINT_OUT, EndpointInfo, Fastboot, InterfaceInfo, PRODUCT_ID,
    Transport, TransportError, VENDOR_ID, select_device,
};
use crate::{Error, Result};

fn io(error: impl std::fmt::Display) -> Error {
    // pyusb's USBError is an OSError subclass, so USB setup failures print
    // the "I/O failed" message, never the detail.
    Error::Io(error.to_string())
}

struct UsbCandidate {
    info: DeviceInfo,
    device: Option<Device>,
}

impl Candidate for UsbCandidate {
    fn vendor_product(&self) -> (u16, u16) {
        (self.info.vendor_id(), self.info.product_id())
    }

    fn active_interfaces(&mut self) -> Result<Vec<InterfaceInfo>> {
        let device = self.info.open().wait().map_err(io)?;
        let interfaces = {
            let config = device.active_configuration().map_err(io)?;
            config
                .interface_alt_settings()
                .map(|alt| InterfaceInfo {
                    number: alt.interface_number(),
                    alternate_setting: alt.alternate_setting(),
                    class: alt.class(),
                    subclass: alt.subclass(),
                    protocol: alt.protocol(),
                    endpoints: alt
                        .endpoints()
                        .map(|ep| EndpointInfo {
                            address: ep.address(),
                            attributes: ep.attributes(),
                            max_packet_size: ep.max_packet_size_raw(),
                        })
                        .collect(),
                })
                .collect()
        };
        self.device = Some(device);
        Ok(interfaces)
    }
}

/// Claimed I14 Fastboot interface with its two bulk endpoints.
pub struct UsbTransport {
    out: Option<Endpoint<Bulk, Out>>,
    input: Option<Endpoint<Bulk, In>>,
    interface: Option<Interface>,
    device: Option<Device>,
    port_chain: Vec<u8>,
}

fn status(completion: &Completion) -> Result<(), TransportError> {
    match completion.status {
        Ok(()) => Ok(()),
        Err(TransferError::Cancelled) => Err(TransportError::Timeout),
        Err(other) => Err(TransportError::Io(other.to_string())),
    }
}

impl Transport for UsbTransport {
    fn write(
        &mut self,
        endpoint: u8,
        data: &[u8],
        timeout_ms: u32,
    ) -> Result<usize, TransportError> {
        let out = match self.out.as_mut() {
            Some(out) if endpoint == ENDPOINT_OUT => out,
            _ => return Err(TransportError::Io("endpoint not open".into())),
        };
        let completion =
            out.transfer_blocking(Buffer::from(data), Duration::from_millis(timeout_ms.into()));
        status(&completion)?;
        Ok(completion.actual_len)
    }

    fn read(
        &mut self,
        endpoint: u8,
        size: usize,
        timeout_ms: u32,
    ) -> Result<Vec<u8>, TransportError> {
        let input = match self.input.as_mut() {
            Some(input) if endpoint == ENDPOINT_IN => input,
            _ => return Err(TransportError::Io("endpoint not open".into())),
        };
        let completion =
            input.transfer_blocking(Buffer::new(size), Duration::from_millis(timeout_ms.into()));
        status(&completion)?;
        let length = completion.actual_len.min(completion.buffer.len());
        Ok(completion.buffer[..length].to_vec())
    }

    fn port_numbers(&self) -> Option<Vec<u8>> {
        (!self.port_chain.is_empty()).then(|| self.port_chain.clone())
    }

    fn close(&mut self) -> Result<(), TransportError> {
        self.out = None;
        self.input = None;
        let released = match self.interface.take() {
            Some(interface) => interface.release().wait(),
            None => Ok(()),
        };
        self.device = None;
        released.map_err(|error| TransportError::Io(error.to_string()))
    }
}

/// Find, verify and claim the single I14 Fastboot interface.
pub fn connect() -> Result<Fastboot<UsbTransport>> {
    let candidates: Vec<UsbCandidate> = nusb::list_devices()
        .wait()
        .map_err(io)?
        .filter(|info| info.vendor_id() == VENDOR_ID && info.product_id() == PRODUCT_ID)
        .map(|info| UsbCandidate { info, device: None })
        .collect();
    let mut selected = select_device(candidates)?;
    let port_chain = port_chain(&selected.info);
    let device = selected
        .device
        .take()
        .ok_or_else(|| Error::Other("device was not opened".into()))?;
    let interface = device.claim_interface(0).wait().map_err(io)?;
    let out = interface.endpoint::<Bulk, Out>(ENDPOINT_OUT).map_err(io)?;
    let input = interface.endpoint::<Bulk, In>(ENDPOINT_IN).map_err(io)?;
    Ok(Fastboot::new(UsbTransport {
        out: Some(out),
        input: Some(input),
        interface: Some(interface),
        device: Some(device),
        port_chain,
    }))
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
fn port_chain(info: &DeviceInfo) -> Vec<u8> {
    info.port_chain().to_vec()
}

#[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
fn port_chain(_info: &DeviceInfo) -> Vec<u8> {
    Vec::new()
}
