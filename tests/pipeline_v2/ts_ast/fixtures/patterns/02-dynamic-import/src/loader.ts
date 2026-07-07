export async function loadFeature(): Promise<string> {
  const mod = await import('./lazy');
  return mod.lazyThing();
}

export function preload(): void {
  void import('react-dom');
}
