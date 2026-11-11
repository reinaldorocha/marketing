import type { TriggerSummary } from "../schema/types";
import { useBuilder } from "../store/context";

const PLATFORM_LABEL: Record<string, string> = {
  whatsapp: "WhatsApp",
  whatsapp_evolution: "WhatsApp",
  instagram: "Instagram",
  messenger: "Messenger",
  telegram: "Telegram",
  sms: "SMS",
  email: "E-mail",
};

export function platformLabel(platform: string): string {
  return PLATFORM_LABEL[platform] ?? platform;
}

export function triggerPlatforms(triggers: readonly TriggerSummary[]): string[] {
  return [...new Set(triggers.flatMap((trigger) => trigger.platforms.length ? trigger.platforms : trigger.connection?.platform ? [trigger.connection.platform] : []))];
}

/**
 * Explains the channel consequences where a message is authored. The server
 * remains authoritative for validation; this is an orientation aid before a
 * person has filled in a block that needs to be downgraded.
 */
export function ChannelGuide({ nodeType }: { nodeType: string }) {
  const triggers = useBuilder((state) => state.triggers);
  if (nodeType !== "send_message" && nodeType !== "data_collection") {
    return null;
  }

  const platforms = triggerPlatforms(triggers);
  const hasWhatsApp = platforms.includes("whatsapp") || platforms.includes("whatsapp_evolution");
  const hasInstagram = platforms.includes("instagram");

  return (
    <aside className="fb-channel-guide" aria-label="Compatibilidade por canal">
      <p className="fb-channel-guide-title">Compatibilidade por canal</p>
      {platforms.length === 0 ? (
        <p>Defina um gatilho de WhatsApp ou Instagram para ver os formatos usados por este fluxo.</p>
      ) : null}
      {hasWhatsApp ? (
        <p>
          <strong>WhatsApp:</strong> texto, mídia, figurinhas, localização, contatos, enquetes, Pix, botões ou listas.
          Use botões <em>ou</em> lista na mesma mensagem. Fora da janela de 24 horas, use um modelo aprovado.
        </p>
      ) : null}
      {hasInstagram ? (
        <p>
          <strong>Instagram:</strong> texto, mídia, cards, galeria, botões e respostas rápidas no Direct. A automação responde dentro da janela de 24 horas.
        </p>
      ) : null}
      {platforms.length > 0 && !hasWhatsApp && !hasInstagram ? (
        <p>Este fluxo usa: {platforms.map(platformLabel).join(", ")}.</p>
      ) : null}
    </aside>
  );
}
