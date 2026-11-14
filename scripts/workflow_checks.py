#!/usr/bin/env python3
"""Run bounded production workflow checks against an ephemeral HTTP server."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import io
import json
import math
import struct
import sys
import tempfile
import threading
import wave
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from avprovenance.http_api import ProvenanceHTTPServer, build_http_server


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_wav(
    *,
    frequency: float,
    duration_seconds: float = 0.6,
    sample_rate: int = 8000,
    amplitude: int = 9000,
) -> bytes:
    frame_count = int(duration_seconds * sample_rate)
    values = [
        int(amplitude * math.sin(2 * math.pi * frequency * index / sample_rate))
        for index in range(frame_count)
    ]
    payload = io.BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(struct.pack(f"<{len(values)}h", *values))
    return payload.getvalue()


def make_y4m(
    *,
    width: int = 64,
    height: int = 36,
    frame_count: int = 5,
    rate_numerator: int = 12,
    rate_denominator: int = 1,
) -> bytes:
    header = (
        "YUV4MPEG2 "
        f"W{width} H{height} "
        f"F{rate_numerator}:{rate_denominator} "
        "Ip C420jpeg\n"
    ).encode("ascii")
    chroma_width = width // 2
    chroma_height = height // 2
    parts = [header]
    for frame_index in range(frame_count):
        parts.append(b"FRAME\n")
        y_plane = bytes(
            (x * 3 + y * 5 + frame_index * 11) % 256
            for y in range(height)
            for x in range(width)
        )
        u_plane = bytes(
            (96 + x * 2 + frame_index * 3) % 256
            for _y in range(chroma_height)
            for x in range(chroma_width)
        )
        v_plane = bytes(
            (144 + y * 2 + frame_index * 5) % 256
            for y in range(chroma_height)
            for _x in range(chroma_width)
        )
        parts.extend((y_plane, u_plane, v_plane))
    return b"".join(parts)


def make_interlaced_y4m(
    *,
    interlace: str,
    width: int = 16,
    height: int = 16,
    frame_count: int = 4,
    rate_numerator: int = 25,
    rate_denominator: int = 1,
) -> bytes:
    """Build an interlaced stream whose rows encode their source field.

    Even and odd scanlines use disjoint value ranges so a deinterlaced frame
    can be proven to preserve the exact field scanlines captured at each
    motion instant, in the order the field-order marker declares.
    """

    if interlace not in {"t", "b"}:
        raise ValueError("interlace must be 't' or 'b' for this fixture.")
    header = (
        "YUV4MPEG2 "
        f"W{width} H{height} "
        f"F{rate_numerator}:{rate_denominator} "
        f"I{interlace} C420jpeg\n"
    ).encode("ascii")
    chroma_width = width // 2
    chroma_height = height // 2
    parts = [header]
    for frame_index in range(frame_count):
        parts.append(b"FRAME\n")
        y_plane = bytes(
            (frame_index * 13 + (40 if y % 2 == 0 else 160) + x) % 256
            for y in range(height)
            for x in range(width)
        )
        u_plane = bytes(
            (frame_index * 7 + (20 if y % 2 == 0 else 120) + x) % 256
            for y in range(chroma_height)
            for x in range(chroma_width)
        )
        v_plane = bytes(
            (frame_index * 9 + (60 if y % 2 == 0 else 200) + x) % 256
            for y in range(chroma_height)
            for x in range(chroma_width)
        )
        parts.extend((y_plane, u_plane, v_plane))
    return b"".join(parts)


class HTTPClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection(
            self.host,
            self.port,
            timeout=15,
        )
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw.decode("utf-8"))
            return response.status, value
        finally:
            connection.close()

    def upload(self, filename: str, data: bytes) -> dict[str, Any]:
        status, body = self.request(
            "POST",
            "/v1/assets",
            {
                "filename": filename,
                "data_base64": base64.b64encode(data).decode("ascii"),
            },
        )
        require_equal(status, 201, "asset upload status")
        return body

    def create_rendition(self, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self.request("POST", "/v1/renditions", payload)
        require_equal(status, 201, "rendition creation status")
        return body


@contextmanager
def running_server() -> Iterator[tuple[HTTPClient, ProvenanceHTTPServer]]:
    with tempfile.TemporaryDirectory(prefix="avprovenance-check-") as temporary:
        server = build_http_server("127.0.0.1", 0, Path(temporary))
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        thread.start()
        host, port = server.server_address[:2]
        try:
            yield HTTPClient(str(host), int(port)), server
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_ingest_media_asset(client: HTTPClient) -> dict[str, Any]:
    health_status, health = client.request("GET", "/health")
    require_equal(health_status, 200, "health status")
    require_equal(health["status"], "ok", "health payload")

    audio = make_wav(frequency=330)
    asset = client.upload("tone-source.wav", audio)
    require_equal(asset["kind"], "audio", "asset kind")
    require_equal(asset["sha256"], _sha256(audio), "asset digest")
    require_equal(asset["descriptor"]["container"], "wav", "asset container")
    require("content_key" not in asset, "public asset exposed an internal content key")

    get_status, fetched = client.request("GET", f"/v1/assets/{asset['id']}")
    require_equal(get_status, 200, "asset get status")
    require_equal(fetched["sha256"], asset["sha256"], "asset read digest")

    bad_status, bad_body = client.request(
        "POST",
        "/v1/assets",
        {
            "filename": "unknown.bin",
            "data_base64": base64.b64encode(b"not-media").decode("ascii"),
        },
    )
    require_equal(bad_status, 415, "unsupported media status")
    require_equal(
        bad_body["error"]["code"],
        "UNSUPPORTED_MEDIA",
        "unsupported media code",
    )
    return {
        "workflow": "ingest-media-asset",
        "asset_id": asset["id"],
        "kind": asset["kind"],
        "digest": asset["sha256"],
    }


def run_execute_rendition(client: HTTPClient) -> dict[str, Any]:
    primary_bytes = make_wav(frequency=440, amplitude=10000)
    secondary_bytes = make_wav(frequency=220, amplitude=7000)
    primary = client.upload("primary-tone.wav", primary_bytes)
    secondary = client.upload("secondary-tone.wav", secondary_bytes)

    audio_rendition = client.create_rendition(
        {
            "primary_asset_id": primary["id"],
            "secondary_asset_id": secondary["id"],
            "output_name": "mixed-tone.wav",
            "operations": [
                {
                    "type": "media_trim",
                    "start_seconds": 0.05,
                    "duration_seconds": 0.3,
                },
                {"type": "audio_gain", "db": -3},
                {
                    "type": "audio_mix",
                    "secondary_gain": -6,
                    "primary_weight": 0.75,
                    "secondary_weight": 0.4,
                },
                {"type": "audio_normalize", "target_peak": 0.8},
            ],
        }
    )
    require_equal(audio_rendition["status"], "completed", "audio rendition status")
    history = [item["to"] for item in audio_rendition["transitions"]]
    require_equal(
        history,
        ["queued", "running", "completed"],
        "audio state history",
    )
    require(
        audio_rendition["output_asset"]["descriptor"]["container"] == "wav",
        "audio output descriptor was not WAV",
    )

    video_bytes = make_y4m()
    video = client.upload("source-frames.y4m", video_bytes)
    video_rendition = client.create_rendition(
        {
            "primary_asset_id": video["id"],
            "output_name": "cropped-frames.y4m",
            "operations": [
                {
                    "type": "video_crop",
                    "x": 8,
                    "y": 6,
                    "width": 32,
                    "height": 18,
                },
                {"type": "video_luma_offset", "amount": 12},
                {"type": "video_mirror"},
            ],
        }
    )
    require_equal(video_rendition["status"], "completed", "video rendition status")
    properties = video_rendition["output_asset"]["descriptor"]["properties"]
    require_equal(properties["width"], 32, "video output width")
    require_equal(properties["height"], 18, "video output height")

    lineage_status, lineage = client.request(
        "GET",
        f"/v1/renditions/{audio_rendition['id']}/lineage",
    )
    require_equal(lineage_status, 200, "lineage status")
    require_equal(len(lineage["parents"]), 2, "lineage parent count")
    require_equal(
        lineage["edge"]["plan_digest"],
        audio_rendition["plan_digest"],
        "lineage plan digest",
    )
    invalid_status, invalid_body = client.request(
        "POST",
        "/v1/renditions",
        {
            "primary_asset_id": primary["id"],
            "output_name": "invalid.wav",
            "operations": [
                {
                    "type": "video_crop",
                    "x": 0,
                    "y": 0,
                    "width": 2,
                    "height": 2,
                }
            ],
        },
    )
    require_equal(invalid_status, 422, "invalid plan status")
    require_equal(invalid_body["error"]["code"], "PLAN_INVALID", "invalid plan code")
    return {
        "workflow": "execute-rendition",
        "audio_rendition_id": audio_rendition["id"],
        "video_rendition_id": video_rendition["id"],
        "audio_history": history,
        "invalid_plan_code": invalid_body["error"]["code"],
        "video_dimensions": [
            properties["width"],
            properties["height"],
        ],
    }


def run_verify_reproduction(
    client: HTTPClient,
    server: ProvenanceHTTPServer,
) -> dict[str, Any]:
    video_bytes = make_y4m(frame_count=7)
    video = client.upload("verification-source.y4m", video_bytes)
    rendition = client.create_rendition(
        {
            "primary_asset_id": video["id"],
            "output_name": "verification-output.y4m",
            "operations": [
                {"type": "media_trim", "start_frame": 1, "frame_count": 4},
                {
                    "type": "video_crop",
                    "x": 4,
                    "y": 4,
                    "width": 40,
                    "height": 20,
                },
                {"type": "video_luma_offset", "amount": -9},
                {"type": "video_mirror"},
            ],
        }
    )
    check_status, check = client.request(
        "POST",
        f"/v1/renditions/{rendition['id']}/integrity-checks",
        {},
    )
    require_equal(check_status, 201, "integrity check status")
    require_equal(check["status"], "verified", "integrity result")
    require_equal(
        check["expected_digest"],
        check["reproduced_digest"],
        "integrity digest equality",
    )
    require_equal(
        check["expected_descriptor"],
        check["reproduced_descriptor"],
        "integrity descriptor equality",
    )

    lineage_status, lineage = client.request(
        "GET",
        f"/v1/renditions/{rendition['id']}/lineage",
    )
    require_equal(lineage_status, 200, "verification lineage status")
    require_equal(len(lineage["parents"]), 1, "verification parent count")

    output_asset_id = rendition["output_asset"]["id"]

    def drift_metadata(state: dict[str, Any]) -> None:
        state["assets"][output_asset_id]["sha256"] = "0" * 64

    server.application.state_file.mutate(drift_metadata)
    mismatch_status, mismatch = client.request(
        "POST",
        f"/v1/renditions/{rendition['id']}/integrity-checks",
        {},
    )
    require_equal(mismatch_status, 409, "mismatch status")
    require_equal(
        mismatch["error"]["code"],
        "INTEGRITY_MISMATCH",
        "mismatch error code",
    )
    require_equal(
        mismatch["error"]["details"]["audit"]["status"],
        "mismatch",
        "persisted mismatch status",
    )
    return {
        "workflow": "verify-reproduction",
        "rendition_id": rendition["id"],
        "integrity_check_id": check["id"],
        "digest": check["reproduced_digest"],
        "mismatch_status": mismatch["error"]["details"]["audit"]["status"],
    }


def run_deinterlace_video(
    client: HTTPClient,
    server: ProvenanceHTTPServer,
) -> dict[str, Any]:
    from avprovenance.media.y4m import decode_y4m

    frame_count = 4
    width = height = 16
    rate_n = 25
    results: dict[str, Any] = {"workflow": "deinterlace-video", "orders": {}}

    for order, top_first in (("t", True), ("b", False)):
        source_bytes = make_interlaced_y4m(
            interlace=order,
            width=width,
            height=height,
            frame_count=frame_count,
            rate_numerator=rate_n,
        )
        source = client.upload(f"interlaced-{order}.y4m", source_bytes)
        source_props = source["descriptor"]["properties"]
        require_equal(source_props["interlace"], order, f"{order}: source field order")

        rendition = client.create_rendition(
            {
                "primary_asset_id": source["id"],
                "output_name": f"progressive-{order}.y4m",
                "operations": [
                    # A crop before deinterlacing must not change the field
                    # rhythm or reverse the output order.
                    {
                        "type": "video_crop",
                        "x": 2,
                        "y": 4,
                        "width": 12,
                        "height": 8,
                    },
                    {"type": "video_deinterlace"},
                    {"type": "video_mirror"},
                ],
            }
        )
        require_equal(rendition["status"], "completed", f"{order}: rendition status")
        props = rendition["output_asset"]["descriptor"]["properties"]
        require_equal(props["interlace"], "p", f"{order}: output is progressive")
        require_equal(
            props["frame_count"],
            frame_count * 2,
            f"{order}: one progressive frame per field",
        )
        require_equal(
            props["rate_numerator"],
            rate_n * 2,
            f"{order}: frame rate doubled",
        )
        require_equal(props["rate_denominator"], 1, f"{order}: rate denominator kept")
        source_duration = frame_count / rate_n
        output_duration = props["frame_count"] / props["rate_numerator"]
        require(
            abs(source_duration - output_duration) < 1e-12,
            f"{order}: playback duration preserved",
        )
        require_equal(
            props["duration_seconds"],
            round(source_duration, 9),
            f"{order}: descriptor duration",
        )

        # Integrity replay must reproduce the exact bytes and descriptor.
        check_status, check = client.request(
            "POST",
            f"/v1/renditions/{rendition['id']}/integrity-checks",
            {},
        )
        require_equal(check_status, 201, f"{order}: integrity status")
        require_equal(check["status"], "verified", f"{order}: integrity verified")
        require_equal(
            check["expected_digest"],
            check["reproduced_digest"],
            f"{order}: replayed digest",
        )

        # Pixel-level proof via the stored content-addressed bytes.
        output_asset = server.application.repository.get_asset(
            rendition["output_asset"]["id"]
        )
        output_bytes = server.application.content_files.read(
            output_asset.content_key
        )
        source_clip = decode_y4m(source_bytes)
        output_clip = decode_y4m(output_bytes)
        crop_x, crop_y = 2, 4

        def source_row(clip, frame_index, row):
            plane = clip.frames[frame_index].y_plane
            return plane[row * clip.width : (row + 1) * clip.width]

        def output_row(clip, frame_index, row):
            plane = clip.frames[frame_index].y_plane
            return plane[row * clip.width : (row + 1) * clip.width]

        for source_index in range(frame_count):
            top_index = 2 * source_index + (0 if top_first else 1)
            bottom_index = 2 * source_index + (1 if top_first else 0)

            # Temporal order: the first emitted frame per source frame carries
            # whichever field the header declares first.
            first_is_top = output_clip.frames[2 * source_index] is output_clip.frames[
                top_index
            ]
            require_equal(
                first_is_top,
                top_first,
                f"{order}: emitted field order follows the field-order marker",
            )

            # Captured field scanlines survive the crop, then mirror, verbatim.
            for output_row_index in range(0, output_clip.height, 2):
                source_row_index = crop_y + output_row_index
                expected = source_row(
                    source_clip, source_index, source_row_index
                )[crop_x : crop_x + output_clip.width][::-1]
                require_equal(
                    output_row(output_clip, top_index, output_row_index),
                    expected,
                    f"{order}: top field preserves its captured even scanlines",
                )
            for output_row_index in range(1, output_clip.height, 2):
                source_row_index = crop_y + output_row_index
                expected = source_row(
                    source_clip, source_index, source_row_index
                )[crop_x : crop_x + output_clip.width][::-1]
                require_equal(
                    output_row(output_clip, bottom_index, output_row_index),
                    expected,
                    f"{order}: bottom field preserves its captured odd scanlines",
                )

        # The progressive output re-entering the chain must be treated as
        # already deinterlaced: a second deinterlace fails rather than
        # splitting real progressive frames into fields again.
        reentry_status, reentry = client.request(
            "POST",
            "/v1/renditions",
            {
                "primary_asset_id": rendition["output_asset"]["id"],
                "output_name": f"re-deinterlace-{order}.y4m",
                "operations": [{"type": "video_deinterlace"}],
            },
        )
        require_equal(
            reentry_status,
            409,
            f"{order}: re-deinterlace rejected at runtime",
        )
        require_equal(
            reentry["error"]["details"]["reason"],
            "VIDEO_DEINTERLACE_ALREADY_PROGRESSIVE",
            f"{order}: re-deinterlace stable reason",
        )

        # A normal downstream derivative on the progressive output still works
        # and keeps the progressive marker and doubled rhythm.
        downstream = client.create_rendition(
            {
                "primary_asset_id": rendition["output_asset"]["id"],
                "output_name": f"downstream-{order}.y4m",
                "operations": [
                    {"type": "media_trim", "start_frame": 1, "frame_count": 3},
                    {
                        "type": "video_crop",
                        "x": 0,
                        "y": 0,
                        "width": 8,
                        "height": 8,
                    },
                ],
            }
        )
        downstream_props = downstream["output_asset"]["descriptor"]["properties"]
        require_equal(
            downstream_props["interlace"],
            "p",
            f"{order}: downstream stays progressive",
        )
        require_equal(
            downstream_props["frame_count"],
            3,
            f"{order}: downstream trim frame count",
        )
        require_equal(
            downstream_props["rate_numerator"],
            rate_n * 2,
            f"{order}: downstream keeps doubled rate",
        )

        results["orders"][order] = {
            "rendition_id": rendition["id"],
            "output_frames": props["frame_count"],
            "rate_numerator": props["rate_numerator"],
            "integrity": check["status"],
        }

    # A plan with two deinterlace operations is rejected before queuing.
    duplicate_status, duplicate = client.request(
        "POST",
        "/v1/renditions",
        {
            "primary_asset_id": source["id"],
            "output_name": "duplicate.y4m",
            "operations": [
                {"type": "video_deinterlace"},
                {"type": "video_deinterlace"},
            ],
        },
    )
    require_equal(duplicate_status, 422, "duplicate deinterlace plan status")
    require_equal(
        duplicate["error"]["code"],
        "PLAN_INVALID",
        "duplicate deinterlace plan code",
    )

    # A progressive source cannot be deinterlaced (no fields exist).
    progressive = client.upload(
        "progressive-source.y4m",
        make_y4m(width=16, height=16, frame_count=3, rate_numerator=25),
    )
    progressive_status, progressive_body = client.request(
        "POST",
        "/v1/renditions",
        {
            "primary_asset_id": progressive["id"],
            "output_name": "bad-deinterlace.y4m",
            "operations": [{"type": "video_deinterlace"}],
        },
    )
    require_equal(
        progressive_status,
        409,
        "progressive deinterlace runtime status",
    )
    require_equal(
        progressive_body["error"]["details"]["reason"],
        "VIDEO_DEINTERLACE_ALREADY_PROGRESSIVE",
        "progressive deinterlace stable reason",
    )

    # Legacy path: a plan without the new operation keeps interlaced content
    # byte-for-byte (the existing crop/mirror chain preserves the field flag).
    legacy_source = client.upload(
        "legacy-interlaced.y4m",
        make_interlaced_y4m(interlace="t", frame_count=3),
    )
    legacy = client.create_rendition(
        {
            "primary_asset_id": legacy_source["id"],
            "output_name": "legacy-preserved.y4m",
            "operations": [
                {
                    "type": "video_crop",
                    "x": 0,
                    "y": 0,
                    "width": 8,
                    "height": 8,
                },
                {"type": "video_mirror"},
            ],
        }
    )
    legacy_props = legacy["output_asset"]["descriptor"]["properties"]
    require_equal(
        legacy_props["interlace"],
        "t",
        "legacy plan preserves the interlace field marker",
    )
    require_equal(
        legacy_props["frame_count"],
        3,
        "legacy plan keeps the original frame count",
    )
    legacy_check_status, legacy_check = client.request(
        "POST",
        f"/v1/renditions/{legacy['id']}/integrity-checks",
        {},
    )
    require_equal(
        legacy_check["status"],
        "verified",
        "legacy interlaced rendition replays identically",
    )
    results["legacy_interlace"] = legacy_props["interlace"]
    results["duplicate_plan_code"] = duplicate["error"]["code"]
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "workflow",
        choices=(
            "ingest-media-asset",
            "execute-rendition",
            "verify-reproduction",
            "deinterlace-video",
        ),
    )
    args = parser.parse_args()

    with running_server() as (client, server):
        if args.workflow == "ingest-media-asset":
            result = run_ingest_media_asset(client)
        elif args.workflow == "execute-rendition":
            result = run_execute_rendition(client)
        elif args.workflow == "deinterlace-video":
            result = run_deinterlace_video(client, server)
        else:
            result = run_verify_reproduction(client, server)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
