//! Scripted fake transport mirroring the `Device` fake in
//! `tests/test_fastboot_usb.py`. It never touches USB.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;

use crate::protocol::{ENDPOINT_IN, ENDPOINT_OUT, READ_SIZE, Transport, TransportError};

/// One scripted reply to a bulk IN read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reply {
    Bytes(Vec<u8>),
    /// Python fake: `TimeoutError(...)` raised from `read`.
    Timeout,
    /// Python fake: `KeyboardInterrupt` raised from `read`.
    Interrupt,
}

impl From<&[u8]> for Reply {
    fn from(bytes: &[u8]) -> Self {
        Reply::Bytes(bytes.to_vec())
    }
}

impl<const N: usize> From<&[u8; N]> for Reply {
    fn from(bytes: &[u8; N]) -> Self {
        Reply::Bytes(bytes.to_vec())
    }
}

impl From<Vec<u8>> for Reply {
    fn from(bytes: Vec<u8>) -> Self {
        Reply::Bytes(bytes)
    }
}

type WriteHook = Box<dyn FnMut(&[u8])>;

/// Observable state of a fake device.
#[derive(Default)]
pub struct FakeState {
    pub responses: VecDeque<Reply>,
    pub writes: Vec<Vec<u8>>,
    pub reads: usize,
    pub timeouts: Vec<u32>,
    /// 1-based write number that returns one byte short.
    pub short_write: Option<usize>,
    /// 1-based write number that fails like a USB timeout.
    pub failed_write: Option<usize>,
    /// 1-based write number that raises an interruption after being recorded.
    pub interrupt_write: Option<usize>,
    pub port_numbers: Option<Vec<u8>>,
    pub close_error: bool,
    pub closed: usize,
    pub on_write: Option<WriteHook>,
}

/// Cloneable handle; clones share one [`FakeState`].
#[derive(Clone, Default)]
pub struct FakeDevice(Rc<RefCell<FakeState>>);

impl FakeDevice {
    pub fn new<R: Into<Reply>, I: IntoIterator<Item = R>>(responses: I) -> Self {
        let state = FakeState {
            responses: responses.into_iter().map(Into::into).collect(),
            ..FakeState::default()
        };
        FakeDevice(Rc::new(RefCell::new(state)))
    }

    pub fn state(&self) -> std::cell::RefMut<'_, FakeState> {
        self.0.borrow_mut()
    }

    pub fn writes(&self) -> Vec<Vec<u8>> {
        self.0.borrow().writes.clone()
    }

    pub fn reads(&self) -> usize {
        self.0.borrow().reads
    }

    pub fn short_write(self, n: usize) -> Self {
        self.state().short_write = Some(n);
        self
    }

    pub fn failed_write(self, n: usize) -> Self {
        self.state().failed_write = Some(n);
        self
    }

    pub fn port(self, ports: Option<&[u8]>) -> Self {
        self.state().port_numbers = ports.map(<[u8]>::to_vec);
        self
    }

    pub fn on_write(self, hook: impl FnMut(&[u8]) + 'static) -> Self {
        self.state().on_write = Some(Box::new(hook));
        self
    }
}

impl Transport for FakeDevice {
    fn write(
        &mut self,
        endpoint: u8,
        data: &[u8],
        timeout_ms: u32,
    ) -> Result<usize, TransportError> {
        assert!(endpoint == ENDPOINT_OUT && 0 < timeout_ms && timeout_ms <= 5000);
        let hook = self.state().on_write.take();
        if let Some(mut hook) = hook {
            hook(data);
            self.state().on_write = Some(hook);
        }
        let mut state = self.state();
        state.writes.push(data.to_vec());
        state.timeouts.push(timeout_ms);
        let count = state.writes.len();
        if state.interrupt_write == Some(count) {
            return Err(TransportError::Interrupted);
        }
        if state.failed_write == Some(count) {
            return Err(TransportError::Timeout);
        }
        if state.short_write == Some(count) {
            return Ok(data.len() - 1);
        }
        Ok(data.len())
    }

    fn read(
        &mut self,
        endpoint: u8,
        size: usize,
        timeout_ms: u32,
    ) -> Result<Vec<u8>, TransportError> {
        assert!(
            endpoint == ENDPOINT_IN && size == READ_SIZE && 0 < timeout_ms && timeout_ms <= 5000
        );
        let mut state = self.state();
        state.reads += 1;
        state.timeouts.push(timeout_ms);
        match state.responses.pop_front() {
            // Python: `list.pop(0)` on an empty list raises IndexError.
            None => Err(TransportError::Io("no scripted response".into())),
            Some(Reply::Bytes(bytes)) => Ok(bytes),
            Some(Reply::Timeout) => Err(TransportError::Timeout),
            Some(Reply::Interrupt) => Err(TransportError::Interrupted),
        }
    }

    fn port_numbers(&self) -> Option<Vec<u8>> {
        self.0.borrow().port_numbers.clone()
    }

    fn close(&mut self) -> Result<(), TransportError> {
        let mut state = self.state();
        state.closed += 1;
        if state.close_error {
            Err(TransportError::Io("private diagnostic".into()))
        } else {
            Ok(())
        }
    }
}
