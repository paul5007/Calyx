use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use calyx_core::{CxId, FixedClock};
use calyx_ledger::{ActorId, DirectoryLedgerStore, EntryKind, LedgerAppender, SubjectId};
use serde_json::json;

#[test]
#[ignore = "requires CALYX_ISSUE70_FSV_ROOT in a manual verification run"]
fn issue70_ledger_verify_job_fsv() {
    let root = PathBuf::from(env::var("CALYX_ISSUE70_FSV_ROOT").expect("set FSV root"));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).expect("create FSV root");

    let intact = root.join("ledger-intact");
    write_ledger(&intact, 4);

    let tampered = root.join("ledger-tampered");
    write_ledger(&tampered, 4);
    flip_last_byte(&tampered.join("0000000000000002.ledger"));

    let truncated = root.join("ledger-truncated");
    write_ledger(&truncated, 4);
    truncate_file(&truncated.join("0000000000000001.ledger"), 12);

    let happy = run_job(
        &root,
        "happy",
        json!([{ "kind": "ledger", "path": intact.display().to_string() }]),
    );
    assert!(happy.status.success(), "stderr: {}", stderr(&happy));
    let happy_report = read_json(root.join("happy-report.json"));
    assert_eq!(happy_report["status"], "ok");
    assert_eq!(happy_report["targets"][0]["ok"], 1);
    assert_eq!(happy_report["targets"][0]["entries"], 4);

    let alert_targets = json!([
        { "kind": "ledger", "path": intact.display().to_string() },
        { "kind": "ledger", "path": tampered.display().to_string() },
        { "kind": "ledger", "path": truncated.display().to_string() },
        { "kind": "ledger", "path": root.join("missing-ledger").display().to_string() }
    ]);
    let alert = run_job(&root, "alert", alert_targets);
    assert_eq!(alert.status.code(), Some(3), "stderr: {}", stderr(&alert));
    let alert_report = read_json(root.join("alert-report.json"));
    assert_eq!(alert_report["status"], "alert");
    assert_eq!(alert_report["targets"][0]["ok"], 1);
    assert_eq!(alert_report["targets"][1]["ok"], 0);
    assert_eq!(
        alert_report["targets"][1]["code"],
        "CALYX_LEDGER_CHAIN_BROKEN"
    );
    assert!(
        alert_report["targets"][1]["stderr"]
            .as_str()
            .expect("stderr")
            .contains("CALYX_LEDGER_CHAIN_BROKEN"),
        "tampered target stderr must carry alert code: {alert_report}"
    );
    assert_eq!(alert_report["targets"][2]["ok"], 0);
    assert_eq!(
        alert_report["targets"][2]["code"],
        "CALYX_LEDGER_CHAIN_BROKEN"
    );
    assert!(
        alert_report["targets"][2]["stderr"]
            .as_str()
            .expect("stderr")
            .contains("CALYX_LEDGER_CORRUPT"),
        "truncated target stderr must carry corrupt code: {alert_report}"
    );
    assert_eq!(alert_report["targets"][3]["ok"], 0);
    assert_eq!(
        alert_report["targets"][3]["code"],
        "CALYX_LEDGER_VERIFY_JOB_COMMAND_FAILED"
    );

    let invalid = run_raw_job(
        &root,
        "invalid",
        "not-json",
        root.join("invalid-report.json"),
        root.join("invalid-job.jsonl"),
    );
    assert_eq!(invalid.status.code(), Some(2));
    let invalid_report = read_json(root.join("invalid-report.json"));
    assert_eq!(invalid_report["status"], "error");
    assert_eq!(
        invalid_report["code"],
        "CALYX_LEDGER_VERIFY_JOB_INVALID_CONFIG"
    );

    let readback = json!({
        "surface": "soccer_lab.ledger_verify_job",
        "source_of_truth": "calyxd --once Prometheus text plus structured JSONL job logs",
        "happy_report": happy_report,
        "alert_report": alert_report,
        "invalid_report": invalid_report,
        "edges": [
            {"case": "intact_ledger", "expected": "status ok", "observed": "ok"},
            {"case": "tampered_row", "expected": "CALYX_LEDGER_CHAIN_BROKEN", "observed": alert_report["targets"][1]["code"]},
            {"case": "truncated_row", "expected": "CALYX_LEDGER_CORRUPT in stderr and alert status", "observed": alert_report["targets"][2]["stderr"]},
            {"case": "missing_target", "expected": "CALYX_LEDGER_VERIFY_JOB_COMMAND_FAILED", "observed": alert_report["targets"][3]["code"]},
            {"case": "invalid_targets_json", "expected": "CALYX_LEDGER_VERIFY_JOB_INVALID_CONFIG", "observed": invalid_report["code"]}
        ]
    });
    fs::write(
        root.join("ledger-verify-readback.json"),
        serde_json::to_vec_pretty(&readback).expect("serialize readback"),
    )
    .expect("write readback");
    write_manifest(
        &root,
        &[
            root.join("happy-report.json"),
            root.join("happy-job.jsonl"),
            root.join("alert-report.json"),
            root.join("alert-job.jsonl"),
            root.join("invalid-report.json"),
            root.join("invalid-job.jsonl"),
            root.join("ledger-verify-readback.json"),
        ],
    );
    println!("ISSUE70_FSV_ROOT={}", root.display());
}

fn run_job(root: &Path, label: &str, targets: serde_json::Value) -> Output {
    run_raw_job(
        root,
        label,
        &serde_json::to_string(&targets).expect("targets JSON"),
        root.join(format!("{label}-report.json")),
        root.join(format!("{label}-job.jsonl")),
    )
}

fn run_raw_job(root: &Path, label: &str, targets: &str, out: PathBuf, log: PathBuf) -> Output {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates dir")
        .parent()
        .expect("repo root");
    let script = repo.join("tools/ops/run_ledger_verify_job.py");
    let calyxd = env!("CARGO_BIN_EXE_calyxd");
    let output = Command::new("python3")
        .arg(script)
        .arg("--targets")
        .arg(targets)
        .arg("--out")
        .arg(out)
        .arg("--log")
        .arg(log)
        .arg("--calyxd-bin")
        .arg(calyxd)
        .current_dir(repo)
        .output()
        .unwrap_or_else(|error| panic!("run job {label} in {}: {error}", root.display()));
    fs::write(root.join(format!("{label}-stdout.txt")), &output.stdout).expect("write stdout");
    fs::write(root.join(format!("{label}-stderr.txt")), &output.stderr).expect("write stderr");
    output
}

fn write_ledger(dir: &Path, count: usize) {
    let store = DirectoryLedgerStore::open(dir).expect("open ledger dir");
    let mut appender = LedgerAppender::open(store, FixedClock::new(10)).expect("open appender");
    for seq in 0..count {
        appender
            .append(
                EntryKind::Ingest,
                SubjectId::Cx(CxId::from_bytes([seq as u8; 16])),
                format!("issue70-ledger-payload-{seq}").into_bytes(),
                ActorId::Service("issue70-fsv".to_string()),
            )
            .expect("append ledger row");
    }
}

fn read_json(path: PathBuf) -> serde_json::Value {
    serde_json::from_slice(&fs::read(path).expect("read JSON")).expect("parse JSON")
}

fn flip_last_byte(path: &Path) {
    let mut bytes = fs::read(path).expect("read ledger row");
    let last = bytes.len() - 1;
    bytes[last] ^= 1;
    fs::write(path, bytes).expect("write tampered row");
}

fn truncate_file(path: &Path, keep: usize) {
    let bytes = fs::read(path).expect("read ledger row");
    fs::write(path, &bytes[..keep]).expect("write truncated row");
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).to_string()
}

fn write_manifest(root: &Path, files: &[PathBuf]) {
    let mut out = String::new();
    for path in files {
        let bytes = fs::read(path).expect("read manifest input");
        let rel = path.strip_prefix(root).expect("relative path");
        out.push_str(&format!(
            "{}  {}\n",
            blake3::hash(&bytes).to_hex(),
            rel.display()
        ));
    }
    fs::write(root.join("BLAKE3SUMS.txt"), out).expect("write manifest");
}
