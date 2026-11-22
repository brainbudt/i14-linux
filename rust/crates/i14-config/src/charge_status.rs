//! The charge guard's status snapshot (`/run/i14-charge-status.json`), as
//! `read_status` in `i14ui/charging.py`: only a fresh snapshot from this boot
//! may label the UI or restore gauge trust.

use serde_json::{Map, Value};

pub const STATUS: &str = "/run/i14-charge-status.json";
pub const BOOT_ID: &str = "/proc/sys/kernel/random/boot_id";
pub const MAX_AGE: f64 = 150.0;
const LIMITS: [i64; 5] = [60, 70, 80, 90, 100];
const REQUIRED: [&str; 11] = [
    "schema",
    "backend",
    "boot_id",
    "updated_monotonic",
    "level",
    "zcv_mv",
    "paused",
    "valid",
    "enabled",
    "limit",
    "error",
];

#[derive(Clone, Debug, PartialEq)]
pub struct ChargeStatus {
    pub level: Option<i64>,
    pub zcv_mv: Option<i64>,
    pub paused: bool,
    pub valid: bool,
    pub enabled: bool,
    pub limit: i64,
    pub error: Option<String>,
    pub online: Option<bool>,
    /// True when `level` comes from an anchored HW coulomb counter (display `N%`, not `~N%`).
    pub anchored: bool,
    /// The whole snapshot, for fields only the guard uses.
    pub raw: Map<String, Value>,
}

/// charging.py's `read`: the file's text, stripped; "" when unreadable.
pub fn read_text(path: &str) -> String {
    std::fs::read_to_string(path)
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

fn int(v: &Value) -> Option<i64> {
    // Python `type(v) is int`: a JSON integer, not a float or bool.
    v.as_i64().filter(|_| v.is_i64() || v.is_u64())
}

/// Parses and checks a snapshot; `now` is CLOCK_MONOTONIC seconds.
pub fn parse(text: &str, now: f64, boot_id: &str, max_age: Option<f64>) -> Option<ChargeStatus> {
    let data: Map<String, Value> = serde_json::from_str::<Value>(text)
        .ok()?
        .as_object()?
        .clone();
    if !REQUIRED.iter().all(|k| data.contains_key(*k)) {
        return None;
    }
    let stamp = data["updated_monotonic"]
        .as_f64()
        .filter(|_| data["updated_monotonic"].is_number())?;
    if !stamp.is_finite() || now - stamp < 0.0 || max_age.is_some_and(|m| now - stamp > m) {
        return None;
    }
    let flag = |k: &str| data.get(k).and_then(Value::as_bool);
    let limit = int(&data["limit"]).filter(|l| LIMITS.contains(l))?;
    if int(&data["schema"]) != Some(1)
        || data["backend"].as_str() != Some("bcct")
        || data["boot_id"].as_str() != Some(boot_id)
    {
        return None;
    }
    let (paused, valid, enabled) = (flag("paused")?, flag("valid")?, flag("enabled")?);
    let ranged = |k: &str, lo: i64, hi: i64| -> Result<Option<i64>, ()> {
        match &data[k] {
            Value::Null => Ok(None),
            v => int(v).filter(|n| (lo..=hi).contains(n)).map(Some).ok_or(()),
        }
    };
    let level = ranged("level", 0, 100).ok()?;
    let zcv_mv = ranged("zcv_mv", 2500, 5000).ok()?;
    let error = match &data["error"] {
        Value::Null => None,
        Value::String(s) => Some(s.clone()),
        _ => return None,
    };
    let online = match data.get("online") {
        None | Some(Value::Null) => None,
        Some(Value::Bool(b)) => Some(*b),
        Some(_) => return None,
    };
    let anchored = match data.get("anchored") {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(_) => return None,
    };
    if level.is_none() != zcv_mv.is_none() {
        return None;
    }
    Some(ChargeStatus {
        level,
        zcv_mv,
        paused,
        valid,
        enabled,
        limit,
        error,
        online,
        anchored,
        raw: data,
    })
}

/// Reads `path` and checks it against this boot (`boot_id_path`) and `now`.
pub fn read_status(
    path: &str,
    boot_id_path: &str,
    now: f64,
    max_age: Option<f64>,
) -> Option<ChargeStatus> {
    parse(&read_text(path), now, &read_text(boot_id_path), max_age)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn good() -> Value {
        json!({"schema": 1, "backend": "bcct", "boot_id": "boot-a", "updated_monotonic": 1000.0, "level": 80,
               "zcv_mv": 4109, "paused": true, "valid": true, "enabled": true, "limit": 80, "error": null, "online": true})
    }

    fn check(v: &Value) -> Option<ChargeStatus> {
        parse(&v.to_string(), 1000.0, "boot-a", Some(MAX_AGE))
    }

    #[test]
    fn accepts_a_fresh_snapshot() {
        let s = check(&good()).unwrap();
        assert_eq!(
            (s.level, s.paused, s.limit, s.online),
            (Some(80), true, 80, Some(true))
        );
    }

    #[test]
    fn rejects_stale_future_bad_and_other_boot() {
        for (k, v) in [
            ("updated_monotonic", json!(849.0)),
            ("updated_monotonic", json!(1001.0)),
            ("boot_id", json!("other-boot")),
            ("level", json!(true)),
            ("level", json!(101)),
            ("level", json!(80.0)),
            ("paused", json!("yes")),
            ("backend", json!("legacy")),
            ("limit", json!(75)),
            ("error", json!(3)),
            ("online", json!(1)),
            ("zcv_mv", json!(null)),
        ] {
            let mut s = good();
            s[k] = v.clone();
            assert!(check(&s).is_none(), "{k}={v}");
        }
        let mut s = good();
        s.as_object_mut().unwrap().remove("error");
        assert!(check(&s).is_none());
        assert!(parse("{broken", 1000.0, "boot-a", None).is_none());
        assert!(parse("NaN", 1000.0, "boot-a", None).is_none());
    }

    #[test]
    fn level_and_zcv_may_be_unknown_together() {
        let mut s = good();
        s["level"] = Value::Null;
        s["zcv_mv"] = Value::Null;
        assert_eq!(check(&s).unwrap().level, None);
    }
}
