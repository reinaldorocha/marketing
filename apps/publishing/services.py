import logging
from typing import Any
from django.utils import timezone

from apps.channels import instagram_oauth
from apps.channels.providers import instagram
from apps.channels.providers.exceptions import APIError
from apps.publishing.models import ContentPost, PostStatus, PublishType

logger = logging.getLogger(__name__)


def publish_post(post: ContentPost) -> ContentPost:
    """Execute the Meta Graph API publishing pipeline for one ContentPost."""
    connection = post.connection
    token = instagram_oauth.access_token(connection)
    ig_user_id = connection.external_id

    post.status = PostStatus.PUBLISHING
    post.last_error = ""

    try:
        # Step 1: Create Container
        if post.publish_type == PublishType.CAROUSEL:
            items = post.media_items if isinstance(post.media_items, list) else []
            if len(items) < 2:
                raise ValueError("Um carrossel precisa de no mínimo 2 itens de mídia.")

            children_ids: list[str] = []
            for item in items[:10]:
                item_type = item.get("type", "image")
                url = item.get("url", "")
                cover = item.get("cover_url") or None
                if not url:
                    continue
                cid = instagram.create_carousel_item(ig_user_id, token, item_type, url, cover)
                if cid:
                    children_ids.append(cid)

            if len(children_ids) < 2:
                raise ValueError("Não foi possível criar os contêineres dos itens do carrossel.")

            container_id = instagram.create_carousel_container(
                ig_user_id, token, children_ids, caption=post.caption
            )
        else:
            container_id = instagram.create_media_container(
                ig_user_id=ig_user_id,
                token=token,
                publish_type=post.publish_type,
                media_url=post.media_url,
                caption=post.caption,
                cover_url=post.cover_url or None,
            )

        if not container_id:
            raise ValueError("A Meta não retornou o identificador do contêiner de mídia.")

        post.container_id = container_id
        post.save(update_fields=["container_id", "status", "last_error", "updated_at"])

        # Step 2: Immediate check if ready
        _poll_and_finish_container(post, ig_user_id, token, container_id)

    except APIError as exc:
        post.status = PostStatus.FAILED
        post.last_error = f"Erro da Meta API ({exc.code}): {exc}"
        logger.warning("Publishing failed for post %s: %s", post.pk, exc)
    except Exception as exc:
        post.status = PostStatus.FAILED
        post.last_error = str(exc)
        logger.exception("Publishing unexpected failure for post %s: %s", post.pk, exc)

    post.save()
    return post


def _poll_and_finish_container(post: ContentPost, ig_user_id: str, token: str, container_id: str) -> bool:
    """Check container status; if ready, publish it immediately."""
    status_info = instagram.get_container_status(container_id, token)
    code = status_info.get("status_code", "")

    if code == "FINISHED" or not code:  # Some image containers are immediately finished
        media_id = instagram.publish_container(ig_user_id, token, container_id)
        post.published_media_id = media_id
        post.status = PostStatus.PUBLISHED
        post.published_at = timezone.now()

        # Fetch permalink
        try:
            media_info = instagram.get_published_media(media_id, token)
            post.permalink = media_info.get("permalink", "")
        except Exception:
            pass

        return True

    if code in ("ERROR", "EXPIRED"):
        post.status = PostStatus.FAILED
        post.last_error = status_info.get("status", f"Contêiner Meta retornou status {code}")
        return True

    # Otherwise still IN_PROGRESS
    post.status = PostStatus.PUBLISHING
    post.last_error = "A Meta está processando o vídeo/carrossel. Clique em 'Atualizar Status' em instantes."
    return False


def refresh_post_status(post: ContentPost) -> bool:
    """Check the status of a pending post on Meta and finalize if ready."""
    if not post.container_id or post.status != PostStatus.PUBLISHING:
        return False

    token = instagram_oauth.access_token(post.connection)
    ig_user_id = post.connection.external_id

    try:
        finished = _poll_and_finish_container(post, ig_user_id, token, post.container_id)
        post.save()
        return finished
    except Exception as exc:
        post.last_error = str(exc)
        post.save(update_fields=["last_error", "updated_at"])
        return False


def check_all_pending_posts() -> int:
    """Housekeeping job to poll all publishing posts across workspaces."""
    pending = ContentPost.objects.unscoped().filter(status=PostStatus.PUBLISHING).exclude(container_id="")
    count = 0
    for post in pending[:20]:
        try:
            if refresh_post_status(post):
                count += 1
        except Exception:
            logger.exception("Error checking pending post %s", post.pk)
    return count
