from django.utils import timezone
"""Instagram's guided connect flow and the comment trigger's post picker (issue #17).

Separate from :mod:`apps.channels.views` because the frame and the flow are
different things — that module ships the connection row, its status and its
webhook URL, and issue #4's docstring is explicit that each platform's real
connect flow belongs to that platform's own issue.

Separate from :mod:`apps.channels.providers.instagram` because the adapter is the
thing four more platforms copy and a view is not part of it. The adapter owns the
Graph API; this module owns what an operator sees. The token exchange itself is a
third thing again, in :mod:`apps.channels.instagram_oauth`.

Three routes:

``instagram/connect/``
    Workspace-scoped, ``manage_channels``. Explains what will happen, then sends
    the operator to Meta with a signed ``state``.

``/channels/instagram/callback/``
    **Not** workspace-scoped, and it cannot be: Meta matches a redirect URI
    against the app's configuration exactly, so one app has one callback URL for
    the whole deployment. The workspace travels in the signed ``state`` and is
    re-authorised here, against the signed-in user, before anything is exchanged
    or written.

``instagram/posts/``
    The post picker for SPEC §10's comment trigger. ``edit_flows`` rather than
    ``manage_channels``, for the same reason ``telegram_preview`` is: the person
    using it is a flow author who may well not administer channels, and it reads
    the connection without changing anything about it.
"""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.channels import instagram_oauth as oauth
from apps.channels.forms import DUPLICATE_ACCOUNT_ERROR
from apps.channels.models import ChannelConnection, ConnectionStatus, InstagramProfileSettings
from apps.flows.models import Flow, FlowStatus
from apps.channels.plan import plan_refusal
from apps.channels.providers import instagram
from apps.channels.providers.exceptions import APIError
from apps.common.platforms import Platform
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.members.requests import WorkspaceRequest

logger = logging.getLogger(__name__)

__all__ = [
    "instagram_callback",
    "instagram_connect",
    "instagram_diagnostics",
    "instagram_follower_settings",
    "instagram_ice_breakers",
    "instagram_persistent_menu",
    "instagram_posts",
]

#: Shown when this deployment has no Meta app credentials to start an OAuth flow
#: with. Names where they go and never a value.
NO_CREDENTIALS = (
    "This deployment has no Instagram app credentials yet. Set "
    "PLATFORM_INSTAGRAM_CLIENT_ID and PLATFORM_INSTAGRAM_CLIENT_SECRET in the "
    "environment. See docs/channels/instagram.md."
)

#: Shown when Meta refuses the code, the token exchange fails, or the profile
#: comes back unusable. Deliberately one message for every reason: the operator's
#: next step is the same in all of them (start again), and distinguishing them
#: would be an oracle for which app ids and codes are real.
REJECTED_MESSAGE = (
    "Instagram did not complete the connection. Start again from Settings -> Channels, "
    "and check that this deployment's callback URL is listed in your Meta app."
)

#: Shown when the ``state`` does not verify. Its own message because the fix is
#: genuinely different — an expired state means "you left the tab open", and it
#: is the only one of these an operator can cause by being slow.
STATE_REJECTED = (
    "That connection attempt could not be verified, so nothing was changed. This usually "
    "means it took more than ten minutes, or the link was not started from this app. "
    "Please start again."
)


@login_required
@require_permission("manage_channels")
def instagram_connect(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Start the OAuth round trip for an Instagram professional account.

    ``GET`` explains what is about to happen; ``POST`` redirects to Meta. A
    redirect on POST rather than a link on the page, so the ``state`` is minted
    at the moment the operator acts rather than whenever the page happened to be
    rendered — a page left open overnight would otherwise carry a state that
    expires before it is used, which reads as a bug rather than as a timeout.
    """
    error = ""
    if request.method == "POST":
        try:
            client_id, _ = oauth.app_credentials(request.workspace)
        except oauth.InstagramCredentialsMissingError:
            error = NO_CREDENTIALS
        else:
            state = oauth.sign_state(workspace_id=request.workspace.pk, user_id=request.user.pk)
            return redirect(oauth.authorize_url(client_id=client_id, state=state))

    return render(
        request,
        "channels/instagram_connect.html",
        {
            "error": error,
            "callback_url": oauth.callback_url(),
            "scopes": oauth.SCOPES,
            "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
        },
    )


@login_required
@require_GET
def instagram_callback(request: Any) -> HttpResponse:
    """Meta's redirect back. Verifies ``state`` before touching anything.

    The order is the security property. ``state`` is checked **first**, then the
    signed-in user is compared against the one it was minted for, then that
    user's permission in the workspace it names — and only then is the code
    exchanged. Without that, anyone who could make an operator's browser land
    here with a code of their own would connect *their* Instagram account into
    the operator's workspace, and every DM to that account would start arriving
    in a stranger's inbox.

    A failed state answers **404**, not 403: this route is reachable without
    knowing anything about the deployment, and a distinguishable refusal would
    confirm that a workspace id or a user id names something real
    (SECURITY-BASELINE §1, and the same rule ``unsign_or_404`` applies).
    """
    payload = oauth.read_state(request.GET.get("state", ""))
    if payload is None:
        logger.info("Instagram callback: the state parameter did not verify.")
        raise Http404(STATE_REJECTED)

    if str(request.user.pk) != payload["u"]:
        # The state was minted for somebody else. A shared machine, a stale tab,
        # or an attempt to graft a connection onto another person's session.
        logger.warning("Instagram callback: the state was minted for a different user.")
        raise Http404(STATE_REJECTED)

    membership = _membership(request.user, payload["ws"])
    if membership is None or not membership.effective_permissions.get("manage_channels", False):
        logger.warning("Instagram callback: the signed-in user may not manage channels in that workspace.")
        raise Http404(STATE_REJECTED)

    workspace = membership.workspace
    settings_url = reverse("channels:list", kwargs={"workspace_id": workspace.pk})

    error = request.GET.get("error_reason") or request.GET.get("error")
    if error:
        # The operator pressed Cancel on Meta's screen, or Meta refused. Neither
        # is our failure and neither needs a scary page.
        messages.info(request, "Instagram was not connected.")
        return redirect(settings_url)

    code = (request.GET.get("code") or "").strip()
    if not code:
        raise Http404(STATE_REJECTED)

    problem = _complete(request, workspace, code)
    if problem:
        messages.error(request, problem)
    return redirect(settings_url)


def _membership(user: Any, workspace_id: str) -> WorkspaceMembership | None:
    """The user's membership in the workspace the state names, or None.

    Resolved here rather than by ``RBACMiddleware`` because this route carries no
    ``workspace_id`` kwarg — see the module docstring on why it cannot. The
    permission decision still goes through ``effective_permissions``, which
    ``apps.members.models`` documents as the only place one is ever made.

    A malformed id is a miss rather than a 500: it arrives from a signed payload,
    but the row it names can have been deleted since.
    """
    try:
        return (
            WorkspaceMembership.objects.filter(
                user=user,
                workspace_id=workspace_id,
                workspace__is_archived=False,
            )
            .select_related("workspace__organization")
            .first()
        )
    except (ValueError, TypeError):
        return None


def _complete(request: Any, workspace: Any, code: str) -> str:
    """Exchange the code and store the connection. "" on success, else a message.

    The order is load-bearing, and mirrors ``views_telegram._connect``. The token
    exchanges and the profile read come **first**, before anything is written,
    because they are the only things that can tell us which account this is — the
    ``external_id`` every inbound webhook is resolved by — and because a code
    that does not work should leave no trace.
    """
    try:
        client_id, client_secret = oauth.app_credentials(workspace)
    except oauth.InstagramCredentialsMissingError:
        return NO_CREDENTIALS

    try:
        short_lived, _ = oauth.exchange_code(code=code, client_id=client_id, client_secret=client_secret)
        token, expires_at = oauth.exchange_for_long_lived(token=short_lived, client_secret=client_secret)
        profile = oauth.account_profile(token)
    except APIError:
        # No exception detail in the message and none in the log: this code path
        # is one of the few places a live token exists in plain text, and an
        # APIError's text names the host it came from (SECURITY-BASELINE §5).
        logger.info("Instagram connect: the token exchange failed for workspace %s.", workspace.pk)
        return REJECTED_MESSAGE

    # The organization's channel limit. See apps/channels/plan.py on why this
    # is called here in all six adapters rather than in one shared service.
    #
    # `workspace`, not `request.workspace`: this is the OAuth callback, mounted
    # at /channels/instagram/callback/ rather than under /w/<workspace_id>/, so
    # RBACMiddleware never resolves a workspace from the URL and
    # request.workspace is None or whichever one the user last visited. The
    # workspace travels in the signed `state` and arrives as this argument,
    # which is what every other line in this function uses.
    refusal = plan_refusal(workspace)
    if refusal:
        return refusal

    connection = ChannelConnection(
        workspace=workspace,
        platform=Platform.INSTAGRAM.value,
        display_name=f"@{profile['username']}"[:200],
        external_id=profile["user_id"],
        status=ConnectionStatus.ACTIVE,
    )
    oauth.store_credentials(connection, token=token, expires_at=expires_at, user_id=profile["user_id"])

    try:
        # The savepoint is not optional: an IntegrityError marks the surrounding
        # transaction unusable, so without atomic() here a duplicate account
        # would poison every query for the rest of the request rather than
        # becoming the message below. Same call ``views.connection_create`` makes.
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        existing = _reconnect(workspace, profile, token, expires_at)
        if existing is not None:
            messages.success(request, f"Reconnected {connection.display_name}.")
            return ""
        # SPEC §5's unique (platform, external_id) is deployment-wide, so this
        # can be another workspace's row. The wording never says which
        # (SECURITY-BASELINE §1).
        return DUPLICATE_ACCOUNT_ERROR

    messages.success(
        request,
        f"Connected {connection.display_name}. Subscribe your Meta app to this account's "
        f"webhook fields to start receiving messages. See docs/channels/instagram.md.",
    )
    return ""


def _reconnect(workspace: Any, profile: dict[str, str], token: str, expires_at: Any) -> ChannelConnection | None:
    """Refresh **this workspace's** existing row for the same account, if any.

    Reconnecting is the ordinary way out of ``needs_reauth``, and it has to work
    without the operator deleting the connection first — deleting would take its
    conversations, triggers and identities with it.

    Scoped to the workspace, so a row belonging to somebody else is not found and
    the caller falls through to the "already connected" message. That is the
    whole tenancy check on this path: without it, connecting an account another
    workspace holds would hand its token — and its inbound traffic — to whoever
    connected second.
    """
    connection = (
        ChannelConnection.objects.for_workspace(workspace)
        .filter(platform=Platform.INSTAGRAM.value, external_id=profile["user_id"])
        .first()
    )
    if connection is None:
        return None
    oauth.store_credentials(connection, token=token, expires_at=expires_at, user_id=profile["user_id"])
    connection.display_name = f"@{profile['username']}"[:200]
    connection.status = ConnectionStatus.ACTIVE
    connection.save(update_fields=["credentials", "display_name", "status", "updated_at"])
    logger.info("Instagram connection %s was reconnected.", connection.pk)
    return connection


@login_required
@require_permission("edit_flows")
@require_GET
def instagram_posts(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Recent posts, for the comment trigger's post picker (SPEC §10).

    The empty and error states are **200s with a reason**, not error statuses.
    The caller is an HTMX fragment inside the trigger drawer; a 4xx would send it
    down its error path and show a failure where "connect an Instagram account
    first" is an ordinary thing to render.
    """
    connection = (
        ChannelConnection.objects.for_workspace(request.workspace)
        .filter(platform=Platform.INSTAGRAM.value, status=ConnectionStatus.ACTIVE)
        .order_by("created_at")
        .first()
    )
    context: dict[str, Any] = {"posts": [], "reason": "", "connect_url": ""}
    if connection is None:
        context["reason"] = "Connect an Instagram account to pick posts from it."
        context["connect_url"] = reverse("channels:instagram_connect", kwargs={"workspace_id": workspace_id})
        return render(request, "channels/_instagram_posts.html", context)

    try:
        context["posts"] = instagram.recent_media(connection)
    except APIError:
        logger.info("Instagram post picker: /me/media was refused for connection %s.", connection.pk)
        context["reason"] = "Instagram would not list this account's posts. Reconnect the channel and try again."
    except Exception:
        logger.exception("Instagram post picker failed for connection %s.", connection.pk)
        context["reason"] = "Instagram's posts could not be loaded just now."
    if not context["posts"] and not context["reason"]:
        context["reason"] = "This account has no posts yet."
    return render(request, "channels/_instagram_posts.html", context)



def _get_or_create_instagram_settings(connection: ChannelConnection) -> InstagramProfileSettings:
    settings, _ = InstagramProfileSettings.objects.get_or_create(connection=connection)
    return settings


@login_required
@require_permission("manage_channels")
@require_http_methods(["GET", "POST"])
def instagram_diagnostics(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Run real-time diagnostics on an Instagram connection and return partial template or JSON."""
    connection = (
        ChannelConnection.objects.for_workspace(request.workspace)
        .filter(pk=connection_id, platform=Platform.INSTAGRAM.value)
        .first()
    )
    if connection is None:
        raise Http404("Instagram connection not found.")

    settings = _get_or_create_instagram_settings(connection)

    diag_data = instagram.run_instagram_diagnostics(connection)
    settings.last_diagnostics = diag_data
    settings.last_diagnostics_at = timezone.now()
    settings.save(update_fields=["last_diagnostics", "last_diagnostics_at", "updated_at"])

    context = {
        "connection": connection,
        "diagnostics": diag_data,
        "settings": settings,
    }
    return render(request, "channels/_instagram_diagnostics.html", context)


@login_required
@require_permission("manage_channels")
@require_http_methods(["GET", "POST"])
def instagram_ice_breakers(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Manage and sync Ice Breakers (up to 4 quick starter questions) with Meta."""
    connection = (
        ChannelConnection.objects.for_workspace(request.workspace)
        .filter(pk=connection_id, platform=Platform.INSTAGRAM.value)
        .first()
    )
    if connection is None:
        raise Http404("Instagram connection not found.")

    settings = _get_or_create_instagram_settings(connection)
    token = oauth.access_token(connection)
    error_msg = ""
    success_msg = ""

    if request.method == "POST":
        action = request.POST.get("action", "save_sync")

        if action == "delete":
            try:
                instagram.delete_ice_breakers(connection.external_id, token)
                settings.ice_breakers = []
                settings.ice_breakers_synced_at = None
                settings.save(update_fields=["ice_breakers", "ice_breakers_synced_at", "updated_at"])
                success_msg = "Iniciadores de conversa excluídos da Meta com sucesso."
            except Exception as exc:
                error_msg = f"Erro ao excluir iniciadores da Meta: {exc}"
        elif action == "fetch":
            try:
                res = instagram.get_messenger_profile(connection.external_id, token, "ice_breakers")
                data = res.get("data") or []
                if data and isinstance(data, list) and data[0].get("ice_breakers"):
                    raw_items = data[0]["ice_breakers"]
                    items = [
                        {"question": item.get("question", ""), "payload": item.get("payload", "")}
                        for item in raw_items
                    ]
                    settings.ice_breakers = items
                    settings.ice_breakers_synced_at = timezone.now()
                    settings.save(update_fields=["ice_breakers", "ice_breakers_synced_at", "updated_at"])
                    success_msg = f"{len(items)} iniciador(es) carregado(s) da Meta."
                else:
                    success_msg = "A Meta respondeu, mas não há iniciadores publicados ativos."
            except Exception as exc:
                error_msg = f"Erro ao carregar iniciadores da Meta: {exc}"
        else:  # save_sync
            # Extract up to 4 items from form
            items = []
            for i in range(1, 5):
                q = request.POST.get(f"question_{i}", "").strip()
                flow_id = request.POST.get(f"flow_id_{i}", "").strip()
                p = request.POST.get(f"payload_{i}", "").strip()
                if q:
                    if flow_id:
                        final_payload = f"flow:{flow_id}"
                    else:
                        final_payload = p or q
                    items.append({"question": q[:80], "payload": final_payload[:1000]})

            try:
                if items:
                    instagram.set_ice_breakers(connection.external_id, token, items)
                    settings.ice_breakers = items
                    settings.ice_breakers_synced_at = timezone.now()
                    settings.save(update_fields=["ice_breakers", "ice_breakers_synced_at", "updated_at"])
                    success_msg = f"{len(items)} iniciador(es) salvos e sincronizados na Meta com sucesso!"
                else:
                    instagram.delete_ice_breakers(connection.external_id, token)
                    settings.ice_breakers = []
                    settings.ice_breakers_synced_at = None
                    settings.save(update_fields=["ice_breakers", "ice_breakers_synced_at", "updated_at"])
                    success_msg = "Iniciadores limpos."
            except Exception as exc:
                error_msg = f"Falha ao sincronizar com a Meta: {exc}"

    raw_flows = (
        Flow.objects.for_workspace(request.workspace)
        .filter(status=FlowStatus.ACTIVE, versions__published=True)
        .distinct()
        .order_by("name")
    )
    available_flows = []
    for fl in raw_flows:
        fl.flow_payload = f"flow:{fl.id}"
        available_flows.append(fl)
    context = {
        "connection": connection,
        "settings": settings,
        "ice_breakers": settings.ice_breakers,
        "available_flows": available_flows,
        "error_msg": error_msg,
        "success_msg": success_msg,
    }
    return render(request, "channels/_instagram_ice_breakers.html", context)


@login_required
@require_permission("manage_channels")
@require_http_methods(["GET", "POST"])
def instagram_persistent_menu(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Manage and sync Instagram Direct persistent menu items with Meta."""
    connection = (
        ChannelConnection.objects.for_workspace(request.workspace)
        .filter(pk=connection_id, platform=Platform.INSTAGRAM.value)
        .first()
    )
    if connection is None:
        raise Http404("Instagram connection not found.")

    settings = _get_or_create_instagram_settings(connection)
    token = oauth.access_token(connection)
    error_msg = ""
    success_msg = ""

    if request.method == "POST":
        action = request.POST.get("action", "save_sync")

        if action == "delete":
            try:
                instagram.delete_persistent_menu(connection.external_id, token)
                settings.persistent_menu = []
                settings.persistent_menu_synced_at = None
                settings.save(update_fields=["persistent_menu", "persistent_menu_synced_at", "updated_at"])
                success_msg = "Menu persistente excluído da Meta com sucesso."
            except Exception as exc:
                error_msg = f"Erro ao excluir menu da Meta: {exc}"
        elif action == "fetch":
            try:
                res = instagram.get_messenger_profile(connection.external_id, token, "persistent_menu")
                data = res.get("data") or []
                if data and isinstance(data, list) and data[0].get("persistent_menu"):
                    raw_menu = data[0]["persistent_menu"]
                    actions = raw_menu[0].get("call_to_actions", []) if raw_menu else []
                    items = [
                        {
                            "type": item.get("type", "postback"),
                            "title": item.get("title", ""),
                            "url": item.get("url", ""),
                            "payload": item.get("payload", ""),
                        }
                        for item in actions
                    ]
                    settings.persistent_menu = items
                    settings.persistent_menu_synced_at = timezone.now()
                    settings.save(update_fields=["persistent_menu", "persistent_menu_synced_at", "updated_at"])
                    success_msg = f"{len(items)} item(ns) de menu carregado(s) da Meta."
                else:
                    success_msg = "A Meta respondeu, mas não há menu persistente publicado."
            except Exception as exc:
                error_msg = f"Erro ao consultar menu da Meta: {exc}"
        else:  # save_sync
            items = []
            titles = request.POST.getlist("menu_title")
            types = request.POST.getlist("menu_type")
            values = request.POST.getlist("menu_value")
            flow_ids = request.POST.getlist("menu_flow_id")

            for idx, (t, kind) in enumerate(zip(titles, types)):
                t = t.strip()
                val = values[idx].strip() if idx < len(values) else ""
                flow_id = flow_ids[idx].strip() if idx < len(flow_ids) else ""
                if not t:
                    continue
                if kind == "web_url":
                    items.append({"type": "web_url", "title": t[:30], "url": val})
                elif kind == "flow" and flow_id:
                    items.append({"type": "postback", "title": t[:30], "payload": f"flow:{flow_id}"})
                else:
                    items.append({"type": "postback", "title": t[:30], "payload": (val or t)[:1000]})

            try:
                if items:
                    instagram.set_persistent_menu(connection.external_id, token, items)
                    settings.persistent_menu = items
                    settings.persistent_menu_synced_at = timezone.now()
                    settings.save(update_fields=["persistent_menu", "persistent_menu_synced_at", "updated_at"])
                    success_msg = f"Menu salvo e sincronizado na Meta com {len(items)} item(ns)!"
                else:
                    instagram.delete_persistent_menu(connection.external_id, token)
                    settings.persistent_menu = []
                    settings.persistent_menu_synced_at = None
                    settings.save(update_fields=["persistent_menu", "persistent_menu_synced_at", "updated_at"])
                    success_msg = "Menu limpo e removido da Meta."
            except Exception as exc:
                error_msg = f"Falha ao sincronizar menu na Meta: {exc}"

    raw_flows = (
        Flow.objects.for_workspace(request.workspace)
        .filter(status=FlowStatus.ACTIVE, versions__published=True)
        .distinct()
        .order_by("name")
    )
    available_flows = []
    for fl in raw_flows:
        fl.flow_payload = f"flow:{fl.id}"
        available_flows.append(fl)
    context = {
        "connection": connection,
        "settings": settings,
        "menu_items": settings.persistent_menu,
        "available_flows": available_flows,
        "error_msg": error_msg,
        "success_msg": success_msg,
    }
    return render(request, "channels/_instagram_menu.html", context)


@login_required
@require_permission("manage_channels")
@require_http_methods(["GET", "POST"])
def instagram_follower_settings(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Manage Follower Gate ('Exigir Seguidor') settings for Instagram."""
    connection = (
        ChannelConnection.objects.for_workspace(request.workspace)
        .filter(pk=connection_id, platform=Platform.INSTAGRAM.value)
        .first()
    )
    if connection is None:
        raise Http404("Instagram connection not found.")

    settings = _get_or_create_instagram_settings(connection)
    success_msg = ""

    if request.method == "POST":
        settings.require_follower = request.POST.get("require_follower") == "on"
        msg = request.POST.get("non_follower_message", "").strip()
        if msg:
            settings.non_follower_message = msg
        btn_follow = request.POST.get("follow_button_label", "").strip()
        if btn_follow:
            settings.follow_button_label = btn_follow[:50]
        btn_confirm = request.POST.get("confirm_button_label", "").strip()
        if btn_confirm:
            settings.confirm_button_label = btn_confirm[:50]

        settings.save(update_fields=[
            "require_follower",
            "non_follower_message",
            "follow_button_label",
            "confirm_button_label",
            "updated_at",
        ])
        success_msg = "Configurações da Trava de Seguidor salvas com sucesso!"

    context = {
        "connection": connection,
        "settings": settings,
        "success_msg": success_msg,
    }
    return render(request, "channels/_instagram_follower_gate.html", context)
