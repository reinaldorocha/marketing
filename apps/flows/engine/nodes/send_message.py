"""SPEC §11.1 — render one abstract message and hand it to contract 1.

    Behavior: renders per capability flags; if buttons/QRs present -> Wait, else
    Continue(default).

The node renders **once**, abstractly, into an ``OutboundMessage``; the adapter
(via ``apps.channels.downgrade``) is what turns that into whatever the platform
can carry. Nothing here branches on a platform, and nothing here builds a
provider payload.

Three rules do the work, and each one is somebody else's decision this node is
obeying:

* **Every author string goes through the shared renderer** (SECURITY-BASELINE
  §3). Block text, captions, card titles, button labels, URLs — all of it, via
  ``ctx.render``, which is plain token substitution with no template engine
  anywhere near it.
* **Media resolves through the library**, ``media_library.resolution.resolve``
  with its required ``workspace`` kwarg. That module's docstring already decided
  what a missing asset means: "a deleted image stops the message, not the flow."
* **A failed send follows ``default``** (SPEC §9.5). The message row carries the
  reason; the run continues. It does *not* then wait for buttons the contact
  never saw — a wait for a reply to a message that was never delivered is a
  contact parked until the 30-day sweep.
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import re
from apps.common.platforms import Platform
from apps.channels.capabilities import capabilities_for
from apps.channels.downgrade import downgrade
from apps.channels.events import (
    Button,
    Card,
    CardBlock,
    ContactBlock,
    GalleryBlock,
    ListBlock,
    LocationBlock,
    MediaBlock,
    OutboundMessage,
    PollBlock,
    QuickReply,
    StickerBlock,
)
from apps.channels.events import TextBlock as OutboundText
from apps.flows.engine.context import NodeContext
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import Continue, Fail, StepResult, Wait
from apps.flows.engine.sending import deliver
from apps.flows.engine.waits import buttons_wait
from apps.flows.messaging import FacadeUnavailableError
from apps.media_library.resolution import MediaNotFoundError, resolve

__all__ = ["SendMessageNode"]

logger = logging.getLogger(__name__)

_MEDIA_KINDS = ("image", "audio", "video", "file")


class _UnresolvableMediaError(Exception):
    """A block names an asset this workspace cannot read. Stops the message."""


def _clean_phone_number(raw: str) -> str:
    """Validate and clean custom phone number. Strictly rejects group JIDs."""
    if not raw or "@" in raw or "g.us" in raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if not (7 <= len(digits) <= 16):
        return ""
    return digits


@register_node
class SendMessageNode(Node):
    """Send a message; wait for a reply when the message asks for one."""

    type = "send_message"
    synchronous_safe = True

    def execute(self, ctx: NodeContext) -> StepResult:
        buttons = _buttons(ctx)
        quick_replies = _quick_replies(ctx)

        try:
            blocks = _blocks(ctx)
        except _UnresolvableMediaError as exc:
            logger.warning("Execution %s: node %s not sent — %s", ctx.execution.pk, ctx.node_id, exc)
            return Continue("default")

        if not blocks:
            # The schema requires at least one block, so this is a draft or a
            # hand-edited graph. Sending an empty message would be an API error
            # per platform; skipping is the quieter equivalent of a failed send.
            logger.warning("Execution %s: node %s has nothing to send.", ctx.execution.pk, ctx.node_id)
            return Continue("default")

        recipient_type = str(ctx.config.get("recipient_type") or "contact").lower().strip()
        recipient_override = ""
        is_one_way = False

        conn = getattr(ctx.execution, "channel_connection", None)
        conn_platform = getattr(conn, "platform", "")
        if hasattr(conn_platform, "value"):
            conn_platform = conn_platform.value
        conn_platform = str(conn_platform or "").lower()

        # Recipient override is ONLY supported on WhatsApp Evolution.
        # Channels like Telegram, Instagram, Messenger, SMS, and Email do not support
        # recipient override; sending with override would leak internal messages to customer contacts.
        if recipient_type in ("custom", "group"):
            if conn and conn_platform and conn_platform != Platform.WHATSAPP_EVOLUTION.value:
                logger.warning(
                    "Execution %s node %s: recipient_type %r not supported on platform %r; skipping send to protect customer contact",
                    ctx.execution.pk, ctx.node_id, recipient_type, conn_platform,
                )
                return Continue("default")

        if recipient_type == "group":
            group_val = str(ctx.config.get("group_id") or "").strip()
            if not group_val:
                logger.warning("Execution %s node %s group_id is empty; skipping send", ctx.execution.pk, ctx.node_id)
                return Continue("default")

            from apps.whatsapp_groups.models import WhatsAppGroup
            group_obj = None
            try:
                from uuid import UUID
                UUID(group_val)
                group_obj = WhatsAppGroup.objects.for_workspace(ctx.workspace).filter(pk=group_val).first()
            except (ValueError, TypeError):
                pass
            if not group_obj:
                group_obj = WhatsAppGroup.objects.for_workspace(ctx.workspace).filter(jid=group_val).first()

            if not group_obj:
                logger.warning("Execution %s node %s names invalid or unshared group %r; skipping send", ctx.execution.pk, ctx.node_id, group_val)
                return Continue("default")

            is_sim = (
                getattr(ctx.execution, "is_simulated", False)
                or getattr(conn, "is_simulated", False)
                or getattr(ctx.contact, "is_simulated", False)
            )
            if not is_sim and conn and getattr(conn, "pk", None) and group_obj.connection_id != conn.pk:
                logger.warning(
                    "Execution %s node %s group %s belongs to connection %s, not %s; skipping send",
                    ctx.execution.pk, ctx.node_id, group_obj.jid, group_obj.connection_id, conn.pk
                )
                return Continue("default")

            recipient_override = group_obj.jid
            is_one_way = True
            logger.info("Execution %s node %s targeting WhatsApp group %s", ctx.execution.pk, ctx.node_id, recipient_override)

        elif recipient_type == "custom":
            raw_custom = ctx.render(str(ctx.config.get("custom_phone") or "")).strip()
            clean_phone = _clean_phone_number(raw_custom)
            if not clean_phone:
                logger.warning("Execution %s node %s custom_phone %r is invalid or contains group JID; skipping send", ctx.execution.pk, ctx.node_id, raw_custom)
                return Continue("default")

            recipient_override = clean_phone
            is_one_way = True
            logger.info("Execution %s node %s targeting custom recipient %s", ctx.execution.pk, ctx.node_id, clean_phone)

        if recipient_override:
            audit = ctx.variables.get("_audit_destinations")
            if not isinstance(audit, dict):
                audit = {}
            from django.utils import timezone
            ref_now = getattr(ctx, "current_now", None) or getattr(ctx, "now", None) or timezone.now()
            audit[ctx.node_id] = {
                "recipient_type": recipient_type,
                "destination": recipient_override,
                "timestamp": ref_now.isoformat(),
            }
            ctx.variables["_audit_destinations"] = audit

        template = _whatsapp_template(ctx)
        outbound = OutboundMessage(
            # A template send stores what the contact will actually read, not
            # what the node's own blocks say — see _whatsapp_template.
            blocks=tuple(template.blocks) if template.blocks else tuple(blocks),
            buttons=tuple(buttons),
            quick_replies=tuple(quick_replies),
            template_ref=template.reference,
            template_variables=template.variables,
            recipient_override=recipient_override,
        )
        try:
            outcome = deliver(ctx.execution, outbound, node_id=ctx.node_id)
        except FacadeUnavailableError as exc:
            # A deployment problem rather than a flow problem, and one no retry
            # fixes, so it is a named failure rather than a silent skip.
            return Fail(f"send_message node {ctx.node_id}: {exc}")

        if not outcome.sent:
            return Continue("default")

        if is_one_way:
            # Notifications to groups or third parties are one-way.
            # Waits (replies, buttons, lists) are restricted to the main contact.
            return Continue("default")

        # Collect list rows if any
        list_rows: list[dict[str, Any]] = []
        for block in blocks:
            if isinstance(block, ListBlock):
                for sec in block.sections:
                    if isinstance(sec, dict):
                        for r in sec.get("rows") or []:
                            if isinstance(r, dict) and r.get("id"):
                                list_rows.append(r)

        wait = buttons_wait(
            ctx.node_id,
            buttons=ctx.config.get("buttons"),
            quick_replies=ctx.config.get("quick_replies"),
            followup=ctx.config.get("followup"),
            retry_unmatched=ctx.config.get("retry_unmatched"),
            labels=_labels(buttons, quick_replies, list_rows) | _numbered_options(ctx, outbound),
            list_rows=list_rows,
            now=getattr(ctx, "current_now", None),
        )
        if wait["handles"] or wait.get("timeout"):
            return Wait(wait)

        # Buttons were present but none of them can ever reply: a message whose
        # only buttons are URL buttons produces no inbound event, and no
        # followup was configured to move it on either. Waiting would park the
        # contact until the 30-day sweep — and since SPEC §22 allows one live
        # execution per contact, that is every other flow dead for a month.
        return Continue("default")


@dataclass(frozen=True)
class _Template:
    """What a resolved template contributes to the outgoing message."""

    reference: str | None = None
    variables: tuple[tuple[str, str], ...] = ()
    #: The rendered copy, for the message row. Empty when no template resolved,
    #: which leaves the node's own blocks standing.
    blocks: tuple[Any, ...] = ()


def _whatsapp_template(ctx: NodeContext) -> _Template:
    """The approved template this node was pointed at, resolved and filled.

    Still not a platform branch. The key is optional config that a *builder*
    only offers for a WhatsApp-targeted flow, and what it produces —
    ``template_ref`` plus ``template_variables`` — is the platform-neutral pair
    :class:`~apps.channels.events.OutboundMessage` already carries for exactly
    this purpose. An adapter with no templates simply ignores both, and
    ``compliance.can_send`` reads ``template_ref`` as data (SPEC §8's
    ``TEMPLATE_SUPPLIED``) without knowing which platform supplied it.

    **The reference is re-derived from the row, against the connection this run
    is on.** Trusting the one the builder wrote is not safe: a template name is
    scoped to a WhatsApp Business Account, so a flow that can run on two numbers
    can carry a reference the second one does not have — and if that second WABA
    holds a same-named template, Meta sends its words instead, to a real
    contact, with nothing reporting a problem. ``whatsapp_templates.sendable``
    is the check.

    **Its rendered copy becomes the message body.** The adapter puts only the
    template on the wire, so storing the node's own blocks would leave the inbox
    showing a conversation that did not happen.

    Failing to resolve is deliberate and quiet-ish: no reference is supplied, so
    outside the window ``can_send`` refuses with ``needs_template`` and the node
    follows ``default`` with the reason on the row (SPEC §8), while inside it the
    author's own blocks still go. Both are better than sending words nobody
    approved.

    **The values are rendered here**, through ``ctx.render``, and that placement
    is the security property: a template slot is filled with contact data, and
    the substitution has to happen where the one shared renderer is
    (SECURITY-BASELINE §3). By the time the adapter sees these pairs they are
    finished strings, and its docstring says it must never render them again.
    """
    config = ctx.config.get("whatsapp_template")
    if not isinstance(config, dict):
        return _Template()

    values: dict[str, str] = {}
    for item in config.get("variables") or []:
        if not isinstance(item, dict):
            continue
        slot = item.get("slot")
        if isinstance(slot, str) and slot:
            values[slot] = ctx.render(item.get("value"))

    # Imported here rather than at module scope: this package is imported from
    # AppConfig.ready(), and that module reaches into the ORM and the provider.
    from apps.channels import whatsapp_templates

    template = whatsapp_templates.sendable(config.get("template_id"), ctx.execution.channel_connection)
    if template is None:
        logger.warning(
            "Execution %s node %s names a WhatsApp template that is not approved on this channel; sending without one.",
            ctx.execution.pk,
            ctx.node_id,
        )
        return _Template()

    text = whatsapp_templates.rendered_text(template, values)
    return _Template(
        reference=template.reference,
        variables=tuple(sorted(values.items())),
        blocks=(OutboundText(text=text),) if text else (),
    )


# ---------------------------------------------------------------------------
# Rendering the config into channel-shaped blocks
# ---------------------------------------------------------------------------


def _blocks(ctx: NodeContext) -> list[Any]:
    rendered: list[Any] = []
    for block in ctx.config.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = ctx.render(block.get("text"))
            if text:
                rendered.append(OutboundText(text=text))
        elif kind in _MEDIA_KINDS:
            rendered.append(
                MediaBlock(kind=str(kind), url=_media_url(ctx, block), caption=ctx.render(block.get("caption")))
            )
        elif kind == "card":
            rendered.append(CardBlock(card=_card(ctx, block)))
        elif kind == "gallery":
            cards = [_card(ctx, card) for card in block.get("cards") or [] if isinstance(card, dict)]
            if cards:
                rendered.append(GalleryBlock(cards=tuple(cards)))
        elif kind == "sticker":
            url = _media_url(ctx, block)
            rendered.append(StickerBlock(url=url))
        elif kind == "location":
            try:
                lat = float(ctx.render(str(block.get("latitude") or 0.0)))
                lon = float(ctx.render(str(block.get("longitude") or 0.0)))
            except (ValueError, TypeError):
                lat, lon = 0.0, 0.0
            rendered.append(
                LocationBlock(
                    latitude=lat,
                    longitude=lon,
                    name=ctx.render(str(block.get("name") or "")),
                    address=ctx.render(str(block.get("address") or "")),
                )
            )
        elif kind == "contact":
            rendered.append(
                ContactBlock(
                    full_name=ctx.render(str(block.get("full_name") or "")),
                    phone=ctx.render(str(block.get("phone") or "")),
                    organization=ctx.render(str(block.get("organization") or "")),
                )
            )
        elif kind == "poll":
            opts = tuple(ctx.render(str(opt)) for opt in block.get("options") or [] if str(opt).strip())
            try:
                max_ans = int(block.get("max_answers") or 1)
            except (ValueError, TypeError):
                max_ans = 1
            rendered.append(
                PollBlock(
                    question=ctx.render(str(block.get("question") or "")),
                    options=opts,
                    max_answers=max_ans,
                )
            )
        elif kind == "list":
            sec_rendered = []
            for sec in block.get("sections") or []:
                if isinstance(sec, dict):
                    rows_rendered = []
                    for r in sec.get("rows") or []:
                        if isinstance(r, dict):
                            rows_rendered.append(
                                {
                                    "id": str(r.get("id") or ""),
                                    "title": ctx.render(str(r.get("title") or "")),
                                    "description": ctx.render(str(r.get("description") or "")),
                                }
                            )
                    sec_rendered.append(
                        {
                            "title": ctx.render(str(sec.get("title") or "")),
                            "rows": rows_rendered,
                        }
                    )
            rendered.append(
                ListBlock(
                    title=ctx.render(str(block.get("title") or "")),
                    description=ctx.render(str(block.get("description") or "")),
                    button_text=ctx.render(str(block.get("button_text") or "")),
                    footer_text=ctx.render(str(block.get("footer_text") or "")),
                    sections=tuple(sec_rendered),
                )
            )
    return rendered


def _card(ctx: NodeContext, card: dict[str, Any]) -> Card:
    """SPEC §11.1's card — the same four fields alone or inside a gallery."""
    url_button = card.get("url_button") if isinstance(card.get("url_button"), dict) else {}
    # Rendered once. It goes in two places — `Card.url` and the Button — and a
    # second `ctx.render` of the same string is a second substitution pass per
    # card, ten of them in a full gallery.
    button_url = ctx.render(url_button.get("url")) if url_button else ""
    buttons: tuple[Button, ...] = ()
    if url_button:
        buttons = (
            Button(
                # A card's URL button has no id in the schema and never comes
                # back as an event, so a stable synthetic one is enough for the
                # adapter to render it.
                id="url",
                label=ctx.render(url_button.get("label")),
                url=button_url,
            ),
        )
    return Card(
        title=ctx.render(card.get("title")),
        subtitle=ctx.render(card.get("subtitle")),
        image_url=_card_image(ctx, card.get("image")),
        url=button_url,
        buttons=buttons,
    )


def _card_image(ctx: NodeContext, reference: Any) -> str:
    """A card's ``image``: "Media library id or URL" (SPEC §11.1), in one field.

    Which one it is decides which path to take, so that is what is tested. An
    earlier version called :func:`resolve` unconditionally and caught
    ``MediaNotFoundError`` to mean "so it was a URL" — an exception on the
    *common* authoring choice, and two branches distinguished by a failure
    rather than by what the value is.
    """
    if not isinstance(reference, str) or not reference:
        return ""
    if _is_media_id(reference):
        try:
            return str(resolve(reference, workspace=ctx.workspace)["url"])
        except MediaNotFoundError:
            # A card is one block of a message and its image is decoration; a
            # deleted one loses the picture rather than the whole send.
            logger.warning("Execution %s: card image %r is not in the library.", ctx.execution.pk, reference)
            return ""
    return ctx.render(reference)


def _media_url(ctx: NodeContext, block: dict[str, Any]) -> str:
    """A deliverable URL for one media block — library id or plain URL.

    SPEC §11.1 allows either. A library id is resolved to a signed delivery URL
    at send time, so a block stores a stable id and the URL is minted fresh from
    whatever storage the deployment runs.
    """
    media_id = block.get("media_id")
    if isinstance(media_id, str) and media_id:
        try:
            return str(resolve(media_id, workspace=ctx.workspace)["url"])
        except MediaNotFoundError:
            # An explicit media_id that does not resolve is a deleted asset,
            # not a URL. The library's own contract: stop the message.
            raise _UnresolvableMediaError(f"media {media_id!r} is not in this workspace's library") from None

    url = ctx.render(block.get("url"))
    if not url:
        raise _UnresolvableMediaError("a media block carries neither a library id nor a URL")
    return url


def _is_media_id(reference: str) -> bool:
    """Whether this string is a library id rather than a URL."""
    try:
        UUID(reference)
    except ValueError:
        return False
    return True


def _buttons(ctx: NodeContext) -> list[Button]:
    buttons = []
    for button in ctx.config.get("buttons") or []:
        if not isinstance(button, dict) or not isinstance(button.get("id"), str):
            continue
        action = str(button.get("action") or "postback").lower()
        buttons.append(
            Button(
                id=button["id"],
                label=ctx.render(str(button.get("label") or "")),
                url=ctx.render(str(button.get("url") or "")) if action == "url" else "",
                action=action,
                copy_code=ctx.render(str(button.get("copy_code") or button.get("copyCode") or "")) if action == "copy" else "",
                phone_number=ctx.render(str(button.get("phone_number") or button.get("phone") or "")) if action == "call" else "",
                pix_key=ctx.render(str(button.get("pix_key") or button.get("key") or "")) if action == "pix" else "",
                pix_key_type=str(button.get("pix_key_type") or button.get("keyType") or "") if action == "pix" else "",
                pix_name=ctx.render(str(button.get("pix_name") or button.get("name") or "")) if action == "pix" else "",
                currency=str(button.get("currency") or "BRL"),
            )
        )
    return buttons


def _quick_replies(ctx: NodeContext) -> list[QuickReply]:
    return [
        QuickReply(id=reply["id"], label=ctx.render(reply.get("label")))
        for reply in ctx.config.get("quick_replies") or []
        if isinstance(reply, dict) and isinstance(reply.get("id"), str)
    ]


def _numbered_options(ctx: NodeContext, outbound: OutboundMessage) -> dict[str, str]:
    """``{"1": reply_id}`` for anything the channel had to number (SPEC §6.1).

    A platform that cannot carry every button appends the leftovers to the text
    as "Reply 1 for ...", and the contact is then expected to type a number. The
    adapter is where that rendering happens, but the answer key has to be in the
    **wait config**, which is written here — and nothing carries a value back
    from the send. So this recomputes it.

    Recomputing is exact rather than approximate: ``downgrade`` is pure by
    construction, documented as such, and reads only the static capability table
    that the adapter passes it. Same message, same platform, same numbering.
    Without this the numbered options are unreachable — ``_match_choice`` looks
    a reply up by button id and then by label, and "11" is neither — so a
    contact who does exactly what the message told them to do falls through to
    the retry or the default edge.

    Empty for the platforms and the messages where nothing overflowed, which is
    almost all of them.
    """
    connection = ctx.execution.channel_connection
    if connection is None:
        return {}
    try:
        capabilities = capabilities_for(connection.platform)
    except KeyError:
        return {}
    return downgrade(outbound, capabilities).numeric_replies


def _labels(
    buttons: list[Button],
    quick_replies: list[QuickReply],
    list_rows: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Rendered label -> id, for platforms that reply with text (SPEC §6.2).

    Built from the *rendered* labels rather than the config, so a quick reply
    reading "Yes, {{first_name}}" matches what the contact was actually shown.
    URL, call, copy, and pix buttons are excluded: they do not produce postbacks.
    """
    labels = {reply.label: reply.id for reply in quick_replies if reply.label}
    labels.update({
        button.label: button.id
        for button in buttons
        if button.label and not button.is_url and button.action not in ("call", "copy", "pix")
    })
    for row in list_rows or []:
        r_title = str(row.get("title") or "")
        r_id = str(row.get("id") or "")
        if r_title and r_id:
            labels[r_title] = r_id
    return labels
