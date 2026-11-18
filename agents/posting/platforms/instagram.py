"""Instagram Reel publishing adapter using official Graph API (Instagram Login)."""
from __future__ import annotations

import re
from typing import Optional

import database as db
import ig_service
from ig_service import IGPublishError
import tunnel
from tunnel import TunnelError
from agents.posting.lock import map_ngrok_error
from core.platforms.base import PublishAdapter, PublishRecord


class InstagramAdapter(PublishAdapter):
    """Publishes Reels to Instagram using ngrok video tunnel and Instagram Graph API."""

    def __init__(self):
        super().__init__(platform="instagram")

    def publish(self, video_path: str, text: str, dry_run: bool = False) -> PublishRecord:
        """Publish video to Instagram Reels.
        
        Enforces character and hashtag limits prior to network calls.
        In dry_run mode, validates configuration without making network or tunnel calls.
        """
        # 1. Validation before network
        if len(text) > 2200:
            return PublishRecord(
                platform="instagram",
                status="failed",
                error=f"Caption exceeds character limit ({len(text)} > 2200)",
            )

        hashtags = re.findall(r"#\w+", text)
        if len(hashtags) > 30:
            return PublishRecord(
                platform="instagram",
                status="failed",
                error=f"Caption exceeds hashtag limit ({len(hashtags)} > 30)",
            )

        # 2. Check credentials
        access_token = db.get_setting("META_ACCESS_TOKEN")
        ig_user_id = db.get_setting("IG_USER_ID")
        if not access_token or not ig_user_id:
            return PublishRecord(
                platform="instagram",
                status="failed",
                error="Missing credentials: META_ACCESS_TOKEN or IG_USER_ID not configured",
            )

        if not access_token.startswith("IGAA"):
            return PublishRecord(
                platform="instagram",
                status="failed",
                error="META_ACCESS_TOKEN must start with 'IGAA' (Instagram Login token required)",
            )

        # 3. Dry run path
        if dry_run:
            ngrok_token = db.get_setting("NGROK_AUTHTOKEN")
            if not ngrok_token:
                return PublishRecord(
                    platform="instagram",
                    status="failed",
                    error="NGROK_AUTHTOKEN is not configured for publishing tunnel",
                )
            return PublishRecord(
                platform="instagram",
                status="dry_run",
                platform_post_id=None,
                permalink=f"dry_run: {text}",
                error=None,
            )

        # 4. Real publish with ngrok tunnel
        httpd = None
        try:
            ig_service.check_video_file(video_path)

            httpd = tunnel.start_file_server(video_path)
            port = httpd.server_address[1]
            public_url = tunnel.open_tunnel(port)
            video_url = f"{public_url}/{tunnel.url_name(video_path)}"
            tunnel.check_public_url(video_url)

            media_id = ig_service.publish_reel(video_url=video_url, caption=text, publish=True)

            # Fetch permalink
            host, version, base_url = ig_service.get_config()
            permalink: Optional[str] = None
            permalink_err_note: Optional[str] = None
            try:
                res = ig_service._request(
                    "GET",
                    f"{base_url}/{media_id}",
                    access_token,
                    f"GET /{media_id}?fields=permalink",
                    params={"fields": "permalink", "access_token": access_token},
                )
                permalink = res.get("permalink")
            except Exception as e:
                permalink_err_note = f"Published successfully but failed to fetch permalink: {e}"

            return PublishRecord(
                platform="instagram",
                status="published",
                platform_post_id=str(media_id),
                permalink=permalink,
                error=permalink_err_note,
            )

        except IGPublishError as e:
            step_str = f" [{e.step}]" if e.step else ""
            err_msg = f"Instagram publish failed{step_str}: {e}"
            return PublishRecord(platform="instagram", status="failed", error=err_msg)

        except TunnelError as e:
            mapped = map_ngrok_error(e)
            if mapped != str(e):
                err_msg = mapped
            else:
                step_str = f" [{e.step}]" if getattr(e, "step", None) else ""
                err_msg = f"Tunnel error{step_str}: {e}"
            return PublishRecord(platform="instagram", status="failed", error=err_msg)

        except Exception as e:
            mapped = map_ngrok_error(e)
            return PublishRecord(platform="instagram", status="failed", error=mapped)

        finally:
            tunnel.stop(httpd)
