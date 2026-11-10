/**
 * What starts this flow, at the top of the left column.
 *
 * It used to be behind a "When it runs" button in the page header, which put
 * the single most important fact about a flow — whether anything will ever
 * reach it — two clicks away and invisible until you went looking. A flow with
 * no trigger looked exactly like a flow with one.
 *
 * **On and off are here; everything else is the drawer's.** Each trigger has
 * a switch (HANDOFF §3: "a switch per trigger"), posting the state to end in to
 * the builder API rather than flipping it, so a stale page cannot switch off a
 * trigger another tab just turned on. After it lands the page hears
 * `triggersChanged`, exactly as after a drawer edit, so the drawer and the
 * validation catch up the same way.
 *
 * **Editing still belongs to the Django drawer.** `templates/flows/_triggers_panel.html`
 * and the `flows:trigger_*` routes already create, bind, reorder and toggle
 * triggers, with the platform gate and the config schemas behind them. A second
 * editor in React would be a second place for that to be wrong, so this is a
 * display plus a door: the button dispatches the same `toggle-triggers` event
 * the header used to, and the drawer answers.
 *
 * The store's trigger list is refreshed on `triggersChanged` (see App.tsx), so
 * closing the drawer updates this without a reload.
 */
import { useState } from "react";

import { ApiError } from "../api/client";
import { setTriggerEnabled } from "../api/flows";
import { useBuilder, useBuilderStore } from "../store/context";
import { showToast } from "../toast";
import { platformLabel, triggerPlatforms } from "./ChannelGuide";

function openDrawer() {
  window.dispatchEvent(new CustomEvent("toggle-triggers", { bubbles: true }));
}

export function TriggerSection() {
  const store = useBuilderStore();
  const triggers = useBuilder((state) => state.triggers);
  const canEdit = useBuilder((state) => state.env.canEdit);
  const env = useBuilder((state) => state.env);
  const [busy, setBusy] = useState<string | null>(null);

  const flip = async (triggerId: string, enabled: boolean) => {
    setBusy(triggerId);
    try {
      const result = await setTriggerEnabled(env, triggerId, enabled);
      store.getState().setTriggers(result.triggers);
      document.body.dispatchEvent(new CustomEvent("triggersChanged"));
    } catch (error) {
      showToast({
        tone: "error",
        title: "Não alterado",
        body: error instanceof ApiError ? error.message : "Não foi possível alterar o gatilho.",
      });
    } finally {
      setBusy(null);
    }
  };
  const selected = useBuilder((state) => state.triggerSelected);
  const status = useBuilder((state) => state.flow?.status);
  const published = status === "active";
  const enabled = triggers.filter((trigger) => trigger.enabled);

  return (
    // Highlighted when the canvas card is selected, the same way a step's row
    // highlights — so clicking either one shows you which is which.
    <section className={selected ? "fb-trigger-section is-selected" : "fb-trigger-section"}>
      <h2 className="fb-trigger-heading">Quando executar</h2>

      {triggers.length === 0 ? (
        <>
          <p className="fb-trigger-empty">Este fluxo ainda não tem um gatilho e não será executado.</p>
          {canEdit ? (
            <button type="button" className="btn-pill-secondary btn-pill-sm mt-2.5" onClick={openDrawer}>
              Escolher gatilho
            </button>
          ) : null}
        </>
      ) : (
        <>
          <ul className="fb-trigger-list">
            {triggers.map((trigger) => (
              <li key={trigger.id} className={trigger.enabled ? "fb-trigger-row" : "fb-trigger-row is-off"}>
                <span className="min-w-0 flex-1">
                  <span className="fb-trigger-name block">{trigger.type_label}</span>
                  {!trigger.enabled ? <span className="fb-trigger-off">Desativado</span> : null}
                  <span className="fb-trigger-detail block">{trigger.summary}</span>
                  {triggerPlatforms([trigger]).length > 0 ? (
                    <span className="fb-trigger-platforms">
                      {triggerPlatforms([trigger]).map((platform) => (
                        <span key={platform} className="fb-trigger-platform">{platformLabel(platform)}</span>
                      ))}
                    </span>
                  ) : null}
                </span>
                {canEdit && env.triggerEnabledUrl ? (
                  <label className="switch mt-0.5" title={trigger.enabled ? "Desativar" : "Ativar"}>
                    <input
                      type="checkbox"
                      role="switch"
                      aria-label={`${trigger.type_label}: ${trigger.enabled ? "ativado" : "desativado"}`}
                      checked={trigger.enabled}
                      disabled={busy !== null}
                      onChange={(event) => void flip(trigger.id, event.target.checked)}
                    />
                    <span className="switch-track" />
                  </label>
                ) : null}
              </li>
            ))}
          </ul>
          {status === "offline" ? (
            // The list above still reads as "this is what starts it", and for
            // an offline flow none of it does — the toolbar pill is easy to miss.
            <p className="fb-trigger-empty mt-2">
              Este fluxo está desativado, então nenhum destes gatilhos o inicia. Eles voltarão a funcionar quando o fluxo for ativado.
            </p>
          ) : enabled.length === 0 ? (
            <p className="fb-trigger-empty mt-2">
              {published
                ? "Este fluxo está ativo, mas todos os gatilhos estão desativados. Ative um deles para iniciá-lo."
                : "Todos os gatilhos estão desativados, então o fluxo ainda não receberá contatos."}
            </p>
          ) : null}
          {canEdit ? (
            <button type="button" className="fb-link-button mt-1" onClick={openDrawer}>
              Gerenciar gatilhos
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}
