use std::env;
use std::fs;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use calyxd::metrics::{
    CALYX_METRICS_INVALID_OBSERVATION, CALYX_METRICS_LOG_WRITE_FAILED, CalyxMetrics,
    ChainVerifyMetrics, PredictionSurface, SearchStrategy, StructuredMetricEvent,
    StructuredMetricLog, VerifyOutcome,
};
use calyxd::server::MetricsServer;
use serde_json::json;
use tokio_util::sync::CancellationToken;

const VAULT: &str = "/data/soccer-lab-vault";

#[test]
#[ignore = "requires CALYX_ISSUE69_FSV_ROOT in a manual verification run"]
fn issue69_monitoring_metrics_and_structured_logs_fsv() {
    let root = PathBuf::from(env::var("CALYX_ISSUE69_FSV_ROOT").expect("set FSV root"));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).expect("create FSV root");

    let metrics = recorded_metrics();
    let server =
        MetricsServer::bind("127.0.0.1:0".parse().unwrap(), Arc::clone(&metrics)).expect("bind");
    let addr = server.local_addr().expect("local_addr").to_string();
    let cancel = CancellationToken::new();
    let stop = cancel.clone();
    let join = std::thread::spawn(move || server.run(cancel).expect("metrics server run"));

    let response = http_get(&addr, "/metrics");
    stop.cancel();
    join.join().expect("metrics server joins after cancel");
    assert!(response.starts_with("HTTP/1.1 200 OK"), "{response}");
    let metrics_body = response
        .split("\r\n\r\n")
        .nth(1)
        .expect("HTTP body")
        .to_string();
    assert_line(
        &metrics_body,
        "calyx_ingest_total{status=\"ok\",vault=\"/data/soccer-lab-vault\"} 1",
    );
    assert_line(
        &metrics_body,
        "calyx_search_total{status=\"ok\",strategy=\"weighted_rrf\",vault=\"/data/soccer-lab-vault\"} 1",
    );
    assert_line(
        &metrics_body,
        "calyx_prediction_total{endpoint=\"match\",status=\"ok\",vault=\"/data/soccer-lab-vault\"} 1",
    );
    assert_line(
        &metrics_body,
        "calyx_guard_far{slot=\"oracle\",vault=\"/data/soccer-lab-vault\"} 0.01",
    );
    assert_line(
        &metrics_body,
        "calyx_guard_frr{slot=\"oracle\",vault=\"/data/soccer-lab-vault\"} 0.03",
    );
    fs::write(root.join("metrics.prom"), &metrics_body).expect("write metrics scrape");

    let log_path = root.join("soccer-lab-ops.jsonl");
    let log = StructuredMetricLog::new(&log_path);
    for event in [
        StructuredMetricEvent::ingest(VAULT, 1_770_000_000, 150.0, true),
        StructuredMetricEvent::search(VAULT, 1_770_000_001, "weighted_rrf", 20.0, true),
        StructuredMetricEvent::prediction(VAULT, 1_770_000_002, "match", 30.0, true),
        StructuredMetricEvent::guard(VAULT, 1_770_000_003, "oracle", 0.01, 0.03),
    ] {
        log.append(&event).expect("append structured log event");
    }
    let log_bytes = fs::read(&log_path).expect("read log bytes");
    let log_text = String::from_utf8(log_bytes).expect("log UTF-8");
    let log_rows: Vec<serde_json::Value> = log_text
        .lines()
        .map(|line| serde_json::from_str(line).expect("JSONL row"))
        .collect();
    assert_eq!(log_rows.len(), 4);
    assert_eq!(log_rows[2]["surface"], "prediction");
    assert_eq!(log_rows[2]["prediction_endpoint"], "match");
    assert_eq!(log_rows[3]["guard_far"], 0.01);
    assert_eq!(log_rows[3]["guard_frr"], 0.03);

    let edges = edge_cases(&root);
    let readback = json!({
        "surface": "soccer_lab.monitoring",
        "source_of_truth": "HTTP /metrics scrape bytes plus structured JSONL log bytes",
        "metrics_path": root.join("metrics.prom").display().to_string(),
        "log_path": log.path().display().to_string(),
        "metrics_contains": {
            "ingest": "calyx_ingest_total",
            "search": "calyx_search_total",
            "prediction": "calyx_prediction_total",
            "guard_far": "calyx_guard_far",
            "guard_frr": "calyx_guard_frr"
        },
        "structured_log_rows": log_rows,
        "edges": edges
    });
    fs::write(
        root.join("monitoring-readback.json"),
        serde_json::to_vec_pretty(&readback).expect("serialize readback"),
    )
    .expect("write readback");
    write_manifest(
        &root,
        &[
            root.join("metrics.prom"),
            root.join("soccer-lab-ops.jsonl"),
            root.join("monitoring-readback.json"),
        ],
    );
    println!("ISSUE69_FSV_ROOT={}", root.display());
}

fn recorded_metrics() -> Arc<CalyxMetrics> {
    let labels = [VAULT.to_string()];
    let chain = Arc::new(ChainVerifyMetrics::new(&labels));
    chain.record(VAULT, &VerifyOutcome::Intact { entries: 11 }, 1_770_000_000);
    let metrics = Arc::new(CalyxMetrics::new(chain, &labels));
    metrics.observe_ingest(VAULT, 0.150, true);
    metrics.observe_search(VAULT, SearchStrategy::WeightedRrf, 0.020, true);
    metrics.observe_prediction(VAULT, PredictionSurface::Match, 0.030, true);
    metrics.set_guard_rates(VAULT, "oracle", 0.01, 0.03);
    metrics
}

fn edge_cases(root: &Path) -> serde_json::Value {
    let log = StructuredMetricLog::new(root.join("edges.jsonl"));

    let mut bad_surface = StructuredMetricEvent::ingest(VAULT, 1, 1.0, true);
    bad_surface.surface = "unknown".to_string();
    let bad_surface_error = log.append(&bad_surface).unwrap_err();
    assert_eq!(bad_surface_error.code, CALYX_METRICS_INVALID_OBSERVATION);

    let bad_duration = StructuredMetricEvent::search(VAULT, 1, "weighted_rrf", -1.0, true);
    let bad_duration_error = log.append(&bad_duration).unwrap_err();
    assert_eq!(bad_duration_error.code, CALYX_METRICS_INVALID_OBSERVATION);

    let bad_endpoint = StructuredMetricEvent::prediction(VAULT, 1, "quarter_finalist", 1.0, true);
    let bad_endpoint_error = log.append(&bad_endpoint).unwrap_err();
    assert_eq!(bad_endpoint_error.code, CALYX_METRICS_INVALID_OBSERVATION);

    let bad_guard = StructuredMetricEvent::guard(VAULT, 1, "oracle", 1.2, 0.0);
    let bad_guard_error = log.append(&bad_guard).unwrap_err();
    assert_eq!(bad_guard_error.code, CALYX_METRICS_INVALID_OBSERVATION);

    let dir_path = root.join("log-path-is-directory");
    fs::create_dir_all(&dir_path).expect("create directory log path");
    let write_error = StructuredMetricLog::new(&dir_path)
        .append(&StructuredMetricEvent::ingest(VAULT, 1, 1.0, true))
        .unwrap_err();
    assert_eq!(write_error.code, CALYX_METRICS_LOG_WRITE_FAILED);

    json!([
        {
            "case": "unknown_surface",
            "expected": CALYX_METRICS_INVALID_OBSERVATION,
            "observed": bad_surface_error.code
        },
        {
            "case": "negative_duration",
            "expected": CALYX_METRICS_INVALID_OBSERVATION,
            "observed": bad_duration_error.code
        },
        {
            "case": "unbounded_prediction_endpoint",
            "expected": CALYX_METRICS_INVALID_OBSERVATION,
            "observed": bad_endpoint_error.code
        },
        {
            "case": "guard_far_out_of_range",
            "expected": CALYX_METRICS_INVALID_OBSERVATION,
            "observed": bad_guard_error.code
        },
        {
            "case": "log_path_is_directory",
            "expected": CALYX_METRICS_LOG_WRITE_FAILED,
            "observed": write_error.code
        }
    ])
}

fn http_get(addr: &str, path: &str) -> String {
    let mut stream = TcpStream::connect(addr).expect("connect");
    write!(stream, "GET {path} HTTP/1.1\r\nHost: {addr}\r\n\r\n").expect("send");
    let mut response = String::new();
    stream.read_to_string(&mut response).expect("read");
    response
}

fn assert_line(text: &str, expected: &str) {
    assert!(
        text.lines().any(|line| line == expected),
        "expected line {expected:?} in:\n{text}"
    );
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
