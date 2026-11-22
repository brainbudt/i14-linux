//! Integration and replay tests for the HW-CAR hardware coulomb-counter fuel gauge.

mod common;
use common::*;

use std::cell::Cell;
use std::fs;
use std::rc::Rc;

use i14_config::charge_status;
use i14_power::fuel_gauge::{
    Calibration, FgInput, FgSnapshot, FuelGauge, SavedState, BCCT_HARD_CEILING_MV,
    DEFAULT_Q_FULL_MAH,
};
use i14_power::Guard;

#[test]
fn nov20_21_discharge_profile_counts_down_linearly_without_4pct_stall_or_25pct_cliff() {
    let tmp = TempDir::new();
    let state_path = tmp.0.join("battery-state.json");
    // Start anchored at 78.6% (2870 / 3650 mAh) at ZCV=4057 mV, matching Nov 20 09:59 UTC unplug.
    SavedState {
        schema: 1,
        boot_id: "boot-nov20".into(),
        boottime_s: Some(1000.0),
        q_rem_mah: 2870.0,
        q_full_mah: DEFAULT_Q_FULL_MAH,
        anchored: true,
        last_car: Some(0),
        last_dod0: Some(0),
    }
    .save_atomic(&state_path, false)
    .unwrap();

    let mut fg = FuelGauge::new(Calibration::default(), state_path);
    // Representative measured waypoints from the 32.5 h Nov 20-21 unplugged discharge:
    // (elapsed_min, batt_vol_mv, zcv_mv, car_mah, expected_soc_lo, expected_soc_hi)
    let waypoints = [
        (0, 4032, 4057, 0, 77, 80),
        (208, 3932, 3955, -290, 69, 72),
        (412, 3843, 3866, -580, 61, 64),
        (614, 3752, 3776, -870, 53, 56),
        (797, 3679, 3704, -1136, 46, 49),
        (992, 3628, 3657, -1424, 38, 41), // Old T2 GUI showed ~4% here!
        (1188, 3600, 3625, -1706, 30, 33), // Old T2 GUI showed ~4% here!
        (1382, 3572, 3597, -1990, 22, 26), // Old T2 GUI showed ~3% here!
        (1575, 3534, 3559, -2294, 14, 18), // Old T2 GUI showed ~2% here!
        (1765, 3480, 3505, -2586, 6, 10), // Old T2 GUI showed ~1% here!
        (1949, 3410, 3436, -2868, 0, 2),  // Stock capacity was still 22-25% here!
    ];

    let mut prev_t = 1000.0;
    let mut prev_car = 0i64;
    let mut prev_v = 4032i64;
    let mut prev_zcv = 4057i64;
    let mut last_out = None;
    for &(min, v_mv, zcv_mv, car, lo, hi) in &waypoints {
        // Step in 60-second increments between waypoints so CAR and V move smoothly.
        let target_t = 1000.0 + (min * 60) as f64;
        let steps = ((target_t - prev_t) / 60.0).round().max(1.0) as i64;
        for s in 1..=steps {
            let frac = s as f64 / steps as f64;
            let t = prev_t + (target_t - prev_t) * frac;
            let c = prev_car + ((car - prev_car) as f64 * frac).round() as i64;
            let v = prev_v + ((v_mv - prev_v) as f64 * frac).round() as i64;
            let z = prev_zcv + ((zcv_mv - prev_zcv) as f64 * frac).round() as i64;
            last_out = Some(fg.update(&FgInput {
                now_mono: t,
                boottime_s: Some(t),
                boot_id: "boot-nov20",
                batt_vol_mv: v,
                fg: FgSnapshot {
                    car_mah: c,
                    current_deci_ma: 1180,
                    zcv_mv: z,
                    dod0: 0,
                },
                online: Some(false),
                kmsg_reset: false,
            }));
        }
        let out = last_out.as_ref().unwrap();
        assert!(
            (lo..=hi).contains(&out.soc_pct),
            "at {min} min (V={v_mv}, ZCV={zcv_mv}, CAR={car}): expected {lo}..={hi}%, got {}%",
            out.soc_pct
        );
        prev_t = target_t;
        prev_car = car;
        prev_v = v_mv;
        prev_zcv = zcv_mv;
    }

    // One more tick at 3410 mV confirms sustained low-voltage safety poweroff.
    let final_out = fg.update(&FgInput {
        now_mono: prev_t + 15.0,
        boottime_s: Some(prev_t + 15.0),
        boot_id: "boot-nov20",
        batt_vol_mv: 3410,
        fg: FgSnapshot {
            car_mah: -2868,
            current_deci_ma: 1200,
            zcv_mv: 3436,
            dod0: 0,
        },
        online: Some(false),
        kmsg_reset: false,
    });
    assert!(final_out.safety_poweroff);
    assert!(final_out.soc_pct <= 2);
}

#[test]
fn guard_hw_car_mode_controls_bcct_enforces_hard_ceiling_and_persists_state() {
    let tmp = TempDir::new();
    let d = &tmp.0;
    let b = d.join("battery");
    fs::create_dir(&b).unwrap();
    for (k, v) in [
        ("capacity", "30"),
        ("status", "Charging"),
        ("batt_vol", "3766"),
        ("batt_temp", "290"),
    ] {
        write(b.join(k), v);
    }
    for (k, v) in [
        ("type", "2"),
        ("usb", "1"),
        ("ac", "0"),
        ("boot_id", "boot-live"),
        ("gauge", "3628"),
        ("activity", "-6600"),
        ("FG_g_fg_dbg_bat_car", "500"),
        ("FG_g_fg_dbg_d0", "82"),
        (
            "readback",
            "get_usb_current_unlimited=0,\ncmd_discharging = -1\n",
        ),
        (
            "ui.json",
            r#"{"charge_limit_enabled": true, "charge_limit": 80}"#,
        ),
    ] {
        write(d.join(k), v);
    }

    let now = Rc::new(Cell::new(100.0));
    let ctrl = Fake::policy();
    let state_path = d.join("battery-state.json");
    let mut g = Guard::new(paths(d), Box::new(ctrl.clone()), clock(&now));
    g.enable_fuel_gauge(state_path.clone(), &d.join("cal.json"), &d.join("no-kmsg"));

    // Tick 1: cold-seed at 3766 mV / -660 mA -> ~32% (NOT 4%!)
    assert!(!g.tick());
    // Tick 2 (15 s later, CAR +2 -> live): publishes ~32%, unpaused
    now.set(115.0);
    write(d.join("FG_g_fg_dbg_bat_car"), "502");
    assert!(!g.tick());
    let st = status(d);
    let lvl = st["level"].as_i64().unwrap();
    assert!((30..=36).contains(&lvl), "expected ~32%, got {lvl}%");
    assert_eq!(st["gauge_mode"], "hw_car");
    assert_eq!(st["anchored"], false);
    assert!(!ctrl.stopped());

    // Hard voltage ceiling: if batt_vol hits 4240 mV even below 80% SOC, BCCT pauses to avoid
    // stock kernel CHR_TOP_OFF (4260 mV) / bat_full=100% lock.
    now.set(130.0);
    write(b.join("batt_vol"), &BCCT_HARD_CEILING_MV.to_string());
    write(d.join("FG_g_fg_dbg_bat_car"), "504");
    assert!(!g.tick());
    assert!(ctrl.stopped());

    // Clean shutdown persists battery-state.json atomically.
    g.close();
    let saved = SavedState::load(&state_path).expect("persisted battery-state.json");
    assert_eq!(saved.boot_id, "boot-live");
    assert_eq!(saved.last_car, Some(504));
    assert!((saved.q_full_mah - DEFAULT_Q_FULL_MAH).abs() < 0.1);

    // Same-boot restart restores saved q_rem + new CAR delta seamlessly (live after 2nd tick).
    let mut g2 = Guard::new(paths(d), Box::new(ctrl.clone()), clock(&now));
    g2.enable_fuel_gauge(state_path.clone(), &d.join("cal.json"), &d.join("no-kmsg"));
    now.set(145.0);
    write(b.join("batt_vol"), "3780");
    write(d.join("FG_g_fg_dbg_bat_car"), "514"); // +10 mAh while restarted
    assert!(!g2.tick());
    now.set(160.0);
    write(d.join("FG_g_fg_dbg_bat_car"), "516");
    assert!(!g2.tick());
    let st2 = status(d);
    let parsed = charge_status::parse(&st2.to_string(), 160.0, "boot-live", Some(150.0)).unwrap();
    assert!(!parsed.anchored);
    assert!((30..=37).contains(&parsed.level.unwrap()));
}

#[test]
fn real_nov7_full_charge_artifact_replays_cleanly_to_anchored_100() {
    let artifact = repo()
        .join("artifacts/battery-multicycle-20261107T142507Z/check-20261107T160657Z/samples.jsonl");
    let Ok(text) = fs::read_to_string(&artifact) else {
        // Artifact is local/gitignored on CI; skip cleanly when absent.
        return;
    };
    let tmp = TempDir::new();
    let mut fg = FuelGauge::new(Calibration::default(), tmp.0.join("state.json"));
    let mut saw_anchored_100 = false;
    let mut last_soc = 0i64;
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        let r: serde_json::Value = serde_json::from_str(line).unwrap();
        let mono = r["monotonic"].as_f64().unwrap();
        let boot_id = r["boot_id"].as_str().unwrap();
        let v = r["voltage_mv"].as_i64().unwrap();
        let car = r["car_mah"].as_i64().unwrap();
        let cur = r["cached_current_deci_ma"].as_i64().unwrap();
        let zcv = r["zcv_mv"].as_i64().unwrap();
        let dod0 = r["dod0"].as_i64().unwrap();
        let online = r["online"].as_bool();
        let kmsg_reset = r["meter_reset_marker"].is_string();
        let out = fg.update(&FgInput {
            now_mono: mono,
            boottime_s: Some(mono),
            boot_id,
            batt_vol_mv: v,
            fg: FgSnapshot {
                car_mah: car,
                current_deci_ma: cur,
                zcv_mv: zcv,
                dod0,
            },
            online,
            kmsg_reset,
        });
        last_soc = out.soc_pct;
        if out.anchored && out.soc_pct == 100 {
            saw_anchored_100 = true;
        }
    }
    assert!(saw_anchored_100, "Nov 7 trace should reach anchored 100%");
    assert!(
        (96..=100).contains(&last_soc),
        "after 27 min idle discharge from full, expected 96-100%, got {last_soc}%"
    );
}
