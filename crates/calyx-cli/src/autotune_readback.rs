use std::fs;
use std::path::Path;

use calyx_anneal::{autotune_config_path, read_autotune_policy_from_vault};
use serde_json::json;

use crate::error::CliError;

pub fn readback_autotune_config(vault: &Path) -> crate::error::CliResult {
    let config_path = autotune_config_path(vault);
    let bytes = fs::read(&config_path)
        .map_err(|error| CliError::io(format!("read {}: {error}", config_path.display())))?;
    let parsed = read_autotune_policy_from_vault(vault)?;
    let readback = json!({
        "surface": "config.autotune",
        "source_of_truth": "vault .anneal/autotune.toml plus .anneal/tripwire.toml",
        "config_path": display_path(&parsed.policy_path),
        "config_len": bytes.len(),
        "config_blake3": blake3::hash(&bytes).to_hex().to_string(),
        "policy": parsed.policy,
        "tripwire_config_path": display_path(&parsed.tripwire_config.config_path),
        "tripwire_thresholds": parsed.tripwire_config.thresholds,
    });
    println!(
        "{}",
        serde_json::to_string_pretty(&readback).map_err(|error| {
            CliError::runtime(format!("serialize autotune config readback: {error}"))
        })?
    );
    Ok(())
}

fn display_path(path: &Path) -> String {
    path.canonicalize()
        .unwrap_or_else(|_| path.to_path_buf())
        .display()
        .to_string()
}
