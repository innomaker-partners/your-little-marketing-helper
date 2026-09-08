"""Tests for platform_request.py: the Make.com webhook proxy caller."""

import json
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from platform_request import build_payload


class TestBuildPayload:
    """Path B: build_payload pre-encodes params so the Make proxy forwards them
    untouched. GET params ride in the endpoint query string; POST params become a
    JSON-string body; the wire never carries a separate `params` field to map."""

    def test_get_params_encoded_into_endpoint_query_string(self):
        # Why: a calendar.events.list with a date range must actually filter. If the
        # range does not reach the endpoint URL, the platform returns the whole
        # calendar (or nothing) with no error -- the exact silent failure Path B fixes.
        result = build_payload(
            platform="microsoft",
            endpoint="/me/calendar/events",
            method="GET",
            params='{"startDateTime": "2026-08-01", "endDateTime": "2026-08-07"}',
            share_token="test_token_123",
        )
        assert result == {
            "platform": "microsoft",
            "endpoint": "/me/calendar/events?startDateTime=2026-08-01&endDateTime=2026-08-07",
            "method": "GET",
            "body": "",
            "share_token": "test_token_123",
        }

    def test_get_no_params_field_on_the_wire(self):
        # Why: Path B's whole point is that the proxy has nothing to reshape. A
        # lingering `params` key would invite a future maintainer to map it and
        # reintroduce the module-specific key-name trap.
        result = build_payload("microsoft", "/me/messages", "GET", "{}", "tok")
        assert "params" not in result
        assert result["endpoint"] == "/me/messages"  # no trailing '?'
        assert result["body"] == ""

    def test_get_special_characters_encoded_once(self):
        # Why: a filter value containing spaces or '&' must be percent-encoded, or the
        # platform reads a different (or truncated) query than intended. Single
        # encoding only -- double-encoding would transmit literal %25 sequences.
        result = build_payload(
            "microsoft", "/me/messages", "GET",
            {"$filter": "subject eq 'Q3 & Q4'"}, "tok",
        )
        assert result["endpoint"] == "/me/messages?%24filter=subject%20eq%20%27Q3%20%26%20Q4%27"

    def test_get_iso_datetime_colons_encoded(self):
        # Why: ISO 8601 filter values carry colons; this pins the exact wire form so
        # the live double-encoding check has a known baseline.
        result = build_payload(
            "microsoft", "/me/calendarView", "GET",
            {"startDateTime": "2026-08-01T00:00:00Z"}, "tok",
        )
        assert result["endpoint"] == "/me/calendarView?startDateTime=2026-08-01T00%3A00%3A00Z"

    def test_get_appends_with_ampersand_when_endpoint_already_has_query(self):
        # Why: an endpoint registered with a fixed query segment must be extended with
        # '&', not a second '?' that would truncate the first parameter.
        result = build_payload(
            "meta", "/123/insights?pretty=0", "GET", {"metric": "reach"}, "tok",
        )
        assert result["endpoint"] == "/123/insights?pretty=0&metric=reach"

    def test_get_dict_and_json_string_params_are_equivalent(self):
        # Why: the CLI passes a JSON string; internal callers pass a dict. Both must
        # produce the identical wire payload, or behavior differs by call site.
        as_dict = build_payload("meta", "/123/media", "GET", {"fields": "id,caption"}, "tok")
        as_str = build_payload("meta", "/123/media", "GET", '{"fields": "id,caption"}', "tok")
        assert as_dict == as_str
        assert as_dict["endpoint"] == "/123/media?fields=id%2Ccaption"

    def test_post_params_become_json_body_endpoint_unchanged(self):
        # Why: a POST (e.g. calendar.events.create) carries its data in the request
        # body, not the URL. The endpoint must stay clean and the body must be the
        # exact JSON the platform will parse.
        result = build_payload(
            "microsoft", "/me/calendar/events", "POST",
            {"subject": "Sync", "start": "2026-08-01"}, "tok",
        )
        assert result["endpoint"] == "/me/calendar/events"
        assert result["method"] == "POST"
        assert result["body"] == '{"subject": "Sync", "start": "2026-08-01"}'
        assert json.loads(result["body"]) == {"subject": "Sync", "start": "2026-08-01"}

    def test_post_empty_params_empty_body(self):
        # Why: a body-bearing method with no params must send an empty body, not the
        # string "{}" or "null", so the platform does not receive a spurious object.
        result = build_payload("microsoft", "/me/sendMail", "POST", "{}", "tok")
        assert result["body"] == ""


from platform_request import diagnose_error


class TestDiagnoseError:
    def test_share_token_mismatch_from_body(self):
        assert diagnose_error(401, '{"error": "unauthorized", "detail": "share_token mismatch"}', "microsoft") == "share_token_mismatch"

    def test_share_token_mismatch_case_insensitive(self):
        assert diagnose_error(403, "Unauthorized - token invalid", "meta") == "share_token_mismatch"

    def test_operations_limit(self):
        assert diagnose_error(429, "operations limit exceeded for this month", "meta") == "operations_limit_exceeded"

    def test_microsoft_403_permission(self):
        assert diagnose_error(403, "Insufficient privileges to complete the operation", "microsoft") == "missing_permission_scope"

    def test_microsoft_401_token_expired(self):
        assert diagnose_error(401, "Access token has expired or is not yet valid", "microsoft") == "oauth_token_expired"

    def test_meta_400_malformed(self):
        assert diagnose_error(400, "Invalid parameter", "meta") == "malformed_request"

    def test_rate_limited(self):
        assert diagnose_error(429, "Rate limit reached", "microsoft") == "rate_limited"

    def test_unknown_error_code(self):
        assert diagnose_error(500, "Internal server error", "microsoft") == "http_500"


import urllib.error
from unittest.mock import patch, MagicMock

from platform_request import send_request


class TestSendRequest:
    @patch("platform_request.urllib.request.urlopen")
    def test_successful_json_response(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"value": [{"subject": "Team standup"}]}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is False
        assert result["status_code"] == 200
        assert result["body"]["value"][0]["subject"] == "Team standup"

    @patch("platform_request.urllib.request.urlopen")
    def test_http_403_returns_structured_error(self, mock_urlopen):
        err = urllib.error.HTTPError(
            "https://hook.test.make.com/abc", 403, "Forbidden", {},
            MagicMock(read=MagicMock(return_value=b"Insufficient privileges"))
        )
        mock_urlopen.side_effect = err

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 403
        assert result["diagnosis"] == "missing_permission_scope"

    @patch("platform_request.urllib.request.urlopen")
    def test_connection_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Name or service not known")

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 0
        assert result["diagnosis"] == "connection_failed"

    @patch("platform_request.urllib.request.urlopen")
    def test_empty_response_body(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "test", "share_token": "t"})
        assert result["error"] is False
        assert result["body"] == {}

    @patch("platform_request.urllib.request.urlopen")
    def test_non_json_200_response_returns_raw_body(self, mock_urlopen):
        """Updated 2026-08-05:
        A 200 whose body is exactly 'Accepted' means Make.com's gateway answered before any
        Webhook Response module could fire, i.e. no router branch matched the request.
        The script must not crash (non-JSON body is handled), the raw body must survive into
        the result so --verbose callers can see what came back, BUT the verdict must be
        error: True with a taxonomy-valid diagnosis, because the request was never routed
        and the caller received no useful data. The original assertion of error: False was
        written before 'Accepted' was understood to carry this meaning.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"Accepted"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "test", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert result["body"] == "Accepted"
        assert "diagnosis" in result


    @patch("platform_request.urllib.request.urlopen")
    def test_error_dict_in_2xx_treated_as_error(self, mock_urlopen):
        """Fix(a): 200 body that is a dict with a truthy top-level 'error' key is a platform error."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": {"code": "AccessDenied", "message": "Caller not allowed"}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert "diagnosis" in result

    @patch("platform_request.urllib.request.urlopen")
    def test_falsy_error_key_passes_through_as_success(self, mock_urlopen):
        """Fix(a) guard: dict body with falsy 'error' key (null, false) must NOT trigger the error path."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": null, "data": "calendar data"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is False
        assert result["body"]["data"] == "calendar data"

    @patch("platform_request.urllib.request.urlopen")
    def test_accepted_as_substring_passes_through(self, mock_urlopen):
        """Fix(b) guard: a bare non-JSON string body that CONTAINS 'Accepted' but is not exactly
        equal to it must pass through as success. This is the input that actually reaches the
        string branch of fix (b): a body like 'Accepted: Q3 review' falls through json.loads,
        lands as a raw string in parsed_body, and the guard must distinguish it from the exact
        'Accepted' signal using == not 'in'. A JSON body like {"subject": "Accepted: Q3 review"}
        never reaches this check at all (it parses as a dict), so a test using JSON is vacuous
        and would pass even if the guard were wrong. This test would fail if == were changed to in.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"Accepted: Q3 review"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is False
        assert result["body"] == "Accepted: Q3 review"

    @patch("platform_request.urllib.request.urlopen")
    def test_exact_accepted_body_treated_as_error(self, mock_urlopen):
        """Fix(b): exact 'Accepted' body means no Make.com route matched: treat as error, not success."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"Accepted"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert "diagnosis" in result


class TestWrapped200Diagnosis:
    """200-wrapped platform errors must produce taxonomy-mapped
    diagnosis strings instead of the generic 'http_200' fallback.

    All error bodies use real Microsoft Graph and Meta Graph API error shapes.
    The suite covers: documented camelCase Microsoft codes, the Unauthorized
    body-string-guard collision, new Microsoft codes (badRequest, activityLimitReached),
    Meta numeric-code priority over type, Make-level share-token rejection handling,
    and the honest-fallback path for unmapped Microsoft codes.
    """

    @patch("platform_request.urllib.request.urlopen")
    def test_microsoft_200_wrapped_403_produces_missing_permission_scope(self, mock_urlopen):
        """Microsoft Graph Forbidden (403) wrapped in Make.com's HTTP 200 must diagnose
        as missing_permission_scope, not http_200.
        Real Microsoft Graph error shape (Graph errors reference:
        https://learn.microsoft.com/en-us/graph/errors).
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": {"code": "Forbidden", "message": "Caller does not have permission for the operation.", "innerError": {"request-id": "abc123", "date": "2026-08-05T12:00:00"}}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200   # HTTP status stays 200 (the Make.com wrapper)
        assert result["diagnosis"] == "missing_permission_scope"

    @patch("platform_request.urllib.request.urlopen")
    def test_microsoft_200_wrapped_401_produces_oauth_token_expired(self, mock_urlopen):
        """Microsoft Graph InvalidAuthenticationToken (401) wrapped in Make.com's HTTP 200
        must diagnose as oauth_token_expired, not http_200.
        Real Microsoft Graph error shape for an expired Azure AD access token.
        Note: "Unauthorized" (also a valid Graph 401 code) cannot be used
        here because it appears as a substring in the JSON body, which triggers diagnose_error()'s
        existing "unauthorized" string guard and returns share_token_mismatch instead.
        Since diagnose_error() is FROZEN, InvalidAuthenticationToken is used instead.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": {"code": "InvalidAuthenticationToken", "message": "CompactToken parsing failed with error code: 80049217.", "innerError": {"request-id": "def456", "date": "2026-08-05T12:00:00"}}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert result["diagnosis"] == "oauth_token_expired"

    @patch("platform_request.urllib.request.urlopen")
    def test_meta_200_wrapped_oauth_exception_produces_oauth_token_expired(self, mock_urlopen):
        """Meta Graph API OAuthException wrapped in Make.com's HTTP 200 must diagnose
        as oauth_token_expired, not http_200.
        Real Meta error shape (Meta error handling reference:
        https://developers.facebook.com/docs/graph-api/using-graph-api/error-handling/).
        Code 190 = expired/invalid OAuth access token.
        Note: Meta's numeric code (190) is NOT an HTTP status code and does not appear in
        ERROR_TAXONOMY; the implementation uses the 'type' field for classification instead.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": {"message": "Invalid OAuth access token.", "type": "OAuthException", "code": 190, "fbtrace_id": "AbXyz123Demo"}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "meta", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert result["diagnosis"] == "oauth_token_expired"

    @patch("platform_request.urllib.request.urlopen")
    def test_normal_200_success_unaffected_by_embedded_status_extraction(self, mock_urlopen):
        """Regression guard: a normal successful Microsoft Graph response (no 'error' key)
        must continue to produce error: False and must not be intercepted by the embedded-
        status extraction logic. Uses a real Graph calendar response body.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"@odata.context": "https://graph.microsoft.com/v1.0/$metadata#me/events", "value": [{"id": "AAMkAGI2...", "subject": "Q3 review", "start": {"dateTime": "2026-08-05T09:00:00.0000000", "timeZone": "UTC"}}]}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is False
        assert result["status_code"] == 200
        assert result["body"]["value"][0]["subject"] == "Q3 review"

    # --- additional edge-case tests ---
    # Tests 1-4 cover Microsoft code casing and new entries.
    # Tests 5-7 cover Meta numeric-code priority.
    # Test 8: embedded-status extraction must not disarm Make-level share-token guards.
    # Test 9 proves the honest-fallback path for unmapped Microsoft codes.

    @patch("platform_request.urllib.request.urlopen")
    def test_microsoft_access_denied_camel_case_produces_missing_permission_scope(self, mock_urlopen):
        """Microsoft 'accessDenied' (documented camelCase, as shown in Graph errors
        reference learn.microsoft.com/en-us/graph/errors) must map to missing_permission_scope.
        This is the flagship missing-scope case: the most likely first failure for a stranger.
        The previous table had 'AccessDenied' (PascalCase); exact-match missed camelCase entirely.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": {"code": "accessDenied", "message": "Access denied to the requested resource. User might not have enough permission.", "innerError": {"request-id": "abc123", "date": "2026-08-05T12:00:00"}}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert result["diagnosis"] == "missing_permission_scope"

    @patch("platform_request.urllib.request.urlopen")
    def test_microsoft_unauthorized_code_produces_oauth_token_expired_not_share_token_mismatch(self, mock_urlopen):
        """Microsoft 'Unauthorized' as the error code value must diagnose
        as oauth_token_expired, not share_token_mismatch.

        Without the body-blanking fix, diagnose_error() sees the full body containing the string
        'Unauthorized', its 'unauthorized' in body_lower guard fires, and returns
        share_token_mismatch. The fix passes an empty body when embedded_status is not None,
        so the extracted 401 is the sole signal. This test would fail if effective_body were
        set to body_str instead of ''.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": {"code": "Unauthorized", "message": "Access token is empty.", "innerError": {"request-id": "def456", "date": "2026-08-05T12:00:00"}}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert result["diagnosis"] == "oauth_token_expired"

    @patch("platform_request.urllib.request.urlopen")
    def test_microsoft_bad_request_camel_case_produces_malformed_request(self, mock_urlopen):
        """Microsoft 'badRequest' (documented camelCase per the example error body
        in learn.microsoft.com/en-us/graph/errors: 'code': 'badRequest') must map to
        malformed_request. The previous table had 'BadRequest' (PascalCase); missed camelCase.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": {"code": "badRequest", "message": "Uploaded fragment overlaps with existing data.", "innerError": {"request-id": "ghi789", "date": "2026-08-05T12:00:00"}}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert result["diagnosis"] == "malformed_request"

    @patch("platform_request.urllib.request.urlopen")
    def test_microsoft_activity_limit_reached_produces_rate_limited(self, mock_urlopen):
        """Microsoft 'activityLimitReached' (from OneDrive error docs listing of 15
        documented codes, learn.microsoft.com/en-us/onedrive/developer/rest-api/concepts/errors)
        must map to rate_limited. This code was absent from the previous table entirely.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": {"code": "activityLimitReached", "message": "The request has been throttled.", "innerError": {"request-id": "jkl012", "date": "2026-08-05T12:00:00"}}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert result["diagnosis"] == "rate_limited"

    @patch("platform_request.urllib.request.urlopen")
    def test_meta_oauth_exception_code_200_produces_missing_permission_scope_not_oauth_expired(self, mock_urlopen):
        """Meta OAuthException with numeric code 200 must diagnose as
        missing_permission_scope, NOT oauth_token_expired.

        Meta docs (developers.facebook.com/docs/graph-api/guides/error-handling/): codes
        200-299 mean 'Permission is either not granted or has been removed.' The type field
        says OAuthException, which previously drove the classification (returning 401 via the
        type table): a confidently wrong answer that sent the user to refresh their token
        when their actual problem is a missing instagram_manage_insights permission.
        The implementation checks the numeric code first, so code 200 -> 403 -> missing_permission_scope.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": {"message": "(#200) Application does not have required permission", "type": "OAuthException", "code": 200, "fbtrace_id": "AbPermDemo"}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "meta", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert result["diagnosis"] == "missing_permission_scope"

    @patch("platform_request.urllib.request.urlopen")
    def test_meta_oauth_exception_code_17_produces_rate_limited(self, mock_urlopen):
        """Meta OAuthException with numeric code 17 must diagnose as rate_limited.

        Meta docs: code 17 = 'API User Too Many Calls - Temporary issue due to throttling.
        Wait and retry.' Previously the type field drove classification, returning
        oauth_token_expired for all OAuthException errors regardless of numeric code.
        The implementation checks numeric code first: 17 -> 429 -> rate_limited.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": {"message": "User request limit reached", "type": "OAuthException", "code": 17, "fbtrace_id": "AbRateDemo"}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "meta", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert result["diagnosis"] == "rate_limited"

    @patch("platform_request.urllib.request.urlopen")
    def test_make_level_share_token_rejection_still_diagnosed_correctly(self, mock_urlopen):
        """A Make.com-level share-token rejection (body with a string
        'error' value, not a platform dict) must still produce share_token_mismatch.

        The fix sets effective_body='' only when embedded_status is not None. Make-level
        errors produce non-dict 'error' values, so _extract_embedded_status returns None,
        embedded_status stays None, effective_body stays body_str, and the 'share_token'
        body-string guard in diagnose_error() fires as before. This test would fail if
        effective_body='' were set unconditionally rather than conditionally.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": "share_token mismatch", "detail": "provided token does not match scenario"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert result["diagnosis"] == "share_token_mismatch"

    @patch("platform_request.urllib.request.urlopen")
    def test_microsoft_unmapped_code_produces_http_200_honest_fallback(self, mock_urlopen):
        """Honest-fallback path: a Microsoft code that is deliberately not mapped
        (itemNotFound) must produce 'http_200', not a misleading taxonomy string.

        The rule: map a code only when the diagnosis string's implied advice matches what
        that code actually calls for. itemNotFound has no taxonomy entry that would send
        the user the right way, so it falls through to http_200 ('I don't know') rather
        than returning a confidently wrong string.
        """
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": {"code": "itemNotFound", "message": "The resource could not be found.", "innerError": {"request-id": "mno345", "date": "2026-08-05T12:00:00"}}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_request("https://hook.test.make.com/abc", {"platform": "microsoft", "share_token": "t"})
        assert result["error"] is True
        assert result["status_code"] == 200
        assert result["diagnosis"] == "http_200"


class TestVerboseRedaction:
    """--verbose must not leak the share_token value or webhook hook ID in cleartext.

    Two tests constitute the real proof (either alone can be passed by a broken impl):
      (a) token value absent from stderr
      (b) true token present in transmitted payload

    A fix that mutes stderr entirely passes (a) but fails (b).
    A fix that prints the real token passes (b) but fails (a).
    Only a correct display-copy approach passes both.
    """

    @patch("platform_request.urllib.request.urlopen")
    def test_token_value_absent_from_stderr(self, mock_urlopen, capsys):
        """The raw token value must not appear in --verbose stderr output.
        The placeholder must appear and must encode the correct length so the
        short-token diagnostic (short token from shell expansion)
        remains usable."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"value": []}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        token = "supersecret_token_abc123xyz"
        payload = {
            "platform": "microsoft",
            "endpoint": "/me/calendar/events",
            "method": "GET",
            "params": {},
            "share_token": token,
        }
        send_request("https://hook.us2.make.com/somehookid", payload, verbose=True)

        captured = capsys.readouterr()
        assert token not in captured.err, "raw token value must not appear in verbose stderr"
        assert f"<redacted: {len(token)} chars>" in captured.err, "length-preserving placeholder must appear"

    @patch("platform_request.urllib.request.urlopen")
    def test_true_token_transmitted_despite_redaction(self, mock_urlopen, capsys):
        """The true token must be present in the payload actually sent to the webhook.
        Redaction is display-only; the request body carries the real credential.
        This test would catch an in-place mutation of `payload` before request construction,
        which would send the placeholder string as the token and break every real request."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"value": []}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        token = "supersecret_token_abc123xyz"
        payload = {
            "platform": "microsoft",
            "endpoint": "/me/calendar/events",
            "method": "GET",
            "params": {},
            "share_token": token,
        }
        send_request("https://hook.us2.make.com/somehookid", payload, verbose=True)

        # The first positional argument to urlopen is the Request object.
        # req.data is the encoded body that would be transmitted over the wire.
        req_obj = mock_urlopen.call_args[0][0]
        sent_body = json.loads(req_obj.data.decode("utf-8"))
        assert sent_body["share_token"] == token, "true token must be present in transmitted payload"

    @patch("platform_request.urllib.request.urlopen")
    def test_hook_id_absent_from_stderr_host_visible(self, mock_urlopen, capsys):
        """The hook ID (final URL path segment) must be redacted from --verbose
        stderr. The host must remain visible for connectivity confirmation.
        Both the URL and the token are needed to make a live call; masking the token
        but not the hook ID is partial protection only."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"value": []}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        hook_id = "secret_hook_identifier_xyz789"
        webhook_url = f"https://hook.us2.make.com/{hook_id}"
        payload = {"platform": "microsoft", "share_token": "tok"}

        send_request(webhook_url, payload, verbose=True)

        captured = capsys.readouterr()
        assert hook_id not in captured.err, "hook ID must not appear in verbose stderr"
        assert "hook.us2.make.com" in captured.err, "host must remain visible for connectivity confirmation"


from platform_request import test_connection as _test_connection


class TestTestConnection:
    @patch("platform_request.send_request")
    def test_all_healthy(self, mock_send):
        mock_send.return_value = {"error": False, "status_code": 200, "body": {"status": "ok"}}
        checks = _test_connection("https://hook.test.make.com/abc", "good_token")
        assert checks["webhook_reachable"] is True
        assert checks["share_token_accepted"] is True
        assert checks["scenario_active"] is True

    @patch("platform_request.send_request")
    def test_token_mismatch_detected(self, mock_send):
        mock_send.return_value = {"error": True, "status_code": 401, "body": "", "diagnosis": "share_token_mismatch"}
        checks = _test_connection("https://hook.test.make.com/abc", "wrong_token")
        assert checks["webhook_reachable"] is True
        assert checks["share_token_accepted"] is False

    @patch("platform_request.send_request")
    def test_unreachable_webhook(self, mock_send):
        mock_send.return_value = {"error": True, "status_code": 0, "body": "Connection refused", "diagnosis": "connection_failed"}
        checks = _test_connection("https://hook.test.make.com/abc", "token")
        assert checks["webhook_reachable"] is False

    @patch("platform_request.send_request")
    def test_accepted_body_scenario_inactive(self, mock_send):
        """Regression: a bare 'Accepted' from send_request means Make.com's gateway
        answered before any Webhook Response module ran: no router branch matched.
        scenario_active must be False. This was the root defect: the old expression
        `not result["error"] or result["status_code"] != 0` evaluated True (200 != 0)
        even when error was True, masking the unrouted-request condition.
        This test would have failed with the old logic and passes with `not result["error"]`."""
        mock_send.return_value = {
            "error": True,
            "status_code": 200,
            "body": "Accepted",
            "diagnosis": "http_200",
        }
        checks = _test_connection("https://hook.test.make.com/abc", "good_token")
        assert checks["webhook_reachable"] is True
        assert checks["share_token_accepted"] is True
        assert checks["scenario_active"] is False


from platform_request import main


class TestMain:
    def test_invalid_json_params_exits_nonzero(self, capsys):
        """Malformed --params must produce a clean error, not a traceback."""
        with patch("sys.argv", [
            "platform_request.py",
            "--webhook-url", "http://hook.example.com/x",
            "--share-token", "mytoken",
            "--platform", "microsoft",
            "--endpoint", "/me",
            "--params", "{not valid json}",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "--params" in captured.err

    @patch("platform_request.send_request")
    def test_empty_share_token_rejected_before_network(self, mock_send, capsys):
        """Empty --share-token must be caught locally, not forwarded to the webhook."""
        mock_send.return_value = {"error": True, "status_code": 401, "body": "", "diagnosis": "share_token_mismatch"}
        with patch("sys.argv", [
            "platform_request.py",
            "--webhook-url", "http://hook.example.com/x",
            "--share-token", "",
            "--platform", "microsoft",
            "--endpoint", "/me",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
        assert not mock_send.called

    @patch("platform_request.send_request")
    def test_whitespace_share_token_rejected_before_network(self, mock_send, capsys):
        """Whitespace-only --share-token is also rejected locally."""
        mock_send.return_value = {"error": True, "status_code": 401, "body": "", "diagnosis": "share_token_mismatch"}
        with patch("sys.argv", [
            "platform_request.py",
            "--webhook-url", "http://hook.example.com/x",
            "--share-token", "   ",
            "--platform", "microsoft",
            "--endpoint", "/me",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
        assert not mock_send.called

    @patch("platform_request.send_request")
    def test_empty_webhook_url_rejected_before_network(self, mock_send, capsys):
        """Empty --webhook-url is also caught before any network call."""
        mock_send.return_value = {"error": False, "status_code": 200, "body": {}}
        with patch("sys.argv", [
            "platform_request.py",
            "--webhook-url", "",
            "--share-token", "mytoken",
            "--platform", "microsoft",
            "--endpoint", "/me",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
        assert not mock_send.called

    @patch("platform_request.send_request")
    def test_delete_method_rejected(self, mock_send, capsys):
        """--method DELETE must be rejected; only GET and POST are valid."""
        mock_send.return_value = {"error": False, "status_code": 200, "body": {}}
        with patch("sys.argv", [
            "platform_request.py",
            "--webhook-url", "http://hook.example.com/x",
            "--share-token", "mytoken",
            "--platform", "microsoft",
            "--endpoint", "/me",
            "--method", "DELETE",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
        assert not mock_send.called

    @patch("platform_request.send_request")
    def test_lowercase_get_accepted(self, mock_send):
        """--method get (lowercase) should be accepted case-insensitively."""
        mock_send.return_value = {"error": False, "status_code": 200, "body": {}}
        with patch("sys.argv", [
            "platform_request.py",
            "--webhook-url", "http://hook.example.com/x",
            "--share-token", "mytoken",
            "--platform", "microsoft",
            "--endpoint", "/me",
            "--method", "get",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 0
        assert mock_send.called
