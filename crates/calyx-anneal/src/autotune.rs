use std::fs;
use std::path::{Path, PathBuf};

use calyx_core::{CalyxError, Result};
use serde::{Deserialize, Serialize};

use crate::{
    TripwireConfigReadback, TripwireMetric, TripwireRegistry, read_tripwire_config_from_vault,
};

pub const CALYX_ANNEAL_AUTOTUNE_INVALID_CONFIG: &str = "CALYX_ANNEAL_AUTOTUNE_INVALID_CONFIG";
pub const CALYX_ANNEAL_AUTOTUNE_NOT_REVERSIBLE: &str = "CALYX_ANNEAL_AUTOTUNE_NOT_REVERSIBLE";

const CONFIG_DIR: &str = ".anneal";
const AUTOTUNE_FILE: &str = "autotune.toml";
const DEFAULT_MIN_REPLAY_QUERIES: usize = 3;
const DEFAULT_BUDGET_CPU_WEIGHT: f64 = 0.01;
const DEFAULT_BUDGET_VRAM_BYTES: u64 = 0;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AutotunePolicy {
    pub enabled: bool,
    pub rollback: RollbackPolicy,
    pub tripwires: AutotuneTripwires,
    pub shadow: ShadowPolicy,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RollbackPolicy {
    pub mode: RollbackMode,
    pub commit_after_successful_shadow: bool,
    pub explicit_rollback_allowed: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RollbackMode {
    RevertOnTripwireOrRegression,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AutotuneTripwires {
    pub recall_at_k_floor: f64,
    pub guard_far_ceiling: f64,
    pub guard_frr_ceiling: f64,
    pub search_p99_ceiling_ms: f64,
    pub ingest_p95_ceiling_ms: f64,
    pub hysteresis_fraction: f64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ShadowPolicy {
    pub required: bool,
    pub min_replay_queries: usize,
    pub budget_cpu_weight: f64,
    pub budget_vram_bytes: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct AutotunePolicyReadback {
    pub policy_path: PathBuf,
    pub policy: AutotunePolicy,
    pub tripwire_config: TripwireConfigReadback,
}

impl Default for AutotunePolicy {
    fn default() -> Self {
        Self::soccer_lab_default()
    }
}

impl AutotunePolicy {
    pub fn soccer_lab_default() -> Self {
        Self {
            enabled: true,
            rollback: RollbackPolicy {
                mode: RollbackMode::RevertOnTripwireOrRegression,
                commit_after_successful_shadow: false,
                explicit_rollback_allowed: true,
            },
            tripwires: AutotuneTripwires {
                recall_at_k_floor: 0.95,
                guard_far_ceiling: 0.01,
                guard_frr_ceiling: 0.05,
                search_p99_ceiling_ms: 200.0,
                ingest_p95_ceiling_ms: 500.0,
                hysteresis_fraction: 0.05,
            },
            shadow: ShadowPolicy {
                required: true,
                min_replay_queries: DEFAULT_MIN_REPLAY_QUERIES,
                budget_cpu_weight: DEFAULT_BUDGET_CPU_WEIGHT,
                budget_vram_bytes: DEFAULT_BUDGET_VRAM_BYTES,
            },
        }
    }

    pub fn validate(&self) -> Result<()> {
        if !self.enabled {
            return Err(invalid_config("autotune policy must be enabled"));
        }
        if self.rollback.mode != RollbackMode::RevertOnTripwireOrRegression {
            return Err(not_reversible(
                "rollback mode must revert on tripwire or regression",
            ));
        }
        if self.rollback.commit_after_successful_shadow {
            return Err(not_reversible(
                "autotune changes must remain explicitly rollbackable after shadow promotion",
            ));
        }
        if !self.rollback.explicit_rollback_allowed {
            return Err(not_reversible("explicit rollback must remain allowed"));
        }
        if !self.shadow.required {
            return Err(not_reversible(
                "shadow evaluation is required before promotion",
            ));
        }
        if self.shadow.min_replay_queries < DEFAULT_MIN_REPLAY_QUERIES {
            return Err(invalid_config(
                "shadow min_replay_queries must be at least 3",
            ));
        }
        if !self.shadow.budget_cpu_weight.is_finite() || self.shadow.budget_cpu_weight <= 0.0 {
            return Err(invalid_config(
                "shadow budget_cpu_weight must be finite and positive",
            ));
        }
        if !self.tripwires.hysteresis_fraction.is_finite()
            || !(0.0..=1.0).contains(&self.tripwires.hysteresis_fraction)
        {
            return Err(invalid_config(
                "tripwire hysteresis_fraction must be finite and within [0, 1]",
            ));
        }
        validate_floor("recall_at_k_floor", self.tripwires.recall_at_k_floor, 0.95)?;
        validate_ceiling("guard_far_ceiling", self.tripwires.guard_far_ceiling, 0.01)?;
        validate_ceiling("guard_frr_ceiling", self.tripwires.guard_frr_ceiling, 0.05)?;
        validate_positive_ceiling(
            "search_p99_ceiling_ms",
            self.tripwires.search_p99_ceiling_ms,
        )?;
        validate_positive_ceiling(
            "ingest_p95_ceiling_ms",
            self.tripwires.ingest_p95_ceiling_ms,
        )?;
        Ok(())
    }
}

pub fn autotune_config_path(vault: &Path) -> PathBuf {
    vault.join(CONFIG_DIR).join(AUTOTUNE_FILE)
}

pub fn enable_autotune_in_vault(
    vault: impl AsRef<Path>,
    policy: AutotunePolicy,
) -> Result<AutotunePolicyReadback> {
    let vault = vault.as_ref();
    policy.validate()?;
    let policy_path = autotune_config_path(vault);
    persist_policy(&policy_path, &policy)?;
    let mut tripwires = TripwireRegistry::load_from_vault(vault)?;
    arm_tripwires(&mut tripwires, &policy)?;
    let readback = read_autotune_policy_from_vault(vault)?;
    if readback.policy != policy {
        return Err(invalid_config(
            "autotune policy readback mismatch after write",
        ));
    }
    Ok(readback)
}

pub fn read_autotune_policy_from_vault(vault: impl AsRef<Path>) -> Result<AutotunePolicyReadback> {
    let vault = vault.as_ref();
    let policy_path = autotune_config_path(vault);
    let policy = read_policy(&policy_path)?;
    policy.validate()?;
    let tripwire_config = read_tripwire_config_from_vault(vault)?;
    Ok(AutotunePolicyReadback {
        policy_path,
        policy,
        tripwire_config,
    })
}

fn arm_tripwires(registry: &mut TripwireRegistry, policy: &AutotunePolicy) -> Result<()> {
    let h = policy.tripwires.hysteresis_fraction;
    registry.set_tripwire(
        TripwireMetric::RecallAtK,
        policy.tripwires.recall_at_k_floor,
        policy.tripwires.recall_at_k_floor * h,
    )?;
    registry.set_tripwire(
        TripwireMetric::GuardFAR,
        policy.tripwires.guard_far_ceiling,
        policy.tripwires.guard_far_ceiling * h,
    )?;
    registry.set_tripwire(
        TripwireMetric::GuardFRR,
        policy.tripwires.guard_frr_ceiling,
        policy.tripwires.guard_frr_ceiling * h,
    )?;
    registry.set_tripwire(
        TripwireMetric::SearchP99,
        policy.tripwires.search_p99_ceiling_ms,
        policy.tripwires.search_p99_ceiling_ms * h,
    )?;
    registry.set_tripwire(
        TripwireMetric::IngestP95,
        policy.tripwires.ingest_p95_ceiling_ms,
        policy.tripwires.ingest_p95_ceiling_ms * h,
    )?;
    Ok(())
}

fn persist_policy(path: &Path, policy: &AutotunePolicy) -> Result<()> {
    let text = toml::to_string_pretty(policy)
        .map_err(|error| invalid_config(format!("serialize autotune policy: {error}")))?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| invalid_config(format!("create {}: {error}", parent.display())))?;
    }
    fs::write(path, text)
        .map_err(|error| invalid_config(format!("write {}: {error}", path.display())))?;
    Ok(())
}

fn read_policy(path: &Path) -> Result<AutotunePolicy> {
    let bytes = fs::read(path)
        .map_err(|error| invalid_config(format!("read {}: {error}", path.display())))?;
    let text = std::str::from_utf8(&bytes)
        .map_err(|error| invalid_config(format!("{} is not UTF-8: {error}", path.display())))?;
    toml::from_str(text)
        .map_err(|error| invalid_config(format!("parse {}: {error}", path.display())))
}

fn validate_floor(name: &str, value: f64, minimum: f64) -> Result<()> {
    if !value.is_finite() || value < minimum {
        return Err(invalid_config(format!(
            "{name} must be finite and >= {minimum}"
        )));
    }
    Ok(())
}

fn validate_ceiling(name: &str, value: f64, maximum: f64) -> Result<()> {
    if !value.is_finite() || value <= 0.0 || value > maximum {
        return Err(invalid_config(format!(
            "{name} must be finite, positive, and <= {maximum}"
        )));
    }
    Ok(())
}

fn validate_positive_ceiling(name: &str, value: f64) -> Result<()> {
    if !value.is_finite() || value <= 0.0 {
        return Err(invalid_config(format!(
            "{name} must be finite and positive"
        )));
    }
    Ok(())
}

fn invalid_config(message: impl Into<String>) -> CalyxError {
    CalyxError {
        code: CALYX_ANNEAL_AUTOTUNE_INVALID_CONFIG,
        message: message.into(),
        remediation: "write a reversible .anneal/autotune.toml with guarded tripwire thresholds",
    }
}

fn not_reversible(message: impl Into<String>) -> CalyxError {
    CalyxError {
        code: CALYX_ANNEAL_AUTOTUNE_NOT_REVERSIBLE,
        message: message.into(),
        remediation: "require shadow evaluation, tripwire rollback, and explicit rollback availability",
    }
}
