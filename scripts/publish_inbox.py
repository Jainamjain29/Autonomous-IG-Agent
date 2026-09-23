"""Publish the Reels waiting in workspace/inbox/, one explicit approval at a time.

Usage (from the repo root):
    python scripts/publish_inbox.py [--dry-run] [--inbox DIR]

Inbox layout:
    workspace/inbox/clip.mp4    the video
    workspace/inbox/clip.txt    optional caption (UTF-8). If it is missing you are asked
                                to type one, which is saved as clip.txt; empty = skip it.
    workspace/inbox/posted/     clip.mp4 + clip.txt + clip.json (media ID, caption, time)
    workspace/inbox/failed/     clip.mp4 + clip.txt + clip.json (failing step, error)

Every pending video is listed with its size and caption, then you answer y/n/q for each:
    y  publish it          n  skip it (it stays in the inbox)
    q  stop reviewing: this and the remaining videos are skipped; ones already
       approved with y are still published.
Nothing is published without an explicit y. A failure on one video is recorded in
failed/ and the batch carries on with the next one.

Uses the same PUBLISH_MODE path as scripts/publish_test.py. In ngrok mode ONE tunnel
serves the whole batch (only the approved videos) and is always torn down on exit.

--dry-run   Create and process each approved container but do NOT call media_publish.
            Videos stay in the inbox; nothing is moved. (Typed captions are still saved.)

Exit codes: 0 all approved videos OK (or nothing to do), 1 any failure, 130 interrupted.
"""
import argparse
import datetime
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ig_service as ig
import tunnel

DEFAULT_INBOX = os.path.join(ROOT, "workspace", "inbox")
DUPLICATE_WARNING = "WARNING: Publish may have succeeded - check Instagram before retrying."


def _log(message, stream=None):
    print(f"[inbox] {message}", file=stream or sys.stdout)


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def caption_path(video):
    return os.path.splitext(video)[0] + ".txt"


def read_caption(video):
    """The sidecar caption, or "" if there is none. utf-8-sig tolerates Notepad's BOM."""
    try:
        with open(caption_path(video), encoding="utf-8-sig") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def find_videos(inbox):
    """The .mp4 files directly in the inbox (not posted/ or failed/), sorted by name."""
    return sorted(os.path.join(inbox, name) for name in os.listdir(inbox)
                  if name.lower().endswith(".mp4") and os.path.isfile(os.path.join(inbox, name)))


def _ask(input_fn, prompt):
    """input() that returns None when stdin is closed."""
    try:
        return input_fn(prompt)
    except EOFError:
        return None


def collect_captions(videos, input_fn):
    """Returns [(video, caption)] for videos that have a caption. Prompts for missing
    ones and saves what is typed; an empty answer skips the video."""
    pending = []
    for video in videos:
        caption = read_caption(video)
        if not caption:
            name = os.path.basename(video)
            caption = (_ask(input_fn, f"Caption for {name} (empty = skip): ") or "").strip()
            if not caption:
                _log(f"Skipping {name}: no caption.")
                continue
            with open(caption_path(video), "w", encoding="utf-8") as f:
                f.write(caption + "\n")
        pending.append((video, caption))
    return pending


def approve(pending, input_fn):
    """Lists the pending videos and asks y/n/q for each. Returns the approved subset."""
    print()
    _log(f"{len(pending)} pending video(s):")
    for i, (video, caption) in enumerate(pending, 1):
        size_mb = os.path.getsize(video) / 1024 / 1024
        print(f"  {i}. {os.path.basename(video)}  ({size_mb:.1f} MB)")
        for line in caption.splitlines() or [""]:
            print(f"       {line}")
    print()

    approved = []
    for i, (video, caption) in enumerate(pending, 1):
        while True:
            answer = _ask(input_fn, f"Publish {i}/{len(pending)} {os.path.basename(video)}? [y/n/q] ")
            if answer is None:
                answer = "q"  # stdin closed: stop, never treat as approval
                break
            answer = answer.strip().lower()
            if answer in ("y", "yes", "n", "no", "q", "quit"):
                break
            print("  Please answer y, n or q.")
        if answer in ("q", "quit"):
            _log("Stopped reviewing; remaining videos stay in the inbox.")
            break
        if answer in ("y", "yes"):
            approved.append((video, caption))
    return approved


def _unique_dest(folder, name):
    """folder/name, or folder/stem-YYYYmmdd-HHMMSS[-n].ext if that is taken."""
    dest = os.path.join(folder, name)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(name)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 0
    while True:
        dest = os.path.join(folder, f"{stem}-{stamp}{'-' + str(n) if n else ''}{ext}")
        if not os.path.exists(dest):
            return dest
        n += 1


def file_away(video, folder, record):
    """Moves the video and its .txt into `folder` and writes record as <stem>.json beside them.
    Returns the new video path."""
    os.makedirs(folder, exist_ok=True)
    dest_video = _unique_dest(folder, os.path.basename(video))
    stem = os.path.splitext(dest_video)[0]
    shutil.move(video, dest_video)
    if os.path.isfile(caption_path(video)):
        shutil.move(caption_path(video), stem + ".txt")
    with open(stem + ".json", "w", encoding="utf-8") as f:
        json.dump({"video": os.path.basename(video), **record}, f, indent=2, ensure_ascii=False)
    return dest_video


def _error_record(e, step, caption):
    record = {
        "step": step,
        "error": f"{type(e).__name__}: {e}",
        "caption": caption,
        "failed_at": _now(),
        "may_have_published": step == "publish",
    }
    response = getattr(e, "response_json", None)
    if response:
        record["response"] = response
    return record


def main(argv=None, input_fn=input):
    parser = argparse.ArgumentParser(description="Publish the Reels in workspace/inbox/ after y/n approval.")
    parser.add_argument("--dry-run", action="store_true",
                        help="process containers but never call media_publish; nothing is moved")
    parser.add_argument("--inbox", default=DEFAULT_INBOX, help=f"inbox folder (default: {DEFAULT_INBOX})")
    args = parser.parse_args(argv)

    inbox = os.path.abspath(args.inbox)
    posted_dir, failed_dir = os.path.join(inbox, "posted"), os.path.join(inbox, "failed")
    for folder in (inbox, posted_dir, failed_dir):
        os.makedirs(folder, exist_ok=True)

    videos = find_videos(inbox)
    if not videos:
        _log(f"Inbox is empty: {inbox}")
        return 0

    try:
        pending = collect_captions(videos, input_fn)
        if not pending:
            _log("Nothing to publish.")
            return 0
        approved = approve(pending, input_fn)
    except KeyboardInterrupt:
        _log("Interrupted during review. Nothing was published.", sys.stderr)
        return 130
    if not approved:
        _log("No videos approved. Nothing was published.")
        return 0

    _log(("DRY RUN: " if args.dry_run else "LIVE RUN: ") + f"{len(approved)} approved video(s).")
    results = []  # (name, "ok" | "failed", detail)
    step = "setup"
    httpd = None
    tunnel_used = False
    current = None
    try:
        # Batch-wide setup. A failure here leaves every video in the inbox.
        try:
            mode = ig.get_publish_mode()
            _log(f"PUBLISH_MODE={mode}")
            step = "verify"
            ig.verify_credentials()
            public_url = None
            if mode == "ngrok":
                step = "tunnel"
                tunnel_used = True
                httpd = tunnel.start_file_server([video for video, _ in approved])
                public_url = tunnel.open_tunnel(httpd.server_address[1])
                _log(f"Tunnel open: {public_url}")
        except (ig.IGPublishError, tunnel.TunnelError) as e:
            _log(f"FAILED at step {getattr(e, 'step', None) or step}: {e}. "
                 "Nothing was published; all videos stay in the inbox.", sys.stderr)
            return 1

        for i, (video, caption) in enumerate(approved, 1):
            name = os.path.basename(video)
            current, step = (video, caption), "setup"
            _log(f"[{i}/{len(approved)}] {name}")
            try:
                if mode == "resumable":
                    result = ig.publish_reel_local(video, caption, publish=not args.dry_run)
                else:
                    ig.check_video_file(video)
                    video_url = f"{public_url}/{tunnel.url_name(video)}"
                    _log(f"Public URL: {video_url}")
                    step = "tunnel"
                    tunnel.check_public_url(video_url)
                    step = "setup"
                    result = ig.publish_reel(video_url, caption, publish=not args.dry_run)
            except Exception as e:  # one bad video must not stop the batch
                failed_step = getattr(e, "step", None) or step
                _log(f"FAILED at step {failed_step}: {e}", sys.stderr)
                response = getattr(e, "response_json", None)
                if response:
                    _log("API response:\n" + json.dumps(response, indent=2), sys.stderr)
                if failed_step == "publish":
                    _log(DUPLICATE_WARNING, sys.stderr)
                results.append((name, "failed", f"step {failed_step}"))
                if not args.dry_run:
                    _safe_file_away(video, failed_dir, _error_record(e, failed_step, caption))
                current = None
                continue

            current = None
            if args.dry_run:
                _log(f"DRY RUN OK: container {result} finished processing. Not published, not moved.")
                results.append((name, "ok", f"container {result} (dry run)"))
                continue
            _log(f"PUBLISHED: IG media ID {result}")
            results.append((name, "ok", f"media {result}"))
            _safe_file_away(video, posted_dir, {
                "media_id": result, "caption": caption, "published_at": _now(), "publish_mode": mode})
    except KeyboardInterrupt as e:
        if current is not None:
            video, caption = current
            failed_step = getattr(e, "step", None) or step
            _log(f"Interrupted during step {failed_step} of {os.path.basename(video)}.", sys.stderr)
            if failed_step == "publish":
                _log(DUPLICATE_WARNING, sys.stderr)
            if not args.dry_run:
                _safe_file_away(video, failed_dir, _error_record(e, failed_step, caption))
        _log("Interrupted; videos not yet started stay in the inbox.", sys.stderr)
        _summary(results)
        return 130
    finally:
        if tunnel_used:
            tunnel.stop(httpd)

    _summary(results)
    return 1 if any(status == "failed" for _, status, _ in results) else 0


def _safe_file_away(video, folder, record):
    """file_away that never raises: a move failure must not stop the batch, but after a
    successful publish it is flagged loudly so the video is not published twice."""
    try:
        dest = file_away(video, folder, record)
        _log(f"Moved to {os.path.relpath(dest, os.path.dirname(folder))}")
    except OSError as e:
        _log(f"WARNING: could not move {video} to {folder}: {e}", sys.stderr)
        if "media_id" in record:
            _log(f"It WAS published (media ID {record['media_id']}). Remove it from the inbox "
                 "by hand so it is not published again.", sys.stderr)


def _summary(results):
    if not results:
        return
    print()
    _log("Summary:")
    for name, status, detail in results:
        print(f"  {'OK    ' if status == 'ok' else 'FAILED'}  {name}  ({detail})")


if __name__ == "__main__":
    # Meta's error messages can contain characters a cp1252 console can't print.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")
    sys.exit(main())
