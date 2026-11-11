/**
 * Flow Conversation Simulator Drawer.
 *
 * Runs interactive simulations of the current flow draft using an isolated
 * in-memory session on Django cache. Does not write to production database tables
 * or send external channel traffic.
 */
import { useEffect, useRef, useState } from "react";

import {
  advanceSimulator,
  replySimulator,
  resetSimulator,
  simulateSaleApproved,
  startSimulator,
  updateSimulatorContact,
  type SimulatorState,
  type SimulatorTraceItem,
} from "../api/flows";
import { useBuilder, useBuilderStore } from "../store/context";
import { toGraph } from "../store/serialize";

export function SimulatorDrawer() {
  const store = useBuilderStore();
  const env = useBuilder((state) => state.env);
  const isOpen = useBuilder((state) => state.simulatorOpen);

  const [state, setState] = useState<SimulatorState | null>(null);
  const [loading, setLoading] = useState(false);
  const [inputText, setInputText] = useState("");
  const [showContactConfig, setShowContactConfig] = useState(false);

  // Virtual contact inputs
  const [firstName, setFirstName] = useState("João");
  const [phone, setPhone] = useState("5511999999999");
  const [email, setEmail] = useState("joao.silva@example.com");

  const chatEndRef = useRef<HTMLDivElement>(null);

  const syncTraceToCanvas = (simState: SimulatorState | null) => {
    if (!simState) {
      store.getState().clearSimulatedTrace();
      return;
    }
    store
      .getState()
      .setSimulatedTrace(simState.current_node_id, simState.visited_nodes);
  };

  const handleStart = async (customContact?: { first_name: string; phone: string; email: string }) => {
    setLoading(true);
    try {
      const graph = toGraph(store.getState());
      const contactPayload = customContact || {
        first_name: firstName,
        phone,
        email,
      };
      const res = await startSimulator(env, {
        graph,
        contact: contactPayload,
      });
      setState(res);
      syncTraceToCanvas(res);
    } catch (err) {
      console.error("Failed to start simulator:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen && !state && !loading) {
      void handleStart();
    }
  }, [isOpen]);

  useEffect(() => {
    if (chatEndRef.current) {
      chatEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [state?.trace]);

  if (!isOpen) {
    return null;
  }

  const handleSendText = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!state || !inputText.trim() || loading) return;

    const text = inputText.trim();
    setInputText("");
    setLoading(true);
    try {
      const res = await replySimulator(env, {
        session_id: state.session_id,
        text,
      });
      setState(res);
      syncTraceToCanvas(res);
    } catch (err) {
      console.error("Failed to reply in simulator:", err);
    } finally {
      setLoading(false);
    }
  };

  const handleButtonClick = async (buttonId: string, label?: string) => {
    if (!state || loading) return;
    setLoading(true);
    try {
      const res = await replySimulator(env, {
        session_id: state.session_id,
        button_id: buttonId,
        text: label || "",
      });
      setState(res);
      syncTraceToCanvas(res);
    } catch (err) {
      console.error("Failed to click button in simulator:", err);
    } finally {
      setLoading(false);
    }
  };

  const handleAdvance = async (seconds = 3600, forceTimeout = false) => {
    if (!state || loading) return;
    setLoading(true);
    try {
      const res = await advanceSimulator(env, {
        session_id: state.session_id,
        seconds,
        force_timeout: forceTimeout,
      });
      setState(res);
      syncTraceToCanvas(res);
    } catch (err) {
      console.error("Failed to advance simulator time:", err);
    } finally {
      setLoading(false);
    }
  };

  const handleReset = async () => {
    if (!state || loading) {
      void handleStart();
      return;
    }
    setLoading(true);
    try {
      const res = await resetSimulator(env, { session_id: state.session_id });
      setState(res);
      syncTraceToCanvas(res);
    } catch (err) {
      console.error("Failed to reset simulator:", err);
      void handleStart();
    } finally {
      setLoading(false);
    }
  };

  const handleSaleApproved = async () => {
    if (!state || loading) return;
    setLoading(true);
    try {
      const res = await simulateSaleApproved(env, { session_id: state.session_id });
      setState(res);
      syncTraceToCanvas(res);
    } catch (err) {
      console.error("Failed to simulate sale approved:", err);
    } finally {
      setLoading(false);
    }
  };

  const handleAddTag = async (tagName: string) => {
    if (!state || !tagName.trim() || loading) return;
    const cleanTag = tagName.trim();
    const currentTags = state.virtual_contact.tags || [];
    if (currentTags.includes(cleanTag)) return;
    const newTags = [...currentTags, cleanTag];
    setLoading(true);
    try {
      const res = await updateSimulatorContact(env, {
        session_id: state.session_id,
        contact: { tags: newTags },
      });
      setState(res);
      syncTraceToCanvas(res);
    } catch (err) {
      console.error("Failed to add tag in simulator:", err);
    } finally {
      setLoading(false);
    }
  };

  const handleRemoveTag = async (tagName: string) => {
    if (!state || loading) return;
    const currentTags = state.virtual_contact.tags || [];
    const newTags = currentTags.filter((t) => t !== tagName);
    setLoading(true);
    try {
      const res = await updateSimulatorContact(env, {
        session_id: state.session_id,
        contact: { tags: newTags },
      });
      setState(res);
      syncTraceToCanvas(res);
    } catch (err) {
      console.error("Failed to remove tag in simulator:", err);
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    store.getState().setSimulatorOpen(false);
    store.getState().clearSimulatedTrace();
  };

  const statusBadge = () => {
    if (!state) return null;
    switch (state.status) {
      case "waiting_reply":
        return (
          <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
            Aguardando resposta
          </span>
        );
      case "waiting_delay":
        return (
          <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
            Aguardando delay
          </span>
        );
      case "completed":
        return (
          <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">
            Conversa finalizada
          </span>
        );
      case "failed":
        return (
          <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300">
            Erro no fluxo
          </span>
        );
      default:
        return (
          <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300">
            {state.status}
          </span>
        );
    }
  };

  return (
    <aside
      className="fixed inset-y-0 right-0 w-[420px] max-w-full z-40 bg-white dark:bg-slate-900 border-l border-slate-200 dark:border-slate-800 shadow-2xl flex flex-col overflow-hidden"
      aria-label="Simulador de Conversa"
    >
      {/* Top Header */}
      <div className="p-4 border-b border-slate-200 dark:border-slate-800 flex items-center justify-between bg-slate-50 dark:bg-slate-900/70">
        <div>
          <div className="flex items-center gap-2">
            <span className="flex h-2.5 w-2.5 relative">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
              <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500"></span>
            </span>
            <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
              Simulador de Conversa
            </h2>
          </div>
          <div className="mt-1 flex items-center gap-2">
            {statusBadge()}
            {loading ? (
              <span className="text-[11px] text-slate-400 animate-pulse">
                Processando…
              </span>
            ) : null}
          </div>
        </div>

        <div className="flex items-center gap-1.5">
          <button
            type="button"
            className="p-1.5 text-slate-500 hover:text-slate-700 dark:hover:text-slate-200 rounded hover:bg-slate-100 dark:hover:bg-slate-800 transition"
            title="Reiniciar Simulação"
            onClick={handleReset}
            disabled={loading}
          >
            <svg
              className="w-4 h-4"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
              />
            </svg>
          </button>
          <button
            type="button"
            className="p-1.5 text-slate-500 hover:text-slate-700 dark:hover:text-slate-200 rounded hover:bg-slate-100 dark:hover:bg-slate-800 transition"
            title="Configurar Contato Virtual"
            onClick={() => setShowContactConfig((v) => !v)}
          >
            <svg
              className="w-4 h-4"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
              />
            </svg>
          </button>
          <button
            type="button"
            className="p-1.5 text-slate-500 hover:text-slate-700 dark:hover:text-slate-200 rounded hover:bg-slate-100 dark:hover:bg-slate-800 transition"
            title="Fechar Simulador"
            onClick={handleClose}
          >
            <svg
              className="w-4 h-4"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>
      </div>

      {/* Virtual Contact Settings Drawer (Expandable) */}
      {showContactConfig ? (
        <div className="p-3 bg-slate-50 dark:bg-slate-800/80 border-b border-slate-200 dark:border-slate-700 text-xs space-y-2">
          <p className="font-semibold text-slate-700 dark:text-slate-200">
            Contato Virtual (Placeholders & Condições)
          </p>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-[10px] text-slate-500">Nome</label>
              <input
                type="text"
                className="w-full px-2 py-1 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded text-xs"
                value={firstName}
                onChange={(e) => setFirstName(e.target.value)}
              />
            </div>
            <div>
              <label className="block text-[10px] text-slate-500">Telefone</label>
              <input
                type="text"
                className="w-full px-2 py-1 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded text-xs"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
              />
            </div>
          </div>
          <div>
            <label className="block text-[10px] text-slate-500">Email</label>
            <input
              type="text"
              className="w-full px-2 py-1 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded text-xs"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>

          <div>
            <label className="block text-[10px] text-slate-500 mb-1">Etiquetas Atuais do Contato Virtual</label>
            <div className="flex flex-wrap gap-1 mb-1.5">
              {(state?.virtual_contact.tags || []).map((tag) => (
                <span
                  key={tag}
                  className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-slate-200 dark:bg-slate-700 text-slate-700 dark:text-slate-200"
                >
                  {tag}
                  <button
                    type="button"
                    className="hover:text-red-500 font-bold text-xs"
                    onClick={() => void handleRemoveTag(tag)}
                    title="Remover etiqueta"
                  >
                    ×
                  </button>
                </span>
              ))}
              {(state?.virtual_contact.tags || []).length === 0 ? (
                <span className="text-[10px] text-slate-400 italic">Sem etiquetas</span>
              ) : null}
            </div>
            <div className="flex gap-1.5 pt-0.5">
              <button
                type="button"
                className="text-[10px] px-1.5 py-0.5 rounded border border-amber-300 bg-amber-50 dark:bg-amber-950/40 text-amber-700 dark:text-amber-300"
                onClick={() => void handleAddTag("Venda pendente")}
              >
                + Venda pendente
              </button>
              <button
                type="button"
                className="text-[10px] px-1.5 py-0.5 rounded border border-emerald-300 bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300"
                onClick={() => void handleAddTag("Cliente")}
              >
                + Cliente
              </button>
              <button
                type="button"
                className="text-[10px] px-1.5 py-0.5 rounded border border-blue-300 bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300"
                onClick={() => void handleAddTag("Carrinho abandonado")}
              >
                + Carrinho abandonado
              </button>
            </div>
          </div>

          <div className="flex justify-end pt-1">
            <button
              type="button"
              className="btn-pill-primary btn-pill-sm text-xs py-0.5 px-3"
              onClick={() => {
                setShowContactConfig(false);
                void handleStart({ first_name: firstName, phone, email });
              }}
            >
              Aplicar e Reiniciar
            </button>
          </div>
        </div>
      ) : null}

      {/* Simulator Control Shortcuts Bar */}
      <div className="px-3 py-2 bg-slate-100/70 dark:bg-slate-800/40 border-b border-slate-200 dark:border-slate-800 flex flex-wrap items-center justify-between gap-1.5 text-xs">
        <button
          type="button"
          className="px-2 py-1 text-[11px] font-semibold bg-emerald-50 dark:bg-emerald-950/50 border border-emerald-300 dark:border-emerald-700 text-emerald-700 dark:text-emerald-300 rounded hover:bg-emerald-100 transition shadow-sm"
          onClick={() => void handleSaleApproved()}
          disabled={loading}
          title="Simula webhook de Venda Aprovada (remove 'Venda pendente' e adiciona 'Cliente')"
        >
          ⚡ Simular Venda Aprovada
        </button>

        <div className="flex items-center gap-1">
          <button
            type="button"
            className="px-1.5 py-1 text-[11px] font-medium bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded hover:bg-slate-50 transition"
            onClick={() => void handleAdvance(900, false)}
            disabled={loading}
            title="Avança o relógio virtual em 15 minutos (tempo comum de recuperação PIX)"
          >
            +15m
          </button>
          <button
            type="button"
            className="px-1.5 py-1 text-[11px] font-medium bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded hover:bg-slate-50 transition"
            onClick={() => void handleAdvance(3600, false)}
            disabled={loading}
            title="Avança o relógio virtual em 1 hora"
          >
            +1h
          </button>
          <button
            type="button"
            className="px-1.5 py-1 text-[11px] font-medium bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded hover:bg-slate-50 transition"
            onClick={() => void handleAdvance(172800, false)}
            disabled={loading}
            title="Avança o relógio virtual em 2 dias (tempo de pós-venda)"
          >
            +2d
          </button>
          <button
            type="button"
            className="px-1.5 py-1 text-[11px] font-medium bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded hover:bg-slate-50 transition text-amber-600 dark:text-amber-400"
            onClick={() => void handleAdvance(0, true)}
            disabled={loading}
            title="Dispara o gatilho de timeout da etapa atual"
          >
            Timeout
          </button>
        </div>
      </div>

      {/* Chat Viewport */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4 bg-slate-100/50 dark:bg-slate-950/40">
        {state?.trace.map((item, index) => (
          <TraceRow
            key={index}
            item={item}
            onButtonClick={handleButtonClick}
            disabled={loading}
          />
        ))}
        <div ref={chatEndRef} />
      </div>

      {/* Reply Input Form */}
      <div className="p-3 border-t border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900">
        <form onSubmit={handleSendText} className="flex gap-2">
          <input
            type="text"
            className="flex-1 px-3 py-2 text-sm bg-slate-50 dark:bg-slate-800/80 border border-slate-200 dark:border-slate-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500"
            placeholder={
              state?.status === "waiting_reply"
                ? "Digite uma resposta para o fluxo…"
                : state?.status === "waiting_delay"
                ? "Fluxo em delay. Use '+1 hora' para avançar…"
                : "Conversa concluída. Clique em Reiniciar para rodar de novo."
            }
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            disabled={loading || state?.status !== "waiting_reply"}
          />
          <button
            type="submit"
            className="btn-pill-primary px-4 py-2 text-sm font-medium disabled:opacity-50"
            disabled={
              loading ||
              !inputText.trim() ||
              state?.status !== "waiting_reply"
            }
          >
            Enviar
          </button>
        </form>
      </div>
    </aside>
  );
}

function TraceRow({
  item,
  onButtonClick,
  disabled,
}: {
  item: SimulatorTraceItem;
  onButtonClick: (id: string, label?: string) => void;
  disabled: boolean;
}) {
  if (item.type === "system") {
    return (
      <div className="flex justify-center my-1.5">
        <span className="text-[11px] px-2.5 py-1 rounded-full bg-slate-200/80 dark:bg-slate-800 text-slate-600 dark:text-slate-400 font-medium text-center max-w-[90%]">
          {item.message}
        </span>
      </div>
    );
  }

  if (item.type === "error") {
    return (
      <div className="p-2.5 rounded-lg bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-900/60 text-xs text-red-700 dark:text-red-300">
        <span className="font-semibold block mb-0.5">Erro na execução:</span>
        {item.message}
      </div>
    );
  }

  if (item.type === "user_reply") {
    return (
      <div className="flex justify-end my-1">
        <div className="max-w-[80%] rounded-2xl rounded-tr-sm bg-emerald-600 text-white px-3.5 py-2 text-sm shadow-sm">
          {item.text || `[Opção: ${item.button_id}]`}
        </div>
      </div>
    );
  }

  // Bot message
  return (
    <div className="flex flex-col items-start space-y-2 max-w-[85%]">
      {item.recipient_override ? (
        <span className="text-[10px] font-semibold text-slate-500 bg-slate-200/60 dark:bg-slate-800 px-1.5 py-0.5 rounded">
          Destino: {item.recipient_override}
        </span>
      ) : null}

      <div className="w-full rounded-2xl rounded-tl-sm bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700/80 shadow-sm overflow-hidden">
        {item.blocks?.map((block, i) => (
          <BlockRenderer
            key={i}
            block={block}
            onSelectRow={(rowId, title) => onButtonClick(rowId, title)}
            disabled={disabled}
          />
        ))}

        {item.buttons && item.buttons.length > 0 ? (
          <div className="border-t border-slate-100 dark:border-slate-700/60 divide-y divide-slate-100 dark:divide-slate-700/60">
            {item.buttons.map((btn) => (
              <ButtonRenderer
                key={btn.id}
                button={btn}
                onClick={() => onButtonClick(btn.id, btn.label)}
                disabled={disabled}
              />
            ))}
          </div>
        ) : null}
      </div>

      {item.quick_replies && item.quick_replies.length > 0 ? (
        <div className="flex flex-wrap gap-1.5 pt-1">
          {item.quick_replies.map((qr) => (
            <button
              key={qr.id}
              type="button"
              className="px-3 py-1.5 text-xs font-medium rounded-full border border-emerald-500 text-emerald-600 dark:text-emerald-400 bg-white dark:bg-slate-800 hover:bg-emerald-50 dark:hover:bg-emerald-950/40 transition disabled:opacity-50"
              onClick={() => onButtonClick(qr.id, qr.label)}
              disabled={disabled}
            >
              {qr.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function BlockRenderer({
  block,
  onSelectRow,
  disabled,
}: {
  block: Record<string, unknown>;
  onSelectRow: (id: string, title: string) => void;
  disabled: boolean;
}) {
  const type = String(block.type || "text");

  if (type === "text") {
    return (
      <div className="p-3 text-sm text-slate-800 dark:text-slate-100 whitespace-pre-wrap leading-relaxed">
        {String(block.text || "")}
      </div>
    );
  }

  if (type === "image") {
    return (
      <div className="overflow-hidden">
        <img
          src={String(block.url || "")}
          alt={String(block.caption || "Image")}
          className="w-full max-h-56 object-cover"
        />
        {block.caption ? (
          <p className="p-2 text-xs text-slate-600 dark:text-slate-300">
            {String(block.caption)}
          </p>
        ) : null}
      </div>
    );
  }

  if (type === "audio") {
    return (
      <div className="p-3">
        <audio controls src={String(block.url || "")} className="w-full" />
      </div>
    );
  }

  if (type === "video") {
    return (
      <div className="overflow-hidden">
        <video controls src={String(block.url || "")} className="w-full max-h-56" />
        {block.caption ? (
          <p className="p-2 text-xs text-slate-600 dark:text-slate-300">
            {String(block.caption)}
          </p>
        ) : null}
      </div>
    );
  }

  if (type === "sticker") {
    return (
      <div className="p-3 flex justify-center">
        <img
          src={String(block.url || "")}
          alt="Sticker"
          className="w-24 h-24 object-contain"
        />
      </div>
    );
  }

  if (type === "location") {
    const lat = block.latitude || 0;
    const lng = block.longitude || 0;
    return (
      <div className="p-3 border-l-4 border-emerald-500 bg-slate-50 dark:bg-slate-900/50">
        <div className="flex items-start gap-2">
          <span className="text-emerald-500 text-base">📍</span>
          <div>
            <p className="text-xs font-bold text-slate-800 dark:text-slate-100">
              {String(block.name || "Localização")}
            </p>
            {block.address ? (
              <p className="text-[11px] text-slate-500 dark:text-slate-400">
                {String(block.address)}
              </p>
            ) : null}
            <a
              href={`https://maps.google.com/?q=${lat},${lng}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[11px] text-emerald-600 hover:underline block mt-1"
            >
              Abrir no mapa ({String(lat).slice(0, 7)}, {String(lng).slice(0, 7)}) →
            </a>
          </div>
        </div>
      </div>
    );
  }

  if (type === "contact") {
    return (
      <div className="p-3 border-l-4 border-blue-500 bg-slate-50 dark:bg-slate-900/50">
        <div className="flex items-center gap-2">
          <span className="text-blue-500 text-base">👤</span>
          <div>
            <p className="text-xs font-bold text-slate-800 dark:text-slate-100">
              {String(block.name || block.full_name || "Contato")}
            </p>
            <p className="text-[11px] text-slate-500 font-mono">
              {String(block.phone || "")}
            </p>
          </div>
        </div>
      </div>
    );
  }

  if (type === "poll") {
    const options = Array.isArray(block.options) ? block.options : [];
    return (
      <div className="p-3 space-y-2">
        <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-800 dark:text-slate-100">
          <span>📊</span>
          <span>{String(block.question || "Enquete")}</span>
        </div>
        <div className="space-y-1.5">
          {options.map((opt, idx) => (
            <div
              key={idx}
              className="p-2 text-xs rounded bg-slate-100 dark:bg-slate-700/50 border border-slate-200 dark:border-slate-600/50"
            >
              {String(opt)}
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (type === "list") {
    const sections = Array.isArray(block.sections) ? block.sections : [];
    return (
      <div className="p-3 space-y-2">
        <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-800 dark:text-slate-100">
          <span>📋</span>
          <span>{String(block.title || "Menu de Opções")}</span>
        </div>
        {block.description ? (
          <p className="text-[11px] text-slate-500">
            {String(block.description)}
          </p>
        ) : null}

        <div className="space-y-2 mt-2">
          {sections.map((sec: any, sIdx: number) => {
            const rows: Array<Record<string, unknown>> = Array.isArray(sec.rows) ? sec.rows : [];
            return (
              <div key={sIdx} className="space-y-1">
                {sec.title ? (
                  <p className="text-[10px] font-bold uppercase text-slate-400">
                    {String(sec.title)}
                  </p>
                ) : null}
                <div className="divide-y divide-slate-100 dark:divide-slate-700/60 rounded border border-slate-200 dark:border-slate-700 overflow-hidden">
                  {rows.map((row) => (
                    <button
                      key={String(row.id)}
                      type="button"
                      className="w-full text-left p-2 hover:bg-emerald-50 dark:hover:bg-emerald-950/30 transition flex items-center justify-between group disabled:opacity-50"
                      onClick={() =>
                        onSelectRow(String(row.id), String(row.title || ""))
                      }
                      disabled={disabled}
                    >
                      <div>
                        <p className="text-xs font-medium text-slate-800 dark:text-slate-100 group-hover:text-emerald-600">
                          {String(row.title || "")}
                        </p>
                        {row.description ? (
                          <p className="text-[10px] text-slate-400">
                            {String(row.description)}
                          </p>
                        ) : null}
                      </div>
                      <span className="text-slate-300 group-hover:text-emerald-500">
                        →
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  return (
    <div className="p-3 text-xs text-slate-500">
      [{type}] {JSON.stringify(block)}
    </div>
  );
}

function ButtonRenderer({
  button,
  onClick,
  disabled,
}: {
  button: {
    id: string;
    label: string;
    action?: string;
    url?: string;
    copy_code?: string;
    phone_number?: string;
    pix_key?: string;
    pix_name?: string;
    currency?: string;
  };
  onClick: () => void;
  disabled: boolean;
}) {
  const action = button.action || "postback";

  if (action === "url") {
    return (
      <a
        href={button.url || "#"}
        target="_blank"
        rel="noopener noreferrer"
        className="w-full py-2.5 px-3 text-xs font-medium text-blue-600 dark:text-blue-400 hover:bg-slate-50 dark:hover:bg-slate-700/50 flex items-center justify-center gap-1.5 transition"
      >
        <span>{button.label}</span>
        <svg
          className="w-3.5 h-3.5"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"
          />
        </svg>
      </a>
    );
  }

  if (action === "call") {
    return (
      <a
        href={`tel:${button.phone_number || ""}`}
        className="w-full py-2.5 px-3 text-xs font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700/50 flex items-center justify-center gap-1.5 transition"
      >
        <span>📞</span>
        <span>
          {button.label} ({button.phone_number})
        </span>
      </a>
    );
  }

  if (action === "copy") {
    const handleCopy = () => {
      if (button.copy_code) {
        void navigator.clipboard.writeText(button.copy_code);
        alert(`Código copiado: ${button.copy_code}`);
      }
    };
    return (
      <button
        type="button"
        onClick={handleCopy}
        className="w-full py-2.5 px-3 text-xs font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700/50 flex items-center justify-center gap-1.5 transition"
      >
        <span>📋</span>
        <span>Copiar: {button.label}</span>
      </button>
    );
  }

  if (action === "pix") {
    const handleCopyPix = () => {
      if (button.pix_key) {
        void navigator.clipboard.writeText(button.pix_key);
        alert(`Chave Pix copiada: ${button.pix_key}`);
      }
    };
    return (
      <button
        type="button"
        onClick={handleCopyPix}
        className="w-full py-2.5 px-3 text-xs font-semibold text-emerald-600 dark:text-emerald-400 hover:bg-emerald-50 dark:hover:bg-emerald-950/40 flex items-center justify-center gap-1.5 transition"
      >
        <span>💠</span>
        <span>
          {button.label} (Pix: {button.pix_key})
        </span>
      </button>
    );
  }

  // Normal postback / reply button
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full py-2.5 px-3 text-xs font-medium text-emerald-600 dark:text-emerald-400 hover:bg-emerald-50 dark:hover:bg-emerald-950/40 flex items-center justify-center gap-1.5 transition disabled:opacity-50"
      disabled={disabled}
    >
      <span>{button.label}</span>
    </button>
  );
}
