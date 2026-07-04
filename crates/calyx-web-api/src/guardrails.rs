use super::*;

/// Is this one of the GPU-backed (calyxd) routes that gets the tighter body cap
/// and rate-limit bucket?
fn is_gpu_route(path: &str) -> bool {
    matches!(
        path,
        "/v1/measure" | "/v1/search" | "/search" | "/v1/guard" | "/kernel-answer"
    )
}

/// A simple global token-bucket per route. "Global" (not per-IP) is the correct
/// key here: the service is loopback-only behind a reverse proxy, so every
/// request shares one peer — the bucket protects the single GPU from pile-up,
/// not individual clients (that is the proxy/WAF's job). Refill is wall-clock
/// based via a monotonic [`Instant`].
struct Bucket {
    tokens: f64,
    last: Instant,
}

/// Per-request resource limits: a route-keyed token-bucket rate limiter (GPU
/// routes get a tighter bucket) plus the request timeout. Carried as shared
/// state so tests can inject a tiny limit / short timeout deterministically.
pub struct Guardrails {
    capacity: f64,
    refill_per_sec: f64,
    gpu_capacity: f64,
    gpu_refill_per_sec: f64,
    timeout: Duration,
    buckets: Mutex<HashMap<String, Bucket>>,
}

impl Guardrails {
    /// Construct explicit guardrails (used by tests to force a tiny limit /
    /// short timeout).
    pub fn new(
        capacity: f64,
        refill_per_sec: f64,
        gpu_capacity: f64,
        gpu_refill_per_sec: f64,
        timeout: Duration,
    ) -> Self {
        Self {
            capacity,
            refill_per_sec,
            gpu_capacity,
            gpu_refill_per_sec,
            timeout,
            buckets: Mutex::new(HashMap::new()),
        }
    }

    /// Production limits: generous on light read routes, tight on GPU routes,
    /// with the standard [`REQUEST_TIMEOUT`]. All values can be overridden via
    /// env; invalid present values fail loud instead of silently defaulting.
    pub fn production() -> Self {
        Self::from_env()
            .unwrap_or_else(|error| panic!("CALYX_WEB_API_GUARDRAILS_CONFIG_FAILED: {error}"))
    }

    /// Build production guardrails from optional env vars. Defaults preserve the
    /// original production posture:
    ///
    /// - `CALYX_WEB_API_RATE_CAPACITY=60`
    /// - `CALYX_WEB_API_RATE_REFILL_PER_SEC=30`
    /// - `CALYX_WEB_API_GPU_RATE_CAPACITY=8`
    /// - `CALYX_WEB_API_GPU_RATE_REFILL_PER_SEC=2`
    /// - `CALYX_WEB_API_REQUEST_TIMEOUT_MS=5000`
    pub fn from_env() -> Result<Self, String> {
        let capacity = parse_env_f64("CALYX_WEB_API_RATE_CAPACITY", 60.0)?;
        let refill_per_sec = parse_env_f64("CALYX_WEB_API_RATE_REFILL_PER_SEC", 30.0)?;
        let gpu_capacity = parse_env_f64("CALYX_WEB_API_GPU_RATE_CAPACITY", 8.0)?;
        let gpu_refill_per_sec = parse_env_f64("CALYX_WEB_API_GPU_RATE_REFILL_PER_SEC", 2.0)?;
        let timeout = parse_env_duration_ms(
            "CALYX_WEB_API_REQUEST_TIMEOUT_MS",
            REQUEST_TIMEOUT.as_millis() as u64,
        )?;
        Ok(Self::new(
            capacity,
            refill_per_sec,
            gpu_capacity,
            gpu_refill_per_sec,
            timeout,
        ))
    }

    /// Take one token for `path`. Returns `true` if allowed, `false` if the
    /// bucket is empty (rate-limited).
    fn allow(&self, path: &str) -> bool {
        let (cap, refill) = if is_gpu_route(path) {
            (self.gpu_capacity, self.gpu_refill_per_sec)
        } else {
            (self.capacity, self.refill_per_sec)
        };
        let now = Instant::now();
        let mut buckets = self.buckets.lock().expect("rate-limiter mutex poisoned");
        let bucket = buckets.entry(path.to_owned()).or_insert(Bucket {
            tokens: cap,
            last: now,
        });
        let elapsed = now.duration_since(bucket.last).as_secs_f64();
        bucket.tokens = (bucket.tokens + elapsed * refill).min(cap);
        bucket.last = now;
        if bucket.tokens >= 1.0 {
            bucket.tokens -= 1.0;
            true
        } else {
            false
        }
    }
}

/// Parse a non-negative finite float env var, returning `default` when unset and
/// a LOUD error when present-but-invalid (never silently defaulted).
fn parse_env_f64(name: &str, default: f64) -> Result<f64, String> {
    match std::env::var(name) {
        Err(_) => Ok(default),
        Ok(raw) => {
            let value = raw.trim().parse::<f64>().map_err(|error| {
                format!("{name} must be a non-negative finite number ({error}); got {raw:?}")
            })?;
            if value.is_finite() && value >= 0.0 {
                Ok(value)
            } else {
                Err(format!(
                    "{name} must be a non-negative finite number; got {raw:?}"
                ))
            }
        }
    }
}

/// Parse a positive integer millisecond env var into a [`Duration`].
fn parse_env_duration_ms(name: &str, default: u64) -> Result<Duration, String> {
    match std::env::var(name) {
        Err(_) => Ok(Duration::from_millis(default)),
        Ok(raw) => {
            let value = raw.trim().parse::<u64>().map_err(|error| {
                format!(
                    "{name} must be a positive integer milliseconds value ({error}); got {raw:?}"
                )
            })?;
            if value == 0 {
                return Err(format!(
                    "{name} must be a positive integer milliseconds value; got {raw:?}"
                ));
            }
            Ok(Duration::from_millis(value))
        }
    }
}

/// Build the application with the production guardrails.
pub fn app() -> Router {
    build_app(Arc::new(Guardrails::production()))
}

/// Build the application with explicit guardrails (testable injection). Wires
/// the route surface, the enveloped 404 + 405 fallbacks, the resource
/// [`guardrails`] (body cap + rate limit + timeout), and the panic-catch layer.
pub fn build_app(limiter: Arc<Guardrails>) -> Router {
    routes()
        .fallback(fallback_404)
        .method_not_allowed_fallback(fallback_405)
        .layer(middleware::from_fn_with_state(limiter, guardrails))
        .layer(panic_catch_layer())
}

/// The read-only route surface fully scaffolded (`measure` + `provenance` to
/// 501). The wired variants are [`build_app_with_provenance`] and
/// [`build_app_with_measure_and_provenance`].
fn routes() -> Router {
    routes_base()
        .route("/v1/health", get(health))
        .route("/v1/measure", post(not_implemented))
        .route("/v1/search", post(not_implemented))
        .route("/search", post(not_implemented))
        .route("/kernel-answer", post(not_implemented))
        .route("/v1/guard", post(not_implemented))
        .route("/v1/kernel", get(not_implemented))
        .route("/v1/assay/bits", get(not_implemented))
        .route("/v1/provenance/{id}", get(provenance_stub))
        .route("/provenance/{id}", get(provenance_stub))
}

/// The shared base with NO route attached — every endpoint (including the now
/// stateful `/v1/health`) is added by the scaffolded or wired builder, so each
/// can choose its handler without a route-overlap panic. The deployed origin
/// uses the stateful [`health_full`] (real gpu/vault/panelVersion for the edge
/// circuit breaker); the scaffold uses the stateless [`health`].
pub(super) fn routes_base() -> Router {
    Router::new()
}

/// Per-request resource guardrails. Order: rate limit (cheapest reject) → body
/// cap (route-aware) → timeout around the handler.
pub async fn guardrails(
    State(limiter): State<Arc<Guardrails>>,
    req: Request,
    next: Next,
) -> Response {
    let path = req.uri().path().to_owned();

    if !limiter.allow(&path) {
        let mut resp = ApiError::new(
            ErrorCode::RateLimited,
            format!("rate limit exceeded for {path}"),
        )
        .into_response();
        resp.headers_mut()
            .insert(header::RETRY_AFTER, header::HeaderValue::from_static("1"));
        return resp;
    }

    let cap = if is_gpu_route(&path) {
        MAX_GPU_BODY_BYTES
    } else {
        MAX_BODY_BYTES
    };
    let (parts, body) = req.into_parts();
    let bytes = match axum::body::to_bytes(body, cap).await {
        Ok(bytes) => bytes,
        Err(_) => {
            return ApiError::new(
                ErrorCode::PayloadTooLarge,
                format!("request body exceeds the {cap}-byte limit for {path}"),
            )
            .into_response();
        }
    };
    let req = Request::from_parts(parts, Body::from(bytes));

    match tokio::time::timeout(limiter.timeout, next.run(req)).await {
        Ok(resp) => resp,
        Err(_elapsed) => {
            tracing::warn!(
                "CALYX_WEB_API_TIMEOUT: request to {path} exceeded {:?}",
                limiter.timeout
            );
            ApiError::of(ErrorCode::Timeout).into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn guardrails_from_env_reads_overrides() {
        let _guard = ENV_LOCK.lock().unwrap();
        for name in [
            "CALYX_WEB_API_RATE_CAPACITY",
            "CALYX_WEB_API_RATE_REFILL_PER_SEC",
            "CALYX_WEB_API_GPU_RATE_CAPACITY",
            "CALYX_WEB_API_GPU_RATE_REFILL_PER_SEC",
            "CALYX_WEB_API_REQUEST_TIMEOUT_MS",
        ] {
            unsafe { std::env::remove_var(name) };
        }

        unsafe {
            std::env::set_var("CALYX_WEB_API_RATE_CAPACITY", "11.5");
            std::env::set_var("CALYX_WEB_API_RATE_REFILL_PER_SEC", "3.25");
            std::env::set_var("CALYX_WEB_API_GPU_RATE_CAPACITY", "2");
            std::env::set_var("CALYX_WEB_API_GPU_RATE_REFILL_PER_SEC", "0.5");
            std::env::set_var("CALYX_WEB_API_REQUEST_TIMEOUT_MS", "250");
        }
        let guardrails = Guardrails::from_env().expect("valid env");
        for name in [
            "CALYX_WEB_API_RATE_CAPACITY",
            "CALYX_WEB_API_RATE_REFILL_PER_SEC",
            "CALYX_WEB_API_GPU_RATE_CAPACITY",
            "CALYX_WEB_API_GPU_RATE_REFILL_PER_SEC",
            "CALYX_WEB_API_REQUEST_TIMEOUT_MS",
        ] {
            unsafe { std::env::remove_var(name) };
        }

        assert_eq!(guardrails.capacity, 11.5);
        assert_eq!(guardrails.refill_per_sec, 3.25);
        assert_eq!(guardrails.gpu_capacity, 2.0);
        assert_eq!(guardrails.gpu_refill_per_sec, 0.5);
        assert_eq!(guardrails.timeout, Duration::from_millis(250));
    }

    #[test]
    fn guardrail_env_parsers_fail_loud_on_bad_present_values() {
        let _guard = ENV_LOCK.lock().unwrap();

        unsafe { std::env::set_var("CALYX_WEB_API_TEST_BAD_RATE", "inf") };
        let err = parse_env_f64("CALYX_WEB_API_TEST_BAD_RATE", 1.0).unwrap_err();
        unsafe { std::env::remove_var("CALYX_WEB_API_TEST_BAD_RATE") };
        assert!(
            err.contains("non-negative finite number"),
            "loud rate parse error: {err}"
        );

        unsafe { std::env::set_var("CALYX_WEB_API_TEST_ZERO_TIMEOUT", "0") };
        let err = parse_env_duration_ms("CALYX_WEB_API_TEST_ZERO_TIMEOUT", 5000).unwrap_err();
        unsafe { std::env::remove_var("CALYX_WEB_API_TEST_ZERO_TIMEOUT") };
        assert!(
            err.contains("positive integer milliseconds"),
            "loud timeout parse error: {err}"
        );
    }
}
