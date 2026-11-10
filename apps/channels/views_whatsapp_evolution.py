"""Workspace-protected management and QR pairing views for WhatsApp (Evolution GO)."""
from __future__ import annotations

import logging
import secrets
import uuid
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.channels import ingest
from apps.channels.models import ChannelConnection, ConnectionStatus, EvolutionSession
from apps.channels.plan import plan_refusal
from apps.channels.providers.exceptions import APIError
from apps.common.outbound import BlockedURLError
from apps.channels.providers.whatsapp_evolution import EvolutionClient, WhatsAppEvolutionAdapter
from apps.common.platforms import Platform
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

logger = logging.getLogger(__name__)

RISK_NOTICE = (
    "Aviso: Esta conexão utiliza a API não oficial Evolution GO para WhatsApp. "
    "Conexões não oficiais não são autorizadas pela Meta e estão sujeitas a riscos de "
    "bloqueio, desconexões ou alterações no protocolo do WhatsApp. "
    "O BrightBean e a Evolution GO não oferecem garantia contra bloqueio de números."
)


def _owned(workspace_id: str, connection_id: str) -> ChannelConnection:
    connection = (
        ChannelConnection.objects.for_workspace(workspace_id)
        .filter(pk=connection_id, platform=Platform.WHATSAPP_EVOLUTION.value)
        .first()
    )
    if connection is None:
        raise Http404("Conexão Evolution GO não encontrada.")
    return connection


@login_required
@require_permission("manage_channels")
def whatsapp_evolution_connect(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Connect a new WhatsApp number via Evolution GO."""
    if request.method == "POST":
        name = (request.POST.get("display_name") or "WhatsApp (Evolution GO)").strip()[:200]
        api_url = (request.POST.get("api_url") or "").strip().rstrip("/")
        api_key = (request.POST.get("api_key") or "").strip()
        instance_name = (request.POST.get("instance_name") or name).strip()
        custom_instance_id = (request.POST.get("instance_id") or "").strip()
        accept_risk = request.POST.get("accept_risk")

        if accept_risk != "yes":
            messages.error(request, "Você deve reconhecer os riscos do canal não oficial antes de conectar.")
        elif not api_url:
            messages.error(request, "Informe a URL da Evolution GO.")
        elif not api_key:
            messages.error(request, "Informe a chave de API ou token da Evolution GO.")
        elif refusal := plan_refusal(request.workspace):
            messages.error(request, refusal)
        else:
            instance_id = custom_instance_id or f"evo_{uuid.uuid4().hex[:12]}"
            instance_token = secrets.token_urlsafe(24)

            # Check if external_id already exists in this deployment
            if ChannelConnection.objects.unscoped().filter(
                platform=Platform.WHATSAPP_EVOLUTION.value, external_id=instance_id
            ).exists():
                messages.error(request, "Já existe uma conexão com este Instance ID. Escolha outro ou deixe em branco.")
                return render(
                    request,
                    "channels/whatsapp_evolution_connect.html",
                    {
                        "risk_notice": RISK_NOTICE,
                        "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
                    },
                )

            connection = ChannelConnection(
                workspace=request.workspace,
                platform=Platform.WHATSAPP_EVOLUTION.value,
                display_name=name,
                external_id=instance_id,
                credentials={
                    "api_url": api_url,
                    "api_key": api_key,
                    "instance_token": instance_token,
                    "instance_id": instance_id,
                    "instance_name": instance_name,
                },
                status=ConnectionStatus.ACTIVE,
            )
            webhook_secret = connection.rotate_webhook_secret()

            try:
                with transaction.atomic():
                    connection.save()
                    session = EvolutionSession.objects.create(
                        connection=connection,
                        api_url=api_url,
                        instance_id=instance_id,
                        state="connecting",
                        risk_accepted_at=timezone.now(),
                    )
            except IntegrityError:
                messages.error(request, "Não foi possível criar esta conexão. Tente novamente.")
            else:
                # Setup instance on Evolution GO
                client = EvolutionClient(api_url=api_url, api_key=api_key, instance_id=instance_id)
                webhook_url = request.build_absolute_uri(
                    reverse("webhook_whatsapp_evolution", kwargs={"connection_id": connection.pk})
                ) + f"?secret={webhook_secret}"

                try:
                    # Attempt to create instance
                    client.create_instance(
                        instance_id=instance_id,
                        name=instance_name,
                        token=instance_token,
                    )
                except (APIError, BlockedURLError) as exc:
                    logger.warning("Evolution GO create_instance info: %s. Proceeding to connect.", exc)

                try:
                    # Connect and bind webhook
                    client.connect_instance(webhook_url=webhook_url)
                except (APIError, BlockedURLError) as exc:
                    logger.warning("Evolution GO connect_instance: %s", exc)
                    session.last_error = str(exc)
                    session.save(update_fields=["last_error", "updated_at"])

                messages.success(request, "Canal criado com sucesso! Escaneie o QR Code para conectar seu WhatsApp.")
                return redirect(
                    "channels:whatsapp_evolution_detail",
                    workspace_id=workspace_id,
                    connection_id=connection.pk,
                )

    return render(
        request,
        "channels/whatsapp_evolution_connect.html",
        {
            "risk_notice": RISK_NOTICE,
            "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
        },
    )


@login_required
@require_permission("manage_channels")
@require_GET
def whatsapp_evolution_detail(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """View instance pairing status, QR code, and connection controls."""
    connection = _owned(workspace_id, connection_id)
    session, _ = EvolutionSession.objects.get_or_create(
        connection=connection,
        defaults={
            "api_url": connection.credentials.get("api_url", ""),
            "instance_id": connection.external_id,
        },
    )

    webhook_url = request.build_absolute_uri(
        reverse("webhook_whatsapp_evolution", kwargs={"connection_id": connection.pk})
    )

    return render(
        request,
        "channels/whatsapp_evolution_detail.html",
        {
            "connection": connection,
            "session": session,
            "webhook_url": webhook_url,
            "risk_notice": RISK_NOTICE,
            "qr_url": reverse(
                "channels:whatsapp_evolution_qr",
                kwargs={"workspace_id": workspace_id, "connection_id": connection.pk},
            ),
            "status_url": reverse(
                "channels:whatsapp_evolution_status",
                kwargs={"workspace_id": workspace_id, "connection_id": connection.pk},
            ),
            "reconnect_url": reverse(
                "channels:whatsapp_evolution_reconnect",
                kwargs={"workspace_id": workspace_id, "connection_id": connection.pk},
            ),
            "disconnect_url": reverse(
                "channels:whatsapp_evolution_disconnect",
                kwargs={"workspace_id": workspace_id, "connection_id": connection.pk},
            ),
            "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
        },
    )


@login_required
@require_permission("manage_channels")
@require_GET
def whatsapp_evolution_qr(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Fetch live QR code from Evolution GO."""
    connection = _owned(workspace_id, connection_id)
    session, _ = EvolutionSession.objects.get_or_create(connection=connection)
    adapter = WhatsAppEvolutionAdapter()
    client = adapter.get_client(connection)

    qr_base64 = ""
    pairing_code = ""
    try:
        res = client.get_qr()
        # Evolution GO returns {"code": "...", "base64": "data:image/png;base64,...", "pairingCode": "..."}
        qr_base64 = res.get("base64") or res.get("qrcode") or ""
        pairing_code = res.get("pairingCode") or res.get("code") or ""
        if qr_base64:
            session.qr_code_base64 = qr_base64
            session.pairing_code = pairing_code
            session.state = "qrcode"
            session.save(update_fields=["qr_code_base64", "pairing_code", "state", "updated_at"])
    except (APIError, BlockedURLError) as exc:
        logger.warning("Could not fetch QR code from Evolution GO: %s", exc)

    if not qr_base64:
        qr_base64 = session.qr_code_base64

    # Ensure format is suitable for <img src="...">
    if qr_base64 and not qr_base64.startswith("data:image"):
        qr_base64 = f"data:image/png;base64,{qr_base64}"

    if request.headers.get("HX-Request") == "true":
        response = render(
            request,
            "channels/_whatsapp_evolution_qr.html",
            {
                "qr": qr_base64,
                "pairing_code": pairing_code or session.pairing_code,
                "connection": connection,
                "session": session,
                "qr_url": reverse(
                    "channels:whatsapp_evolution_qr",
                    kwargs={"workspace_id": workspace_id, "connection_id": connection.pk},
                ),
            },
        )
        response["Cache-Control"] = "no-store, private"
        return response

    return JsonResponse({"qr": qr_base64, "pairing_code": pairing_code})


@login_required
@require_permission("manage_channels")
@require_GET
def whatsapp_evolution_status(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Poll live connection status from Evolution GO."""
    connection = _owned(workspace_id, connection_id)
    session, _ = EvolutionSession.objects.get_or_create(connection=connection)
    adapter = WhatsAppEvolutionAdapter()
    client = adapter.get_client(connection)

    live_state = session.state
    phone_number = session.phone_number
    profile_name = session.profile_name

    try:
        res = client.get_status()
        # res has: {"instance": {"instanceName": "...", "status": "open", "owner": "..."}, "state": "open"}
        inst_info = res.get("instance") or {}
        st = res.get("state") or inst_info.get("status") or ""
        if st:
            live_state = st.lower()
            session.state = live_state
            if live_state in {"open", "connected"}:
                session.last_connected_at = session.last_connected_at or timezone.now()
                owner = inst_info.get("owner") or res.get("owner") or ""
                if owner:
                    phone_number = owner
                    session.phone_number = owner
                profile = inst_info.get("profileName") or res.get("profileName") or ""
                if profile:
                    profile_name = profile
                    session.profile_name = profile
                if connection.status != ConnectionStatus.ACTIVE:
                    connection.status = ConnectionStatus.ACTIVE
                    connection.save(update_fields=["status"])
            session.save(
                update_fields=[
                    "state",
                    "phone_number",
                    "profile_name",
                    "last_connected_at",
                    "updated_at",
                ]
            )
    except (APIError, BlockedURLError) as exc:
        logger.warning("Could not fetch status from Evolution GO: %s", exc)

    status_data = {
        "state": live_state,
        "is_connected": live_state in {"open", "connected"},
        "connection_status": connection.status,
        "phone_number": phone_number,
        "profile_name": profile_name,
        "last_connected_at": session.last_connected_at.isoformat() if session.last_connected_at else None,
        "last_error": session.last_error,
    }

    if request.headers.get("HX-Request") == "true":
        return render(
            request,
            "channels/_whatsapp_evolution_status.html",
            {
                "status_data": status_data,
                "connection": connection,
                "session": session,
                "status_url": reverse(
                    "channels:whatsapp_evolution_status",
                    kwargs={"workspace_id": workspace_id, "connection_id": connection.pk},
                ),
            },
        )

    response = JsonResponse(status_data)
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_permission("manage_channels")
@require_POST
def whatsapp_evolution_reconnect(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Trigger reconnect on Evolution GO."""
    connection = _owned(workspace_id, connection_id)
    session, _ = EvolutionSession.objects.get_or_create(connection=connection)
    adapter = WhatsAppEvolutionAdapter()
    client = adapter.get_client(connection)

    webhook_url = request.build_absolute_uri(
        reverse("webhook_whatsapp_evolution", kwargs={"connection_id": connection.pk})
    )
    if connection.webhook_secret:
        webhook_url += f"?secret={connection.webhook_secret}"

    try:
        client.connect_instance(webhook_url=webhook_url)
        session.state = "connecting"
        session.save(update_fields=["state", "updated_at"])
        messages.success(request, "Tentativa de reconexão iniciada. Verifique o QR Code se necessário.")
    except (APIError, BlockedURLError) as exc:
        messages.error(request, f"Erro ao reconectar na Evolution GO: {exc}")

    return redirect(
        "channels:whatsapp_evolution_detail",
        workspace_id=workspace_id,
        connection_id=connection.pk,
    )


@login_required
@require_permission("manage_channels")
@require_POST
def whatsapp_evolution_disconnect(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Disconnect session from WhatsApp."""
    connection = _owned(workspace_id, connection_id)
    session, _ = EvolutionSession.objects.get_or_create(connection=connection)
    adapter = WhatsAppEvolutionAdapter()
    client = adapter.get_client(connection)

    try:
        client.disconnect()
    except (APIError, BlockedURLError) as exc:
        logger.warning("Error disconnecting from Evolution GO: %s", exc)

    connection.status = ConnectionStatus.NEEDS_REAUTH
    connection.save(update_fields=["status"])
    session.state = "disconnected"
    session.last_disconnected_at = timezone.now()
    session.save(update_fields=["state", "last_disconnected_at", "updated_at"])

    messages.success(request, "WhatsApp desconectado. Você pode reconectar escaneando o QR Code novamente.")
    return redirect(
        "channels:whatsapp_evolution_detail",
        workspace_id=workspace_id,
        connection_id=connection.pk,
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_evolution_webhook(request: HttpRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Dedicated workspace-routed webhook callback for Evolution GO events."""
    connection = (
        ChannelConnection.objects.unscoped()
        .filter(pk=connection_id, platform=Platform.WHATSAPP_EVOLUTION.value)
        .first()
    )
    if not connection or connection.status == ConnectionStatus.DISABLED:
        return HttpResponse("Connection unavailable", status=404)

    adapter = WhatsAppEvolutionAdapter()
    if not adapter.verify_webhook(request, connection):
        return HttpResponse("Forbidden", status=403)

    events = adapter.parse_events(request, connection)
    if events:
        ingest.process_events(connection, events)

    return JsonResponse({"status": "ok"})
