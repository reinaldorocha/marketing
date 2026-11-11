"""Workspace lead-webhook settings page."""

import pytest

from apps.flows.models import Flow, FlowStatus


@pytest.mark.django_db
def test_list_loads_and_only_offers_active_flows(client_for, tenancy):
    active = Flow.objects.create(workspace=tenancy.workspace, name="Ativo", status=FlowStatus.ACTIVE)
    Flow.objects.create(workspace=tenancy.workspace, name="Rascunho", status=FlowStatus.DRAFT)

    response = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.pk}/settings/lead-webhooks/")

    assert response.status_code == 200
    assert list(response.context["flows"]) == [active]
