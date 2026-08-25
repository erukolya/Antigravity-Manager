#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / ".fork" / "patch-manifest.json"
STATE_PATH = ROOT / ".fork" / "upstream-state.json"

CLIENT_PATH = ROOT / "src-tauri/src/proxy/upstream/client.rs"
QUOTA_PATH = ROOT / "src-tauri/src/modules/quota.rs"
PACKAGE_PATH = ROOT / "package.json"
CARGO_PATH = ROOT / "src-tauri/Cargo.toml"
TAURI_PATH = ROOT / "src-tauri/tauri.conf.json"

SANDBOX_HOST = "daily-cloudcode-pa.sandbox.googleapis.com"
FORK_REPO = "erukolya/Antigravity-Manager"

CLIENT_ENDPOINT_START = "// Cloud Code v1internal endpoints"
CLIENT_ENDPOINT_END = "pub struct UpstreamClient {"
CLIENT_FALLBACK_START = "    /// Determine if we should try next endpoint (fallback logic)"
CLIENT_FALLBACK_END = "    /// Call v1internal API (Basic Method)"
CLIENT_RETRY_START = "        let mut has_triggered_downgrade = false;"
CLIENT_RETRY_END = "    /// 调用 v1internal API（带 429 重试,支持闭包）"
QUOTA_ENDPOINT_START = "// Quota API endpoints"
QUOTA_ENDPOINT_END = "/// Critical retry threshold"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def extract_between(text: str, start: str, end: str) -> str:
    start_idx = text.find(start)
    if start_idx < 0:
        raise RuntimeError(f"start marker not found: {start!r}")
    end_idx = text.find(end, start_idx)
    if end_idx < 0:
        raise RuntimeError(f"end marker not found: {end!r}")
    if text.find(start, start_idx + 1) >= 0:
        raise RuntimeError(f"start marker is not unique: {start!r}")
    return text[start_idx:end_idx]


def replace_between(text: str, start: str, end: str, replacement: str) -> str:
    old = extract_between(text, start, end)
    return text.replace(old, replacement, 1)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def parse_revision(version: str, upstream_version: str) -> int | None:
    match = re.fullmatch(re.escape(upstream_version) + r"-(\d+)", version)
    return int(match.group(1)) if match else None


def build_manifest() -> dict:
    client = read(CLIENT_PATH)
    quota = read(QUOTA_PATH)
    return {
        "client_endpoints_sha256": sha256(
            extract_between(client, CLIENT_ENDPOINT_START, CLIENT_ENDPOINT_END)
        ),
        "client_fallback_sha256": sha256(
            extract_between(client, CLIENT_FALLBACK_START, CLIENT_FALLBACK_END)
        ),
        "client_retry_sha256": sha256(
            extract_between(client, CLIENT_RETRY_START, CLIENT_RETRY_END)
        ),
        "quota_endpoints_sha256": sha256(
            extract_between(quota, QUOTA_ENDPOINT_START, QUOTA_ENDPOINT_END)
        ),
    }


def require_manifest_block(manifest: dict, key: str, block: str) -> None:
    expected = manifest.get(key)
    actual = sha256(block)
    if expected != actual:
        raise RuntimeError(
            f"upstream block changed for {key}: expected {expected}, got {actual}; "
            "refusing to guess a patch"
        )


def patch_client(manifest: dict) -> None:
    text = read(CLIENT_PATH)

    endpoints = extract_between(text, CLIENT_ENDPOINT_START, CLIENT_ENDPOINT_END)
    require_manifest_block(manifest, "client_endpoints_sha256", endpoints)
    endpoints_new = '''// Cloud Code v1internal endpoints (fallback order: Daily → Prod)
const V1_INTERNAL_BASE_URL_PROD: &str = "https://cloudcode-pa.googleapis.com/v1internal";
const V1_INTERNAL_BASE_URL_DAILY: &str = "https://daily-cloudcode-pa.googleapis.com/v1internal";

const V1_INTERNAL_BASE_URL_FALLBACKS: [&str; 2] = [
    V1_INTERNAL_BASE_URL_DAILY,
    V1_INTERNAL_BASE_URL_PROD,
];

const V1_INTERNAL_RETRY_ROUND_DELAYS_SECS: [u64; 2] = [5, 30];
const V1_INTERNAL_RETRY_ROUNDS: usize = V1_INTERNAL_RETRY_ROUND_DELAYS_SECS.len() + 1;

'''
    text = replace_between(text, CLIENT_ENDPOINT_START, CLIENT_ENDPOINT_END, endpoints_new)

    fallback = extract_between(text, CLIENT_FALLBACK_START, CLIENT_FALLBACK_END)
    require_manifest_block(manifest, "client_fallback_sha256", fallback)
    fallback_new = '''    /// Determine if we should try the next endpoint / retry round.
    fn should_try_next_endpoint(status: StatusCode) -> bool {
        status == StatusCode::REQUEST_TIMEOUT
            || status == StatusCode::NOT_FOUND
            || status == StatusCode::TOO_MANY_REQUESTS
            || status.is_server_error()
    }

    fn is_geo_location_error_body(body: &[u8]) -> bool {
        String::from_utf8_lossy(body).contains("User location is not supported")
    }

    /// Inspect a 400 response without losing its body for downstream error handling.
    async fn inspect_bad_request(resp: Response) -> Result<(Response, bool), String> {
        if resp.status() != StatusCode::BAD_REQUEST {
            return Ok((resp, false));
        }

        let status = resp.status();
        let version = resp.version();
        let headers = resp.headers().clone();
        let url = resp.url().clone();
        let body = resp
            .bytes()
            .await
            .map_err(|e| format!("Failed to buffer 400 response body: {}", e))?;
        let is_geo_error = Self::is_geo_location_error_body(&body);

        use rquest::ResponseBuilderExt;
        let mut builder = hyper::Response::builder().status(status).version(version).url(url);
        *builder
            .headers_mut()
            .ok_or_else(|| "Failed to rebuild 400 response headers".to_string())? = headers;
        let rebuilt = builder
            .body(body)
            .map_err(|e| format!("Failed to rebuild 400 response: {}", e))?;

        Ok((Response::from(rebuilt), is_geo_error))
    }

'''
    text = replace_between(text, CLIENT_FALLBACK_START, CLIENT_FALLBACK_END, fallback_new)

    retry = extract_between(text, CLIENT_RETRY_START, CLIENT_RETRY_END)
    require_manifest_block(manifest, "client_retry_sha256", retry)
    retry_new = '''        let mut has_triggered_downgrade = false;
        let mut fallback_attempts: Vec<FallbackAttemptLog> = Vec::new();

        // Preserve the existing one-shot 403 project-header downgrade, but each mode gets
        // a fresh set of three complete Daily → Prod rounds.
        'request_mode: loop {
            let mut last_err: Option<String> = None;
            let mut should_retry_without_header = false;

            'rounds: for round_idx in 0..V1_INTERNAL_RETRY_ROUNDS {
                for (idx, base_url) in V1_INTERNAL_BASE_URL_FALLBACKS.iter().enumerate() {
                    let url = Self::build_url(base_url, method, query_string);
                    let has_next = idx + 1 < V1_INTERNAL_BASE_URL_FALLBACKS.len();

                    let body_bytes = serde_json::to_vec(&body).map_err(|e| e.to_string())?;

                    let mut req_builder = client.post(&url).headers(headers.clone());

                    // [FIX] 仅对流式接口 (streamGenerateContent) 使用分块传输仿真
                    // 对其他接口 (如 generateContent, loadCodeAssist) 发送正常的固定长度 Body
                    // 否则图像生成会因为缺少 Content-Length 而被 Google 服务端拒绝或限流 (429)
                    if method == "streamGenerateContent" {
                        let stream_bytes = body_bytes.clone();
                        req_builder = req_builder.body(rquest::Body::wrap_stream(
                            futures::stream::once(async move { Ok::<_, std::io::Error>(stream_bytes) }),
                        ));
                    } else {
                        req_builder = req_builder.body(body_bytes.clone());
                    }

                    match req_builder.send().await {
                        Ok(mut resp) => {
                            let status = resp.status();
                            if status.is_success() {
                                if idx > 0 || round_idx > 0 {
                                    tracing::info!(
                                        "✓ Upstream fallback succeeded | Endpoint: {} | Round: {}/{} | Status: {}",
                                        base_url,
                                        round_idx + 1,
                                        V1_INTERNAL_RETRY_ROUNDS,
                                        status
                                    );
                                } else {
                                    tracing::debug!(
                                        "✓ Upstream request succeeded | Endpoint: {} | Status: {}",
                                        base_url,
                                        status
                                    );
                                }
                                return Ok(UpstreamCallResult {
                                    response: resp,
                                    fallback_attempts,
                                });
                            }

                            // Existing one-shot downgrade: any 403 with x-goog-user-project
                            // removes the header and restarts all retry rounds exactly once.
                            if status == StatusCode::FORBIDDEN
                                && !has_triggered_downgrade
                                && headers.contains_key("x-goog-user-project")
                            {
                                tracing::warn!(
                                    "Detected 403 Forbidden with project header, retrying WITHOUT x-goog-user-project header (Account: {:?})",
                                    account_id
                                );
                                should_retry_without_header = true;
                                break 'rounds;
                            }

                            let mut is_geo_location_error = false;
                            if status == StatusCode::BAD_REQUEST {
                                match Self::inspect_bad_request(resp).await {
                                    Ok((rebuilt, is_geo)) => {
                                        resp = rebuilt;
                                        is_geo_location_error = is_geo;
                                    }
                                    Err(e) => {
                                        let msg = format!(
                                            "HTTP response read failed at {}: {}",
                                            base_url, e
                                        );
                                        tracing::warn!("{}", msg);
                                        fallback_attempts.push(FallbackAttemptLog {
                                            endpoint_url: url.clone(),
                                            status: Some(status.as_u16()),
                                            error: msg.clone(),
                                        });
                                        last_err = Some(msg);

                                        if has_next {
                                            continue;
                                        }
                                        if round_idx + 1 < V1_INTERNAL_RETRY_ROUNDS {
                                            let delay = V1_INTERNAL_RETRY_ROUND_DELAYS_SECS[round_idx];
                                            tracing::warn!(
                                                "Upstream round {}/{} exhausted after response-read failure; retrying Daily → Prod in {}s",
                                                round_idx + 1,
                                                V1_INTERNAL_RETRY_ROUNDS,
                                                delay
                                            );
                                            tokio::time::sleep(Duration::from_secs(delay)).await;
                                            continue 'rounds;
                                        }
                                        break 'rounds;
                                    }
                                }
                            }

                            let retryable =
                                Self::should_try_next_endpoint(status) || is_geo_location_error;

                            if retryable {
                                let err_msg = if is_geo_location_error {
                                    format!(
                                        "Upstream {} returned 400 User location is not supported",
                                        base_url
                                    )
                                } else {
                                    format!("Upstream {} returned {}", base_url, status)
                                };

                                tracing::warn!(
                                    "Upstream endpoint retryable failure at {} (method={}, round={}/{}): {}",
                                    base_url,
                                    method,
                                    round_idx + 1,
                                    V1_INTERNAL_RETRY_ROUNDS,
                                    err_msg
                                );
                                fallback_attempts.push(FallbackAttemptLog {
                                    endpoint_url: url.clone(),
                                    status: Some(status.as_u16()),
                                    error: err_msg.clone(),
                                });
                                last_err = Some(err_msg);

                                if has_next {
                                    continue;
                                }

                                if round_idx + 1 < V1_INTERNAL_RETRY_ROUNDS {
                                    let delay = V1_INTERNAL_RETRY_ROUND_DELAYS_SECS[round_idx];
                                    tracing::warn!(
                                        "Upstream round {}/{} exhausted; retrying Daily → Prod in {}s",
                                        round_idx + 1,
                                        V1_INTERNAL_RETRY_ROUNDS,
                                        delay
                                    );
                                    tokio::time::sleep(Duration::from_secs(delay)).await;
                                    continue 'rounds;
                                }

                                // All three rounds are exhausted. Preserve the final HTTP response
                                // so the agent receives the real terminal Google error body/status.
                                return Ok(UpstreamCallResult {
                                    response: resp,
                                    fallback_attempts,
                                });
                            }

                            // All other statuses, including non-geo HTTP 400, are terminal.
                            return Ok(UpstreamCallResult {
                                response: resp,
                                fallback_attempts,
                            });
                        }
                        Err(e) => {
                            let msg = format!("HTTP request failed at {}: {}", base_url, e);
                            tracing::debug!("{}", msg);
                            fallback_attempts.push(FallbackAttemptLog {
                                endpoint_url: url.clone(),
                                status: None,
                                error: msg.clone(),
                            });
                            last_err = Some(msg);

                            if has_next {
                                continue;
                            }

                            if round_idx + 1 < V1_INTERNAL_RETRY_ROUNDS {
                                let delay = V1_INTERNAL_RETRY_ROUND_DELAYS_SECS[round_idx];
                                tracing::warn!(
                                    "Upstream round {}/{} exhausted by network errors; retrying Daily → Prod in {}s",
                                    round_idx + 1,
                                    V1_INTERNAL_RETRY_ROUNDS,
                                    delay
                                );
                                tokio::time::sleep(Duration::from_secs(delay)).await;
                                continue 'rounds;
                            }

                            break 'rounds;
                        }
                    }
                }
            }

            if should_retry_without_header {
                headers.remove("x-goog-user-project");
                has_triggered_downgrade = true;
                continue 'request_mode;
            }

            return Err(last_err.unwrap_or_else(|| "All endpoints failed".to_string()));
        }
    }

'''
    text = replace_between(text, CLIENT_RETRY_START, CLIENT_RETRY_END, retry_new)

    tests_anchor = """    #[test]
    fn test_build_url() {
"""
    if "test_geo_location_error_is_selective" not in text:
        tests = '''    #[test]
    fn test_geo_location_error_is_selective() {
        assert!(UpstreamClient::is_geo_location_error_body(
            br#"{\"error\":{\"message\":\"User location is not supported\"}}"#
        ));
        assert!(!UpstreamClient::is_geo_location_error_body(
            br#"{\"error\":{\"message\":\"Malformed request\"}}"#
        ));
        assert!(!UpstreamClient::should_try_next_endpoint(StatusCode::BAD_REQUEST));
        assert!(UpstreamClient::should_try_next_endpoint(
            StatusCode::TOO_MANY_REQUESTS
        ));
    }

'''
        text = replace_once(text, tests_anchor, tests + tests_anchor, "client tests anchor")

    if SANDBOX_HOST in text:
        raise RuntimeError("sandbox host still present in patched client.rs")
    write(CLIENT_PATH, text)


def patch_quota(manifest: dict) -> None:
    text = read(QUOTA_PATH)
    endpoints = extract_between(text, QUOTA_ENDPOINT_START, QUOTA_ENDPOINT_END)
    require_manifest_block(manifest, "quota_endpoints_sha256", endpoints)
    endpoints_new = '''// Quota API endpoints (fallback order: Daily → Prod)
const QUOTA_API_ENDPOINTS: [&str; 2] = [
    "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
    "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
];

// Quota Summary API endpoints (weekly + 5h grouped quota, fallback order 同上)
const QUOTA_SUMMARY_ENDPOINTS: [&str; 2] = [
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
];

'''
    text = replace_between(text, QUOTA_ENDPOINT_START, QUOTA_ENDPOINT_END, endpoints_new)
    text = replace_once(
        text,
        'const CLOUD_CODE_BASE_URL: &str = "https://daily-cloudcode-pa.sandbox.googleapis.com";',
        'const CLOUD_CODE_BASE_URL: &str = "https://daily-cloudcode-pa.googleapis.com";',
        "quota loadCodeAssist base URL",
    )
    if SANDBOX_HOST in text:
        raise RuntimeError("sandbox host still present in patched quota.rs")
    write(QUOTA_PATH, text)


def patch_remaining_runtime() -> None:
    project_path = ROOT / "src-tauri/src/proxy/project_resolver.rs"
    project = read(project_path)
    project = replace_once(
        project,
        "// 使用 Sandbox 环境，避免 Prod 环境的 429 错误\n    let url = \"https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:loadCodeAssist\";",
        "// Fork policy: use Daily directly; Sandbox is excluded from active runtime.\n    let url = \"https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist\";",
        "project_resolver Sandbox endpoint",
    )
    write(project_path, project)

    wrapper_path = ROOT / "src-tauri/src/proxy/mappers/gemini/wrapper.rs"
    wrapper = read(wrapper_path)
    wrapper = replace_once(
        wrapper,
        "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal/projects/{}/locations/global/models/{}:generateContent",
        "https://daily-cloudcode-pa.googleapis.com/v1internal/projects/{}/locations/global/models/{}:generateContent",
        "Gemini background-summary Sandbox endpoint",
    )
    write(wrapper_path, wrapper)


def determine_versions(bump_revision: bool) -> tuple[str, str]:
    package = json.loads(read(PACKAGE_PATH))
    upstream_version = package["version"]
    if re.search(r"-\d+$", upstream_version):
        raise RuntimeError(
            f"expected a clean upstream version before customization, got {upstream_version}"
        )

    previous = None
    if STATE_PATH.exists():
        previous = json.loads(read(STATE_PATH))

    if previous and previous.get("upstream_version") == upstream_version:
        revision = parse_revision(previous.get("fork_version", ""), upstream_version)
        if revision is None:
            raise RuntimeError("invalid fork_version in upstream-state.json")
        if bump_revision:
            revision += 1
    else:
        revision = 1

    return upstream_version, f"{upstream_version}-{revision}"


def patch_versions(upstream_version: str, fork_version: str) -> None:
    package = read(PACKAGE_PATH)
    package = replace_once(
        package,
        f'"version": "{upstream_version}"',
        f'"version": "{fork_version}"',
        "package.json version",
    )
    write(PACKAGE_PATH, package)

    cargo = read(CARGO_PATH)
    cargo = replace_once(
        cargo,
        f'version = "{upstream_version}"',
        f'version = "{fork_version}"',
        "Cargo.toml package version",
    )
    write(CARGO_PATH, cargo)

    tauri = read(TAURI_PATH)
    tauri = replace_once(
        tauri,
        f'"version": "{upstream_version}"',
        f'"version": "{fork_version}"',
        "tauri.conf.json version",
    )
    tauri = replace_once(
        tauri,
        "https://github.com/lbjlaq/Antigravity-Manager/releases/latest/download/updater.json",
        f"https://github.com/{FORK_REPO}/releases/latest/download/updater.json",
        "fork updater endpoint",
    )
    write(TAURI_PATH, tauri)

    write(
        STATE_PATH,
        json.dumps(
            {"upstream_version": upstream_version, "fork_version": fork_version},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )


def validate_runtime() -> None:
    runtime_root = ROOT / "src-tauri/src"
    hits = []
    for path in runtime_root.rglob("*.rs"):
        if SANDBOX_HOST in read(path):
            hits.append(str(path.relative_to(ROOT)))
    if hits:
        raise RuntimeError(f"sandbox host remains in runtime files: {hits}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-manifest", action="store_true")
    parser.add_argument("--bump-revision", action="store_true")
    args = parser.parse_args()

    if args.bootstrap_manifest:
        if MANIFEST_PATH.exists():
            raise RuntimeError("refusing to overwrite existing patch-manifest.json")
        write(MANIFEST_PATH, json.dumps(build_manifest(), indent=2) + "\n")

    if not MANIFEST_PATH.exists():
        raise RuntimeError("missing .fork/patch-manifest.json")

    manifest = json.loads(read(MANIFEST_PATH))
    upstream_version, fork_version = determine_versions(args.bump_revision)

    patch_client(manifest)
    patch_quota(manifest)
    patch_remaining_runtime()
    patch_versions(upstream_version, fork_version)
    validate_runtime()

    print(f"Applied fork customizations: {upstream_version} -> {fork_version}")


if __name__ == "__main__":
    main()
