// Import block with comments and a rename, spanning multiple lines.
import {
  alphaThing, // first value
  betaThing as beta, /* renamed on import */
  type GammaKind,
} from './things';
import {
  useMemo,
} from 'react';

export function multiUse(): number {
  const pick: GammaKind = 'g';
  void pick;
  return useMemo(() => alphaThing + beta, []);
}
