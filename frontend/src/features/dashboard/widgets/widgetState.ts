/**
 * Per-widget derivation of the shared module state from the composed 360 view.
 *
 * Every Phase 13 widget renders inside a {@link ../../../components/ModuleState} wrapper, and the
 * state that wrapper shows — ready / partial / restricted / error / empty — is a function of the
 * envelope meta scoped to that widget's module. `DashboardPage` did this inline for its placeholder
 * slots; the real widgets share it here so the vocabulary is identical and derived in one place.
 *
 * The `module` a widget belongs to is the failure-bucket the aggregator records against (see
 * `services/aggregator.py`): profile, contact, financial, risk, offers, journey, household. A
 * widget also declares the payload key(s) it reads, because `meta.masked_fields` and the payload
 * are keyed by the top-level slot name (`profile.customer_name`, `financial_profile.net_worth_cents`)
 * which is not always the same string as the failure module (financial owns both `financial_profile`
 * and `expense_analytics`).
 */

import { deriveModuleStatus, type ModuleStateModel } from '../../../components/ModuleState';
import type { Dashboard360 } from '../dashboardData';

export interface WidgetStateInput {
  /** The failure-bucket module name a thrown read is recorded against (`meta.errors[].module`). */
  readonly module: string;
  /** The payload slot keys this widget reads; masked-field paths are prefixed with these. */
  readonly slots: readonly string[];
}

export interface WidgetState {
  readonly model: ModuleStateModel;
  /** True when the widget's slots are all absent from the payload but the fetch succeeded. */
  readonly isEmpty: boolean;
}

/**
 * Compute the {@link ModuleStateModel} and emptiness for a widget from the resolved 360 view.
 *
 * A slot the aggregator failed to build appears in `meta.errors[]` and is absent from the payload,
 * which is `error`. A slot present with masked fields is `restricted`. A slot present with a
 * partial-failure notice is `partial`. A slot the fetch returned but with no rows is empty-`ready`.
 */
export function deriveWidgetState(view: Dashboard360, input: WidgetStateInput): WidgetState {
  const maskedFields = view.maskedFields.filter((path) =>
    input.slots.some((slot) => path.startsWith(`${slot}.`)),
  );
  const errors = view.errors.filter((error) => error.module === input.module);
  const anyPresent = input.slots.some((slot) => view.present.has(slot));
  const failed = errors.length > 0 && !anyPresent;
  const status = deriveModuleStatus({ failed, maskedFields, errors });
  const firstMessage = errors[0]?.message;

  const model: ModuleStateModel = {
    status,
    maskedFields,
    errors,
    ...(failed && firstMessage !== undefined ? { errorMessage: firstMessage } : {}),
  };
  return { model, isEmpty: !anyPresent && status !== 'error' };
}
