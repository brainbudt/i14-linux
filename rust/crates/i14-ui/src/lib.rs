//! The I14 on-device interface, ported from `rootfs/overlay/usr/lib/i14ui/`.
//!
//! This library holds the parts that need no device (drawing, fonts, settings,
//! input decoding) so they can be tested on a host against the Python version.

pub mod app;
pub mod bt;
pub mod canvas;
pub mod font;
pub mod input;
pub mod runner;
pub mod screen;
pub mod splash;
pub mod status;
pub mod system;
pub mod term;
pub mod vt;
pub mod widgets;
pub mod wifi;
