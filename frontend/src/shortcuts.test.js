import { describe, expect, it } from 'vitest';

import { describeMemoryAction, describeReset, isResetMemoryShortcut } from './shortcuts.js';

describe('the reset-memory shortcut', () => {
  it('is the modifier plus shift plus M, on either platform', () => {
    expect(isResetMemoryShortcut({ metaKey: true, shiftKey: true, key: 'M' })).toBe(true);
    expect(isResetMemoryShortcut({ ctrlKey: true, shiftKey: true, key: 'm' })).toBe(true);
  });

  it('is not plain ⌘M, nor shift alone', () => {
    expect(isResetMemoryShortcut({ metaKey: true, shiftKey: false, key: 'm' })).toBe(false);
    expect(isResetMemoryShortcut({ metaKey: false, shiftKey: true, key: 'M' })).toBe(false);
  });
});

describe('what the notice says', () => {
  it('names the copy and what it held', () => {
    const said = describeReset({ backup: '/data/memory-backups/20260907-160213', graph_id: 'crm',
      cleared: true, sizes: { memory: 1258291, tables: 53248, stm: 0 } });
    expect(said).toContain('for crm');
    expect(said).toContain('memory-backups/20260907-160213');
    expect(said).toContain('memory 1.2 MB');
    expect(said).toContain('tables 52 KB');
    expect(said).not.toContain('stm');
  });

  it('says so when there was nothing to clear', () => {
    expect(describeReset({ backup: '/b', sizes: { memory: 0 } })).toContain('nothing was stored');
  });
});

describe('backing up and clearing are separate', () => {
  it('says memory is unchanged after a backup', () => {
    const said = describeMemoryAction({ backup: '/d/memory-backups/20260925-101500', saved: true,
      cleared: false, graph_id: 'crm', sizes: { tables: 53248 } });
    expect(said).toContain('backed up');
    expect(said).toContain('memory is unchanged');
  });

  it('does not name a folder when there was nothing to copy', () => {
    const said = describeMemoryAction({ backup: null, saved: false, cleared: false, sizes: { tables: 0 } });
    expect(said).toBe('Nothing to back up: nothing was stored.');
  });

  it('says plainly when a reset kept no copy', () => {
    const said = describeMemoryAction({ backup: null, cleared: true, sizes: { tables: 1024 } });
    expect(said).toContain('no copy was kept');
  });
});
