use std::fs;
use std::path::{Path, PathBuf};

use calyx_anneal::{AutotunePolicy, autotune_config_path, enable_autotune_in_vault};
use serde_json::json;

use crate::error::{CliError, CliResult};

pub(crate) fn run(args: &[String]) -> CliResult {
    let vault = parse_args(args)?;
    fs::create_dir_all(&vault)
        .map_err(|error| CliError::io(format!("create vault dir {}: {error}", vault.display())))?;
    let readback = enable_autotune_in_vault(&vault, AutotunePolicy::soccer_lab_default())?;
    let policy_bytes = fs::read(autotune_config_path(&vault)).map_err(|error| {
        CliError::io(format!(
            "read enabled autotune config {}: {error}",
            autotune_config_path(&vault).display()
        ))
    })?;
    let out = json!({
        "status": "ok",
        "surface": "anneal.autotune.enable",
        "source_of_truth": "vault .anneal/autotune.toml plus .anneal/tripwire.toml",
        "vault": display_path(&vault),
        "policy_path": display_path(&readback.policy_path),
        "policy_blake3": blake3::hash(&policy_bytes).to_hex().to_string(),
        "policy": readback.policy,
        "tripwire_config_path": display_path(&readback.tripwire_config.config_path),
        "tripwire_thresholds": readback.tripwire_config.thresholds,
    });
    println!(
        "{}",
        serde_json::to_string_pretty(&out)
            .map_err(|error| CliError::runtime(format!("serialize autotune enable: {error}")))?
    );
    Ok(())
}

fn parse_args(args: &[String]) -> CliResult<PathBuf> {
    match args {
        [vault_flag, vault] if vault_flag == "--vault" => Ok(PathBuf::from(vault)),
        _ => Err(CliError::usage(
            "anneal enable-autotune requires --vault <dir>",
        )),
    }
}

fn display_path(path: &Path) -> String {
    path.canonicalize()
        .unwrap_or_else(|_| path.to_path_buf())
        .display()
        .to_string()
}
