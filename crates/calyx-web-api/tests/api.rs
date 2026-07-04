//! Integration FSV for the calyx-web-api error envelope + resource guardrails.
//!
//! No mocks: every test drives the REAL `app()`/`build_app()` router (or the
//! REAL `guardrails`/`panic_catch_layer` middleware) in-process via
//! `tower::ServiceExt::oneshot` and inspects the actual response status +
//! JSON body + headers. Synthetic inputs with known expected outputs (an
//! oversized body, a tiny rate-limit bucket, a deliberately-slow handler, a
//! deliberately-panicking handler whose payload carries a sentinel that MUST
//! NOT appear in the response).

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
    AuthCtx, ErrorCode, Guardrails, PredictionCtx, app, build_app, build_app_with_predictions,
    guardrails, panic_catch_layer, require_bearer,
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
