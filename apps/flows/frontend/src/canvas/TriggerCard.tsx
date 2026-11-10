/**
 * What starts the flow, as the first card on the canvas.
 *
 * A flow reads as one sentence — this happens, then this — and the canvas used
 * to start halfway through it: the card badged "Starts here" was a step that
 * answers something, with no sign of what.
 *
 * **It looks like a step on purpose.** An earlier cut gave it its own chrome to
 * say "this is not a step", and what that actually communicated was "this is
 * not part of the flow" — which is the opposite of why it is here. It uses
 * `.fb-node` and the same header/title/body structure, so it stays identical to
 * a step card by construction rather than by two sets of values kept in step.
 * The one difference is the accent, which comes from `--flow-accent` like every
 * other card's does.
 *
 * **It is not a node, and must never become one.** Triggers are `Trigger` rows,
 * not graph nodes: they carry a platform binding, a priority that is
 * workspace-wide, and an enabled flag, none of which a graph node has. So this
 * card is injected into the *projection* in store/selectors.ts and never into
 * `nodeType` / `nodeOrder`, which is what `toGraph()` serializes.
 *
 * Clicking it selects it, exactly as clicking a step does, and the left column
 * shows what can be changed — see editor/StepEditor.tsx. The fields themselves
 * still belong to the Django drawer: a second trigger editor in React would be
 * a second place for the platform gate to be wrong.
 */
import { Handle, Position as HandlePosition } from "@xyflow/react";
import { memo } from "react";

import { TRIGGER_PHRASE } from "../schema/plain";
import { useBuilder, useBuilderStore } from "../store/context";
import { platformLabel, triggerPlatforms } from "../editor/ChannelGuide";

/** The id the synthetic node and its edge are addressed by, and its React Flow type. */
export const TRIGGER_NODE_ID = "__trigger__";
export const TRIGGER_CARD_TYPE = TRIGGER_NODE_ID;

function TriggerCardInner() {
  const store = useBuilderStore();
  const triggers = useBuilder((state) => state.triggers);
  const selected = useBuilder((state) => state.triggerSelected);
  const enabled = triggers.filter((trigger) => trigger.enabled);
  const off = triggers.length > 0 && enabled.length === 0;

  return (
    <div
      className={["fb-node", "fb-node-trigger", selected ? "is-selected" : "", off ? "is-quiet" : ""]
        .filter(Boolean)
        .join(" ")}
      data-node-type={TRIGGER_NODE_ID}
      onClick={() => store.getState().selectTrigger()}
    >
      <div className="fb-node-header">
        <span className="fb-node-kind">
          <span className="fb-node-dot" aria-hidden="true" />
          {TRIGGER_PHRASE}
        </span>
      </div>

      {/* The title is the sentence, not the type name. "Keyword" over "quote,
          estimate, how much" was two fragments that read as one mashed line;
          `plain` is the whole thing — "When someone sends “quote”" — and it is
          the same sentence the flow list shows. */}
      <div className="fb-node-title">
        {triggers.length === 0 ? "Este fluxo ainda não tem um gatilho" : triggers[0]?.plain}
      </div>

      <div className="fb-node-body">
        {triggers.length === 0 ? (
          <span className="fb-empty">Clique para escolher o que inicia o fluxo.</span>
        ) : (
          <>
            {triggers.length > 1 ? (
              <span className="fb-empty">
                e mais {triggers.length - 1} gatilho{triggers.length === 2 ? "" : "s"}
              </span>
            ) : null}
            {off ? <span className="fb-node-warn">Desativado: nenhum contato chegará a este fluxo</span> : null}
            {triggerPlatforms(triggers).length > 0 ? (
              <span className="fb-trigger-platforms">
                {triggerPlatforms(triggers).map((platform) => (
                  <span key={platform} className="fb-trigger-platform">{platformLabel(platform)}</span>
                ))}
              </span>
            ) : null}
          </>
        )}
      </div>

      {/* Source only. Nothing routes *into* what starts the flow, and
          Canvas.tsx refuses a connection at either end of this id anyway. */}
      <Handle type="source" position={HandlePosition.Right} id="starts" isConnectable={false} />
    </div>
  );
}

export const TriggerCard = memo(TriggerCardInner);
