/**
 * Which channels a message's limits will be checked against.
 *
 * Deliberately not a capability table. apps/flows/capabilities.py owns that,
 * the server emits `capability_unsupported` and `capability_limit_exceeded`
 * against `config.buttons`, and those already render inline through the
 * ordinary issue path. A second copy here is the vendored duplicate the brief
 * forbids, and it would go stale the day a platform changes.
 */
import { useField } from "../FieldContext";

export function ChannelHint() {
  const { picklists } = useField();

  if (picklists.connections.length === 0) {
    return <p className="fb-field-help">Os limites dependem do canal do fluxo; ainda não há nenhum canal conectado.</p>;
  }
  const names = [...new Set(picklists.connections.map((connection) => connection.platform))].join(", ");
  return <p className="fb-field-help">Verificado para os canais conectados: {names}.</p>;
}
