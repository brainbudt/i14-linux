//! Port of tests/test_i14_bcct.py: BCCT policy against a temporary kernel-shaped
//! tree, with no device access. Fixture: mock.patch("i14ui.bcct.write_command")
//! becomes Bcct::write; the os.write mocks become a fake io::Write for write_once.

mod common;
use common::*;

use std::cell::RefCell;
use std::fs;
use std::io;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::rc::Rc;

use i14_power::bcct::{write_command, write_once};
use i14_power::{Bcct, Control};

struct Kernel {
    config: PathBuf,
    device: PathBuf,
    klog: i64,
    limits: Vec<i64>,
    states: Vec<i64>,
    writes: Vec<(PathBuf, Vec<u8>)>,
}

impl Kernel {
    fn render(&self, effective: Option<i64>) {
        let effective = effective.unwrap_or_else(|| {
            self.limits
                .iter()
                .zip(&self.states)
                .filter(|(l, s)| **s != 0 && **l > 0)
                .map(|(l, _)| *l)
                .min()
                .unwrap_or(65535)
        });
        let mut lines = vec![
            format!("klog {}", self.klog),
            format!("curr_limit {effective}"),
        ];
        for (i, (l, s)) in self.limits.iter().zip(&self.states).enumerate() {
            lines.push(format!("mtk-cl-bcct{i:02} {l} mA, state {s}"));
        }
        write(&self.config, &(lines.join("\n") + "\n"));
        write(
            self.device.join("cur_state"),
            &format!("{}\n", self.states[0]),
        );
    }

    fn kernel_write(&mut self, path: &Path, text: &str) {
        let data = format!("{text}\n").into_bytes();
        self.writes.push((path.to_path_buf(), data.clone()));
        assert!(data.ends_with(b"\n"));
        assert_eq!(data.iter().filter(|b| **b == b'\n').count(), 1);
        let words: Vec<i64> = text
            .split_whitespace()
            .map(|w| w.parse().unwrap())
            .collect();
        if path == self.config {
            self.klog = words[0];
            self.limits = words[1..].to_vec();
        } else {
            assert_eq!(path, self.device.join("cur_state"));
            self.states[0] = words[0];
        }
        self.render(None);
    }
}

struct T {
    _tmp: TempDir,
    root: PathBuf,
    thermal: PathBuf,
    device: PathBuf,
    k: Rc<RefCell<Kernel>>,
    backend: Bcct,
}

impl T {
    fn new() -> Self {
        let tmp = TempDir::new();
        let root = tmp.0.clone();
        let config = root.join("clbcct");
        let thermal = root.join("thermal");
        let device = thermal.join("cooling_device25");
        fs::create_dir_all(&device).unwrap();
        write(device.join("type"), "mtk-cl-bcct00\n");
        write(device.join("max_state"), "1\n");
        let k = Rc::new(RefCell::new(Kernel {
            config: config.clone(),
            device: device.clone(),
            klog: 0,
            limits: vec![0, -1, 0],
            states: vec![0, 0, 0],
            writes: Vec::new(),
        }));
        k.borrow().render(None);
        let mut backend = Bcct::new(&config, &thermal);
        let kk = k.clone();
        backend.write = Box::new(move |p, t| {
            kk.borrow_mut().kernel_write(p, t);
            Ok(())
        });
        T {
            _tmp: tmp,
            root,
            thermal,
            device,
            k,
            backend,
        }
    }

    fn set(&self, klog: i64, limits: [i64; 3], states: [i64; 3]) {
        let mut k = self.k.borrow_mut();
        (k.klog, k.limits, k.states) = (klog, limits.to_vec(), states.to_vec());
        k.render(None);
    }

    fn writes(&self) -> Vec<(PathBuf, Vec<u8>)> {
        self.k.borrow().writes.clone()
    }

    fn config(&self) -> PathBuf {
        self.k.borrow().config.clone()
    }

    fn noop_writer(&mut self) {
        self.backend.write = Box::new(|_, _| Ok(()));
    }
}

#[test]
fn pause_and_release_preserve_other_limits_states_and_logging() {
    let mut t = T::new();
    t.set(1, [-1, 500, 300], [0, 1, 1]);
    t.backend.set_paused(true).unwrap();
    assert!(t.backend.paused().unwrap());
    assert_eq!(
        t.writes(),
        vec![
            (t.config(), b"1 1 500 300\n".to_vec()),
            (t.device.join("cur_state"), b"1\n".to_vec())
        ]
    );
    t.backend.release().unwrap();
    assert!(!t.backend.paused().unwrap());
    let k = t.k.borrow();
    assert_eq!(
        (k.klog, k.limits.clone(), k.states.clone()),
        (1, vec![1, 500, 300], vec![0, 1, 1])
    );
    assert!(read(&k.config).contains("curr_limit 300\n"));
}

#[test]
fn restart_adopts_own_pause_and_noops_when_already_confirmed() {
    let mut t = T::new();
    t.set(0, [1, -1, 0], [1, 0, 0]);
    assert!(t.backend.paused().unwrap());
    t.backend.set_paused(true).unwrap();
    assert_eq!(t.writes(), vec![]);
    t.backend.release().unwrap();
    t.backend.release().unwrap();
    assert_eq!(
        t.writes(),
        vec![(t.device.join("cur_state"), b"0\n".to_vec())]
    );
}

#[test]
fn release_does_not_claim_a_free_slot() {
    let mut t = T::new();
    t.backend.release().unwrap();
    assert_eq!(t.writes(), vec![]);
    assert_eq!(t.k.borrow().limits[0], 0);
}

#[test]
fn owned_slot_is_reused_without_rewriting_the_whole_config() {
    let mut t = T::new();
    t.set(0, [1, -1, 0], [0, 0, 0]);
    t.backend.set_paused(true).unwrap();
    assert_eq!(
        t.writes(),
        vec![(t.device.join("cur_state"), b"1\n".to_vec())]
    );
}

#[test]
fn foreign_own_slot_or_unowned_active_slot_is_not_changed() {
    let mut t = T::new();
    for (limit, state) in [(70, 0), (-1, 1), (0, 1)] {
        t.set(0, [limit, -1, 0], [state, 0, 0]);
        assert!(t.backend.set_paused(true).is_err(), "{limit} {state}");
    }
    assert_eq!(t.writes(), vec![]);
}

#[test]
fn sysfs_and_config_state_must_match() {
    let mut t = T::new();
    write(t.device.join("cur_state"), "1\n");
    assert!(t.backend.set_paused(true).is_err());
    assert_eq!(t.writes(), vec![]);
}

#[test]
fn pause_requires_effective_one_ma_readback_even_on_noop() {
    let mut t = T::new();
    {
        let mut k = t.k.borrow_mut();
        (k.limits[0], k.states[0]) = (1, 1);
        k.render(Some(65535));
    }
    assert_eq!(
        t.backend.set_paused(true).unwrap_err().0,
        "BCCT pause is not effective"
    );
    assert_eq!(t.writes(), vec![]);
}

#[test]
fn release_clears_own_state_even_when_pause_is_ineffective() {
    let mut t = T::new();
    {
        let mut k = t.k.borrow_mut();
        (k.limits[0], k.states[0]) = (1, 1);
        k.render(Some(65535));
    }
    t.backend.release().unwrap();
    assert_eq!(
        t.writes(),
        vec![(t.device.join("cur_state"), b"0\n".to_vec())]
    );
    assert_eq!(t.k.borrow().states[0], 0);
}

#[test]
fn thermal_binding_rejects_control_but_unrelated_binding_is_allowed() {
    let mut t = T::new();
    let zone = t.thermal.join("thermal_zone0");
    fs::create_dir(&zone).unwrap();
    symlink(&t.device, zone.join("cdev0")).unwrap();
    assert_eq!(
        t.backend.set_paused(true).unwrap_err().0,
        "BCCT slot 00 belongs to a thermal zone"
    );
    assert_eq!(t.writes(), vec![]);
    fs::remove_file(zone.join("cdev0")).unwrap();
    let other = t.thermal.join("cooling_device1");
    fs::create_dir(&other).unwrap();
    write(other.join("type"), "other\n");
    symlink(&other, zone.join("cdev0")).unwrap();
    t.backend.set_paused(true).unwrap();
    assert!(t.backend.paused().unwrap());
}

#[test]
fn missing_duplicate_or_changed_device_is_rejected() {
    let mut t = T::new();
    let duplicate = t.thermal.join("cooling_device4");
    fs::create_dir(&duplicate).unwrap();
    write(duplicate.join("type"), "mtk-cl-bcct00\n");
    assert_eq!(
        t.backend.set_paused(true).unwrap_err().0,
        "BCCT slot 00 missing or ambiguous"
    );
    write(duplicate.join("type"), "other\n");
    write(t.device.join("type"), "changed\n");
    assert_eq!(
        t.backend.set_paused(true).unwrap_err().0,
        "BCCT slot 00 missing or ambiguous"
    );
    assert_eq!(t.writes(), vec![]);
}

#[test]
fn damaged_or_missing_config_is_rejected_without_writes() {
    let mut t = T::new();
    let config = t.config();
    let original = read(&config);
    for text in [
        String::new(),
        original.clone() + &original,
        original.replace("klog 0", "klog 2"),
        original.replace("state 0", "state 2"),
        original.replace("-1 mA", "40000 mA"),
    ] {
        write(&config, &text);
        let e = t.backend.set_paused(true).unwrap_err();
        assert!(!e.0.starts_with("[Errno"), "ValueError expected: {e}");
    }
    fs::remove_file(&config).unwrap();
    let e = t.backend.set_paused(true).unwrap_err();
    assert_eq!(
        e.0,
        format!(
            "[Errno 2] No such file or directory: '{}'",
            config.display()
        )
    ); // OSError
    assert_eq!(t.writes(), vec![]);
}

#[test]
fn unaccepted_config_or_state_write_readback_raises() {
    let mut t = T::new();
    t.noop_writer();
    assert_eq!(
        t.backend.set_paused(true).unwrap_err().0,
        "BCCT configuration write did not read back"
    );
    t.set(0, [1, -1, 0], [0, 0, 0]);
    assert_eq!(
        t.backend.set_paused(true).unwrap_err().0,
        "BCCT state write did not read back"
    );
}

struct CountingWrite {
    calls: usize,
    ret: Option<usize>,
    inner: Vec<u8>,
}

impl io::Write for CountingWrite {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.calls += 1;
        let n = self.ret.unwrap_or(buf.len());
        self.inner.extend_from_slice(&buf[..n]);
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[test]
fn each_kernel_command_is_one_write_and_partial_write_is_not_retried() {
    let t = T::new();
    let path = t.root.join("write-boundary");
    fs::write(&path, b"").unwrap();
    write_command(&path, "0 1 -1 0").unwrap();
    assert_eq!(fs::read(&path).unwrap(), b"0 1 -1 0\n");
    let mut once = CountingWrite {
        calls: 0,
        ret: None,
        inner: Vec::new(),
    };
    write_once(&mut once, b"0 1 -1 0\n", &path).unwrap();
    assert_eq!(once.calls, 1);
    let mut short = CountingWrite {
        calls: 0,
        ret: Some(2),
        inner: Vec::new(),
    };
    let e = write_once(&mut short, b"0 1 -1 0\n", &path).unwrap_err();
    assert_eq!(
        e.0,
        format!("[Errno 5] short kernel command write: '{}'", path.display())
    );
    assert_eq!(short.calls, 1);
    assert_eq!(
        write_command(&path, "a\nb").unwrap_err().0,
        "kernel command must be one line"
    );
}
