use std::env;
use std::fs;
use std::path::PathBuf;

use calyx_anneal::{
    AutotunePolicy, CALYX_ANNEAL_AUTOTUNE_INVALID_CONFIG, CALYX_ANNEAL_AUTOTUNE_NOT_REVERSIBLE,
    autotune_config_path, enable_autotune_in_vault, read_autotune_policy_from_vault,
    tripwire_config_path,
};
use serde_json::json;

#[path = "fsv_support/mod.rs"]
mod fsv_support;
use fsv_support::{write_json, write_manifest};

#[test]
#[ignore = "requires CALYX_ISSUE68_FSV_ROOT in a manual verification run"]
fn issue68_autotune_policy_fsv() {
    let root =
        PathBuf::from(env::var("CALYX_ISSUE68_FSV_ROOT").expect("set CALYX_ISSUE68_FSV_ROOT"));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).expect("create FSV root");
    let vault = root.join("vault");
    fs::create_dir_all(&vault).expect("create vault");

    let policy = AutotunePolicy::soccer_lab_default();
    let readback = enable_autotune_in_vault(&vault, policy.clone()).expect("enable autotune");
    let reopened = read_autotune_policy_from_vault(&vault).expect("reopen autotune");
    assert_eq!(reopened.policy, policy);

    let policy_path = autotune_config_path(&vault);
    let tripwire_path = tripwire_config_path(&vault);
    let policy_bytes = fs::read(&policy_path).expect("read policy bytes");
    let tripwire_bytes = fs::read(&tripwire_path).expect("read tripwire bytes");
    assert_eq!(
        policy_bytes,
        fs::read(&readback.policy_path).expect("read readback policy path")
    );
    assert_eq!(
        tripwire_bytes,
        fs::read(&readback.tripwire_config.config_path).expect("read readback tripwire path")
    );

    let edges = edge_cases(&vault);
    write_json(
        &root.join("autotune-readback.json"),
        &json!({
            "surface": "anneal.autotune_policy",
            "source_of_truth": "vault .anneal/autotune.toml plus .anneal/tripwire.toml bytes",
            "vault": vault.display().to_string(),
            "policy_path": policy_path.display().to_string(),
            "tripwire_path": tripwire_path.display().to_string(),
            "policy": readback.policy,
            "tripwire_thresholds": readback.tripwire_config.thresholds,
            "policy_toml": String::from_utf8(policy_bytes).expect("policy UTF-8"),
            "tripwire_toml": String::from_utf8(tripwire_bytes).expect("tripwire UTF-8"),
            "edges": edges
        }),
    );
    write_manifest(
        &root,
        &[
            policy_path,
            tripwire_path,
            root.join("autotune-readback.json"),
        ],
    );
    println!("ISSUE68_FSV_ROOT={}", root.display());
}

fn edge_cases(vault: &std::path::Path) -> serde_json::Value {
    let mut low_recall = AutotunePolicy::soccer_lab_default();
    low_recall.tripwires.recall_at_k_floor = 0.94;
    let low_recall_error = enable_autotune_in_vault(vault, low_recall).unwrap_err();
    assert_eq!(low_recall_error.code, CALYX_ANNEAL_AUTOTUNE_INVALID_CONFIG);

    let mut no_shadow = AutotunePolicy::soccer_lab_default();
    no_shadow.shadow.required = false;
    let no_shadow_error = enable_autotune_in_vault(vault, no_shadow).unwrap_err();
    assert_eq!(no_shadow_error.code, CALYX_ANNEAL_AUTOTUNE_NOT_REVERSIBLE);

    let mut auto_commit = AutotunePolicy::soccer_lab_default();
    auto_commit.rollback.commit_after_successful_shadow = true;
    let auto_commit_error = enable_autotune_in_vault(vault, auto_commit).unwrap_err();
    assert_eq!(auto_commit_error.code, CALYX_ANNEAL_AUTOTUNE_NOT_REVERSIBLE);

    let mut too_little_replay = AutotunePolicy::soccer_lab_default();
    too_little_replay.shadow.min_replay_queries = 2;
    let replay_error = enable_autotune_in_vault(vault, too_little_replay).unwrap_err();
    assert_eq!(replay_error.code, CALYX_ANNEAL_AUTOTUNE_INVALID_CONFIG);

    json!([
        {
            "case": "recall_floor_below_0_95",
            "expected": CALYX_ANNEAL_AUTOTUNE_INVALID_CONFIG,
            "observed": low_recall_error.code,
        },
        {
            "case": "shadow_disabled",
            "expected": CALYX_ANNEAL_AUTOTUNE_NOT_REVERSIBLE,
            "observed": no_shadow_error.code,
        },
        {
            "case": "commit_after_shadow_disables_explicit_rollback_window",
            "expected": CALYX_ANNEAL_AUTOTUNE_NOT_REVERSIBLE,
            "observed": auto_commit_error.code,
        },
        {
            "case": "min_replay_queries_below_three",
            "expected": CALYX_ANNEAL_AUTOTUNE_INVALID_CONFIG,
            "observed": replay_error.code,
        }
    ])
}
