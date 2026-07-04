use super::cache::{ResponseCache, cached_json_response, store_and_respond};
use super::*;

const MATCH_DOMAIN: &str = "soccer_lab.match_result";

/// Loaded Soccer Lab prediction export, indexed by match id.
///
/// This is a startup dependency for the live serving surface: if the export is
/// missing, malformed, or does not contain match predictions, the API must not
/// advertise `/predict/match`.
pub struct PredictionCtx {
    match_records: HashMap<String, Value>,
    cache: ResponseCache,
}

impl PredictionCtx {
    pub fn load(path: &FsPath) -> Result<Self, String> {
        let raw = std::fs::read(path)
            .map_err(|error| format!("read prediction export {}: {error}", path.display()))?;
        let root: Value = serde_json::from_slice(&raw)
            .map_err(|error| format!("parse prediction export JSON {}: {error}", path.display()))?;
        let records = root
            .get("records")
            .and_then(Value::as_array)
            .ok_or_else(|| format!("prediction export {} missing records[]", path.display()))?;
        let mut match_records = HashMap::new();
        for record in records {
            if record.get("domain").and_then(Value::as_str) != Some(MATCH_DOMAIN) {
                continue;
            }
            let entity_id = record
                .pointer("/input/entity_id")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    format!(
                        "match prediction record missing input.entity_id in {}",
                        path.display()
                    )
                })?;
            if match_records
                .insert(entity_id.to_owned(), record.clone())
                .is_some()
            {
                return Err(format!(
                    "duplicate match prediction id {entity_id} in {}",
                    path.display()
                ));
            }
        }
        if match_records.is_empty() {
            return Err(format!(
                "prediction export {} contains no {MATCH_DOMAIN} records",
                path.display()
            ));
        }
        Ok(Self {
            match_records,
            cache: ResponseCache::from_env()?,
        })
    }

    pub fn from_env() -> Result<Self, String> {
        let path = std::env::var("CALYX_WEB_API_PREDICTION_EXPORT").map_err(|_| {
            "CALYX_WEB_API_PREDICTION_EXPORT is required (path to Soccer Lab prediction export)"
                .to_string()
        })?;
        Self::load(PathBuf::from(path).as_path())
    }

    pub fn match_count(&self) -> usize {
        self.match_records.len()
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct MatchPredictionReq {
    match_id: String,
}

pub(super) async fn predict_match(
    State(ctx): State<Arc<PredictionCtx>>,
    body: axum::body::Bytes,
) -> Response {
    let req: MatchPredictionReq = match serde_json::from_slice(&body) {
        Ok(req) => req,
        Err(error) => {
            tracing::warn!(error = ?error, "CALYX_WEB_API_PREDICT_MATCH_BAD_REQUEST");
            return ApiError::new(
                ErrorCode::BadRequest,
                "request body must be JSON object {\"matchId\":\"WC-2026-M089\"}",
            )
            .into_response();
        }
    };
    let match_id = req.match_id.trim();
    if match_id.is_empty() {
        return ApiError::new(ErrorCode::BadRequest, "matchId must be non-empty").into_response();
    }
    let cache_key = format!("predict_match\u{1f}{match_id}");
    if let Some((body, age)) = ctx.cache.get(&cache_key) {
        return cached_json_response(body, "HIT", age);
    }
    let Some(record) = ctx.match_records.get(match_id) else {
        return ApiError::new(
            ErrorCode::NotFound,
            format!("no Soccer Lab match prediction for {match_id}"),
        )
        .into_response();
    };
    store_and_respond(&ctx.cache, cache_key, record)
}
