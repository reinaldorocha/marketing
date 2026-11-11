"""Inbound lead webhooks for receiving external leads and triggering flows."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.api.models import InboundLeadWebhook, LeadWebhookLog
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.common.addresses import normalize_email, normalize_phone
from apps.common.shortcuts import get_scoped_object_or_404
from apps.contacts import services as contact_services
from apps.contacts.models import Contact
from apps.flows.engine.runner import EngineError, start_flow
from apps.flows.models import Flow, FlowStatus, StartedBy
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest
from apps.messaging.ingest import apply_opt_in
from apps.messaging.models import ContactChannelIdentity, OptInSource

logger = logging.getLogger(__name__)


def _extract_val(data: dict[str, Any], path: str) -> Any:
    """Extract nested dictionary value like 'customer.phone' or 'telefone'."""
    if not path or not isinstance(data, dict):
        return None
    curr: Any = data
    for part in path.split("."):
        if isinstance(curr, dict):
            curr = curr.get(part)
        else:
            return None
    return curr


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def lead_webhook_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """List all lead webhooks configured for this workspace."""
    webhooks = (
        InboundLeadWebhook.objects.for_workspace(request.workspace)
        .select_related("target_flow", "channel_connection")
        .order_by("-created_at")
    )
    flows = Flow.objects.for_workspace(request.workspace).filter(status=FlowStatus.ACTIVE)
    connections = ChannelConnection.objects.for_workspace(request.workspace).filter(status=ConnectionStatus.ACTIVE)

    return render(
        request,
        "api/lead_webhooks_list.html",
        {
            "webhooks": webhooks,
            "flows": flows,
            "connections": connections,
        },
    )


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def lead_webhook_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Create a new lead webhook endpoint."""
    name = (request.POST.get("name") or "Entrada de Leads").strip()[:200]
    flow_id = request.POST.get("target_flow")
    connection_id = request.POST.get("channel_connection")
    tags_raw = request.POST.get("default_tags", "")
    remove_tags_raw = request.POST.get("remove_tags", "")

    flow = None
    if flow_id and flow_id != "none":
        flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)

    connection = None
    if connection_id and connection_id != "none":
        connection = get_scoped_object_or_404(ChannelConnection, request.workspace, pk=connection_id)

    default_tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    remove_tags = [t.strip() for t in remove_tags_raw.split(",") if t.strip()]

    # Mapping fields from form
    field_mapping = {
        "name_field": request.POST.get("name_field", "name").strip(),
        "phone_field": request.POST.get("phone_field", "phone").strip(),
        "email_field": request.POST.get("email_field", "email").strip(),
        "tags_field": request.POST.get("tags_field", "tags").strip(),
        "opt_in_field": request.POST.get("opt_in_field", "opt_in").strip(),
    }

    webhook = InboundLeadWebhook(
        workspace=request.workspace,
        name=name,
        target_flow=flow,
        channel_connection=connection,
        field_mapping=field_mapping,
        default_tags=default_tags,
        remove_tags=remove_tags,
    )
    secret = webhook.rotate_secret()
    webhook.save()

    messages.success(request, f"Webhook de leads '{name}' criado com sucesso! Guarde o token de autenticação.")
    return redirect("lead_webhooks:detail", workspace_id=workspace_id, webhook_id=webhook.pk)


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def lead_webhook_update(request: WorkspaceRequest, workspace_id: str, webhook_id: str) -> HttpResponse:
    """Update settings and field mapping of an existing lead webhook."""
    webhook = get_scoped_object_or_404(InboundLeadWebhook, request.workspace, pk=webhook_id)
    name = (request.POST.get("name") or webhook.name).strip()[:200]
    flow_id = request.POST.get("target_flow")
    connection_id = request.POST.get("channel_connection")
    tags_raw = request.POST.get("default_tags", "")
    remove_tags_raw = request.POST.get("remove_tags", "")

    flow = None
    if flow_id and flow_id != "none":
        flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)

    connection = None
    if connection_id and connection_id != "none":
        connection = get_scoped_object_or_404(ChannelConnection, request.workspace, pk=connection_id)

    default_tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    remove_tags = [t.strip() for t in remove_tags_raw.split(",") if t.strip()]

    field_mapping = {
        "name_field": request.POST.get("name_field", "name").strip(),
        "phone_field": request.POST.get("phone_field", "phone").strip(),
        "email_field": request.POST.get("email_field", "email").strip(),
        "tags_field": request.POST.get("tags_field", "tags").strip(),
        "opt_in_field": request.POST.get("opt_in_field", "opt_in").strip(),
    }

    webhook.name = name
    webhook.target_flow = flow
    webhook.channel_connection = connection
    webhook.default_tags = default_tags
    webhook.remove_tags = remove_tags
    webhook.field_mapping = field_mapping
    webhook.save()

    messages.success(request, f"Webhook '{name}' atualizado com sucesso!")
    return redirect("lead_webhooks:detail", workspace_id=workspace_id, webhook_id=webhook.pk)


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def lead_webhook_test_event(request: WorkspaceRequest, workspace_id: str, webhook_id: str) -> HttpResponse:
    """Simulate receiving a test event payload or load sample payload (Hotmart, Kiwify, etc.)."""
    webhook = get_scoped_object_or_404(InboundLeadWebhook, request.workspace, pk=webhook_id)
    sample_type = request.POST.get("sample_type", "kiwify_pix")

    samples = {
        "kiwify_pix": {
            "event": "pix_generated",
            "Customer": {
                "full_name": "Marcos Oliveira",
                "email": "marcos@exemplo.com",
                "mobile": "+5511988887777",
            },
            "order_id": "kiw_987654321",
            "order_status": "waiting_payment",
            "pix_code": "00020126580014br.gov.bcb.pix0136123e4567-e89b-12d3-a456-426614174000520400005303986540510.005802BR5913Prof Jonathan6009SAO PAULO62070503***6304ABCD",
        },
        "kiwify_approved": {
            "event": "order_approved",
            "Customer": {
                "full_name": "Marcos Oliveira",
                "email": "marcos@exemplo.com",
                "mobile": "+5511988887777",
            },
            "order_id": "kiw_987654321",
            "order_status": "paid",
        },
        "hotmart_pix": {
            "event": "PURCHASE_BILLET_PRINTED",
            "data": {
                "buyer": {
                    "name": "Carla Mendes",
                    "email": "carla@exemplo.com",
                    "checkout_phone": "+5521977776666",
                },
                "purchase": {
                    "status": "WAITING_PAYMENT",
                    "transaction": "HP123456789",
                },
            },
        },
        "cart_abandoned": {
            "event": "cart_abandoned",
            "customer": {
                "name": "Lucas Souza",
                "phone": "+5531999998888",
                "email": "lucas@exemplo.com",
            },
            "checkout_url": "https://pay.exemplo.com/carrinho/abc123xyz",
        },
    }

    chosen_payload = samples.get(sample_type, samples["kiwify_pix"])
    webhook.recent_test_payload = chosen_payload
    webhook.save(update_fields=["recent_test_payload"])
    messages.success(request, f"Payload de exemplo '{sample_type}' carregado para teste e mapeamento visual.")
    return redirect("lead_webhooks:detail", workspace_id=workspace_id, webhook_id=webhook.pk)


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def lead_webhook_detail(request: WorkspaceRequest, workspace_id: str, webhook_id: str) -> HttpResponse:
    """View details, endpoint URL, mapping, and logs for a lead webhook."""
    webhook = get_scoped_object_or_404(InboundLeadWebhook, request.workspace, pk=webhook_id)
    logs = webhook.logs.select_related("contact", "execution").order_by("-received_at")[:50]
    flows = Flow.objects.for_workspace(request.workspace).filter(status=FlowStatus.ACTIVE)
    connections = ChannelConnection.objects.for_workspace(request.workspace).filter(status=ConnectionStatus.ACTIVE)

    try:
        receiver_path = reverse("api:inbound_lead_receiver", kwargs={"webhook_id": webhook.pk})
    except NoReverseMatch:
        receiver_path = reverse("inbound_lead_receiver", kwargs={"webhook_id": webhook.pk})
    endpoint_url = request.build_absolute_uri(receiver_path)

    # Preview extraction against recent_test_payload if available
    mapping = webhook.field_mapping or {}
    test_payload = webhook.recent_test_payload or {}
    extracted_preview = None
    if test_payload:
        extracted_preview = {
            "name": _extract_val(test_payload, mapping.get("name_field", "name")),
            "phone": _extract_val(test_payload, mapping.get("phone_field", "phone")),
            "email": _extract_val(test_payload, mapping.get("email_field", "email")),
            "tags": _extract_val(test_payload, mapping.get("tags_field", "tags")),
            "opt_in": _extract_val(test_payload, mapping.get("opt_in_field", "opt_in")),
        }

    return render(
        request,
        "api/lead_webhook_detail.html",
        {
            "webhook": webhook,
            "endpoint_url": endpoint_url,
            "logs": logs,
            "flows": flows,
            "connections": connections,
            "extracted_preview": extracted_preview,
        },
    )


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def lead_webhook_rotate_secret(request: WorkspaceRequest, workspace_id: str, webhook_id: str) -> HttpResponse:
    """Rotate the secret token for this lead webhook."""
    webhook = get_scoped_object_or_404(InboundLeadWebhook, request.workspace, pk=webhook_id)
    new_secret = webhook.rotate_secret()
    webhook.save(update_fields=["secret_token", "secret_token_digest", "updated_at"])
    messages.success(request, "Novo segredo gerado com sucesso!")
    return redirect("lead_webhooks:detail", workspace_id=workspace_id, webhook_id=webhook.pk)


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def lead_webhook_delete(request: WorkspaceRequest, workspace_id: str, webhook_id: str) -> HttpResponse:
    """Delete a lead webhook."""
    webhook = get_scoped_object_or_404(InboundLeadWebhook, request.workspace, pk=webhook_id)
    webhook.delete()
    messages.success(request, "Webhook de leads removido.")
    return redirect("lead_webhooks:list", workspace_id=workspace_id)


@csrf_exempt
@require_POST
def inbound_lead_receiver(request: HttpRequest, webhook_id: str) -> HttpResponse:
    """Public receiver endpoint for inbound lead webhooks.

    1. Authenticates secret token
    2. Deduplicates repeated requests
    3. Maps and normalizes lead contact fields
    4. Finds or creates Contact without duplicates
    5. Binds channel identity (WhatsApp)
    6. Triggers configured flow
    7. Logs outcome
    """
    webhook = (
        InboundLeadWebhook.objects.unscoped()
        .filter(pk=webhook_id, is_active=True)
        .select_related("workspace", "target_flow", "channel_connection")
        .first()
    )
    if webhook is None:
        return JsonResponse({"error": "webhook_not_found"}, status=404)

    # 1. Secret authentication
    presented_secret = (
        request.headers.get("X-Webhook-Secret")
        or request.GET.get("secret")
        or ""
    )
    if not presented_secret and "Authorization" in request.headers:
        auth = request.headers["Authorization"].strip()
        if auth.lower().startswith("bearer "):
            presented_secret = auth[7:].strip()

    if not webhook.verify_secret(presented_secret):
        return JsonResponse({"error": "invalid_secret"}, status=403)

    # 2. Parse payload
    try:
        raw_body = request.body
        payload = json.loads(raw_body) if raw_body else {}
        if not isinstance(payload, dict):
            return JsonResponse({"error": "invalid_json_payload"}, status=400)
    except Exception as exc:
        return JsonResponse({"error": f"malformed_json: {exc}"}, status=400)

    # Save to recent_test_payload for visual preview / mapping
    try:
        webhook.recent_test_payload = payload
        webhook.save(update_fields=["recent_test_payload"])
    except Exception:
        pass

    # Test mode: return immediately with parsed payload confirmation
    if request.headers.get("X-Test-Event") == "true" or request.GET.get("test") in ("1", "true"):
        return JsonResponse({
            "status": "test_received",
            "message": "Evento de teste recebido e salvo para mapeamento visual!",
            "payload": payload,
        }, status=200)

    # 3. Idempotency / Deduplication
    idempotency_key = str(
        payload.get("id")
        or payload.get("event_id")
        or payload.get("idempotency_key")
        or request.headers.get("X-Idempotency-Key")
        or ""
    )
    if not idempotency_key:
        idempotency_key = f"sha256:{hashlib.sha256(raw_body).hexdigest()}"

    recent_dup = LeadWebhookLog.objects.filter(
        webhook=webhook,
        idempotency_key=idempotency_key,
        status="success",
        received_at__gte=timezone.now() - timedelta(days=7),
    ).first()
    if recent_dup:
        return JsonResponse(
            {
                "status": "duplicate",
                "message": "Event already processed",
                "log_id": str(recent_dup.pk),
            },
            status=200,
        )

    lock_key = f"lead_webhook_lock:{webhook.pk}:{idempotency_key}"
    if not cache.add(lock_key, "locked", timeout=60):
        return JsonResponse(
            {
                "status": "duplicate",
                "message": "Event is currently being processed",
            },
            status=200,
        )

    pending_log = (
        LeadWebhookLog.objects.filter(
            webhook=webhook,
            idempotency_key=idempotency_key,
            status="pending",
        ).first()
    )
    if pending_log:
        if (timezone.now() - pending_log.received_at).total_seconds() > 300:
            pending_log.status = "error"
            pending_log.error_message = "Pending processing timed out"
            pending_log.save(update_fields=["status", "error_message"])
        else:
            cache.delete(lock_key)
            return JsonResponse(
                {
                    "status": "duplicate",
                    "message": "Event is currently being processed",
                },
                status=200,
            )

    try:
        with transaction.atomic():
            log = LeadWebhookLog.objects.create(
                webhook=webhook,
                received_at=timezone.now(),
                status="pending",
                idempotency_key=idempotency_key,
                payload=payload,
            )
    except IntegrityError:
        cache.delete(lock_key)
        return JsonResponse(
            {
                "status": "duplicate",
                "message": "Event is currently being processed",
            },
            status=200,
        )

    # 4. Extract mapped fields
    mapping = webhook.field_mapping or {}
    name_field = mapping.get("name_field") or "name"
    phone_field = mapping.get("phone_field") or "phone"
    email_field = mapping.get("email_field") or "email"
    tags_field = mapping.get("tags_field") or "tags"
    opt_in_field = mapping.get("opt_in_field") or "opt_in"

    raw_name = str(_extract_val(payload, name_field) or "").strip()
    raw_phone = str(_extract_val(payload, phone_field) or "").strip()
    raw_email = str(_extract_val(payload, email_field) or "").strip()
    raw_tags = _extract_val(payload, tags_field) or []
    raw_opt_in = _extract_val(payload, opt_in_field)

    norm_phone = normalize_phone(raw_phone)
    if not norm_phone and raw_phone:
        # Fallback if bare international digits
        clean_d = "".join(ch for ch in raw_phone if ch.isdigit())
        if 8 <= len(clean_d) <= 15:
            norm_phone = f"+{clean_d}"

    norm_email = normalize_email(raw_email)

    first_name, last_name = "", ""
    if raw_name:
        parts = raw_name.split(" ", 1)
        first_name = parts[0][:150]
        last_name = parts[1][:150] if len(parts) > 1 else ""

    # 5. Find or create Contact in workspace (preserving identity-determined contact)
    conn = webhook.channel_connection
    channel_user_id = (norm_phone if norm_phone.startswith("+") else f"+{norm_phone}") if (conn and norm_phone) else ""
    existing_identity = None
    if conn and channel_user_id:
        existing_identity = (
            ContactChannelIdentity.objects.for_workspace(webhook.workspace)
            .filter(channel_connection=conn, platform_user_id=channel_user_id)
            .select_related("contact")
            .first()
        )

    contact = existing_identity.contact if existing_identity else None
    if not contact and norm_phone:
        contact = (
            Contact.objects.for_workspace(webhook.workspace)
            .filter(phone=norm_phone)
            .first()
        )
    if not contact and norm_email:
        contact_by_email = (
            Contact.objects.for_workspace(webhook.workspace)
            .filter(email=norm_email)
            .first()
        )
        if contact_by_email:
            if not norm_phone or not contact_by_email.phone or contact_by_email.phone == norm_phone:
                contact = contact_by_email
            else:
                logger.info(
                    "Lead webhook: contact with email %s has a different phone (%s vs %s); creating separate contact",
                    norm_email,
                    contact_by_email.phone,
                    norm_phone,
                )

    initial_contact = contact

    try:
        with transaction.atomic():
            if contact is None:
                contact = contact_services.create_contact(
                    workspace=webhook.workspace,
                    first_name=first_name,
                    last_name=last_name,
                    phone=norm_phone,
                    email=norm_email,
                )
            else:
                # Update existing contact if data is missing, treating conflicting email as divergence without merge
                update_kwargs: dict[str, Any] = {}
                if not contact.first_name and first_name:
                    update_kwargs["first_name"] = first_name
                if not contact.last_name and last_name:
                    update_kwargs["last_name"] = last_name
                if not contact.phone and norm_phone:
                    phone_conflict = (
                        Contact.objects.for_workspace(webhook.workspace)
                        .filter(phone=norm_phone)
                        .exclude(pk=contact.pk)
                        .exists()
                    )
                    if not phone_conflict:
                        update_kwargs["phone"] = norm_phone
                    else:
                        logger.warning(
                            "Lead webhook: phone conflict for contact %s: payload phone %s already belongs to another contact",
                            contact.pk,
                            norm_phone,
                        )

                if norm_email:
                    if contact.email and contact.email.lower() != norm_email.lower():
                        logger.warning(
                            "Lead webhook: email divergence for contact %s: payload email '%s' differs from registered email '%s'",
                            contact.pk,
                            norm_email,
                            contact.email,
                        )
                    elif not contact.email:
                        email_conflict = (
                            Contact.objects.for_workspace(webhook.workspace)
                            .filter(email=norm_email)
                            .exclude(pk=contact.pk)
                            .exists()
                        )
                        if email_conflict:
                            logger.warning(
                                "Lead webhook: email conflict for contact %s: payload email '%s' already belongs to another contact in workspace %s",
                                contact.pk,
                                norm_email,
                                webhook.workspace_id,
                            )
                        else:
                            update_kwargs["email"] = norm_email

                if update_kwargs:
                    contact_services.update_contact(contact, **update_kwargs)

            # Multi-event and rule matching
            raw_event = str(
                payload.get("event")
                or payload.get("status")
                or payload.get("order_status")
                or payload.get("type")
                or payload.get("event_type")
                or ""
            ).strip()
            rule_add_tags: list[str] = []
            rule_remove_tags: list[str] = []
            target_flow = webhook.target_flow

            for rule in (webhook.event_rules or []):
                if isinstance(rule, dict) and rule.get("event_value"):
                    ev_val = str(rule.get("event_value")).strip().lower()
                    if ev_val and ev_val == raw_event.lower():
                        rule_add_tags = rule.get("add_tags") or []
                        rule_remove_tags = rule.get("remove_tags") or []
                        if rule.get("target_flow_id"):
                            flow_cand = Flow.objects.for_workspace(webhook.workspace).filter(pk=rule.get("target_flow_id"), status=FlowStatus.ACTIVE).first()
                            if flow_cand:
                                target_flow = flow_cand
                        break

            # Remove tags
            all_remove_tags = list(webhook.remove_tags or [])
            all_remove_tags.extend(rule_remove_tags)
            for r_tag in all_remove_tags:
                r_clean = str(r_tag).strip()
                if r_clean:
                    from apps.contacts.models import Tag
                    t_obj = Tag.objects.for_workspace(webhook.workspace).filter(name__iexact=r_clean).first()
                    if t_obj:
                        contact_services.remove_tag(contact, t_obj)

            # Apply default and payload tags
            all_tags = list(webhook.default_tags or [])
            all_tags.extend(rule_add_tags)
            if isinstance(raw_tags, list):
                all_tags.extend(str(t).strip() for t in raw_tags if str(t).strip())
            elif isinstance(raw_tags, str) and raw_tags.strip():
                all_tags.extend(str(t).strip() for t in raw_tags.split(",") if str(t).strip())

            for tag_name in all_tags:
                tag_clean = str(tag_name).strip()
                if tag_clean:
                    tag_obj, _ = contact_services.get_or_create_tag(webhook.workspace, tag_clean)
                    contact_services.add_tag(contact, tag_obj)

            # 6. Bind channel identity (WhatsApp)
            identity = existing_identity
            if conn and channel_user_id and identity is None:
                identity = (
                    ContactChannelIdentity.objects.for_workspace(webhook.workspace)
                    .filter(
                        contact=contact,
                        channel_connection=conn,
                    )
                    .first()
                )
                if identity is None:
                    identity = ContactChannelIdentity.objects.for_workspace(webhook.workspace).create(
                        contact=contact,
                        channel_connection=conn,
                        platform=conn.platform,
                        platform_user_id=channel_user_id,
                    )

            # 7. Consent / Opt-in registration
            # "Não marque o contato como autorizado automaticamente apenas porque recebeu um telefone.
            # Permita registrar o consentimento quando ele vier comprovado da origem."
            if identity and raw_opt_in in (True, "true", "True", "yes", "1", 1):
                apply_opt_in(identity, source=OptInSource.API.value)

            # 8. Start the configured flow (if defined)
            execution = None
            if target_flow:
                execution = start_flow(
                    contact=contact,
                    flow=target_flow,
                    connection=webhook.channel_connection,
                    variables=payload,
                    started_by=StartedBy.API,
                )

            # 9. Complete delivery log
            log.status = "success"
            log.contact = contact
            log.execution = execution
            if not target_flow:
                log.error_message = "Atualização de contato e etiquetas concluída (sem fluxo vinculado)."
            log.save(update_fields=["status", "contact", "execution", "error_message"])
            cache.delete(lock_key)

            return JsonResponse(
                {
                    "status": "success",
                    "contact_id": str(contact.pk),
                    "execution_id": str(execution.pk) if execution else None,
                    "log_id": str(log.pk),
                    "tags_added": [str(t) for t in all_tags if t],
                    "tags_removed": [str(t) for t in all_remove_tags if t],
                },
                status=200,
            )

    except EngineError as exc:
        logger.warning("Lead webhook flow failed to start: %s", exc)
        cache.delete(lock_key)
        persisted_contact = None
        if initial_contact and getattr(initial_contact, "pk", None):
            try:
                persisted_contact = (
                    Contact.objects.for_workspace(webhook.workspace)
                    .filter(pk=initial_contact.pk)
                    .first()
                )
            except Exception:
                persisted_contact = None

        log.status = "error"
        log.contact = persisted_contact
        log.error_message = str(exc)
        try:
            log.save(update_fields=["status", "contact", "error_message"])
        except Exception:
            log.contact = None
            log.save(update_fields=["status", "contact", "error_message"])
        return JsonResponse({"error": f"flow_error: {exc}"}, status=422)
    except Exception as exc:
        logger.exception("Lead webhook processing failed: %s", exc)
        cache.delete(lock_key)
        persisted_contact = None
        if initial_contact and getattr(initial_contact, "pk", None):
            try:
                persisted_contact = (
                    Contact.objects.for_workspace(webhook.workspace)
                    .filter(pk=initial_contact.pk)
                    .first()
                )
            except Exception:
                persisted_contact = None

        log.status = "error"
        log.contact = persisted_contact
        log.error_message = str(exc)
        try:
            log.save(update_fields=["status", "contact", "error_message"])
        except Exception:
            log.contact = None
            log.save(update_fields=["status", "contact", "error_message"])
        return JsonResponse({"error": "internal_error", "detail": str(exc)}, status=500)
