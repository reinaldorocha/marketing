/**
 * The outline: the builder's left column (HANDOFF §3, Flow builder).
 *
 * Two lists that answer the two questions a flow is made of — what starts it,
 * and what it does, in order. It replaced a trigger column on the left and a
 * step list stacked inside the inspector on the right: the steps are an index
 * of the canvas, and an index belongs beside the map rather than above the
 * settings of one step.
 */
import { useBuilder } from "../store/context";
import { StepList } from "./StepList";
import { TriggerSection } from "./TriggerSection";

export function TriggerSidebar() {
  const triggerCount = useBuilder((state) => state.triggers.length);
  const stepCount = useBuilder((state) => state.nodeOrder.length);

  return (
    <aside className="fb-trigger-sidebar" aria-label="Estrutura do fluxo">
      <TriggerSection />
      {triggerCount === 0 && stepCount === 0 ? (
        <section className="fb-editor-start">
          <p className="fb-editor-start-title">Um fluxo tem duas partes</p>
          <p className="fb-editor-start-body">
            O que o inicia e o que ele faz. Escolha o gatilho aqui e depois adicione a primeira etapa no desenho.
          </p>
        </section>
      ) : null}
      {stepCount > 0 ? (
        <section className="fb-outline-steps">
          <h2 className="fb-trigger-heading">Etapas</h2>
          <StepList />
        </section>
      ) : null}
    </aside>
  );
}
