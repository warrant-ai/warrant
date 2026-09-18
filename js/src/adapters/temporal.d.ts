import type { Warrant } from "../index.js";

export const DENIED: "WarrantDenied";
export const ESCALATED: "WarrantEscalated";
export const UNREADABLE: "WarrantUnreadable";

/** The activity's single object argument, or `{ args }` for any other call shape. */
export type ActivityArguments = Record<string, unknown>;

export interface ActivityDecisionOptions {
  decisionClass: string;
  /** The argument that identifies what is being decided on, or a function of the arguments. */
  subject: string | ((args: ActivityArguments) => string);
  /** Defaults to the activity type. */
  action?: string;
  /** The facts the policy is checked against: argument names, a function of the arguments, or undefined for every argument. */
  inputs?: string[] | ((args: ActivityArguments) => Record<string, unknown>);
  costCentre?: string;
}

/** How one activity type's runs map to decisions. */
export class ActivityDecision {
  readonly decisionClass: string;
  readonly subject: string | ((args: ActivityArguments) => string);
  readonly action: string | undefined;
  readonly inputs: string[] | ((args: ActivityArguments) => Record<string, unknown>) | undefined;
  readonly costCentre: string | undefined;
  constructor(options: ActivityDecisionOptions);
  read(activityType: string, args: ActivityArguments): { subject: string; inputs: Record<string, unknown> };
}

/** `(provider, model, tokensIn, tokensOut) => amount` in the client's currency. */
export type Pricer = (provider: string, model: string, tokensIn: number, tokensOut: number) => number;

export interface TemporalAdapterOptions {
  /** Which unmapped activities are evidence for the run's next decision. All, by default. */
  evidence?: (activityType: string) => boolean;
  pricer?: Pricer;
  /** How many runs' evidence to remember; the oldest are forgotten first. Default 1000. */
  maxRuns?: number;
}

/** Shaped like `@temporalio/worker`'s ActivityInterceptorsFactory, without depending on its types. */
export type ActivityInterceptorFactory = (ctx: unknown) => {
  inbound: { execute(input: { args: unknown[]; headers: unknown }, next: (input: { args: unknown[]; headers: unknown }) => Promise<unknown>): Promise<unknown> };
};

/** Build the factory for `Worker.create({ interceptors: { activity: [factory] } })`. */
export function warrantActivityInterceptor(client: Warrant, decisions: Record<string, ActivityDecision>, options?: TemporalAdapterOptions): ActivityInterceptorFactory;

/** Report a model call made inside an activity, so its cost lands on that activity's decision. Throws outside an activity. */
export function modelUsage(provider: string, model: string, options?: { tokensIn?: number; tokensOut?: number }): void;
