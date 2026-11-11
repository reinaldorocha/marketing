"""Workspace-scoped views.

Every route here takes ``workspace_id``, which is ``RBACMiddleware``'s entire
resolution contract: by the time a view body runs, ``request.workspace`` and
``request.workspace_membership`` are set, and a workspace the user cannot reach
has already answered 404.
"""

from typing import Any

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.common.validators import is_valid_hex_color
from apps.common.windows import is_valid_timezone, timezone_choices
from apps.flows.portability.library import gallery_entries
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.members.requests import WorkspaceRequest
from apps.workspaces.models import Workspace


@login_required
@require_GET
def dashboard(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The workspace landing page. Every role can see it.

    The KPI cards are issue #26's, and they are fetched through
    :func:`apps.analytics.selectors.dashboard_kpis` — a late import, because
    ``apps.analytics`` sits far above this app and a deployment without it must
    still have a landing page. No cards is a degraded dashboard; an ImportError
    is no dashboard at all.
    """
    permissions = request.workspace_membership.effective_permissions
    templates = gallery_entries()
    setup_steps = _setup_steps(request)
    setup_done = sum(1 for step in setup_steps if step["done"])
    return render(
        request,
        "workspaces/dashboard.html",
        {
            "kpis": _kpis(request),
            "greeting": _greeting(),
            "setup_steps": setup_steps,
            "setup_done": setup_done,
            "setup_remaining": len(setup_steps) - setup_done,
            "setup_percent": round(setup_done / len(setup_steps) * 100),
            # The whole panel goes away for good once setup is finished. A
            # permanent "you have done everything" card is furniture.
            "setup_incomplete": setup_done < len(setup_steps),
            "needs_you": _needs_you(request),
            # The shipped starter templates, four of them, with the real total
            # beside "Browse all" — the page counts what is in flow-templates/
            # rather than claiming a number. Bound once above: calling
            # gallery_entries() twice here read and revalidated every shipped
            # file twice.
            "flow_templates": _template_sample(templates),
            "flow_template_total": len(templates),
            # Each card links to the section that can explain its number, and a
            # link a viewer would be refused at is worse than no link — so the
            # cards that need a permission carry it rather than guessing from
            # the role.
            "can_view_analytics": permissions.get("view_analytics", False),
            "can_send_broadcasts": permissions.get("send_broadcasts", False),
            "can_edit_flows": permissions.get("edit_flows", False),
        },
    )


def _greeting() -> str:
    """Morning, afternoon or evening, in the server's local time.

    Not the viewer's: the User model carries a time zone, but Django is already
    activating it for this request, so ``localtime`` is the reader's clock
    wherever that is configured — and where it is not, the server's is a better
    guess than UTC.
    """
    hour = timezone.localtime().hour
    if hour < 12:
        return "Good morning"
    return "Good afternoon" if hour < 18 else "Good evening"


def _kpis(request: WorkspaceRequest) -> dict[str, Any] | None:
    """The dashboard's numbers, or ``None`` when they should not be shown.

    ``None`` for two reasons: no analytics app, or a member without
    ``view_analytics``. Every workspace role holds that key today
    (``apps.members.roles``), so the second is not currently reachable — it is
    written anyway, because a permission that is only enforced where it happens
    to matter is a permission nobody can safely narrow later.
    """
    if not apps.is_installed("apps.analytics"):
        return None
    if not request.workspace_membership.effective_permissions.get("view_analytics", False):
        return None
    from apps.analytics.selectors import dashboard_daily_messages, dashboard_kpis

    kpis = dashboard_kpis(request.workspace)
    series = dashboard_daily_messages(request.workspace)
    peak = max((day["messages_in"] for day in series), default=0)
    # The bar heights are worked out here rather than in the template: Django's
    # template language has no arithmetic, and the alternative is a widthratio
    # tag whose rounding nobody can predict. A flat-zero week gets flat bars
    # rather than a division by zero.
    kpis["daily_messages"] = [
        {**day, "height": round(day["messages_in"] / peak * 100) if peak else 0} for day in series
    ]
    return kpis


#: How many template tiles Home shows. Four, because the row is four wide.
TEMPLATE_TILES = 4


def _template_sample(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Four templates, spread across platforms rather than the first four.

    `gallery_entries` is sorted by platform so the full page reads in runs, and
    slicing that gave Home four Facebook Messenger tiles in a row — which looks
    like the only thing the product does. One per platform first, then whatever
    fills the row.
    """
    seen: set[str] = set()
    picked: list[dict[str, Any]] = []
    for entry in templates:
        if entry["platform_label"] not in seen:
            seen.add(entry["platform_label"])
            picked.append(entry)
    for entry in templates:
        if len(picked) >= TEMPLATE_TILES:
            break
        if entry not in picked:
            picked.append(entry)
    return picked[:TEMPLATE_TILES]


def _setup_steps(request: WorkspaceRequest) -> list[dict[str, Any]]:
    """The first-run checklist: connect a channel, build a flow, invite people.

    Read from what the workspace actually has rather than from a stored
    "onboarding state" field. Nothing to migrate, nothing to keep in sync, and
    a workspace that had a channel and lost it honestly goes back to step one.

    Ordered by dependency — a flow with no channel has nothing to reply on —
    and the first incomplete step is the one the page offers to do next.
    """
    from apps.channels.models import ChannelConnection, ConnectionStatus
    from apps.flows.models import Flow, FlowStatus

    workspace = request.workspace
    # ACTIVE, not merely present. `disabled` and `needs_reauth` are the two
    # states a connection sits in when it cannot deliver, so counting a row in
    # either of them ticks the step off and tells the reader "Messages arrive
    # here now" about a channel that receives nothing. Same rule
    # apps.flows.capabilities.connected_platforms applies.
    has_channel = ChannelConnection.objects.for_workspace(workspace).filter(status=ConnectionStatus.ACTIVE).exists()
    # Published is not the same as answering: a flow can go live with no trigger
    # at all, or with every trigger switched off, and the builder reports both as
    # non-blocking warnings rather than refusing the publish. Such a flow runs
    # for nobody, so it does not finish the step that claims one is answering.
    live_flows = (
        Flow.objects.for_workspace(workspace)
        .filter(status=FlowStatus.ACTIVE, triggers__enabled=True)
        .distinct()
        .count()
    )
    teammates = WorkspaceMembership.objects.filter(workspace=workspace).count()
    permissions = request.workspace_membership.effective_permissions

    steps = [
        {
            "key": "channel",
            "permission": "manage_channels",
            "title": "Conecte um canal",
            "body": "Onde as pessoas já falam com você: Telegram, Instagram, WhatsApp ou e-mail.",
            "cta": "Conectar canal",
            "url": reverse("channels:list", kwargs={"workspace_id": workspace.id}),
            "done": has_channel,
            "done_body": "As mensagens já chegam por aqui.",
        },
        {
            "key": "flow",
            "permission": "edit_flows",
            "title": "Crie seu primeiro fluxo",
            "body": "Um modelo já inclui o gatilho e as respostas prontas para você adaptar.",
            "cta": "Escolher modelo",
            "url": reverse("flows:list", kwargs={"workspace_id": workspace.id}),
            "done": live_flows > 0,
            "done_body": "Um fluxo já responde por você.",
        },
        {
            "key": "team",
            "title": "Convide sua equipe",
            "body": "Compartilhe a caixa de entrada para que todas as respostas não dependam de você.",
            "cta": "Convidar pessoas",
            "url": reverse("members:list"),
            "done": teammates > 1,
            "done_body": "Outras pessoas também podem responder.",
            # members:list is gated on an organisation role rather than a
            # workspace permission, and every member holds "member".
            "permission": "",
        },
    ]
    # Exactly one step is "current": the first unfinished one. The rest are
    # either done or not yet reachable, and showing three live calls to action
    # is the thing the redesign is trying to stop.
    current = next((step for step in steps if not step["done"]), None)
    for index, step in enumerate(steps, start=1):
        step["number"] = index
        step["current"] = step is current
        # A call to action nobody may act on is worse than none: every one of
        # these links at a view gated on the key beside it, so an Agent opening
        # Home would have been sent to a 403. The step still shows — what the
        # workspace is missing is worth knowing either way — it just stops
        # offering to do it for somebody who cannot.
        needed = str(step["permission"])
        step["actionable"] = not needed or permissions.get(needed, False)
    return steps


def _needs_you(request: WorkspaceRequest) -> list[dict[str, Any]]:
    """The short list of things actually waiting on a person.

    Each row is a real state the product already models — a revoked channel, an
    unassigned conversation, a broadcast about to go out — rather than a
    notification feed. Empty is a good answer and renders nothing.
    """
    from apps.broadcasts.models import Broadcast, BroadcastStatus
    from apps.channels.models import ChannelConnection, ConnectionStatus
    from apps.messaging.models import Conversation, ConversationState

    workspace = request.workspace
    permissions = request.workspace_membership.effective_permissions
    items: list[dict[str, Any]] = []

    if permissions.get("manage_channels", False):
        for connection in ChannelConnection.objects.for_workspace(workspace).filter(
            status=ConnectionStatus.NEEDS_REAUTH
        )[:3]:
            items.append(
                {
                    "tone": "bad",
                    "title": f"{connection.display_name} precisa ser reconectado",
                    "body": "Ele parou de receber mensagens. Reconecte para continuar.",
                    "url": reverse(
                        "channels:detail",
                        kwargs={"workspace_id": workspace.id, "connection_id": connection.id},
                    ),
                }
            )

    if permissions.get("use_inbox", False):
        unassigned = (
            Conversation.objects.for_workspace(workspace)
            .filter(state=ConversationState.OPEN, assignee__isnull=True)
            .count()
        )
        if unassigned:
            items.append(
                {
                    "tone": "warn",
                    "title": f"{unassigned} conversa{'s' if unassigned != 1 else ''} sem responsável",
                    "body": "Ninguém assumiu essas conversas ainda.",
                    "url": reverse("inbox:list", kwargs={"workspace_id": workspace.id}),
                }
            )

    if permissions.get("send_broadcasts", False):
        for broadcast in Broadcast.objects.for_workspace(workspace).filter(status=BroadcastStatus.SCHEDULED)[:2]:
            items.append(
                {
                    "tone": "quiet",
                    "title": f"“{broadcast.name}” is scheduled",
                    "body": "Review it before it goes.",
                    "url": reverse(
                        "broadcasts:detail",
                        kwargs={"workspace_id": workspace.id, "broadcast_id": broadcast.id},
                    ),
                }
            )
    return items


@login_required
@require_POST
def switch(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Make this the user's current workspace.

    No UI reaches this today: the sidebar switcher navigates straight to a
    workspace, and ``RBACMiddleware`` records ``last_workspace_id`` on the way
    through, which is why the dashboard's duplicate switcher panel could go. The
    route stays because it is the correct way to say "switch, and only switch",
    and because its tests are where cross-tenant switching is proved to 404.

    POST rather than Studio's GET link: it writes ``last_workspace_id``, and a
    state-changing GET is both CSRF-exposed and prefetchable. The membership
    check has already happened in the middleware, so reaching here means the
    switch is legitimate — a role check would be wrong, since which workspace
    you are looking at is a personal preference, not workspace data.

    The write is done here rather than left to the middleware's own
    keep-in-sync side effect. That side effect is an optimisation for ordinary
    navigation; making the switcher depend on it would mean any future narrowing
    of it — skipping GETs, gating it behind a check — silently turns this view
    into a redirect that changes nothing.
    """
    request.user.last_workspace_id = request.workspace.pk
    request.user.save(update_fields=["last_workspace_id", "updated_at"])
    return redirect(reverse("workspaces:dashboard", kwargs={"workspace_id": workspace_id}))


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def settings_view(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    # The stored value is passed in so a zone this list filters out — a legacy
    # alias, an Etc/* — still renders as the selected option rather than being
    # silently swapped for the first entry on the next save.
    return render(
        request,
        "workspaces/settings.html",
        {"timezone_choices": timezone_choices(request.workspace.timezone)},
    )


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def update_settings(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    workspace = request.workspace
    # Strip first, then reject: `or workspace.name` only catches an empty
    # string, so "   " would survive it and be stored as a nameless workspace.
    name = (request.POST.get("name") or "").strip()[:100]
    if not name:
        messages.error(request, "O espaço de trabalho precisa de um nome.")
        return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
    # (organization, name) is unique, so without this an ordinary rename onto a
    # sibling's name reaches the constraint and 500s. The create flow already
    # checks; this one has to as well.
    clash = Workspace.objects.for_org(workspace.organization_id).filter(name=name).exclude(pk=workspace.pk)
    if clash.exists():
        messages.error(request, "Já existe outro espaço de trabalho com esse nome nesta organização.")
        return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
    workspace.name = name
    workspace.icon = (request.POST.get("icon") or "").strip()[:8]
    workspace.description = (request.POST.get("description") or "").strip()[:500]
    # Blank means "use the organization default" (Workspace.effective_timezone),
    # so it stays allowed. Anything else has to be a real zone: the field has
    # been free text with no validation, and a typo does not raise — it reaches
    # ZoneInfo through effective_timezone, gets caught, and silently falls back,
    # which is a wrong-clock bug with nothing on screen to explain it. Checked
    # against is_valid_timezone rather than timezone_choices() so a legacy value
    # already in the database is not rejected the first time its owner opens
    # this page — the picker's list is narrower than the column's rule.
    submitted_timezone = (request.POST.get("timezone") or "").strip()[:63]
    if submitted_timezone and not is_valid_timezone(submitted_timezone):
        messages.error(request, "Esse fuso horário não é reconhecido.")
        return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
    workspace.timezone = submitted_timezone

    for field in ("primary_color", "secondary_color"):
        value = (request.POST.get(field) or "").strip()
        if not is_valid_hex_color(value):
            messages.error(request, "As cores devem usar seis dígitos hexadecimais, como #3B82F6.")
            return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
        setattr(workspace, field, value)

    workspace.save(
        update_fields=["name", "icon", "description", "timezone", "primary_color", "secondary_color", "updated_at"]
    )
    messages.success(request, "Configurações do espaço de trabalho salvas.")
    return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def operational_settings(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Consolidated operational settings: Meta app credentials, Instagram profile tools,
    WhatsApp instances, SMTP tests, and webhooks overview.
    """
    from apps.api.models import InboundLeadWebhook, OutboundWebhook
    from apps.channels.models import ChannelConnection
    from apps.common.platforms import Platform
    from apps.credentials.models import PlatformCredential, mask_credentials
    from apps.credentials.resolution import resolve_platform_credentials

    # 1. Meta App credentials (Instagram / Messenger)
    resolution = resolve_platform_credentials(Platform.INSTAGRAM.value, workspace=request.workspace)
    meta_is_configured = bool(resolution)
    meta_source = resolution.source
    meta_masked = mask_credentials(resolution.credentials) if meta_is_configured else {}

    # Check if configured at organization level
    org_cred = (
        PlatformCredential.objects.for_org(request.workspace.organization_id)
        .filter(platform=Platform.INSTAGRAM.value)
        .first()
    )

    # 2. WhatsApp Connections (Evolution GO and Cloud API)
    whatsapp_evolution_conns = ChannelConnection.objects.for_workspace(request.workspace).filter(
        platform=Platform.WHATSAPP_EVOLUTION.value
    )
    whatsapp_cloud_conns = ChannelConnection.objects.for_workspace(request.workspace).filter(
        platform=Platform.WHATSAPP.value
    )

    # 3. Instagram Connections
    instagram_conns = ChannelConnection.objects.for_workspace(request.workspace).filter(
        platform=Platform.INSTAGRAM.value
    )

    # 4. Webhooks
    lead_webhooks = InboundLeadWebhook.objects.for_workspace(request.workspace).order_by("-created_at")[:10]
    outbound_webhooks = OutboundWebhook.objects.for_workspace(request.workspace).order_by("-created_at")[:10]

    # Meta webhook callback URLs to show operator
    callback_path = reverse("channels:instagram_callback")
    meta_callback_url = request.build_absolute_uri(callback_path)
    meta_webhook_url = request.build_absolute_uri("/webhooks/instagram/")

    return render(
        request,
        "workspaces/operational_settings.html",
        {
            "meta_is_configured": meta_is_configured,
            "meta_source": meta_source,
            "meta_masked": meta_masked,
            "org_cred": org_cred,
            "meta_callback_url": meta_callback_url,
            "meta_webhook_url": meta_webhook_url,
            "whatsapp_evolution_conns": whatsapp_evolution_conns,
            "whatsapp_cloud_conns": whatsapp_cloud_conns,
            "instagram_conns": instagram_conns,
            "lead_webhooks": lead_webhooks,
            "outbound_webhooks": outbound_webhooks,
        },
    )


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def operational_meta_app_save(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Save Meta App credentials (client_id, client_secret, verify_token) directly to organization."""
    from apps.common.platforms import Platform
    from apps.credentials.models import PlatformCredential

    client_id = (request.POST.get("client_id") or "").strip()
    client_secret = (request.POST.get("client_secret") or "").strip()
    verify_token = (request.POST.get("verify_token") or "").strip()

    if not client_id or not client_secret:
        messages.error(request, "App ID (Client ID) e App Secret (Client Secret) são obrigatórios.")
        return redirect(reverse("workspaces:operational_settings", kwargs={"workspace_id": workspace_id}))

    creds = {
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if verify_token:
        creds["verify_token"] = verify_token

    org = request.workspace.organization
    PlatformCredential.objects.update_or_create(
        organization=org,
        platform=Platform.INSTAGRAM.value,
        defaults={"credentials": creds},
    )
    PlatformCredential.objects.update_or_create(
        organization=org,
        platform=Platform.MESSENGER.value,
        defaults={"credentials": creds},
    )

    messages.success(request, "Credenciais do Meta App (Instagram / Facebook) salvas e criptografadas com sucesso!")
    return redirect(reverse("workspaces:operational_settings", kwargs={"workspace_id": workspace_id}))


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def operational_smtp_test(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Test outbound SMTP email delivery."""
    from django.conf import settings
    from django.core.mail import send_mail

    recipient = (request.POST.get("test_email") or request.user.email or "").strip()
    if not recipient:
        messages.error(request, "Informe um endereço de e-mail de destino para o teste.")
        return redirect(reverse("workspaces:operational_settings", kwargs={"workspace_id": workspace_id}))

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@marketing.profjonathanrocha.com.br")

    try:
        sent = send_mail(
            subject="Teste de Envio SMTP · Marketing",
            message=f"Olá!\n\nEste é um e-mail de teste confirmando que a sua configuração SMTP está funcionando corretamente.\n\nEspaço: {request.workspace.name}\nData: {timezone.now().strftime('%d/%m/%Y %H:%M:%S')}",
            from_email=from_email,
            recipient_list=[recipient],
            fail_silently=False,
        )
        if sent:
            messages.success(request, f"E-mail de teste enviado com sucesso para {recipient}!")
        else:
            messages.warning(request, f"O servidor SMTP não reportou envio para {recipient}.")
    except Exception as exc:
        messages.error(request, f"Falha no envio SMTP: {exc}")

    return redirect(reverse("workspaces:operational_settings", kwargs={"workspace_id": workspace_id}))
