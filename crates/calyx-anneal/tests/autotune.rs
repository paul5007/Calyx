use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

use calyx_anneal::{
    AutotunePolicy, CALYX_ANNEAL_AUTOTUNE_INVALID_CONFIG, CALYX_ANNEAL_AUTOTUNE_NOT_REVERSIBLE,
    TripwireMetric, autotune_config_path, enable_autotune_in_vault,
    read_autotune_policy_from_vault, tripwire_config_path,
};

static NEXT_ROOT: AtomicUsize = AtomicUsize::new(0);

struct TestRoot(PathBuf);

impl TestRoot {
    fn new(label: &str) -> Self {
        let id = NEXT_ROOT.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "calyx-autotune-{label}-{}-{id}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).expect("create temp root");
        Self(path)
    }

    fn path(&self) -> &PathBuf {
        &self.0
    }
}

impl Drop for TestRoot {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn enable_autotune_persists_reversible_policy_and_tripwires() {
    let root = TestRoot::new("persist");
    let readback = enable_autotune_in_vault(root.path(), AutotunePolicy::soccer_lab_default())
        .expect("enable autotune");

    assert_eq!(readback.policy_path, autotune_config_path(root.path()));
    assert!(readback.policy.enabled);
    assert!(readback.policy.shadow.required);
    assert_eq!(readback.policy.shadow.min_replay_queries, 3);
    assert_eq!(readback.tripwire_config.thresholds.len(), 5);

    let recall = readback
        .tripwire_config
        .thresholds
        .iter()
        .find(|entry| entry.metric == TripwireMetric::RecallAtK)
        .expect("recall tripwire");
    assert_eq!(recall.threshold.bound, 0.95);

    let policy_toml = fs::read_to_string(autotune_config_path(root.path())).expect("policy toml");
    assert!(policy_toml.contains("enabled = true"));
    assert!(policy_toml.contains("revert_on_tripwire_or_regression"));
    let tripwire_toml =
        fs::read_to_string(tripwire_config_path(root.path())).expect("tripwire toml");
    assert!(tripwire_toml.contains("[thresholds.recall_at_k]"));
    assert!(tripwire_toml.contains("bound = 0.95"));

    let reopened = read_autotune_policy_from_vault(root.path()).expect("read policy");
    assert_eq!(reopened.policy, readback.policy);
}

#[test]
fn policy_edges_fail_closed() {
    let root = TestRoot::new("edges");

    let mut low_recall = AutotunePolicy::soccer_lab_default();
    low_recall.tripwires.recall_at_k_floor = 0.94;
    let error = enable_autotune_in_vault(root.path(), low_recall).unwrap_err();
    assert_eq!(error.code, CALYX_ANNEAL_AUTOTUNE_INVALID_CONFIG);

    let mut no_shadow = AutotunePolicy::soccer_lab_default();
    no_shadow.shadow.required = false;
    let error = enable_autotune_in_vault(root.path(), no_shadow).unwrap_err();
    assert_eq!(error.code, CALYX_ANNEAL_AUTOTUNE_NOT_REVERSIBLE);

    let mut auto_commit = AutotunePolicy::soccer_lab_default();
    auto_commit.rollback.commit_after_successful_shadow = true;
    let error = enable_autotune_in_vault(root.path(), auto_commit).unwrap_err();
    assert_eq!(error.code, CALYX_ANNEAL_AUTOTUNE_NOT_REVERSIBLE);

    let mut too_little_replay = AutotunePolicy::soccer_lab_default();
    too_little_replay.shadow.min_replay_queries = 2;
    let error = enable_autotune_in_vault(root.path(), too_little_replay).unwrap_err();
    assert_eq!(error.code, CALYX_ANNEAL_AUTOTUNE_INVALID_CONFIG);
}
