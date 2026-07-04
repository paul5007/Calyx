use super::cache::{ResponseCache, cached_json_response, store_and_respond};
use super::*;

const MATCH_DOMAIN: &str = "soccer_lab.match_result";
const PROGRESSION_RECORD_TYPE: &str = "tournament_progression";
const PROGRESSION_AXES: [&str; 3] = ["winner", "finalist", "semi_finalist"];
const PLAYER_RECORD_TYPE: &str = "player_impact";

/// Loaded Soccer Lab prediction export, indexed by match id.
///
/// This is a startup dependency for the live serving surface: if the export is
/// missing, malformed, or does not contain match predictions, the API must not
/// advertise `/predict/match`.
pub struct PredictionCtx {
    match_records: HashMap<String, Value>,
    progression_records: HashMap<String, Value>,
    player_records: HashMap<String, Value>,
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
        let mut progression_records = HashMap::new();
        let mut player_records = HashMap::new();
        for record in records {
            if record.get("domain").and_then(Value::as_str) != Some(MATCH_DOMAIN) {
                if record.get("record_type").and_then(Value::as_str)
                    == Some(PROGRESSION_RECORD_TYPE)
                {
                    let version = record
                        .pointer("/input/attributes/version")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            format!(
                                "tournament progression record missing input.attributes.version in {}",
                                path.display()
                            )
                        })?;
                    let team = record
                        .pointer("/input/attributes/team")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            format!(
                                "tournament progression record missing input.attributes.team in {}",
                                path.display()
                            )
                        })?;
                    let axis = record
                        .pointer("/input/attributes/axis")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            format!(
                                "tournament progression record missing input.attributes.axis in {}",
                                path.display()
                            )
                        })?;
                    if !PROGRESSION_AXES.contains(&axis) {
                        return Err(format!(
                            "unknown tournament progression axis {axis} in {}",
                            path.display()
                        ));
                    }
                    let key = progression_key(version, team, axis);
                    if progression_records
                        .insert(key.clone(), record.clone())
                        .is_some()
                    {
                        return Err(format!(
                            "duplicate tournament progression prediction key {key} in {}",
                            path.display()
                        ));
                    }
                }
                if record.get("record_type").and_then(Value::as_str) == Some(PLAYER_RECORD_TYPE) {
                    let player_id = record
                        .pointer("/input/attributes/player_id")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            format!(
                                "player impact record missing input.attributes.player_id in {}",
                                path.display()
                            )
                        })?;
                    let entity_id = record
                        .pointer("/input/entity_id")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            format!(
                                "player impact record missing input.entity_id in {}",
                                path.display()
                            )
                        })?;
                    if player_id != entity_id {
                        return Err(format!(
                            "player impact record player_id/entity_id mismatch {player_id}/{entity_id} in {}",
                            path.display()
                        ));
                    }
                    if player_records
                        .insert(player_id.to_owned(), record.clone())
                        .is_some()
                    {
                        return Err(format!(
                            "duplicate player impact prediction id {player_id} in {}",
                            path.display()
                        ));
                    }
                }
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
        if progression_records.is_empty() {
            return Err(format!(
                "prediction export {} contains no {PROGRESSION_RECORD_TYPE} records",
                path.display()
            ));
        }
        if player_records.is_empty() {
            return Err(format!(
                "prediction export {} contains no {PLAYER_RECORD_TYPE} records",
                path.display()
            ));
        }
        Ok(Self {
            match_records,
            progression_records,
            player_records,
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

    pub fn progression_count(&self) -> usize {
        self.progression_records.len()
    }

    pub fn player_count(&self) -> usize {
        self.player_records.len()
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct MatchPredictionReq {
    match_id: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ProgressionPredictionReq {
    version: String,
    team: String,
    axis: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct PlayerPredictionReq {
    player_id: String,
}

fn progression_key(version: &str, team: &str, axis: &str) -> String {
    format!(
        "{}\u{1f}{}\u{1f}{}",
        version.trim(),
        team.trim(),
        axis.trim()
    )
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

pub(super) async fn predict_progression(
    State(ctx): State<Arc<PredictionCtx>>,
    body: axum::body::Bytes,
) -> Response {
    let req: ProgressionPredictionReq = match serde_json::from_slice(&body) {
        Ok(req) => req,
        Err(error) => {
            tracing::warn!(error = ?error, "CALYX_WEB_API_PREDICT_PROGRESSION_BAD_REQUEST");
            return ApiError::new(
                ErrorCode::BadRequest,
                "request body must be JSON object {\"version\":\"2026\",\"team\":\"France\",\"axis\":\"winner\"}",
            )
            .into_response();
        }
    };
    let version = req.version.trim();
    let team = req.team.trim();
    let axis = req.axis.trim();
    if version.is_empty() {
        return ApiError::new(ErrorCode::BadRequest, "version must be non-empty").into_response();
    }
    if team.is_empty() {
        return ApiError::new(ErrorCode::BadRequest, "team must be non-empty").into_response();
    }
    if !PROGRESSION_AXES.contains(&axis) {
        return ApiError::new(
            ErrorCode::BadRequest,
            "axis must be one of winner|finalist|semi_finalist",
        )
        .into_response();
    }
    let key = progression_key(version, team, axis);
    let cache_key = format!("predict_progression\u{1f}{key}");
    if let Some((body, age)) = ctx.cache.get(&cache_key) {
        return cached_json_response(body, "HIT", age);
    }
    let Some(record) = ctx.progression_records.get(&key) else {
        return ApiError::new(
            ErrorCode::NotFound,
            format!("no Soccer Lab tournament progression prediction for {version}:{team}:{axis}"),
        )
        .into_response();
    };
    store_and_respond(&ctx.cache, cache_key, record)
}

pub(super) async fn predict_player(
    State(ctx): State<Arc<PredictionCtx>>,
    body: axum::body::Bytes,
) -> Response {
    let req: PlayerPredictionReq = match serde_json::from_slice(&body) {
        Ok(req) => req,
        Err(error) => {
            tracing::warn!(error = ?error, "CALYX_WEB_API_PREDICT_PLAYER_BAD_REQUEST");
            return ApiError::new(
                ErrorCode::BadRequest,
                "request body must be JSON object {\"playerId\":\"1\"}",
            )
            .into_response();
        }
    };
    let player_id = req.player_id.trim();
    if player_id.is_empty() {
        return ApiError::new(ErrorCode::BadRequest, "playerId must be non-empty").into_response();
    }
    let cache_key = format!("predict_player\u{1f}{player_id}");
    if let Some((body, age)) = ctx.cache.get(&cache_key) {
        return cached_json_response(body, "HIT", age);
    }
    let Some(record) = ctx.player_records.get(player_id) else {
        return ApiError::new(
            ErrorCode::NotFound,
            format!("no Soccer Lab player impact prediction for {player_id}"),
        )
        .into_response();
    };
    store_and_respond(&ctx.cache, cache_key, record)
}
