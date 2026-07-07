import type { WidgetConfig } from './config';
import { applyTheme, type Theme } from './theme';

export function makeWidget(cfg: WidgetConfig, theme: Theme): WidgetConfig {
  applyTheme(theme);
  return cfg;
}
