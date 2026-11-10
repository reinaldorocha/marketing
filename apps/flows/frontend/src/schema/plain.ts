/**
 * Reader-facing names for node types.
 *
 * The registry's own `label` is developer copy — it names the mechanism
 * ("Send Message", "Smart Delay") and its `description` quotes SPEC sections.
 * That is right for the registry, which is read by the API, the exporter and
 * whoever is adding a node type. It is the wrong register for a canvas somebody
 * is reading to work out what their flow does.
 *
 * So a card now carries two lines: an eyebrow saying what KIND of step this is
 * in the reader's words ("Then send", "Then decide"), and the node's own
 * title underneath.
 *
 * ## Why this is keyed by group, not by type
 *
 * Because that is the axis the phrasing actually varies on, and the groups are
 * already data: `apps/flows/schema/nodes.py` assigns every type to one of
 * content / logic / actions / other, and those four are exactly the four
 * phrases the design draws. Keying by type would mean a new entry for every
 * node type ever added and a blank eyebrow the day somebody forgot one.
 *
 * ## Why TypeScript and not the Python registry
 *
 * Three reasons, in order of weight. The registry's labels are a different
 * register and should stay literal. A new field on `NodeSpec` changes the
 * committed artefact at `static/flows/flow-schema.json`, so a copy tweak would
 * mean `make schema`, a re-commit and a look at the export tests. And the
 * mapping is not 1:1 with types anyway — it is group-shaped, and the group is
 * already exported.
 *
 * `plain.test.ts` asserts every registered type and group resolves to a
 * non-empty phrase, so a group added in Python without copy here is a red build
 * rather than a blank line on a card.
 */

import { FALLBACK_GROUP, groupOf } from "./artifact";
import type { NodeTypeSpec } from "./types";

/** One phrase per palette group, written to sit above the step's own title. */
export const GROUP_PHRASE: Record<string, string> = {
  content: "Enviar",
  // Not "If they reply", which the artboards used: this group holds
  // `condition`, `smart_delay`, `randomizer` and `start_flow`, and only one of
  // them has anything to do with a reply. As a card eyebrow it was merely odd;
  // as the heading over those four in the Add a step menu it was wrong.
  logic: "Decidir",
  actions: "Executar",
  other: "Outros",
};

/**
 * The handful of types their group's phrase is wrong for.
 *
 * `smart_delay` and `start_flow` are both `logic`, and "Then decide" is close
 * but not right for either — one waits and the other hands over. `note` is `content`
 * but is not a send. Everything else falls through to its group, which is what
 * keeps a node type registered by a later layer readable with no edit here.
 */
export const TYPE_PHRASE: Record<string, string> = {
  smart_delay: "Aguardar",
  start_flow: "Encaminhar",
  note: "Anotação",
};

/** Portuguese copy used by the authoring surface, independent of wire names. */
export const NODE_COPY: Record<string, { label: string; description: string; channel?: string }> = {
  send_message: {
    label: "Enviar mensagem",
    description: "Envie texto, mídia e elementos interativos ao contato.",
    channel: "WhatsApp e Instagram",
  },
  data_collection: { label: "Fazer uma pergunta", description: "Pergunte e salve a resposta no contato.", channel: "WhatsApp e Instagram" },
  smart_delay: { label: "Aguardar", description: "Espere um período fixo ou aleatório antes de continuar." },
  condition: { label: "Condição", description: "Envie o contato por caminhos diferentes conforme seus dados." },
  randomizer: { label: "Teste A/B", description: "Divida contatos entre caminhos para comparar resultados." },
  action: { label: "Atualizar contato ou conversa", description: "Aplique etiquetas, campos, sequências ou responsáveis." },
  start_flow: { label: "Iniciar outro fluxo", description: "Transfira o contato para outra automação." },
  external_request: { label: "Chamar API externa", description: "Envie dados a outro sistema e aproveite a resposta." },
  send_sms: { label: "Enviar SMS", description: "Envie uma mensagem de texto por SMS.", channel: "Somente SMS" },
  send_email: { label: "Enviar e-mail", description: "Envie um e-mail ao contato.", channel: "Somente e-mail" },
  note: { label: "Anotação", description: "Registre uma observação no desenho; ela não é executada." },
};

export function nodeCopy(spec: NodeTypeSpec | undefined, type: string) {
  return NODE_COPY[type] ?? { label: spec?.label ?? type, description: spec?.description ?? "" };
}

/** The eyebrow for a node type: what kind of step this is, in plain words. */
export function plainKind(spec: NodeTypeSpec | undefined, type: string): string {
  // `?? FALLBACK` twice over, deliberately: a group registered in Python with
  // no phrase here reads "Also" rather than rendering an empty eyebrow, which
  // is what plain.test.ts is there to catch before anybody sees it.
  const fallback = GROUP_PHRASE[FALLBACK_GROUP] ?? "Step";
  const byType = TYPE_PHRASE[type];
  if (byType) return byType;
  if (!spec) return fallback;
  return GROUP_PHRASE[groupOf(spec)] ?? fallback;
}

/**
 * The trigger card's eyebrow.
 *
 * A trigger is not a node and has no group, but it reads as the first step of
 * the flow and the design gives it the same treatment.
 */
export const TRIGGER_PHRASE = "Quando";
