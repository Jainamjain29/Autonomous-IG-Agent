"""Publish one Reel from the command line, bypassing the dashboard and its workflow state.

Usage (from the repo root):
    python scripts/publish_test.py path/to/video.mp4 "caption" [--dry-run]

--dry-run   Verify credentials, create the container, get the video to Meta and wait until
            Meta finishes processing it, but do NOT call media_publish. Nothing is
            posted; the unpublished container expires on its own after 24 hours.

Respects PUBLISH_MODE:
  resumable  upload the local file directly (graph.facebook.com only).
  ngrok      serve ONLY this video on 127.0.0.1, expose it through an ngrok tunnel
             (NGROK_AUTHTOKEN from .env), HEAD-check the public URL, then create the
             container with video_url. Required on graph.instagram.com (Instagram Login).
             The tunnel and server are always torn down on exit.
Exit codes: 0 success, 1 publish error, 130 interrupted.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ig_service as ig
import tunnel

DUPLICATE_WARNING = "WARNING: Publish may have succeeded - check Instagram before retrying."


def _log(message, stream=None):
    print(f"[publish_test] {message}", file=stream or sys.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Publish one Reel to Instagram (honours PUBLISH_MODE).")
    parser.add_argument("video", help="path to the .mp4 file")
    parser.add_argument("caption", help="Reel caption (quote it)")
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except media_publish; nothing is posted")
    args = parser.parse_args(argv)

    if args.dry_run:
        _log("DRY RUN: the video will be sent to Meta and processed but NOT published.")
    else:
        _log("LIVE RUN: the Reel will be published to Instagram.")

    step = "setup"
    httpd = None
    tunnel_used = False
    try:
        mode = ig.get_publish_mode()
        _log(f"PUBLISH_MODE={mode}")
        step = "verify"
        ig.verify_credentials()
        step = "setup"
        if mode == "resumable":
            result = ig.publish_reel_local(args.video, args.caption, publish=not args.dry_run)
        else:
            ig.check_video_file(args.video)
            step = "tunnel"
            tunnel_used = True
            httpd = tunnel.start_file_server(args.video)
            public_url = tunnel.open_tunnel(httpd.server_address[1])
            video_url = f"{public_url}/{tunnel.url_name(args.video)}"
            _log(f"Public URL: {video_url}")
            tunnel.check_public_url(video_url)
            step = "setup"
            result = ig.publish_reel(video_url, args.caption, publish=not args.dry_run)
    except (ig.IGPublishError, tunnel.TunnelError) as e:
        failed_step = e.step or step
        _log(f"FAILED at step {failed_step}: {e}", sys.stderr)
        response = getattr(e, "response_json", None)
        if response:
            _log("API response:\n" + json.dumps(response, indent=2), sys.stderr)
        if failed_step == "publish":
            _log(DUPLICATE_WARNING, sys.stderr)
        return 1
    except KeyboardInterrupt as e:
        failed_step = getattr(e, "step", None) or step
        _log(f"Interrupted during step {failed_step}.", sys.stderr)
        if failed_step == "publish":
            _log(DUPLICATE_WARNING, sys.stderr)
        return 130
    finally:
        if tunnel_used:
            tunnel.stop(httpd)

    if args.dry_run:
        _log(f"DRY RUN OK: container {result} finished processing. Nothing was published.")
    else:
        _log(f"PUBLISHED: IG media ID {result}")
    return 0


if __name__ == "__main__":
    # Meta's error messages can contain characters a cp1252 console can't print.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")
    sys.exit(main())
