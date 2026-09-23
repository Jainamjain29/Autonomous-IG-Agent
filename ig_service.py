"""Instagram Graph API client: credential checks, Reel publishing and analytics.

Reel publishing follows Meta's content publishing flow:
  1. POST /{ig_user_id}/media          -> create a REELS container
  2. (resumable only) POST to rupload   -> send the local file bytes
     (ngrok mode instead passes a public video_url in step 1 and skips this)
  3. GET /{container_id}                -> poll status_code until FINISHED
  4. POST /{ig_user_id}/media_publish   -> publish the container
Docs: https://developers.facebook.com/docs/instagram-platform/content-publishing/

Every request uses a 30s timeout, logs the status code and full JSON on error,
and raises IGPublishError instead of returning None.
"""
import os
import json
import time
import requests
import database as db

DEFAULT_GRAPH_HOST = "graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v26.0"
PUBLISH_MODES = ("resumable", "ngrok")

REQUEST_TIMEOUT = 30
UPLOAD_TIMEOUT = (10, 120)  # (connect, read)
POLL_INTERVAL = 10
POLL_TIMEOUT = 10 * 60
MAX_POLL_RETRIES = 3  # consecutive transient failures tolerated while polling
MAX_REEL_BYTES = 300 * 1024 * 1024  # Meta's Reels limit


class IGPublishError(Exception):
    """A Graph API call failed. Carries the HTTP status and response body, if any.

    `transient` is True for network errors, timeouts and 5xx responses, which may
    succeed on retry. 4xx responses and error payloads on 2xx are not transient.
    `step` is the publish step that failed (see PUBLISH_STEPS), or None if the
    error happened before the first step.
    """

    def __init__(self, message, status_code=None, response_json=None, transient=False, step=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_json = response_json
        self.transient = transient
        self.step = step


PUBLISH_STEPS = ("container", "upload", "poll", "publish")


def _step(name, fn, *args, **kwargs):
    """Runs one publish step, tagging any exception with the step name if it has none."""
    try:
        return fn(*args, **kwargs)
    except BaseException as e:
        if getattr(e, "step", None) is None:
            try:
                e.step = name
            except AttributeError:
                pass
        raise


def _log(message):
    print(f"[ig] {message}")


def get_config():
    """Returns (host, version, base_url) from settings/.env."""
    host = db.get_setting("GRAPH_HOST") or DEFAULT_GRAPH_HOST
    version = db.get_setting("GRAPH_VERSION") or DEFAULT_GRAPH_VERSION
    return host, version, f"https://{host}/{version}"


def get_publish_mode():
    mode = (db.get_setting("PUBLISH_MODE") or "resumable").strip().lower()
    if mode not in PUBLISH_MODES:
        raise IGPublishError(f"PUBLISH_MODE must be one of {PUBLISH_MODES}, got {mode!r}")
    return mode


def get_credentials():
    access_token = db.get_setting("META_ACCESS_TOKEN")
    ig_user_id = db.get_setting("IG_USER_ID")
    if not access_token or not ig_user_id:
        raise IGPublishError("Missing META_ACCESS_TOKEN or IG_USER_ID. Set them in .env or the dashboard.")
    return access_token, ig_user_id


def _redact(text, token):
    return text.replace(token, "***") if token else text


def _request(method, url, token, label, timeout=REQUEST_TIMEOUT, **kwargs):
    """Sends one request and returns the parsed JSON, or raises IGPublishError.

    `label` is a short token-free description (e.g. "POST /123/media") used in logs.
    """
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
    except requests.RequestException as e:
        # requests exceptions can include the URL, and so the token in its query string.
        raise IGPublishError(f"{label} failed: {_redact(str(e), token)}", transient=True) from None

    try:
        payload = response.json()
    except ValueError:
        payload = {"raw_body": response.text[:2000]}

    if not response.ok or (isinstance(payload, dict) and "error" in payload):
        body = _redact(json.dumps(payload, indent=2), token)
        _log(f"{label} -> HTTP {response.status_code}\n{body}")
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        message = error.get("message") if isinstance(error, dict) else None
        raise IGPublishError(
            f"{label} -> HTTP {response.status_code}: {message or 'see logged response'}",
            status_code=response.status_code,
            response_json=json.loads(body),
            transient=response.status_code >= 500,
        )
    return payload


def verify_credentials():
    """Checks the token and IG user ID. Prints the username and, on graph.facebook.com,
    the token expiry and scopes. Returns a dict with what it found."""
    access_token, ig_user_id = get_credentials()
    host, version, base_url = get_config()
    _log(f"Using {host} {version}")

    user = _request(
        "GET", f"{base_url}/{ig_user_id}", access_token, f"GET /{ig_user_id}",
        params={"fields": "id,username", "access_token": access_token},
    )
    info = {"id": user.get("id"), "username": user.get("username"), "expires_at": None, "scopes": None}
    _log(f"Account: @{info['username']} (id {info['id']})")

    if host != "graph.facebook.com":
        _log("Token expiry and scopes: not available (debug_token only exists on graph.facebook.com)")
        return info

    token_info = _request(
        "GET", f"{base_url}/debug_token", access_token, "GET /debug_token",
        params={"input_token": access_token, "access_token": access_token},
    ).get("data", {})

    if not token_info.get("is_valid"):
        raise IGPublishError("Access token is not valid", response_json=token_info)

    expires_at = token_info.get("expires_at", 0)
    info["expires_at"] = expires_at
    info["scopes"] = token_info.get("scopes", [])
    if expires_at:
        _log(f"Token expires: {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(expires_at))}")
    else:
        _log("Token expires: never")
    _log(f"Scopes: {', '.join(info['scopes']) or '(none)'}")
    if "instagram_content_publish" not in info["scopes"]:
        _log("WARNING: token lacks instagram_content_publish; publishing will fail")
    return info


def _create_container(params):
    access_token, ig_user_id = get_credentials()
    _, _, base_url = get_config()
    result = _request(
        "POST", f"{base_url}/{ig_user_id}/media", access_token, f"POST /{ig_user_id}/media",
        data={**params, "access_token": access_token},
    )
    if "id" not in result:
        raise IGPublishError("Container creation returned no id", response_json=result)
    _log(f"Container created: {result['id']}")
    return result


def _upload_file(upload_uri, path):
    access_token, _ = get_credentials()
    file_size = os.path.getsize(path)
    _log(f"Uploading {file_size / 1024 / 1024:.1f} MB to rupload...")
    with open(path, "rb") as f:
        result = _request(
            "POST", upload_uri, access_token, "POST rupload",
            headers={
                "Authorization": f"OAuth {access_token}",
                "offset": "0",
                "file_size": str(file_size),
            },
            data=f,
            timeout=UPLOAD_TIMEOUT,
        )
    if not result.get("success"):
        _log(f"rupload response:\n{_redact(json.dumps(result, indent=2), access_token)}")
        debug_message = result.get("debug_info", {}).get("message", "see logged response")
        raise IGPublishError(f"Upload failed: {debug_message}", response_json=result)
    _log("Upload complete")


def wait_for_container(container_id, interval=POLL_INTERVAL, timeout=POLL_TIMEOUT):
    """Polls the container until FINISHED. Raises on ERROR, EXPIRED or timeout.

    Transient failures (network errors, timeouts, 5xx) are retried with backoff,
    up to MAX_POLL_RETRIES in a row; the count resets after any successful poll.
    """
    access_token, _ = get_credentials()
    _, _, base_url = get_config()
    deadline = time.monotonic() + timeout
    failures = 0

    while True:
        try:
            result = _request(
                "GET", f"{base_url}/{container_id}", access_token, f"GET /{container_id}",
                params={"fields": "status_code,status", "access_token": access_token},
            )
        except IGPublishError as e:
            if not e.transient:
                raise
            failures += 1
            if failures > MAX_POLL_RETRIES:
                raise IGPublishError(
                    f"Gave up polling container {container_id} after {failures} consecutive failures: {e}",
                    status_code=e.status_code, response_json=e.response_json, transient=True,
                ) from None
            backoff = interval * 2 ** (failures - 1)
            if time.monotonic() + backoff > deadline:
                raise IGPublishError(
                    f"Timed out after {timeout}s waiting for container {container_id} (last error: {e})",
                    status_code=e.status_code, response_json=e.response_json, transient=True,
                ) from None
            _log(f"Transient poll failure {failures}/{MAX_POLL_RETRIES}: {e}. Retrying in {backoff}s")
            time.sleep(backoff)
            continue

        failures = 0
        status_code = result.get("status_code")
        _log(f"Container status: {status_code} ({result.get('status', '')})")

        if status_code in ("FINISHED", "PUBLISHED"):
            return result
        if status_code in ("ERROR", "EXPIRED"):
            _log(f"Container {container_id} failed:\n{json.dumps(result, indent=2)}")
            raise IGPublishError(
                f"Container {status_code}: {result.get('status', 'no status message')}",
                response_json=result,
            )
        if time.monotonic() + interval > deadline:
            raise IGPublishError(
                f"Timed out after {timeout}s waiting for container {container_id} (last status: {status_code})",
                response_json=result,
            )
        time.sleep(interval)


def publish_container(container_id):
    access_token, ig_user_id = get_credentials()
    _, _, base_url = get_config()
    result = _request(
        "POST", f"{base_url}/{ig_user_id}/media_publish", access_token, f"POST /{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": access_token},
    )
    if "id" not in result:
        raise IGPublishError("media_publish returned no id", response_json=result)
    _log(f"Reel published. IG media ID: {result['id']}")
    return result["id"]


def check_video_file(path):
    """Raises IGPublishError unless `path` is a file of 1 byte to 300 MB."""
    if not os.path.isfile(path):
        raise IGPublishError(f"Video file not found: {path}")
    file_size = os.path.getsize(path)
    if file_size == 0 or file_size > MAX_REEL_BYTES:
        raise IGPublishError(f"Video must be between 1 byte and 300 MB, got {file_size} bytes")


def publish_reel_local(path, caption, publish=True):
    """Publishes a local video file as a Reel using resumable upload.

    Returns the IG media ID, or the container ID if publish=False (dry run).
    Not available on graph.instagram.com (Instagram Login); use PUBLISH_MODE=ngrok there.
    """
    check_video_file(path)

    host, version, _ = get_config()
    if host == "graph.instagram.com":
        # Confirmed by a dry run: Meta answers upload_type=resumable with
        # "The parameter video_url is required" (code 100).
        raise IGPublishError(
            "Resumable upload is not supported on graph.instagram.com (Instagram Login). "
            "Set PUBLISH_MODE=ngrok in .env to publish via a public video URL instead.")
    if host != "graph.facebook.com":
        _log(f"WARNING: resumable upload is only documented for graph.facebook.com, not {host}. "
             "If it fails, set PUBLISH_MODE=ngrok.")

    container = _step("container", _create_container,
                      {"media_type": "REELS", "upload_type": "resumable", "caption": caption})
    container_id = container["id"]
    upload_uri = container.get("uri")
    if not upload_uri:
        # Documented format; only used if Meta ever omits the uri field.
        upload_uri = f"https://rupload.facebook.com/ig-api-upload/{version}/{container_id}"
        _log(f"No upload uri in response; using {upload_uri}")

    _step("upload", _upload_file, upload_uri, path)
    _step("poll", wait_for_container, container_id)

    if not publish:
        _log(f"Dry run: container {container_id} is FINISHED; skipping media_publish")
        return container_id
    return _step("publish", publish_container, container_id)


def publish_reel(video_url, caption, publish=True):
    """Publishes a Reel from a publicly reachable URL (the ngrok path).

    Returns the IG media ID, or the container ID if publish=False (dry run).
    The URL must stay reachable until polling finishes, since Meta fetches it then.
    """
    container = _step("container", _create_container,
                      {"media_type": "REELS", "video_url": video_url, "caption": caption})
    container_id = container["id"]
    _step("poll", wait_for_container, container_id)

    if not publish:
        _log(f"Dry run: container {container_id} is FINISHED; skipping media_publish")
        return container_id
    return _step("publish", publish_container, container_id)


def get_recent_analytics():
    """Fetches analytics for recent posts. Returns the list, or None on failure."""
    try:
        access_token, ig_user_id = get_credentials()
        _, _, base_url = get_config()
        response = _request(
            "GET", f"{base_url}/{ig_user_id}/media", access_token, f"GET /{ig_user_id}/media",
            params={"fields": "id,caption,media_type,like_count,comments_count", "access_token": access_token},
        )
        posts = response.get("data", [])
        _log("RECENT CHANNEL ANALYTICS:")
        for post in posts[:3]:
            caption_preview = (post.get("caption") or "No caption")[:30].replace("\n", " ")
            _log(f"- [{post.get('media_type')}] Likes: {post.get('like_count')} | "
                 f"Comments: {post.get('comments_count')} | Caption: {caption_preview}...")
        return posts
    except IGPublishError as e:
        _log(f"Could not fetch analytics: {e}")
        return None


if __name__ == "__main__":
    # Read-only check: verifies credentials and reads analytics. Never publishes.
    try:
        verify_credentials()
        get_recent_analytics()
    except IGPublishError as e:
        _log(f"Setup error: {e}")
