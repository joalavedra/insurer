use axum::{
    body::Bytes,
    extract::{DefaultBodyLimit, State},
    http::{header::CONTENT_TYPE, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    env, fs, io,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
    time::SystemTime,
};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};
use tokio::{io::AsyncWriteExt, net::TcpListener, sync::Mutex};

const MAX_BODY_BYTES: usize = 10 * 1024 * 1024;

struct AppState {
    data_dir: PathBuf,
    file_locks: Mutex<HashMap<PathBuf, Arc<Mutex<()>>>>,
}

#[derive(Serialize)]
struct AgentSummary {
    agent_id: String,
    files: usize,
    documents: usize,
    spans: usize,
    last_received_at: Option<String>,
}

fn app(data_dir: PathBuf) -> Router {
    Router::new()
        .route("/v1/traces", post(receive_trace))
        .route("/agents", get(get_agents))
        .route("/healthz", get(healthz))
        .layer(DefaultBodyLimit::max(MAX_BODY_BYTES))
        .with_state(Arc::new(AppState {
            data_dir,
            file_locks: Mutex::new(HashMap::new()),
        }))
}

fn error(status: StatusCode, message: &'static str) -> Response {
    (status, message).into_response()
}

fn valid_agent_id(agent_id: &str) -> bool {
    !agent_id.is_empty()
        && agent_id.len() <= 64
        && agent_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
}

fn is_json_content_type(headers: &HeaderMap) -> bool {
    headers
        .get(CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|value| value.trim().eq_ignore_ascii_case("application/json"))
}

fn utc_date() -> String {
    let now = OffsetDateTime::now_utc();
    format!(
        "{:04}-{:02}-{:02}",
        now.year(),
        u8::from(now.month()),
        now.day()
    )
}

async fn receive_trace(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if !is_json_content_type(&headers) {
        return error(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "OTLP protobuf is not supported; use the http/json exporter",
        );
    }

    let Some(agent_id) = headers
        .get("x-insurer-agent")
        .and_then(|value| value.to_str().ok())
        .filter(|value| valid_agent_id(value))
        .map(str::to_owned)
    else {
        return error(
            StatusCode::BAD_REQUEST,
            "x-insurer-agent must match [A-Za-z0-9_-]{1,64}",
        );
    };

    let Ok(document) = serde_json::from_slice::<Value>(&body) else {
        return error(
            StatusCode::BAD_REQUEST,
            "body must be a JSON object with a resourceSpans array",
        );
    };
    if !document
        .as_object()
        .and_then(|object| object.get("resourceSpans"))
        .is_some_and(Value::is_array)
    {
        return error(
            StatusCode::BAD_REQUEST,
            "body must be a JSON object with a resourceSpans array",
        );
    }

    let Ok(compact) = serde_json::to_vec(&document) else {
        return error(StatusCode::BAD_REQUEST, "could not serialize OTLP document");
    };
    let path = state
        .data_dir
        .join(agent_id)
        .join(format!("{}.jsonl", utc_date()));
    let file_lock = {
        let mut locks = state.file_locks.lock().await;
        Arc::clone(
            locks
                .entry(path.clone())
                .or_insert_with(|| Arc::new(Mutex::new(()))),
        )
    };
    let _guard = file_lock.lock().await;
    let result = async {
        let parent = path
            .parent()
            .ok_or_else(|| io::Error::other("trace path has no parent directory"))?;
        tokio::fs::create_dir_all(parent).await?;
        let mut file = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .await?;
        file.write_all(&compact).await?;
        file.write_all(b"\n").await
    }
    .await;

    match result {
        Ok(()) => Json(json!({"partialSuccess": {}})).into_response(),
        Err(_) => error(StatusCode::INTERNAL_SERVER_ERROR, "could not append trace"),
    }
}

fn span_count(document: &Value) -> usize {
    document
        .get("resourceSpans")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|resource| {
            resource
                .get("scopeSpans")
                .or_else(|| resource.get("instrumentationLibrarySpans"))
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(|scope| {
                    scope
                        .get("spans")
                        .and_then(Value::as_array)
                        .map_or(0, Vec::len)
                })
                .sum::<usize>()
        })
        .sum()
}

fn received_at(time: SystemTime) -> Option<String> {
    OffsetDateTime::from(time).format(&Rfc3339).ok()
}

fn summarize_agents(data_dir: &Path) -> io::Result<Vec<AgentSummary>> {
    let entries = match fs::read_dir(data_dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    let mut agents = Vec::new();
    for entry in entries {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let agent_id = entry.file_name().to_string_lossy().into_owned();
        if !valid_agent_id(&agent_id) {
            continue;
        }
        let mut summary = AgentSummary {
            agent_id,
            files: 0,
            documents: 0,
            spans: 0,
            last_received_at: None,
        };
        let mut last_modified = None;
        for file in fs::read_dir(entry.path())? {
            let file = file?;
            if !file.file_type()?.is_file()
                || file
                    .path()
                    .extension()
                    .is_none_or(|extension| extension != "jsonl")
            {
                continue;
            }
            summary.files += 1;
            if let Ok(modified) = file.metadata()?.modified() {
                last_modified =
                    Some(last_modified.map_or(modified, |latest: SystemTime| latest.max(modified)));
            }
            for line in fs::read_to_string(file.path())?.lines() {
                let Ok(document) = serde_json::from_str::<Value>(line) else {
                    continue;
                };
                summary.documents += 1;
                summary.spans += span_count(&document);
            }
        }
        if summary.files > 0 {
            summary.last_received_at = last_modified.and_then(received_at);
            agents.push(summary);
        }
    }
    agents.sort_by(|left, right| left.agent_id.cmp(&right.agent_id));
    Ok(agents)
}

async fn get_agents(
    State(state): State<Arc<AppState>>,
) -> Result<Json<Vec<AgentSummary>>, Response> {
    summarize_agents(&state.data_dir).map(Json).map_err(|_| {
        error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "could not read trace store",
        )
    })
}

async fn healthz() -> Json<Value> {
    Json(json!({"status": "ok"}))
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let addr: SocketAddr = env::var("COLLECTOR_ADDR")
        .unwrap_or_else(|_| "127.0.0.1:4318".to_owned())
        .parse()?;
    let data_dir = env::var_os("COLLECTOR_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("./data/traces"));
    let listener = TcpListener::bind(addr).await?;
    println!(
        "insurer-collector listening on {addr}; data in {}",
        data_dir.display()
    );
    axum::serve(listener, app(data_dir)).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{to_bytes, Body},
        http::{Method, Request},
    };
    use tempfile::tempdir;
    use tower::ServiceExt;

    async fn request(
        app: &Router,
        method: Method,
        uri: &str,
        body: impl Into<Body>,
        content_type: Option<&str>,
        agent_id: Option<&str>,
    ) -> Response {
        let mut builder = Request::builder().method(method).uri(uri);
        if let Some(content_type) = content_type {
            builder = builder.header(CONTENT_TYPE, content_type);
        }
        if let Some(agent_id) = agent_id {
            builder = builder.header("x-insurer-agent", agent_id);
        }
        app.clone()
            .oneshot(builder.body(body.into()).unwrap())
            .await
            .unwrap()
    }

    async fn response_json(response: Response) -> Value {
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&body).unwrap()
    }

    #[tokio::test]
    async fn appends_compact_json_and_preserves_large_timestamps() {
        let directory = tempdir().unwrap();
        let app = app(directory.path().to_path_buf());
        let timestamp = "1792014281950903701";
        let body = format!(
            r#"{{ "resourceSpans" : [{{ "scopeSpans" : [{{ "spans" : [{{"startTimeUnixNano":{timestamp}}},{{"startTimeUnixNano":"{timestamp}"}}] }}] }}] }}"#
        );

        let response = request(
            &app,
            Method::POST,
            "/v1/traces",
            body,
            Some("application/json"),
            Some("kyc_bot-1"),
        )
        .await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response_json(response).await, json!({"partialSuccess": {}}));

        let path = directory
            .path()
            .join("kyc_bot-1")
            .join(format!("{}.jsonl", utc_date()));
        let stored = tokio::fs::read_to_string(path).await.unwrap();
        assert_eq!(stored.lines().count(), 1);
        assert!(stored.contains(&format!(r#""startTimeUnixNano":{timestamp}"#)));
        assert!(stored.contains(&format!(r#""startTimeUnixNano":"{timestamp}""#)));
        assert!(!stored.contains(": "));
        let document: Value = serde_json::from_str(stored.trim()).unwrap();
        assert_eq!(
            document["resourceSpans"][0]["scopeSpans"][0]["spans"]
                .as_array()
                .unwrap()
                .len(),
            2
        );

        let response = request(&app, Method::GET, "/agents", Body::empty(), None, None).await;
        assert_eq!(response.status(), StatusCode::OK);
        let agents = response_json(response).await;
        assert_eq!(agents[0]["agent_id"], "kyc_bot-1");
        assert_eq!(agents[0]["files"], 1);
        assert_eq!(agents[0]["documents"], 1);
        assert_eq!(agents[0]["spans"], 2);
        assert!(agents[0]["last_received_at"].as_str().is_some());
    }

    #[tokio::test]
    async fn rejects_missing_or_invalid_agent_and_invalid_documents() {
        let directory = tempdir().unwrap();
        let app = app(directory.path().to_path_buf());
        for (body, agent_id) in [
            (r#"{"resourceSpans":[]}"#, None),
            (r#"{"resourceSpans":[]}"#, Some("bad id")),
            (r#"[]"#, Some("valid-agent")),
            (r#"{"resourceSpans":{}}"#, Some("valid-agent")),
            ("not-json", Some("valid-agent")),
        ] {
            let response = request(
                &app,
                Method::POST,
                "/v1/traces",
                body,
                Some("application/json"),
                agent_id,
            )
            .await;
            assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        }
    }

    #[tokio::test]
    async fn rejects_non_json_content_types() {
        let directory = tempdir().unwrap();
        let app = app(directory.path().to_path_buf());
        let response = request(
            &app,
            Method::POST,
            "/v1/traces",
            vec![0_u8, 1, 2, 3],
            Some("application/x-protobuf"),
            Some("agent-1"),
        )
        .await;
        assert_eq!(response.status(), StatusCode::UNSUPPORTED_MEDIA_TYPE);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        assert!(String::from_utf8(body.to_vec())
            .unwrap()
            .contains("http/json exporter"));
    }

    #[tokio::test]
    async fn rejects_bodies_over_ten_megabytes() {
        let directory = tempdir().unwrap();
        let app = app(directory.path().to_path_buf());
        let response = request(
            &app,
            Method::POST,
            "/v1/traces",
            vec![b' '; MAX_BODY_BYTES + 1],
            Some("application/json"),
            Some("agent-1"),
        )
        .await;
        assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    }

    #[tokio::test]
    async fn concurrent_appends_are_complete_json_lines() {
        let directory = tempdir().unwrap();
        let app = app(directory.path().to_path_buf());
        let mut tasks = Vec::new();
        for index in 0..32 {
            let app = app.clone();
            tasks.push(tokio::spawn(async move {
                let body = format!(
                    r#"{{"resourceSpans":[{{"scopeSpans":[{{"spans":[{{"index":{index}}}]}}]}}]}}"#
                );
                request(
                    &app,
                    Method::POST,
                    "/v1/traces",
                    body,
                    Some("application/json"),
                    Some("same-agent"),
                )
                .await
                .status()
            }));
        }
        for task in tasks {
            assert_eq!(task.await.unwrap(), StatusCode::OK);
        }

        let path = directory
            .path()
            .join("same-agent")
            .join(format!("{}.jsonl", utc_date()));
        let stored = tokio::fs::read_to_string(path).await.unwrap();
        let lines: Vec<_> = stored.lines().collect();
        assert_eq!(lines.len(), 32);
        for line in lines {
            serde_json::from_str::<Value>(line).unwrap();
        }
    }

    #[tokio::test]
    async fn health_endpoint_is_available() {
        let directory = tempdir().unwrap();
        let app = app(directory.path().to_path_buf());
        let response = request(&app, Method::GET, "/healthz", Body::empty(), None, None).await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response_json(response).await, json!({"status": "ok"}));
    }
}
