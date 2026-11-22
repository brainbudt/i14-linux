//! Bounded Fastboot tools for the I14 (MT6735), ported from
//! `scripts/fastboot_usb.py` and `scripts/install_boot_usb.py`.
//!
//! The protocol and install logic only talk to hardware through the
//! [`Transport`] trait. [`usb`] holds the real nusb transport; [`fake`] is a
//! scripted transport for tests. Constructing or importing anything in this
//! crate never opens USB: only [`usb::connect`] does.

pub mod cli;
pub mod fake;
pub mod image;
pub mod install;
pub mod json;
pub mod protocol;
pub mod usb;

use std::fmt;

pub use image::{MAX_IMAGE, validate_image};
pub use install::{
    Receipt, STOCK_SHA256, Value, install_boot, validate_install_cmdline, validate_stock,
};
pub use protocol::{
    Candidate, EndpointInfo, Fastboot, InterfaceInfo, PRODUCT, QUERIES, Transport, TransportError,
    select_device,
};

/// Error classes mirroring the Python exception classes the CLIs distinguish.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// `FastbootError(message)`: a deliberate, concise stop.
    Fastboot(String),
    /// `OSError` (file I/O, USB setup/claim errors). Detail is never printed.
    Io(String),
    /// `FileExistsError` from the exclusive receipt.
    Exists,
    /// `KeyboardInterrupt` (Ctrl-C) or a transport that reports interruption.
    Interrupted,
    /// Any other `Exception` (e.g. non-OS USB setup failure).
    Other(String),
}

impl Error {
    pub fn fastboot(message: &str) -> Self {
        Error::Fastboot(message.to_owned())
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Fastboot(message) => f.write_str(message),
            Error::Io(detail) => write!(f, "I/O error: {detail}"),
            Error::Exists => f.write_str("file exists"),
            Error::Interrupted => f.write_str("interrupted"),
            Error::Other(detail) => write!(f, "error: {detail}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(error: std::io::Error) -> Self {
        if error.kind() == std::io::ErrorKind::AlreadyExists {
            Error::Exists
        } else {
            Error::Io(error.to_string())
        }
    }
}

pub type Result<T, E = Error> = std::result::Result<T, E>;
