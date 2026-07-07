import { kitRoot } from '@acme/kit';
import { kitSub } from '@acme/kit/sub';

export function useKit(): string {
  return kitRoot + kitSub;
}
