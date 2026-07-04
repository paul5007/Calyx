use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const CALYX_METRICS_INVALID_OBSERVATION: &str = "CALYX_METRICS_INVALID_OBSERVATION";
pub const CALYX_METRICS_LOG_WRITE_FAILED: &str = "CALYX_METRICS_LOG_WRITE_FAILED";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StructuredMetricLogError {
    pub code: &'static str,
    pub message: String,
    pub remediation: &'static str,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StructuredMetricEvent {
    pub ts_unix_secs: u64,
    pub surface: String,
    pub vault: String,
    pub status: String,
    pub duration_ms: Option<f64>,
    pub search_strategy: Option<String>,
    pub prediction_endpoint: Option<String>,
    pub guard_slot: Option<String>,
    pub guard_far: Option<f64>,
    pub guard_frr: Option<f64>,
    pub message: Option<String>,
}

impl StructuredMetricEvent {
    pub fn ingest(vault: &str, ts_unix_secs: u64, duration_ms: f64, ok: bool) -> Self {
        Self::base("ingest", vault, ts_unix_secs, ok).with_duration(duration_ms)
    }

    pub fn search(
        vault: &str,
        ts_unix_secs: u64,
        strategy: &str,
        duration_ms: f64,
        ok: bool,
    ) -> Self {
        let mut event = Self::base("search", vault, ts_unix_secs, ok).with_duration(duration_ms);
        event.search_strategy = Some(strategy.to_string());
        event
    }

    pub fn prediction(
        vault: &str,
        ts_unix_secs: u64,
        endpoint: &str,
        duration_ms: f64,
        ok: bool,
    ) -> Self {
        let mut event =
            Self::base("prediction", vault, ts_unix_secs, ok).with_duration(duration_ms);
        event.prediction_endpoint = Some(endpoint.to_string());
        event
    }

    pub fn guard(vault: &str, ts_unix_secs: u64, slot: &str, far: f64, frr: f64) -> Self {
        let mut event = Self::base("guard", vault, ts_unix_secs, true);
        event.guard_slot = Some(slot.to_string());
        event.guard_far = Some(far);
        event.guard_frr = Some(frr);
        event
    }

    fn base(surface: &str, vault: &str, ts_unix_secs: u64, ok: bool) -> Self {
        Self {
            ts_unix_secs,
            surface: surface.to_string(),
            vault: vault.to_string(),
            status: if ok { "ok" } else { "err" }.to_string(),
            duration_ms: None,
            search_strategy: None,
            prediction_endpoint: None,
            guard_slot: None,
            guard_far: None,
            guard_frr: None,
            message: None,
        }
    }

    fn with_duration(mut self, duration_ms: f64) -> Self {
        self.duration_ms = Some(duration_ms);
        self
    }

    pub fn validate(&self) -> Result<(), StructuredMetricLogError> {
        if self.ts_unix_secs == 0 {
            return Err(invalid("ts_unix_secs must be non-zero"));
        }
        if self.vault.trim().is_empty() {
            return Err(invalid("vault label must be non-empty"));
        }
        if !matches!(self.status.as_str(), "ok" | "err") {
            return Err(invalid("status must be ok or err"));
        }
        match self.surface.as_str() {
            "ingest" => require_duration(self),
            "search" => {
                require_duration(self)?;
                require_present("search_strategy", self.search_strategy.as_deref())
            }
            "prediction" => {
                require_duration(self)?;
                match self.prediction_endpoint.as_deref() {
                    Some("match" | "progression" | "player") => Ok(()),
                    _ => Err(invalid(
                        "prediction_endpoint must be match, progression, or player",
                    )),
                }
            }
            "guard" => {
                require_present("guard_slot", self.guard_slot.as_deref())?;
                require_rate("guard_far", self.guard_far)?;
                require_rate("guard_frr", self.guard_frr)
            }
            _ => Err(invalid(
                "surface must be ingest, search, prediction, or guard",
            )),
        }
    }
}

pub struct StructuredMetricLog {
    path: PathBuf,
}

impl StructuredMetricLog {
    pub fn new(path: impl AsRef<Path>) -> Self {
        Self {
            path: path.as_ref().to_path_buf(),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn append(&self, event: &StructuredMetricEvent) -> Result<(), StructuredMetricLogError> {
        event.validate()?;
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent).map_err(|error| write_failed(error.to_string()))?;
        }
        let line = serde_json::to_string(event)
            .map_err(|error| write_failed(format!("serialize event: {error}")))?;
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(|error| write_failed(format!("open {}: {error}", self.path.display())))?;
        writeln!(file, "{line}")
            .map_err(|error| write_failed(format!("write {}: {error}", self.path.display())))?;
        Ok(())
    }
}

fn require_duration(event: &StructuredMetricEvent) -> Result<(), StructuredMetricLogError> {
    let Some(duration_ms) = event.duration_ms else {
        return Err(invalid("duration_ms is required"));
    };
    if !duration_ms.is_finite() || duration_ms < 0.0 {
        return Err(invalid("duration_ms must be finite and non-negative"));
    }
    Ok(())
}

fn require_present(name: &str, value: Option<&str>) -> Result<(), StructuredMetricLogError> {
    if value.is_some_and(|value| !value.trim().is_empty()) {
        return Ok(());
    }
    Err(invalid(format!("{name} must be non-empty")))
}

fn require_rate(name: &str, value: Option<f64>) -> Result<(), StructuredMetricLogError> {
    let Some(value) = value else {
        return Err(invalid(format!("{name} is required")));
    };
    if !value.is_finite() || !(0.0..=1.0).contains(&value) {
        return Err(invalid(format!("{name} must be finite and within [0, 1]")));
    }
    Ok(())
}

fn invalid(message: impl Into<String>) -> StructuredMetricLogError {
    StructuredMetricLogError {
        code: CALYX_METRICS_INVALID_OBSERVATION,
        message: message.into(),
        remediation: "emit bounded labels and finite metric values before appending the log",
    }
}

fn write_failed(message: impl Into<String>) -> StructuredMetricLogError {
    StructuredMetricLogError {
        code: CALYX_METRICS_LOG_WRITE_FAILED,
        message: message.into(),
        remediation: "write structured metric logs to a writable regular JSONL file",
    }
}
