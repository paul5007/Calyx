//! Integration FSV for the calyx-web-api error envelope + resource guardrails.
//!
//! No mocks: every test drives the REAL `app()`/`build_app()` router (or the
//! REAL `guardrails`/`panic_catch_layer` middleware) in-process via
//! `tower::ServiceExt::oneshot` and inspects the actual response status +
//! JSON body + headers. Synthetic inputs with known expected outputs (an
//! oversized body, a tiny rate-limit bucket, a deliberately-slow handler, a
//! deliberately-panicking handler whose payload carries a sentinel that MUST
//! NOT appear in the response).

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode, header},
    middleware::from_fn_with_state,
    routing::get,
};
use calyx_web_api::{
    AuthCtx, ErrorCode, Guardrails, PredictionCtx, ProvenanceCtx, app, build_app,
    build_app_with_predictions, build_app_with_provenance, build_app_with_search, guardrails,
    panic_catch_layer, require_bearer,
};
use http_body_util::BodyExt;
use serde_json::Value;
use tower::ServiceExt;

/// Sentinel embedded in the synthetic panic payload; it MUST NOT appear in any
/// response body (the no-leak invariant of the panic handler).
const PANIC_SENTINEL: &str = "PANIC_SENTINEL_DO_NOT_LEAK_a1b2c3";

/// A deliberately-panicking handler with a concrete return type (a bare
/// panicking closure cannot infer `IntoResponse` from the never type).
async fn boom() -> StatusCode {
    panic!("{} at /boom", PANIC_SENTINEL)
}

/// A deliberately-slow handler used to exercise the request timeout.
async fn slow() -> StatusCode {
    tokio::time::sleep(Duration::from_millis(400)).await;
    StatusCode::OK
}

/// Drive one request through a router and return (status, parsed JSON body).
async fn call(app: Router, req: Request<Body>) -> (StatusCode, Value) {
    let resp = app.oneshot(req).await.expect("router is infallible");
    let status = resp.status();
    let bytes = resp
        .into_body()
        .collect()
        .await
        .expect("collect body")
        .to_bytes();
    let json: Value = serde_json::from_slice(&bytes).expect("error responses are JSON envelopes");
    (status, json)
}

/// Drive one request through a router and return status, selected headers, and
/// parsed JSON body.
async fn call_with_headers(
    app: Router,
    req: Request<Body>,
) -> (StatusCode, axum::http::HeaderMap, Value) {
    let resp = app.oneshot(req).await.expect("router is infallible");
    let status = resp.status();
    let headers = resp.headers().clone();
    let bytes = resp
        .into_body()
        .collect()
        .await
        .expect("collect body")
        .to_bytes();
    let json: Value = serde_json::from_slice(&bytes).expect("response is JSON");
    (status, headers, json)
}

/// Assert a body is the closed `{code,message,remediation}` envelope for `code`.
fn assert_envelope(body: &Value, code: ErrorCode) {
    assert_eq!(body["code"], code.code(), "code mismatch in {body}");
    assert_eq!(
        body["remediation"],
        code.remediation(),
        "remediation mismatch"
    );
    assert!(
        body["message"].as_str().is_some_and(|m| !m.is_empty()),
        "message present"
    );
}

#[tokio::test]
async fn health_is_ok_and_not_an_error_envelope() {
    let (status, body) = call(
        app(),
        Request::get("/v1/health").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["status"], "ok");
    assert_eq!(body["readOnly"], true);
    assert!(
        body.get("code").is_none(),
        "success body carries no error code"
    );
}

#[tokio::test]
async fn scaffolded_route_returns_not_implemented_envelope() {
    let (status, body) = call(
        app(),
        Request::post("/v1/measure").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED);
    assert_envelope(&body, ErrorCode::NotImplemented);
}

#[tokio::test]
async fn public_search_scaffold_returns_not_implemented_envelope() {
    let (status, body) = call(app(), Request::post("/search").body(Body::empty()).unwrap()).await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED);
    assert_envelope(&body, ErrorCode::NotImplemented);
}

#[tokio::test]
async fn public_kernel_answer_scaffold_returns_not_implemented_envelope() {
    let (status, body) = call(
        app(),
        Request::post("/kernel-answer").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED);
    assert_envelope(&body, ErrorCode::NotImplemented);
}

#[tokio::test]
async fn scaffolded_assay_bits_route_returns_not_implemented_envelope() {
    let (status, body) = call(
        app(),
        Request::get("/v1/assay/bits").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED);
    assert_envelope(&body, ErrorCode::NotImplemented);
}

#[tokio::test]
async fn unknown_route_returns_not_found_envelope() {
    let (status, body) = call(
        app(),
        Request::get("/v1/does-not-exist")
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_envelope(&body, ErrorCode::NotFound);
}

#[tokio::test]
async fn wrong_method_returns_method_not_allowed_envelope() {
    let (status, body) = call(
        app(),
        Request::delete("/v1/health").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
    assert_envelope(&body, ErrorCode::MethodNotAllowed);
}

#[tokio::test]
async fn read_only_mutating_method_on_data_route_is_405() {
    let (status, body) = call(
        app(),
        Request::delete("/v1/measure").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
    assert_envelope(&body, ErrorCode::MethodNotAllowed);
}

#[tokio::test]
async fn oversized_body_on_gpu_route_returns_413_envelope() {
    // GPU routes cap at MAX_GPU_BODY_BYTES (4 KiB). A 5 KiB body -> 413.
    let big = "x".repeat(5 * 1024);
    let (status, body) = call(
        app(),
        Request::post("/v1/measure").body(Body::from(big)).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
    assert_envelope(&body, ErrorCode::PayloadTooLarge);
}

#[tokio::test]
async fn oversized_body_on_public_search_route_returns_413_envelope() {
    let big = "x".repeat(5 * 1024);
    let (status, body) = call(
        app(),
        Request::post("/search").body(Body::from(big)).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
    assert_envelope(&body, ErrorCode::PayloadTooLarge);
}

#[tokio::test]
async fn within_cap_body_passes_guardrails_to_handler() {
    // A 1 KiB body on /v1/measure is within the cap and reaches the 501 handler.
    let small = "x".repeat(1024);
    let (status, body) = call(
        app(),
        Request::post("/v1/measure")
            .body(Body::from(small))
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED);
    assert_envelope(&body, ErrorCode::NotImplemented);
}

#[tokio::test]
async fn rate_limit_returns_429_envelope_with_retry_after() {
    // Tiny GPU bucket: capacity 1, no refill. 1st passes, 2nd -> 429.
    let limiter = Arc::new(Guardrails::new(
        100.0,
        0.0,
        1.0,
        0.0,
        Duration::from_secs(5),
    ));
    let app = build_app(limiter);

    let r1 = app
        .clone()
        .oneshot(Request::post("/v1/guard").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(
        r1.status(),
        StatusCode::NOT_IMPLEMENTED,
        "first request consumes the token"
    );

    let resp = app
        .oneshot(Request::post("/v1/guard").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::TOO_MANY_REQUESTS);
    assert!(
        resp.headers().get(header::RETRY_AFTER).is_some(),
        "429 carries a Retry-After header"
    );
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let json: Value = serde_json::from_slice(&bytes).unwrap();
    assert_envelope(&json, ErrorCode::RateLimited);
}

#[tokio::test]
async fn slow_handler_times_out_with_504_envelope() {
    // The EXACT production guardrails middleware with a short 100ms timeout over
    // a handler that sleeps 400ms -> 504 (deterministic, fast).
    let limiter = Arc::new(Guardrails::new(
        100.0,
        100.0,
        100.0,
        100.0,
        Duration::from_millis(100),
    ));
    let app = Router::new()
        .route("/slow", get(slow))
        .layer(from_fn_with_state(limiter, guardrails));

    let (status, body) = call(app, Request::get("/slow").body(Body::empty()).unwrap()).await;
    assert_eq!(status, StatusCode::GATEWAY_TIMEOUT);
    assert_envelope(&body, ErrorCode::Timeout);
}

#[tokio::test]
async fn panic_maps_to_internal_500_envelope_and_never_leaks_detail() {
    // The EXACT production panic layer, over a synthetic panicking handler whose
    // payload carries a sentinel that must never reach the response body.
    let app = Router::new()
        .route("/boom", get(boom))
        .layer(panic_catch_layer());

    let (status, body) = call(app, Request::get("/boom").body(Body::empty()).unwrap()).await;

    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    assert_envelope(&body, ErrorCode::Internal);
    let raw = body.to_string();
    assert!(
        !raw.contains(PANIC_SENTINEL),
        "panic sentinel leaked into response body: {raw}"
    );
    assert!(
        !raw.contains("/boom"),
        "panic location leaked into response body: {raw}"
    );
}

#[tokio::test]
async fn error_code_catalog_is_closed_unique_and_complete() {
    let mut seen = std::collections::HashSet::new();
    for code in ErrorCode::ALL {
        let wire = code.code();
        assert!(
            wire.starts_with("CALYX_WEB_API_"),
            "code {wire} must use the prefix"
        );
        assert!(
            seen.insert(wire),
            "duplicate wire code {wire} in the catalog"
        );
        assert!(
            !code.remediation().is_empty(),
            "{wire} missing a remediation"
        );
        assert!(
            !code.default_message().is_empty(),
            "{wire} missing a default message"
        );
        assert!(code.status().is_client_error() || code.status().is_server_error());
    }
    assert_eq!(seen.len(), ErrorCode::ALL.len());
}

// --- #572: MeasureCtx fail-loud config (no mocks, no silent fallback) ---

#[test]
fn measure_ctx_load_fails_loud_on_unopenable_vault() {
    let err = match calyx_web_api::MeasureCtx::load(
        std::path::Path::new("/nonexistent-calyx/01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        "absent",
    ) {
        Ok(_) => panic!("an unopenable vault dir must fail loud, never silently succeed"),
        Err(e) => e,
    };
    assert!(err.contains("vault"), "error must name the failure: {err}");
}

#[test]
fn measure_ctx_load_rejects_non_vault_id_dir_name() {
    let err =
        match calyx_web_api::MeasureCtx::load(std::path::Path::new("/tmp/not-a-vault-id"), "x") {
            Ok(_) => panic!("a dir name that is not a vault id must fail loud"),
            Err(e) => e,
        };
    assert!(err.contains("not a vault id"), "got: {err}");
}

// --- #1906: fail-closed bearer auth (the origin is never anonymous) ---

async fn bearer_ok() -> StatusCode {
    StatusCode::OK
}

/// Build a minimal router behind the REAL `require_bearer` layer with a known
/// secret — exercises the actual middleware, not a stand-in.
fn bearer_app(secret: &str) -> Router {
    let auth = Arc::new(AuthCtx::new(secret).expect("non-empty secret"));
    Router::new()
        .route("/v1/measure", get(bearer_ok))
        .layer(from_fn_with_state(auth, require_bearer))
}

#[tokio::test]
async fn missing_bearer_is_401_envelope_with_www_authenticate() {
    let app = bearer_app("s3cret-FSV");
    let resp = app
        .oneshot(Request::get("/v1/measure").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(
        resp.headers()
            .get(header::WWW_AUTHENTICATE)
            .and_then(|v| v.to_str().ok()),
        Some("Bearer realm=\"calyx-origin\""),
        "401 must carry the Bearer challenge"
    );
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_envelope(&body, ErrorCode::Unauthorized);
}

#[tokio::test]
async fn wrong_bearer_is_401() {
    let app = bearer_app("s3cret-FSV");
    let (status, body) = call(
        app,
        Request::get("/v1/measure")
            .header(header::AUTHORIZATION, "Bearer not-the-secret")
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_envelope(&body, ErrorCode::Unauthorized);
}

#[tokio::test]
async fn valid_bearer_reaches_the_handler() {
    let app = bearer_app("s3cret-FSV");
    let resp = app
        .oneshot(
            Request::get("/v1/measure")
                .header(header::AUTHORIZATION, "Bearer s3cret-FSV")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "valid bearer passes through");
}

#[test]
fn auth_ctx_rejects_empty_secret_loud() {
    assert!(
        AuthCtx::new("   ").is_err(),
        "an empty/blank bearer secret must fail loud, never anonymous"
    );
}

// ---------------------------------------------------------------------------
// #54: GET /provenance/{id} serves real Ledger explainability from Aster CF.
// ---------------------------------------------------------------------------

struct ProvenanceFixture {
    vault_dir: PathBuf,
    answer_id: String,
    kernel_ref: calyx_core::LedgerRef,
    guard_ref: calyx_core::LedgerRef,
    answer_ref: calyx_core::LedgerRef,
}

fn provenance_fixture() -> ProvenanceFixture {
    let root = std::env::temp_dir().join(format!(
        "calyx-web-api-provenance-{}-{}",
        std::process::id(),
        std::thread::current().name().unwrap_or("test")
    ));
    std::fs::remove_dir_all(&root).ok();
    std::fs::create_dir_all(&root).expect("create provenance fixture root");
    let vault_dir = root.join("01KWNP54FSV54FSV54FSV54FSV");
    let vault_id: calyx_core::VaultId = vault_dir
        .file_name()
        .and_then(|value| value.to_str())
        .expect("vault dir name")
        .parse()
        .expect("parse vault id");
    let vault = calyx_aster::vault::AsterVault::new_durable_with_clock(
        &vault_dir,
        vault_id,
        b"issue54-provenance-fsv".to_vec(),
        calyx_aster::vault::VaultOptions::default(),
        calyx_core::FixedClock::new(54_000),
    )
    .expect("create durable vault");

    let kernel_id = "kernel-issue54".as_bytes().to_vec();
    let guard_id = "guard-issue54".as_bytes().to_vec();
    let answer_id = "answer-issue54".to_string();
    let kernel_ref = vault
        .append_ledger_entry(
            calyx_ledger::EntryKind::Kernel,
            calyx_ledger::SubjectId::Kernel(kernel_id.clone()),
            serde_json::to_vec(&serde_json::json!({
                "kernel_id": "kernel-issue54",
                "recall_ratio": 0.99,
                "bits": {"attack": 0.12, "context": 0.08}
            }))
            .unwrap(),
            calyx_ledger::ActorId::Service("calyx-web-api-test".to_string()),
        )
        .expect("append kernel ledger row");
    let guard_ref = vault
        .append_ledger_entry(
            calyx_ledger::EntryKind::Guard,
            calyx_ledger::SubjectId::Guard(guard_id.clone()),
            serde_json::to_vec(&serde_json::json!({
                "guard_id": "guard-issue54",
                "pass": true,
                "tau": 0.8
            }))
            .unwrap(),
            calyx_ledger::ActorId::Service("calyx-web-api-test".to_string()),
        )
        .expect("append guard ledger row");
    let answer_ref = vault
        .append_ledger_entry(
            calyx_ledger::EntryKind::Answer,
            calyx_ledger::SubjectId::Query(answer_id.as_bytes().to_vec()),
            serde_json::to_vec(&serde_json::json!({
                "complete": true,
                "expected_hops": 2,
                "kernel_ref": {"seq": kernel_ref.seq, "hash": hex(&kernel_ref.hash)},
                "guard_ref": {"seq": guard_ref.seq, "hash": hex(&guard_ref.hash)},
                "path": [
                    {
                        "from_id": cx(10).to_string(),
                        "cx_id": cx(11).to_string(),
                        "hop": 0,
                        "score": 0.9,
                        "lens_id": lens(1).to_string(),
                        "ledger_ref": {"seq": kernel_ref.seq}
                    },
                    {
                        "from_id": cx(11).to_string(),
                        "cx_id": cx(12).to_string(),
                        "hop": 1,
                        "score": 0.7,
                        "lens_id": lens(2).to_string(),
                        "ledger_seq": guard_ref.seq
                    }
                ],
                "fusion_weights": {
                    "mode": "weighted_rrf",
                    "k": 2,
                    "candidates": [cx(1).to_string(), cx(2).to_string()],
                    "weights": [{"slot_id": 0, "weight": 1.0}],
                    "single_slot": null
                },
                "freshness_ts": 54000
            }))
            .unwrap(),
            calyx_ledger::ActorId::Service("calyx-web-api-test".to_string()),
        )
        .expect("append answer ledger row");
    vault.flush().expect("flush ledger CF");
    drop(vault);

    ProvenanceFixture {
        vault_dir,
        answer_id,
        kernel_ref,
        guard_ref,
        answer_ref,
    }
}

fn provenance_app(vault_dir: &Path) -> Router {
    let prov = Arc::new(ProvenanceCtx::open(vault_dir).expect("open provenance ctx"));
    build_app_with_provenance(
        Arc::new(Guardrails::new(
            100.0,
            100.0,
            100.0,
            100.0,
            Duration::from_secs(5),
        )),
        prov,
    )
}

#[tokio::test]
async fn public_provenance_returns_ledger_trace_and_cache_headers() {
    let fixture = provenance_fixture();
    let store = calyx_aster::ledger_view::AsterLedgerCfStore::open(&fixture.vault_dir)
        .expect("open physical ledger store");
    let rows = calyx_ledger::LedgerCfStore::scan(&store).expect("scan physical ledger rows");
    assert_eq!(rows.len(), 3, "physical Ledger CF has kernel/guard/answer");
    assert_eq!(
        calyx_ledger::verify_chain(&store, 0..rows.len() as u64).expect("verify physical chain"),
        calyx_ledger::VerifyResult::Intact { count: 3 }
    );

    let app = provenance_app(&fixture.vault_dir);
    let path = format!("/provenance/{}", fixture.answer_id);
    let (status, headers, body) = call_with_headers(
        app.clone(),
        Request::get(&path).body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers.get("x-cache").and_then(|value| value.to_str().ok()),
        Some("MISS")
    );
    assert_eq!(body["id"], fixture.answer_id);
    assert_eq!(body["found"], true);
    assert_eq!(body["trusted"], true);
    assert_eq!(body["complete"], true);
    assert_eq!(
        body["chain"],
        serde_json::json!({"result": "intact", "count": 3})
    );
    assert_eq!(body["entries"].as_array().expect("entries").len(), 3);
    assert_eq!(body["entries"][0]["seq"], fixture.kernel_ref.seq);
    assert_eq!(body["entries"][1]["seq"], fixture.guard_ref.seq);
    assert_eq!(body["entries"][2]["seq"], fixture.answer_ref.seq);
    assert_eq!(body["path"].as_array().expect("path").len(), 2);
    assert_eq!(body["path"][0]["cxId"], cx(11).to_string());
    assert_eq!(body["path"][0]["ledgerSeq"], fixture.kernel_ref.seq);
    assert_eq!(body["path"][1]["ledgerSeq"], fixture.guard_ref.seq);
    assert_eq!(body["fusionWeights"]["mode"], "weighted_rrf");
    assert_eq!(body["guardResult"]["guard_id"], "guard-issue54");
    assert_eq!(body["freshnessTs"], 54000);

    let (status, headers, replay) =
        call_with_headers(app, Request::get(&path).body(Body::empty()).unwrap()).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers.get("x-cache").and_then(|value| value.to_str().ok()),
        Some("HIT")
    );
    assert_eq!(replay, body, "cache hit must replay byte-equivalent JSON");
}

#[tokio::test]
async fn public_provenance_unknown_id_is_found_false() {
    let fixture = provenance_fixture();
    let (status, body) = call(
        provenance_app(&fixture.vault_dir),
        Request::get("/provenance/not-present")
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["found"], false);
    assert_eq!(body["trusted"], false);
    assert_eq!(body["complete"], false);
    assert_eq!(
        body["chain"],
        serde_json::json!({"result": "intact", "count": 3})
    );
    assert_eq!(body["entries"].as_array().expect("entries").len(), 0);
    assert_eq!(body["path"].as_array().expect("path").len(), 0);
}

#[tokio::test]
async fn public_provenance_real_loopback_http_equals_ledger_truth() {
    let fixture = provenance_fixture();
    let app = provenance_app(&fixture.vault_dir);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind provenance HTTP listener");
    let addr = listener.local_addr().expect("local_addr");
    let server = tokio::spawn(async move {
        axum::serve(listener, app)
            .await
            .expect("serve provenance app");
    });

    let request = format!(
        "GET /provenance/{} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n\r\n",
        fixture.answer_id
    );
    let mut stream = tokio::net::TcpStream::connect(addr)
        .await
        .expect("connect provenance HTTP listener");
    {
        use tokio::io::AsyncWriteExt;
        stream
            .write_all(request.as_bytes())
            .await
            .expect("write request");
    }
    let mut response = Vec::new();
    {
        use tokio::io::AsyncReadExt;
        stream
            .read_to_end(&mut response)
            .await
            .expect("read response");
    }
    server.abort();

    let response = String::from_utf8(response).expect("response utf8");
    assert!(
        response.starts_with("HTTP/1.1 200 OK"),
        "response: {response}"
    );
    let (_, json) = response
        .split_once("\r\n\r\n")
        .expect("HTTP response has body separator");
    let served: Value = serde_json::from_str(json).expect("served JSON");
    let store = calyx_aster::ledger_view::AsterLedgerCfStore::open(&fixture.vault_dir)
        .expect("open physical ledger store");
    let rows = calyx_ledger::LedgerCfStore::scan(&store).expect("scan physical ledger rows");
    assert_eq!(served["chain"]["count"], rows.len());
    assert_eq!(
        served["entries"][2]["entryHash"],
        hex(&fixture.answer_ref.hash),
        "HTTP answer entry hash must equal physical Ledger CF readback"
    );
}

#[tokio::test]
async fn public_provenance_wrong_method_is_405() {
    let fixture = provenance_fixture();
    let (status, body) = call(
        provenance_app(&fixture.vault_dir),
        Request::post(format!("/provenance/{}", fixture.answer_id))
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
    assert_envelope(&body, ErrorCode::MethodNotAllowed);
}

fn cx(seed: u8) -> calyx_core::CxId {
    calyx_core::CxId::from_bytes([seed; 16])
}

fn lens(seed: u8) -> calyx_core::LensId {
    calyx_core::LensId::from_bytes([seed; 16])
}

fn hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

// ---------------------------------------------------------------------------
// #57: wired `/search` real HTTP response bytes equal physical vault truth.
// ---------------------------------------------------------------------------

struct SearchFixture {
    root: PathBuf,
    vault_dir: PathBuf,
    vault_id: calyx_core::VaultId,
    vault_name: String,
    cx_ids: Vec<calyx_core::CxId>,
}

fn search_fixture() -> SearchFixture {
    use calyx_aster::vault::{AsterVault, VaultOptions};
    use calyx_core::{
        Asymmetry, Input, Modality, Panel, QuantPolicy, Slot, SlotKey, SlotShape, SlotState,
        VaultStore,
    };
    use calyx_registry::measure::measure_constellation;

    let root = std::env::temp_dir().join(format!(
        "calyx-web-api-search-{}-{}",
        std::process::id(),
        std::thread::current().name().unwrap_or("test")
    ));
    std::fs::remove_dir_all(&root).ok();
    std::fs::create_dir_all(&root).expect("create search fixture root");
    let vault_dir = root.join("01KWNP57FSV57FSV57FSV57FSV");
    let vault_id: calyx_core::VaultId = vault_dir
        .file_name()
        .and_then(|value| value.to_str())
        .expect("vault dir name")
        .parse()
        .expect("parse vault id");
    let vault_name = "issue57-search".to_string();
    let salt = format!("calyx-cli-vault:{vault_id}:{vault_name}").into_bytes();

    let mut registry = calyx_registry::Registry::new();
    let lens = calyx_registry::AlgorithmicLens::byte_features("issue57-byte", Modality::Text);
    let contract = lens.contract().clone();
    let lens_id = contract.lens_id();
    let spec = calyx_registry::LensSpec {
        name: "issue57-byte".to_string(),
        runtime: calyx_registry::LensRuntime::Algorithmic {
            kind: "byte-features".to_string(),
        },
        output: contract.shape(),
        modality: contract.modality(),
        weights_sha256: contract.weights_sha256(),
        corpus_hash: contract.corpus_hash(),
        norm_policy: contract.norm_policy(),
        max_batch: None,
        axis: Some("issue57-byte".to_string()),
        asymmetry: Asymmetry::None,
        quant_default: QuantPolicy::turboquant_default(),
        truncate_dim: None,
        recall_delta: calyx_registry::spec::default_recall_delta(),
        retrieval_only: false,
        excluded_from_dedup: false,
    };
    registry
        .register_frozen_with_spec(lens, contract, spec)
        .expect("register fixture lens");
    let slot_id = calyx_core::SlotId::new(0);
    let panel = Panel {
        version: 1,
        slots: vec![Slot {
            slot_id,
            slot_key: SlotKey::new(slot_id, "issue57-byte"),
            lens_id,
            shape: SlotShape::Dense(16),
            modality: Modality::Text,
            asymmetry: Asymmetry::None,
            quant: QuantPolicy::None,
            resource: Default::default(),
            axis: Some("issue57-byte".to_string()),
            retrieval_only: false,
            excluded_from_dedup: false,
            bits_about: Default::default(),
            state: SlotState::Active,
            added_at_panel_version: 1,
        }],
        created_at: 1,
        kernel_ref: None,
        guard_ref: None,
    };
    let vault = AsterVault::new_durable(
        &vault_dir,
        vault_id,
        salt,
        VaultOptions {
            panel: Some(panel.clone()),
            ..VaultOptions::default()
        },
    )
    .expect("create durable search vault");
    calyx_registry::persist_vault_panel_state(&vault_dir, &panel, &registry)
        .expect("persist panel");
    let state = calyx_registry::VaultPanelState {
        panel,
        registry,
        registry_snapshot: None,
    };
    let mut cx_ids = Vec::new();
    for input in ["alpha", "alphabet", "beta"] {
        let measured = measure_constellation(
            &vault,
            &state,
            Input::new(Modality::Text, input.as_bytes().to_vec()),
            57_000,
        )
        .expect("measure fixture row");
        let cx_id = measured.cx_id;
        vault.put(measured).expect("put fixture constellation");
        cx_ids.push(cx_id);
    }
    vault.flush().expect("flush search fixture vault");
    calyx_search::rebuild_for_vault(&vault_dir, &vault).expect("rebuild persisted search index");
    drop(vault);

    SearchFixture {
        root,
        vault_dir,
        vault_id,
        vault_name,
        cx_ids,
    }
}

fn search_app(fixture: &SearchFixture) -> Router {
    let measure = Arc::new(
        calyx_web_api::MeasureCtx::load(&fixture.vault_dir, &fixture.vault_name)
            .expect("load search measure ctx"),
    );
    let auth = Arc::new(AuthCtx::new("search-secret-FSV").expect("secret"));
    build_app_with_search(
        Arc::new(Guardrails::new(
            100.0,
            100.0,
            100.0,
            100.0,
            Duration::from_secs(5),
        )),
        measure,
        auth,
    )
}

fn open_search_fixture_vault(fixture: &SearchFixture) -> calyx_aster::vault::AsterVault {
    calyx_aster::vault::AsterVault::open(
        &fixture.vault_dir,
        fixture.vault_id,
        format!(
            "calyx-cli-vault:{}:{}",
            fixture.vault_id, fixture.vault_name
        )
        .into_bytes(),
        calyx_aster::vault::VaultOptions::default(),
    )
    .expect("open search fixture vault")
}

#[tokio::test]
async fn public_search_real_loopback_http_equals_vault_search_truth() {
    use calyx_aster::cf::ColumnFamily;
    use calyx_core::VaultStore;

    let fixture = search_fixture();
    let app = search_app(&fixture);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind search HTTP listener");
    let addr = listener.local_addr().expect("local_addr");
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.expect("serve search app");
    });

    let body = r#"{"query":"alpha","k":2,"fusion":"rrf"}"#;
    let request = format!(
        "POST /search HTTP/1.1\r\nHost: {addr}\r\nAuthorization: Bearer search-secret-FSV\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let mut stream = tokio::net::TcpStream::connect(addr)
        .await
        .expect("connect search HTTP listener");
    {
        use tokio::io::AsyncWriteExt;
        stream
            .write_all(request.as_bytes())
            .await
            .expect("write search request");
    }
    let mut response = Vec::new();
    {
        use tokio::io::AsyncReadExt;
        stream
            .read_to_end(&mut response)
            .await
            .expect("read search response");
    }
    server.abort();

    let response = String::from_utf8(response).expect("response utf8");
    assert!(
        response.starts_with("HTTP/1.1 200 OK"),
        "response: {response}"
    );
    assert!(
        response.contains("x-cache: MISS"),
        "first real HTTP search must be a cache miss: {response}"
    );
    let (_, json) = response
        .split_once("\r\n\r\n")
        .expect("HTTP response has body separator");
    let served: Value = serde_json::from_str(json).expect("served JSON");

    let vault = open_search_fixture_vault(&fixture);
    let state =
        calyx_registry::load_vault_panel_state(&fixture.vault_dir).expect("load persisted panel");
    let base_rows = vault
        .scan_cf_at(vault.snapshot(), ColumnFamily::Base)
        .expect("scan physical Base CF");
    assert_eq!(
        base_rows.len(),
        fixture.cx_ids.len(),
        "physical Base CF row count is the source-of-truth corpus size"
    );
    let outcome = calyx_search::search_outcome(
        &vault,
        &state,
        &fixture.vault_dir,
        "alpha",
        2,
        calyx_search::FusionChoice::Rrf,
        calyx_search::GuardChoice::Off,
        None,
        false,
    )
    .expect("direct search truth");
    let ledger_store = calyx_aster::ledger_view::AsterLedgerCfStore::open(&fixture.vault_dir)
        .expect("open physical Ledger CF");
    let ledger_rows =
        calyx_ledger::LedgerCfStore::scan(&ledger_store).expect("scan physical Ledger CF");
    let expected_hits: Vec<Value> = outcome
        .hits
        .iter()
        .map(|hit| {
            serde_json::json!({
                "rank": hit.rank,
                "cxId": hit.cx_id.to_string(),
                "score": hit.score,
                "provenance": {
                    "ledgerSeq": hit.provenance.seq,
                    "chainHash": hex(&hit.provenance.hash),
                },
            })
        })
        .collect();
    let expected = serde_json::json!({
        "query": "alpha",
        "k": 2,
        "guardTau": outcome.guard_tau,
        "hits": expected_hits,
    });
    let expected_bytes = serde_json::to_vec(&expected).expect("serialize expected truth");
    assert_eq!(
        json.as_bytes(),
        expected_bytes.as_slice(),
        "raw HTTP /search response body bytes must equal direct source-of-truth serialization"
    );
    assert_eq!(
        served, expected,
        "real HTTP /search JSON must equal direct calyx_search source of truth"
    );
    for hit in &outcome.hits {
        assert!(
            ledger_rows
                .iter()
                .any(|row| calyx_ledger::decode(&row.bytes)
                    .map(|entry| {
                        row.seq == hit.provenance.seq && entry.entry_hash == hit.provenance.hash
                    })
                    .unwrap_or(false)),
            "hit {} provenance seq/hash must exist in physical Ledger CF",
            hit.cx_id
        );
    }

    let (slot, query) = calyx_search::measure_query_vectors(&state, "alpha")
        .expect("measure query vectors")
        .into_iter()
        .next()
        .expect("query vector");
    let index_candidates = calyx_search::PersistedSearchIndexes::open(&fixture.vault_dir)
        .expect("open persisted indexes")
        .search(slot, &query, 2)
        .expect("search persisted index");
    let served_first = served["hits"][0]["cxId"].as_str().expect("served hit id");
    assert!(
        index_candidates
            .iter()
            .any(|hit| hit.cx_id.to_string() == served_first),
        "served first hit {served_first} must come from persisted index readback"
    );

    std::fs::remove_dir_all(&fixture.root).ok();
}

// ---------------------------------------------------------------------------
// #50: POST /predict/match serves real Soccer Lab Oracle export records.
// ---------------------------------------------------------------------------

fn soccer_prediction_export() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../docs/data/soccer_lab_prediction_export.json")
}

fn prediction_app() -> Router {
    let predict =
        Arc::new(PredictionCtx::load(&soccer_prediction_export()).expect("load prediction export"));
    assert_eq!(
        predict.match_count(),
        16,
        "real export has 16 match records"
    );
    assert_eq!(
        predict.progression_count(),
        144,
        "real export has 144 tournament progression records"
    );
    assert_eq!(
        predict.player_count(),
        1248,
        "real export has 1248 player impact records"
    );
    let auth = Arc::new(AuthCtx::new("predict-secret-FSV").expect("secret"));
    build_app_with_predictions(
        Arc::new(Guardrails::new(
            100.0,
            100.0,
            100.0,
            100.0,
            Duration::from_secs(5),
        )),
        predict,
        auth,
    )
}

fn match_truth(match_id: &str) -> Value {
    let export: Value =
        serde_json::from_slice(&std::fs::read(soccer_prediction_export()).expect("read export"))
            .expect("parse export");
    export["records"]
        .as_array()
        .expect("records array")
        .iter()
        .find(|record| record.pointer("/input/entity_id").and_then(Value::as_str) == Some(match_id))
        .expect("match record present")
        .clone()
}

fn progression_truth(version: &str, team: &str, axis: &str) -> Value {
    let export: Value =
        serde_json::from_slice(&std::fs::read(soccer_prediction_export()).expect("read export"))
            .expect("parse export");
    export["records"]
        .as_array()
        .expect("records array")
        .iter()
        .find(|record| {
            record.get("record_type").and_then(Value::as_str) == Some("tournament_progression")
                && record
                    .pointer("/input/attributes/version")
                    .and_then(Value::as_str)
                    == Some(version)
                && record
                    .pointer("/input/attributes/team")
                    .and_then(Value::as_str)
                    == Some(team)
                && record
                    .pointer("/input/attributes/axis")
                    .and_then(Value::as_str)
                    == Some(axis)
        })
        .expect("progression record present")
        .clone()
}

fn player_truth(player_id: &str) -> Value {
    let export: Value =
        serde_json::from_slice(&std::fs::read(soccer_prediction_export()).expect("read export"))
            .expect("parse export");
    export["records"]
        .as_array()
        .expect("records array")
        .iter()
        .find(|record| {
            record.get("record_type").and_then(Value::as_str) == Some("player_impact")
                && record.pointer("/input/entity_id").and_then(Value::as_str) == Some(player_id)
        })
        .expect("player record present")
        .clone()
}

fn predict_req(body: &'static str) -> Request<Body> {
    Request::post("/predict/match")
        .header(header::AUTHORIZATION, "Bearer predict-secret-FSV")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body))
        .unwrap()
}

fn progression_req(body: &'static str) -> Request<Body> {
    Request::post("/predict/progression")
        .header(header::AUTHORIZATION, "Bearer predict-secret-FSV")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body))
        .unwrap()
}

fn player_req(body: &'static str) -> Request<Body> {
    Request::post("/predict/player")
        .header(header::AUTHORIZATION, "Bearer predict-secret-FSV")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body))
        .unwrap()
}

#[tokio::test]
async fn predict_match_returns_exact_export_record_and_cache_headers() {
    let app = prediction_app();
    let truth = match_truth("WC-2026-M089");
    let (status, headers, body) =
        call_with_headers(app.clone(), predict_req(r#"{"matchId":"WC-2026-M089"}"#)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers.get("x-cache").and_then(|v| v.to_str().ok()),
        Some("MISS")
    );
    assert_eq!(
        body, truth,
        "HTTP response must equal export source of truth"
    );

    let (status, headers, body) =
        call_with_headers(app, predict_req(r#"{"matchId":"WC-2026-M089"}"#)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers.get("x-cache").and_then(|v| v.to_str().ok()),
        Some("HIT")
    );
    assert_eq!(body, truth, "cache hit must replay the same record");
}

#[tokio::test]
async fn predict_match_real_loopback_http_equals_export_truth() {
    let app = prediction_app();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind prediction HTTP listener");
    let addr = listener.local_addr().expect("local_addr");
    let server = tokio::spawn(async move {
        axum::serve(listener, app)
            .await
            .expect("serve prediction app");
    });

    let body = r#"{"matchId":"WC-2026-M090"}"#;
    let request = format!(
        "POST /predict/match HTTP/1.1\r\nHost: {addr}\r\nAuthorization: Bearer predict-secret-FSV\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let mut stream = tokio::net::TcpStream::connect(addr)
        .await
        .expect("connect prediction HTTP listener");
    {
        use tokio::io::AsyncWriteExt;
        stream
            .write_all(request.as_bytes())
            .await
            .expect("write request");
    }
    let mut response = Vec::new();
    {
        use tokio::io::AsyncReadExt;
        stream
            .read_to_end(&mut response)
            .await
            .expect("read response");
    }
    server.abort();

    let response = String::from_utf8(response).expect("response utf8");
    assert!(
        response.starts_with("HTTP/1.1 200 OK"),
        "response: {response}"
    );
    let (_, json) = response
        .split_once("\r\n\r\n")
        .expect("HTTP response has body separator");
    let served: Value = serde_json::from_str(json).expect("served JSON");
    assert_eq!(
        served,
        match_truth("WC-2026-M090"),
        "real HTTP response must equal export source of truth"
    );
}

#[tokio::test]
async fn predict_match_requires_bearer() {
    let (status, body) = call(
        prediction_app(),
        Request::post("/predict/match")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(r#"{"matchId":"WC-2026-M089"}"#))
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_envelope(&body, ErrorCode::Unauthorized);
}

#[tokio::test]
async fn predict_match_unknown_id_is_404_envelope() {
    let (status, body) = call(
        prediction_app(),
        predict_req(r#"{"matchId":"WC-2026-M999"}"#),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_envelope(&body, ErrorCode::NotFound);
}

#[tokio::test]
async fn predict_match_rejects_malformed_json() {
    let (status, body) = call(
        prediction_app(),
        predict_req(r#"{"matchId":"WC-2026-M089""#),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_envelope(&body, ErrorCode::BadRequest);
}

#[tokio::test]
async fn predict_match_rejects_empty_match_id() {
    let (status, body) = call(prediction_app(), predict_req(r#"{"matchId":"   "}"#)).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_envelope(&body, ErrorCode::BadRequest);
}

#[tokio::test]
async fn predict_match_rejects_unknown_fields() {
    let (status, body) = call(
        prediction_app(),
        predict_req(r#"{"matchId":"WC-2026-M089","extra":true}"#),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_envelope(&body, ErrorCode::BadRequest);
}

#[tokio::test]
async fn predict_progression_returns_exact_export_record_and_cache_headers() {
    let app = prediction_app();
    let truth = progression_truth("2026", "France", "winner");
    let (status, headers, body) = call_with_headers(
        app.clone(),
        progression_req(r#"{"version":"2026","team":"France","axis":"winner"}"#),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers.get("x-cache").and_then(|v| v.to_str().ok()),
        Some("MISS")
    );
    assert_eq!(
        body, truth,
        "HTTP response must equal progression export source of truth"
    );

    let (status, headers, body) = call_with_headers(
        app,
        progression_req(r#"{"version":"2026","team":"France","axis":"winner"}"#),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers.get("x-cache").and_then(|v| v.to_str().ok()),
        Some("HIT")
    );
    assert_eq!(body, truth, "cache hit must replay the same record");
}

#[tokio::test]
async fn predict_progression_real_loopback_http_equals_export_truth() {
    let app = prediction_app();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind progression HTTP listener");
    let addr = listener.local_addr().expect("local_addr");
    let server = tokio::spawn(async move {
        axum::serve(listener, app)
            .await
            .expect("serve prediction app");
    });

    let body = r#"{"version":"2026","team":"Canada","axis":"finalist"}"#;
    let request = format!(
        "POST /predict/progression HTTP/1.1\r\nHost: {addr}\r\nAuthorization: Bearer predict-secret-FSV\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let mut stream = tokio::net::TcpStream::connect(addr)
        .await
        .expect("connect progression HTTP listener");
    {
        use tokio::io::AsyncWriteExt;
        stream
            .write_all(request.as_bytes())
            .await
            .expect("write request");
    }
    let mut response = Vec::new();
    {
        use tokio::io::AsyncReadExt;
        stream
            .read_to_end(&mut response)
            .await
            .expect("read response");
    }
    server.abort();

    let response = String::from_utf8(response).expect("response utf8");
    assert!(
        response.starts_with("HTTP/1.1 200 OK"),
        "response: {response}"
    );
    let (_, json) = response
        .split_once("\r\n\r\n")
        .expect("HTTP response has body separator");
    let served: Value = serde_json::from_str(json).expect("served JSON");
    assert_eq!(
        served,
        progression_truth("2026", "Canada", "finalist"),
        "real HTTP response must equal progression export source of truth"
    );
}

#[tokio::test]
async fn predict_progression_requires_bearer() {
    let (status, body) = call(
        prediction_app(),
        Request::post("/predict/progression")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(
                r#"{"version":"2026","team":"France","axis":"winner"}"#,
            ))
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_envelope(&body, ErrorCode::Unauthorized);
}

#[tokio::test]
async fn predict_progression_unknown_key_is_404_envelope() {
    let (status, body) = call(
        prediction_app(),
        progression_req(r#"{"version":"2026","team":"Atlantis","axis":"winner"}"#),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_envelope(&body, ErrorCode::NotFound);
}

#[tokio::test]
async fn predict_progression_rejects_malformed_json() {
    let (status, body) = call(
        prediction_app(),
        progression_req(r#"{"version":"2026","team":"France","axis":"winner""#),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_envelope(&body, ErrorCode::BadRequest);
}

#[tokio::test]
async fn predict_progression_rejects_empty_team() {
    let (status, body) = call(
        prediction_app(),
        progression_req(r#"{"version":"2026","team":" ","axis":"winner"}"#),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_envelope(&body, ErrorCode::BadRequest);
}

#[tokio::test]
async fn predict_progression_rejects_unknown_axis() {
    let (status, body) = call(
        prediction_app(),
        progression_req(r#"{"version":"2026","team":"France","axis":"quarter_finalist"}"#),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_envelope(&body, ErrorCode::BadRequest);
}

#[tokio::test]
async fn predict_progression_rejects_unknown_fields() {
    let (status, body) = call(
        prediction_app(),
        progression_req(r#"{"version":"2026","team":"France","axis":"winner","extra":true}"#),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_envelope(&body, ErrorCode::BadRequest);
}

#[tokio::test]
async fn predict_player_returns_exact_export_record_and_cache_headers() {
    let app = prediction_app();
    let truth = player_truth("1");
    let (status, headers, body) =
        call_with_headers(app.clone(), player_req(r#"{"playerId":"1"}"#)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers.get("x-cache").and_then(|v| v.to_str().ok()),
        Some("MISS")
    );
    assert_eq!(
        body, truth,
        "HTTP response must equal player export source of truth"
    );

    let (status, headers, body) = call_with_headers(app, player_req(r#"{"playerId":"1"}"#)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers.get("x-cache").and_then(|v| v.to_str().ok()),
        Some("HIT")
    );
    assert_eq!(body, truth, "cache hit must replay the same record");
}

#[tokio::test]
async fn predict_player_real_loopback_http_equals_export_truth() {
    let app = prediction_app();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind player HTTP listener");
    let addr = listener.local_addr().expect("local_addr");
    let server = tokio::spawn(async move {
        axum::serve(listener, app)
            .await
            .expect("serve prediction app");
    });

    let body = r#"{"playerId":"59"}"#;
    let request = format!(
        "POST /predict/player HTTP/1.1\r\nHost: {addr}\r\nAuthorization: Bearer predict-secret-FSV\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let mut stream = tokio::net::TcpStream::connect(addr)
        .await
        .expect("connect player HTTP listener");
    {
        use tokio::io::AsyncWriteExt;
        stream
            .write_all(request.as_bytes())
            .await
            .expect("write request");
    }
    let mut response = Vec::new();
    {
        use tokio::io::AsyncReadExt;
        stream
            .read_to_end(&mut response)
            .await
            .expect("read response");
    }
    server.abort();

    let response = String::from_utf8(response).expect("response utf8");
    assert!(
        response.starts_with("HTTP/1.1 200 OK"),
        "response: {response}"
    );
    let (_, json) = response
        .split_once("\r\n\r\n")
        .expect("HTTP response has body separator");
    let served: Value = serde_json::from_str(json).expect("served JSON");
    assert_eq!(
        served,
        player_truth("59"),
        "real HTTP response must equal player export source of truth"
    );
}

#[tokio::test]
async fn predict_player_requires_bearer() {
    let (status, body) = call(
        prediction_app(),
        Request::post("/predict/player")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(r#"{"playerId":"1"}"#))
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_envelope(&body, ErrorCode::Unauthorized);
}

#[tokio::test]
async fn predict_player_unknown_id_is_404_envelope() {
    let (status, body) = call(prediction_app(), player_req(r#"{"playerId":"999999"}"#)).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_envelope(&body, ErrorCode::NotFound);
}

#[tokio::test]
async fn predict_player_rejects_malformed_json() {
    let (status, body) = call(prediction_app(), player_req(r#"{"playerId":"1""#)).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_envelope(&body, ErrorCode::BadRequest);
}

#[tokio::test]
async fn predict_player_rejects_empty_player_id() {
    let (status, body) = call(prediction_app(), player_req(r#"{"playerId":"   "}"#)).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_envelope(&body, ErrorCode::BadRequest);
}

#[tokio::test]
async fn predict_player_rejects_unknown_fields() {
    let (status, body) = call(
        prediction_app(),
        player_req(r#"{"playerId":"1","extra":true}"#),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_envelope(&body, ErrorCode::BadRequest);
}

// ---------------------------------------------------------------------------
// #1908: HHEM faithfulness liveness probe aggregated into /v1/health.
//
// FSV with REAL loopback sockets (no mocks): each test binds a synthetic TCP
// listener exhibiting one HHEM failure/success mode and asserts the probe's
// verdict. Known synthetic input -> known expected output. Covers the happy
// path (live, even a 401), and three edges: silent hang (timeout), non-HTTP
// bytes, and connection refused (truly down). The socket-activation rationale
// (#1807) is exactly why a bare TCP connect is INSUFFICIENT and we read a
// status line instead.
// ---------------------------------------------------------------------------

use calyx_web_api::probe_hhem_faithfulness_at;

/// Bind a loopback listener, run `behavior` on the first accepted connection in
/// a background task, and return the bound address string for the probe.
async fn spawn_synthetic_hhem<F, Fut>(behavior: F) -> String
where
    F: FnOnce(tokio::net::TcpStream) -> Fut + Send + 'static,
    Fut: std::future::Future<Output = ()> + Send,
{
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind synthetic HHEM listener");
    let addr = listener.local_addr().expect("local_addr").to_string();
    tokio::spawn(async move {
        if let Ok((stream, _)) = listener.accept().await {
            behavior(stream).await;
        }
    });
    addr
}

#[tokio::test]
async fn hhem_probe_ok_when_server_speaks_http_even_401() {
    // A live-but-unauthorized HHEM still proves the process is SERVING.
    let addr = spawn_synthetic_hhem(|mut stream| async move {
        use tokio::io::AsyncWriteExt;
        let _ = stream
            .write_all(b"HTTP/1.0 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
            .await;
        let _ = stream.shutdown().await;
    })
    .await;
    assert_eq!(probe_hhem_faithfulness_at(&addr).await, "ok");
}

#[tokio::test]
async fn hhem_probe_degraded_on_silent_hang_timeout() {
    // Accepts the connection (socket-activation would do this) but NEVER writes
    // a response: a bare TCP connect would falsely report "up"; the HTTP read
    // must time out -> degraded.
    let addr = spawn_synthetic_hhem(|stream| async move {
        // Hold the connection open well past HHEM_PROBE_TIMEOUT (1500ms).
        tokio::time::sleep(Duration::from_secs(10)).await;
        drop(stream);
    })
    .await;
    assert_eq!(probe_hhem_faithfulness_at(&addr).await, "degraded");
}

#[tokio::test]
async fn hhem_probe_degraded_on_non_http_bytes() {
    let addr = spawn_synthetic_hhem(|mut stream| async move {
        use tokio::io::AsyncWriteExt;
        let _ = stream.write_all(b"GARBAGE-NOT-HTTP\r\n").await;
        let _ = stream.shutdown().await;
    })
    .await;
    assert_eq!(probe_hhem_faithfulness_at(&addr).await, "degraded");
}

#[tokio::test]
async fn hhem_probe_degraded_when_connection_refused() {
    // Reserve a port, then drop the listener so nothing is listening: connect
    // is refused -> degraded (the genuinely-down case).
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let addr = listener.local_addr().expect("local_addr").to_string();
    drop(listener);
    assert_eq!(probe_hhem_faithfulness_at(&addr).await, "degraded");
}
