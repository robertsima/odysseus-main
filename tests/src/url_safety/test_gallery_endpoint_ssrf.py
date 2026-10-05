"""The URL check the gallery image proxies run on a client-supplied endpoint.

``POST /api/image/harmonize`` and ``POST /api/image/inpaint`` accept an
``_endpoint`` and fetch it server-side. The route behavior is tested through
HTTP in tests/routes/gallery/gallery_routes/test_image_endpoints.py; this
checks that the validator itself rejects the cloud metadata range.
"""


def test_url_safety_blocks_metadata_endpoint():
    # The guard is only as strong as the checker: confirm the link-local cloud
    # metadata address is rejected even with private IPs otherwise allowed.
    from src.url_safety import check_outbound_url
    ok, _ = check_outbound_url("http://169.254.169.254/latest/meta-data")
    assert ok is False
