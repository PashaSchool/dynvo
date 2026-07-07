import { formatLabel } from '@lib/format';
import { deepHelper } from 'src/deep/helper';

export function main(): string {
  return formatLabel(deepHelper());
}
