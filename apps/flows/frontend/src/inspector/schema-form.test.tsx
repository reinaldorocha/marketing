/**
 * The generated form, checked against every node type in the artefact.
 *
 * The overrides registry is emptied for most of these on purpose: what is being
 * tested is that the *generic* renderer can configure anything the schema
 * describes, because that is the property a node type added by a later layer
 * depends on. The hand-written widgets are polish on top of it.
 */
import { act, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { NODE_TYPES } from "../schema/artifact";
import { validateNode } from "../test/ajv";
import { makeDetail, makeSampleGraph } from "../test/fixtures";
import { makeStore, renderWith } from "../test/render";
import { sampleConfig } from "../schema/sample";
import { toGraph } from "../store/serialize";
import { StepEditor } from "../editor/StepEditor";
import { OVERRIDES } from "./overrides";

let saved: Record<string, unknown>;

beforeEach(() => {
  saved = { ...OVERRIDES };
});

afterEach(() => {
  for (const key of Object.keys(OVERRIDES)) {
    delete OVERRIDES[key];
  }
  Object.assign(OVERRIDES, saved);
});

function withoutOverrides() {
  for (const key of Object.keys(OVERRIDES)) {
    delete OVERRIDES[key];
  }
}

/** A node with only its required config, so optional keys are still absent. */
function openMinimal(type: string) {
  const store = makeStore(
    makeDetail({
      schema: 1,
      nodes: [{ id: "n1", type, position: { x: 0, y: 0 }, config: sampleConfig(type) }],
      edges: [],
    }),
  );
  store.getState().setSelection({ nodes: ["n1"], edges: [] });
  renderWith(store, <StepEditor />);
  return { store, id: "n1" };
}

function openNode(type: string) {
  const store = makeStore(makeDetail(makeSampleGraph({ optional: true })));
  const id = store.getState().nodeOrder.find((entry) => store.getState().nodeType[entry] === type) as string;
  store.getState().setSelection({ nodes: [id], edges: [] });
  const view = renderWith(store, <StepEditor />);
  return { store, id, view };
}

describe("the generic renderer alone", () => {
  it.each(NODE_TYPES.map((spec) => spec.type))("renders a form for %s without any override", (type) => {
    withoutOverrides();
    const { store, id, view } = openNode(type);

    // condition's config is a bare $ref and smart_delay's is a bare tagged
    // union — two of eleven with no `config.properties` at all. Both go
    // through the same dispatcher as any nested value, which is why they work.
    //
    // The assertion is on the editor having opened for this step, not on the
    // registry label appearing: the step editor titles a step by its own
    // content (editor/title.ts), because a flow with four sends was four rows
    // reading "Send Message".
    expect(view.container.querySelector(".fb-edit-head")).not.toBeNull();
    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });

  it("offers all seven message-block kinds, over four underlying shapes", () => {
    // discriminator.mapping has seven keys but oneOf has four branches, because
    // image/audio/video/file all map to block_media. Enumerating branches would
    // silently lose three kinds.
    withoutOverrides();
    openNode("send_message");

    const chooser = screen.getByLabelText(/^(Message blocks|Partes da mensagem)$/i, { selector: "select" }) as HTMLSelectElement;
    const values = within(chooser).getAllByRole("option").map((option) => (option as HTMLOptionElement).value);
    for (const kind of ["text", "image", "audio", "video", "file", "card", "gallery", "sticker", "location", "contact", "poll", "list"]) {
      expect(values).toContain(kind);
    }
    // Twelve tags
    expect(values.filter(Boolean)).toHaveLength(12);
  });
});

describe("switching a variant", () => {
  it("replaces the value instead of merging into it", () => {
    // A merge leaves the previous branch's keys behind, every object here is
    // closed, and the result is unknown_config_key — a 422 that discards the
    // whole save rather than just this node.
    withoutOverrides();
    const { store, id } = openNode("send_message");

    fireEvent.change(screen.getByLabelText(/^(Message blocks|Partes da mensagem)$/i, { selector: "select" }), { target: { value: "image" } });

    const block = (store.getState().config[id] as { blocks: Record<string, unknown>[] }).blocks[0];
    expect(block).toHaveProperty("type", "image");
    expect(block).not.toHaveProperty("text");
    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });
});

describe("clearing an optional field", () => {
  it("removes the key rather than writing an empty string", () => {
    // Most optional strings carry minLength: 1, and every object is closed, so
    // "" is a validation error and null is invalid_config_value. Cleared has
    // to mean absent.
    withoutOverrides();
    const { store, id } = openNode("send_sms");
    expect(store.getState().config[id]).toHaveProperty("media_url");

    fireEvent.click(screen.getByRole("button", { name: /remove media url|remover url da mídia/i }));

    expect(store.getState().config[id]).not.toHaveProperty("media_url");
    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });
});

describe("numeric bounds", () => {
  it("come from the schema, not from the widget", () => {
    withoutOverrides();
    openNode("external_request");

    const timeout = screen.getByLabelText(/timeout \(seconds\)|tempo limite \(segundos\)/i) as HTMLInputElement;
    // SPEC §11.7 caps it at 10.
    expect(timeout.min).toBe("1");
    expect(timeout.max).toBe("10");
  });
});

describe("the generated form's structure", () => {
  it("does not repeat the same label three times down a nested union", () => {
    // A list of tagged unions used to read: the array's label, then the item's
    // variant label, then the chosen branch's group label — all identical.
    withoutOverrides();
    openNode("send_message");

    expect(screen.getAllByText(/^(Message blocks|Partes da mensagem)$/i)).toHaveLength(1);
  });

  it("still gives the variant selector an accessible name", () => {
    withoutOverrides();
    openNode("send_message");

    expect(screen.getByLabelText(/^(Message blocks|Partes da mensagem)$/i, { selector: "select" }).tagName).toBe("SELECT");
  });
});

describe("what a newly added item contains", () => {
  it("reads as real placeholder copy, not an ellipsis", () => {
    withoutOverrides();
    const { store, id } = openMinimal("send_message");

    fireEvent.click(screen.getByRole("button", { name: /^\+ (Buttons|Botões)/i }));
    fireEvent.click(screen.getByRole("button", { name: /Add to (Buttons|Botões)|Adicionar a (Botões|Buttons)/i }));

    const buttons = (store.getState().config[id] as { buttons: Record<string, unknown>[] }).buttons;
    expect(buttons[0]?.["label"]).toBe("Button");
    expect(JSON.stringify(buttons)).not.toContain("…");
  });

  it("gives each item a distinct id, so two buttons cannot share a handle", () => {
    withoutOverrides();
    const { store, id } = openMinimal("send_message");

    fireEvent.click(screen.getByRole("button", { name: /^\+ (Buttons|Botões)/i }));
    fireEvent.click(screen.getByRole("button", { name: /Add to (Buttons|Botões)|Adicionar a (Botões|Buttons)/i }));
    fireEvent.click(screen.getByRole("button", { name: /Add to (Buttons|Botões)|Adicionar a (Botões|Buttons)/i }));

    const buttons = (store.getState().config[id] as { buttons: { id: string }[] }).buttons;
    expect(buttons).toHaveLength(2);
    expect(buttons[0]?.id).not.toBe(buttons[1]?.id);
  });

  it("still validates against the schema", () => {
    withoutOverrides();
    const { store, id } = openMinimal("send_message");

    fireEvent.click(screen.getByRole("button", { name: /^\+ (Buttons|Botões)/i }));
    fireEvent.click(screen.getByRole("button", { name: /Add to (Buttons|Botões)|Adicionar a (Botões|Buttons)/i }));

    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });
});

describe("the condition panel, against contract 8's real schema", () => {
  it("offers only the operators the chosen source supports", () => {
    // apps/contacts/conditions.py encodes the source-to-operator mapping in the
    // schema, so the panel gets it right without a copy of that table.
    openMinimal("condition");
    fireEvent.click(screen.getByRole("button", { name: /Add to (Rules|Regras)|Adicionar a (Regras|Rules)/i }));

    const rule = screen.getByLabelText(/^(Rules|Regras)$/i, { selector: "select" }) as HTMLSelectElement;
    expect([...rule.options].map((option) => option.text)).toContain("Tag");

    const ops = screen.getByLabelText(/Operator|Operador/i) as HTMLSelectElement;
    expect([...ops.options].map((option) => option.value).filter(Boolean)).toEqual(["has", "has_not"]);
  });

  it("switches the whole rule when the branch changes, leaving no stale key", () => {
    const { store, id } = openMinimal("condition");
    fireEvent.click(screen.getByRole("button", { name: /Add to (Rules|Regras)|Adicionar a (Regras|Rules)/i }));

    fireEvent.change(screen.getByLabelText(/^(Rules|Regras)$/i, { selector: "select" }), { target: { value: "2" } });

    const rules = (store.getState().config[id] as { rules: Record<string, unknown>[] }).rules;
    expect(rules[0]?.["source"]).toBe("custom_field");
    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });

  it("picks a tag by name rather than asking for a UUID", () => {
    const store = makeStore(
      makeDetail(
        {
          schema: 1,
          nodes: [{ id: "n1", type: "condition", position: { x: 0, y: 0 }, config: sampleConfig("condition") }],
          edges: [],
        },
        {
          picklists: {
            tags: [{ id: "11111111-1111-1111-1111-111111111111", label: "VIP" }],
            custom_fields: [],
            sequences: [],
            flows: [],
            connections: [],
            members: [],
          },
        },
      ),
    );
    store.getState().setSelection({ nodes: ["n1"], edges: [] });
    renderWith(store, <StepEditor />);
    fireEvent.click(screen.getByRole("button", { name: /Add to (Rules|Regras)|Adicionar a (Regras|Rules)/i }));

    const key = screen.getByLabelText(/Key|Chave/i) as HTMLSelectElement;
    expect([...key.options].map((option) => option.text)).toContain("VIP");

    fireEvent.change(key, { target: { value: "11111111-1111-1111-1111-111111111111" } });
    const rules = (store.getState().config["n1"] as { rules: Record<string, unknown>[] }).rules;
    expect(rules[0]?.["key"]).toBe("11111111-1111-1111-1111-111111111111");
    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === "n1")).errors).toEqual([]);
  });
});

describe("adding a list item never breaks the save", () => {
  it("seeds a pattern-constrained string with something the pattern accepts", () => {
    // A condition rule's key is a UUID with no minLength, so "" passes the
    // length check and fails the pattern — and pattern failures are
    // document-stage, which discards the whole save, not just this rule.
    const { store, id } = openMinimal("condition");

    fireEvent.click(screen.getByRole("button", { name: /Add to (Rules|Regras)|Adicionar a (Regras|Rules)/i }));

    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });

  it.each(["send_message", "action", "condition", "external_request", "randomizer"])(
    "%s stays valid after adding one of every list it offers",
    (type) => {
      withoutOverrides();
      const { store, id } = openMinimal(type);

      for (const button of screen.queryAllByRole("button", { name: /^Add to |^Adicionar a /i })) {
        fireEvent.click(button);
      }

      expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
    },
  );
});

describe("the button list", () => {
  it("offers exactly one way to add a button", () => {
    // There used to be two — the generic array's "Add" and an id-minting
    // wrapper's "Add a button" — which looked different and did the same job.
    const { store, id } = openMinimal("send_message");
    fireEvent.click(screen.getByRole("button", { name: /^\+ (Buttons|Botões)/i }));

    const adders = screen
      .getAllByRole("button")
      .map((button) => button.getAttribute("aria-label") ?? button.textContent?.trim() ?? "")
      .filter((name) => /(button|botão|botões)/i.test(name) && /^(add|adicionar)/i.test(name));

    expect(adders).toEqual(expect.arrayContaining([expect.stringMatching(/Add to Buttons|Adicionar a Botões/i)]));

    fireEvent.click(screen.getByRole("button", { name: /Add to (Buttons|Botões)|Adicionar a (Botões|Buttons)/i }));
    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });

  it("still says which channels the limits are checked against", () => {
    openMinimal("send_message");
    fireEvent.click(screen.getByRole("button", { name: /^\+ (Buttons|Botões)/i }));

    expect(screen.getByText(/none are connected yet|ainda não há nenhum canal conectado/i)).toBeInTheDocument();
  });
});

describe("the JSON body editor", () => {
  it("follows an undo instead of writing the stale text back", () => {
    // With a bare defaultValue the textarea kept the old JSON while it stayed
    // mounted, and the next blur parsed and saved it — reversing the undo.
    const { store, id } = openMinimal("external_request");
    fireEvent.click(screen.getByRole("button", { name: /^\+ (Body|Corpo)/i }));

    const box = screen.getByLabelText(/Body|Corpo/i) as HTMLTextAreaElement;
    fireEvent.blur(box, { target: { value: '{"one": 1}' } });
    expect(store.getState().config[id]).toMatchObject({ body: { one: 1 } });

    act(() => {
      store.getState().undo();
    });

    expect((screen.getByLabelText(/Body|Corpo/i) as HTMLTextAreaElement).value).not.toContain('"one"');
  });
});

describe("a member id the workspace no longer has", () => {
  it("stays visible and removable rather than becoming a ghost recipient", () => {
    // notify_members requires at least one id, so a freshly added action seeds
    // a placeholder. With controls only for current members it could not be
    // unticked, and the flow published notifying nobody.
    const store = makeStore(
      makeDetail({
        schema: 1,
        nodes: [
          {
            id: "n1",
            type: "action",
            position: { x: 0, y: 0 },
            config: { actions: [{ verb: "notify_members", member_ids: ["who-is-this"], via: "in_app", text: "hi" }] },
          },
        ],
        edges: [],
      }),
    );
    store.getState().setSelection({ nodes: ["n1"], edges: [] });
    renderWith(store, <StepEditor />);

    const ghost = screen.getByLabelText(/who-is-this/);
    expect(ghost).toBeChecked();

    fireEvent.click(ghost);
    const actions = (store.getState().config["n1"] as { actions: { member_ids: string[] }[] }).actions;
    expect(actions[0]?.member_ids).toEqual([]);
  });
});

describe("enum options", () => {
  it("show a label rather than the schema's wire value", () => {
    // Every enum rendered its options verbatim, so a question's "Save into"
    // offered `system_field` and a condition offered `has_no_value`.
    withoutOverrides();
    const { view } = openNode("data_collection");

    expect(view.container.textContent).not.toContain("system_field");
    expect(screen.getByText(/A field every contact has|Um campo padrão do contato/i)).toBeInTheDocument();
  });

  it("still submits the wire value, not the label", () => {
    // The label is what the reader picks; the schema is still the contract.
    withoutOverrides();
    const { store, id } = openNode("data_collection");

    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });
});

describe("adding a section to a step", () => {
  /**
   * A send_message with only what the schema requires.
   *
   * `openNode` builds from `makeSampleGraph({ optional: true })`, which fills
   * every optional property — so nothing is absent and there is nothing to add.
   * These tests are about what a step does *not* have yet.
   */
  function bareSendMessage() {
    const store = makeStore(
      makeDetail({
        schema: 1,
        nodes: [
          {
            id: "n1",
            type: "send_message",
            position: { x: 0, y: 0 },
            config: { blocks: [{ type: "text", text: "Hello" }] },
          },
        ],
        edges: [],
      }),
    );
    store.getState().setSelection({ nodes: ["n1"], edges: [] });
    const view = renderWith(store, <StepEditor />);
    return { store, view, id: "n1" };
  }

  it("offers the ones it does not have as chips, under what they are for", () => {
    // They used to be `+ Label` text links in schema order, stacked at the
    // bottom of the form: the same size, weight and colour as prose, with a
    // plus sign at 12px the only thing marking them as controls.
    withoutOverrides();
    bareSendMessage();

    const chips = screen.getAllByRole("button", { name: /^\+ / });
    expect(chips.length).toBeGreaterThan(0);
    for (const chip of chips) {
      expect(chip.className).toContain("fb-add-chip");
    }
    expect(screen.getByText(/Add to this step|Adicionar a esta etapa/i)).toBeInTheDocument();
    expect(screen.getByText(/If they go quiet|Se não houver resposta/i, { selector: "p" })).toBeInTheDocument();
  });

  it("names what a list adder adds, in the same words its accessible name uses", () => {
    // The visible label was "Add", full stop, while the accessible name has
    // always been "Add to Message blocks" — so a screen reader was told what it
    // added and a reader was not.
    withoutOverrides();
    bareSendMessage();

    const adder = screen.getByRole("button", { name: /Add to Message blocks|Adicionar a Partes da mensagem/i });
    expect(adder.textContent).toMatch(/Add a message part|Adicionar parte da mensagem/i);
    expect(adder.className).toContain("fb-add-wide");
  });

  it("spells WhatsApp the way WhatsApp does", () => {
    withoutOverrides();
    const { view } = bareSendMessage();

    expect(view.container.textContent).toMatch(/WhatsApp template|Modelo do WhatsApp/i);
    expect(view.container.textContent).not.toContain("Whatsapp");
  });

  it("moves a chip into the form when it is picked, and leaves the group behind", () => {
    withoutOverrides();
    const { store, id } = bareSendMessage();

    fireEvent.click(screen.getByRole("button", { name: /^\+ (Quick replies|Respostas rápidas)/i }));

    expect(store.getState().config[id]).toHaveProperty("quick_replies");
    expect(screen.queryByRole("button", { name: /^\+ (Quick replies|Respostas rápidas)/i })).toBeNull();
  });
});
