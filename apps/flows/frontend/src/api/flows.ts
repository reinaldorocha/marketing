/**
 * The five endpoints apps/flows/api.py serves, plus the media picker.
 *
 * Every URL is a data attribute on the mount div, so nothing here is
 * constructed — see src/env.ts.
 */
import { triggerEnabledUrl, type BuilderEnv } from "../env";
import type {
  FlowDetail,
  FlowGraph,
  FlowMeta,
  TriggerSummary,
  OfflineResult,
  PickerPayload,
  PublishResult,
  SaveResult,
  StatsPayload,
} from "../schema/types";
import { request } from "./client";

export function loadFlow(env: BuilderEnv): Promise<FlowDetail> {
  return request<FlowDetail>(env.detailUrl);
}

/**
 * A 200 here means the draft was written. It may still carry `errors` — those
 * are graph-stage problems that block publish but not saving, because a draft
 * is allowed to be half-wired and an autosaving canvas that refused them would
 * throw away the user's work mid-edit.
 */
export function saveGraph(env: BuilderEnv, graph: FlowGraph): Promise<SaveResult> {
  return request<SaveResult>(env.detailUrl, { method: "PUT", body: { graph } });
}

export function publishFlow(env: BuilderEnv): Promise<PublishResult> {
  return request<PublishResult>(env.publishUrl, { method: "POST" });
}

/** A 409 when the flow is not live; the envelope's message says so. */
export function takeFlowOffline(env: BuilderEnv): Promise<OfflineResult> {
  return request<OfflineResult>(env.offlineUrl, { method: "POST" });
}

/** The top bar's rename. A 400 for a blank name; the envelope says so. */
export function renameFlow(env: BuilderEnv, name: string): Promise<{ flow: FlowMeta }> {
  return request<{ flow: FlowMeta }>(env.renameUrl, { method: "POST", body: { name } });
}

/**
 * One trigger on or off, from the outline's switch. The state to end in, not a
 * toggle, so a stale page cannot flip a trigger the wrong way. Answers with
 * every trigger's summary, the same list the detail read carries.
 */
export function setTriggerEnabled(
  env: BuilderEnv,
  triggerId: string,
  enabled: boolean,
): Promise<{ triggers: TriggerSummary[] }> {
  return request<{ triggers: TriggerSummary[] }>(triggerEnabledUrl(env, triggerId), {
    method: "POST",
    body: { enabled },
  });
}

export function fetchStats(env: BuilderEnv): Promise<StatsPayload> {
  return request<StatsPayload>(env.statsUrl);
}

/**
 * SPEC §16's preview link.
 *
 * A 200 either way: "you have no Instagram account connected" is an ordinary
 * state for the builder to render, not a request that failed, and a 4xx would
 * send this down the API-error path and show a failure instead of an
 * explanation. `unsupported_platform` arrives the same way — a flow that only
 * runs on WhatsApp has no live test, and that is a fact about the channel
 * rather than something the reader has got wrong.
 *
 * `settings_url` is absent on `unsupported_platform`: there is nothing to go
 * and connect that would change the answer.
 */
export type PreviewLink =
  | {
      ok: true;
      deep_link: string;
      platform: string;
      platform_label: string;
      account: string;
      /** How to actually start the test, where tapping the link is not enough. */
      instructions: string;
      expires_in: number;
    }
  | { ok: false; reason: string; message: string; settings_url?: string };

export function requestPreviewLink(env: BuilderEnv): Promise<PreviewLink> {
  return request<PreviewLink>(env.previewUrl, { method: "POST" });
}

export interface PickerQuery {
  q?: string;
  kind?: string;
  folder?: string;
  platform?: string;
  cursor?: string;
}

export function fetchPicker(env: BuilderEnv, query: PickerQuery = {}): Promise<PickerPayload> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value) {
      params.set(key, value);
    }
  }
  const suffix = params.toString();
  return request<PickerPayload>(suffix ? `${env.mediaPickerUrl}?${suffix}` : env.mediaPickerUrl);
}


export interface SimulatorTraceItem {
  type: "bot_message" | "user_reply" | "system" | "error";
  timestamp: string;
  node_id?: string;
  kind?: string;
  message?: string;
  text?: string;
  button_id?: string;
  recipient_override?: string;
  blocks?: Array<Record<string, unknown>>;
  buttons?: Array<{
    id: string;
    label: string;
    action?: string;
    url?: string;
    copy_code?: string;
    phone_number?: string;
    pix_key?: string;
    pix_name?: string;
    currency?: string;
  }>;
  quick_replies?: Array<{ id: string; label: string }>;
  is_retry?: boolean;
}

export interface SimulatorState {
  session_id: string;
  workspace_id: string;
  flow_id: string;
  status: "idle" | "running" | "waiting_reply" | "waiting_delay" | "completed" | "failed";
  virtual_time: string;
  virtual_contact: {
    first_name: string;
    last_name: string;
    phone: string;
    email: string;
    custom_fields: Record<string, unknown>;
    tags: string[];
  };
  variables: Record<string, unknown>;
  current_node_id: string | null;
  wait_config: Record<string, unknown>;
  visited_nodes: string[];
  trace: SimulatorTraceItem[];
}

export function startSimulator(
  env: BuilderEnv,
  payload: { graph?: unknown; contact?: unknown; variables?: unknown } = {},
): Promise<SimulatorState> {
  const url = `${env.detailUrl.replace(/\/$/, "")}/simulator/start/`;
  return request<SimulatorState>(url, { method: "POST", body: payload });
}

export function replySimulator(
  env: BuilderEnv,
  payload: { session_id: string; text?: string; button_id?: string },
): Promise<SimulatorState> {
  const url = `${env.detailUrl.replace(/\/$/, "")}/simulator/reply/`;
  return request<SimulatorState>(url, { method: "POST", body: payload });
}

export function advanceSimulator(
  env: BuilderEnv,
  payload: { session_id: string; seconds?: number; force_timeout?: boolean },
): Promise<SimulatorState> {
  const url = `${env.detailUrl.replace(/\/$/, "")}/simulator/advance/`;
  return request<SimulatorState>(url, { method: "POST", body: payload });
}

export function resetSimulator(
  env: BuilderEnv,
  payload: { session_id: string },
): Promise<SimulatorState> {
  const url = `${env.detailUrl.replace(/\/$/, "")}/simulator/reset/`;
  return request<SimulatorState>(url, { method: "POST", body: payload });
}
