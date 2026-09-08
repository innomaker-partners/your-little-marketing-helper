#!/usr/bin/env python3
"""Make.com webhook proxy caller for the workflow-workarounds plugin.

Sends structured requests to a Make.com universal proxy scenario that
delegates authenticated API calls to Microsoft Graph and Meta.

Usage:
    python3 platform_request.py --webhook-url URL --share-token TOKEN \\
        --platform microsoft --endpoint /me/calendar/events --method GET \\
        --params '{"startDateTime": "2026-08-01"}'

    python3 platform_request.py --webhook-url URL --share-token TOKEN --test
    python3 platform_request.py --webhook-url URL --share-token TOKEN --verbose ...
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from urllib.parse import urlencode, quote


def build_payload(platform, endpoint, method, params, share_token):
    """Construct the webhook request payload (Path B: params pre-encoded).

    The caller passes params as a flat JSON object (the same friendly `--params`
    CLI shape as before). This function reshapes them onto the wire so that the
    Make.com proxy never has to map a query-parameter array -- the trap this
    avoids: the inner-pair key name varies per Make module (`{name, value}` for
    the HTTP module, `{key, value}` for the Microsoft modules), so a hand-mapped
    array is a coin-flip that drops params silently with no error.

    - GET/HEAD/DELETE: params are URL-encoded and appended to `endpoint` as a
      query string. The blueprint's `url` mapper (`base{{1.endpoint}}`) forwards
      the whole URL untouched -- no query-parameter field is mapped at all.
    - POST/PUT/PATCH: params are serialized to a JSON string and sent as `body`,
      which a Make `http:MakeRequest` module maps via contentType=json /
      inputMethod=jsonString / jsonStringBodyContent (SKILL.md). This POST leg is
      designed against the confirmed module schema but is not yet exercised
      end-to-end -- it is a gated write (calendar.events.create) pending owner
      consent for a live run.

    The wire payload carries `endpoint` (with any GET query string), `method`,
    and `body` (empty for GET) -- there is deliberately no separate `params`
    field, because Path B leaves nothing for the proxy to reshape.

    Encoding note: values are single-encoded with `urllib.parse.quote` (space ->
    %20). Whether Make's v4 URL field re-encodes an already-encoded query string
    (double-encoding) can only be confirmed by a live run; if it is ever observed,
    the fallback is a `queryParameters` array with `{name, value}` pairs.
    """
    method = method.upper()
    params_obj = json.loads(params) if isinstance(params, str) else params
    params_obj = params_obj or {}

    body = ""
    if method in ("POST", "PUT", "PATCH"):
        body = json.dumps(params_obj) if params_obj else ""
    elif params_obj:
        separator = "&" if "?" in endpoint else "?"
        endpoint = endpoint + separator + urlencode(params_obj, quote_via=quote)

    return {
        "platform": platform,
        "endpoint": endpoint,
        "method": method,
        "body": body,
        "share_token": share_token,
    }


ERROR_TAXONOMY = {
    401: {"default": "oauth_token_expired"},
    403: {"default": "missing_permission_scope"},
    400: {"default": "malformed_request"},
    429: {"default": "rate_limited"},
}

# Microsoft Graph embeds a string 'code' in the error body when stopOnHttpError:false
# causes a real 4xx to arrive wrapped in HTTP 200. These strings map to the integer
# HTTP statuses that ERROR_TAXONOMY is keyed on.
#
# Keys are lowercased. The lookup site calls .lower() on the code string so casing in
# the API response can never cause a miss. Microsoft Graph uses camelCase for most codes
# (accessDenied, badRequest) but some services and versions use PascalCase variants
# (Unauthorized, Forbidden). A case-insensitive table covers both without accumulating
# near-duplicate keys.
#
# A code is mapped only when the diagnosis string's implied advice matches what that code
# actually calls for. Deliberately excluded: quotaLimitReached (storage exhaustion, not
# throttling), notAllowed/notSupported (system restriction, not a missing scope),
# itemNotFound, generalException, and the rest of the 15-code documented set.
_MICROSOFT_CODE_TO_STATUS = {
    # 401: token expired or invalid
    "unauthenticated": 401,
    "unauthorized": 401,
    "invalidauthenticationtoken": 401,
    # 403: caller lacks required permission
    "accessdenied": 403,
    "erroraccessdenied": 403,
    "forbidden": 403,
    # 400: malformed request
    "badrequest": 400,
    "invalidrequest": 400,
    # 429: rate limited
    "toomanyrequests": 429,
    "activitylimitreached": 429,
    "throttledrequest": 429,
}

# Meta Graph API type-based fallback table. Used when no numeric code is present, or the
# numeric code is not in the documented set. Meta's numeric code is the authoritative signal
# (see meta branch in _extract_embedded_status), but 'type' provides a coarse classification
# when no specific code is known. Narrowed to entries where the implied advice is correct
# regardless of which numeric code is absent.
_META_ERROR_TYPE_TO_STATUS = {
    "OAuthException": 401,          # expired or revoked token (no subcode present)
    "OAuthAccessTokenException": 401,
    "GraphMethodException": 400,    # method or parameter error
}


def _extract_embedded_status(parsed_body, platform):
    """Extract a synthetic HTTP-like status from a 200-wrapped platform error body.

    When Make.com's stopOnHttpError:false is active, real platform 4xx/5xx errors
    arrive wrapped in HTTP 200. The error body contains platform-specific codes that
    we translate to HTTP-like integers, allowing diagnose_error() to return a
    taxonomy-mapped string instead of the fallback 'http_200'.

    Returns an int status code on successful extraction, or None to fall back to the
    real HTTP status (200) when the body shape is unrecognised.
    """
    if not isinstance(parsed_body, dict):
        return None
    error = parsed_body.get("error")
    if not isinstance(error, dict):
        # error is truthy but not a dict (e.g. a plain string): no structured code to extract
        return None

    if platform == "microsoft":
        code_str = error.get("code", "")
        return _MICROSOFT_CODE_TO_STATUS.get(code_str.lower())

    if platform == "meta":
        code = error.get("code")
        type_str = error.get("type", "")
        # Numeric code is the authoritative signal. Meta's documentation distinguishes
        # OAuthException code 190 (expired token), code 10/200-299 (missing permission),
        # and code 17/341 (rate limit) using the numeric code, not the type string.
        # The type field is only a coarse family that cannot distinguish these cases.
        if isinstance(code, int):
            if code == 190:
                return 401
            if code in (4, 10) or (200 <= code <= 299):
                return 403
            if code in (17, 341):
                return 429
            if code == 100:
                return 400
            # code is present but not in the documented set: fall through to type table
        # Type table: used when no numeric code is present, or the numeric code is not
        # in the documented set above.
        return _META_ERROR_TYPE_TO_STATUS.get(type_str)

    return None


def diagnose_error(status_code, body, platform):
    """Map an HTTP error to a human-actionable diagnosis string."""
    body_lower = body.lower()
    if "share_token" in body_lower or "unauthorized" in body_lower or "token invalid" in body_lower:
        return "share_token_mismatch"
    if "operations limit" in body_lower:
        return "operations_limit_exceeded"
    taxonomy_entry = ERROR_TAXONOMY.get(status_code, {})
    return taxonomy_entry.get(platform, taxonomy_entry.get("default", f"http_{status_code}"))


def send_request(webhook_url, payload, verbose=False):
    """POST the payload to the Make.com webhook, return structured result."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    if verbose:
        # Build a display-only copy: redact the token VALUE but preserve its length
        # so the short-token diagnostic (short token from shell variable
        # expansion) remains usable. The `req` object above was already built from the
        # original payload and is unchanged -- the true token is what gets transmitted.
        display_payload = dict(payload)
        token_val = display_payload.get("share_token", "")
        display_payload["share_token"] = f"<redacted: {len(token_val)} chars>"
        # Redact the hook ID (final path segment of the URL). The hook ID and the
        # share token together form a complete usable credential pair, so printing one
        # without the other is partial protection at best. Keep the host visible so the
        # user can confirm they are hitting the right Make.com region/endpoint.
        url_parts = webhook_url.rsplit("/", 1)
        if len(url_parts) == 2 and url_parts[1]:
            redacted_url = url_parts[0] + f"/<hook-id-redacted: {len(url_parts[1])} chars>"
        else:
            redacted_url = webhook_url
        print(f"POST {redacted_url}", file=sys.stderr)
        print(f"Body: {json.dumps(display_payload, indent=2)}", file=sys.stderr)

    try:
        with urllib.request.urlopen(req) as resp:
            body_bytes = resp.read()
            body_str = body_bytes.decode("utf-8")
            if verbose:
                print(f"Status: {resp.status}", file=sys.stderr)
                print(f"Response: {body_str}", file=sys.stderr)
            try:
                parsed_body = json.loads(body_str) if body_str.strip() else {}
            except json.JSONDecodeError:
                parsed_body = body_str
            # Fix (a): 2xx body that is a dict with a truthy top-level 'error' key is a platform
            # error disguised behind a 200 (consequence of stopOnHttpError: false in the blueprint).
            # Guard is narrow: dict only, and error value must be truthy. Null/false pass through.
            if isinstance(parsed_body, dict) and parsed_body.get("error"):
                platform = payload.get("platform", "")
                # Bridge: extract an embedded platform error code to drive diagnosis. Without this,
                # diagnose_error(200, ...) has no ERROR_TAXONOMY entry and falls through to 'http_200'
                # regardless of the actual cause (e.g. a real Graph 403 for a missing scope).
                embedded_status = _extract_embedded_status(parsed_body, platform)
                effective_status = embedded_status if embedded_status is not None else resp.status
                # When an embedded platform status was extracted, we know the request reached the
                # platform and returned a structured platform error. diagnose_error()'s body-string
                # guards exist to catch Make.com-level failures (share-token rejection, operations
                # limit) and can false-positive on platform error code values such as "Unauthorized".
                # Pass an empty body so the extracted status is the sole signal.
                # Make-level failures produce non-dict error values and so return embedded_status=None,
                # leaving effective_body=body_str and the guards intact for those cases.
                effective_body = "" if embedded_status is not None else body_str
                diagnosis = diagnose_error(effective_status, effective_body, platform)
                return {"error": True, "status_code": resp.status, "body": parsed_body, "diagnosis": diagnosis}
            # Fix (b): exact "Accepted" string body means Make.com's gateway answered before any
            # Webhook Response module ran: no router branch matched. Guard is exact-match only;
            # substrings (e.g. {"subject": "Accepted: Q3 review"}) are JSON and parse as dicts,
            # never reaching this branch.
            if isinstance(parsed_body, str) and parsed_body.strip() == "Accepted":
                diagnosis = diagnose_error(resp.status, body_str, payload.get("platform", ""))
                return {"error": True, "status_code": resp.status, "body": parsed_body, "diagnosis": diagnosis}
            return {"error": False, "status_code": resp.status, "body": parsed_body}
    except urllib.error.HTTPError as e:
        error_body = ""
        if e.fp:
            try:
                error_body = e.fp.read().decode("utf-8")
            except Exception:
                error_body = str(e)
        diagnosis = diagnose_error(e.code, error_body, payload.get("platform", ""))
        if verbose:
            print(f"Status: {e.code}", file=sys.stderr)
            print(f"Response: {error_body}", file=sys.stderr)
        return {"error": True, "status_code": e.code, "body": error_body, "diagnosis": diagnosis}
    except urllib.error.URLError as e:
        if verbose:
            print(f"Connection error: {e.reason}", file=sys.stderr)
        return {"error": True, "status_code": 0, "body": str(e.reason), "diagnosis": "connection_failed"}


def test_connection(webhook_url, share_token, verbose=False):
    """Health check: verify webhook reachability, token acceptance, scenario activity."""
    payload = {
        "platform": "test",
        "endpoint": "/__health",
        "method": "GET",
        "params": {},
        "share_token": share_token,
    }
    result = send_request(webhook_url, payload, verbose=verbose)

    return {
        "webhook_reachable": result["status_code"] != 0,
        "share_token_accepted": result.get("diagnosis") != "share_token_mismatch",
        "scenario_active": not result["error"],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Make.com webhook proxy caller for workflow-workarounds"
    )
    parser.add_argument("--webhook-url", required=True, help="Make.com webhook endpoint URL")
    parser.add_argument("--share-token", required=True, help="Shared secret for webhook auth")
    parser.add_argument("--platform", default=None, help="Target platform: microsoft or meta")
    parser.add_argument("--endpoint", default=None, help="API endpoint path (e.g. /me/calendar/events)")
    parser.add_argument("--method", default="GET", choices=["GET", "POST"], type=str.upper,
                        help="HTTP method: GET or POST (default: GET)")
    parser.add_argument("--params", default="{}", help="JSON string of request parameters")
    parser.add_argument("--test", action="store_true", help="Run connection health check")
    parser.add_argument("--verbose", action="store_true", help="Print request/response details to stderr")

    args = parser.parse_args()

    if not args.webhook_url.strip():
        parser.error("--webhook-url cannot be empty. Check the value in your .env file.")
    if not args.share_token.strip():
        parser.error("--share-token cannot be empty. Check the value in your .env file.")

    if args.test:
        checks = test_connection(args.webhook_url, args.share_token, verbose=args.verbose)
        for check_name, passed in checks.items():
            status = "PASS" if passed else "FAIL"
            print(f"{check_name}: {status}")
        sys.exit(0 if all(checks.values()) else 1)

    if not args.platform or not args.endpoint:
        parser.error("--platform and --endpoint are required (unless using --test)")

    try:
        payload = build_payload(args.platform, args.endpoint, args.method, args.params, args.share_token)
    except json.JSONDecodeError as e:
        parser.error(f"--params is not valid JSON: {e}")
    result = send_request(args.webhook_url, payload, verbose=args.verbose)
    print(json.dumps(result, indent=2))
    sys.exit(0 if not result["error"] else 1)


if __name__ == "__main__":
    main()
