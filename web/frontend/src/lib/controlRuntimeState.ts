export interface ControlTaskProjection {
  present: boolean;
  done: boolean | null;
  cancelled: boolean | null;
  shutdown_requested: boolean;
}

/** Match the backend's ownership rule exactly: a present, unfinished task owns
 * the runtime even after cancellation or shutdown has been requested. */
export function controlTaskActive(
  task: ControlTaskProjection | null | undefined,
): boolean {
  return Boolean(task?.present && task.done === false);
}

export function controlTaskStopping(
  task: ControlTaskProjection | null | undefined,
): boolean {
  return controlTaskActive(task) && task?.shutdown_requested === true;
}
