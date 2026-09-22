import { describe, expect, it } from 'vitest';

import { buildTree, countFiles } from './WorkspacePanel.jsx';

const file = (path, size = 10) => ({ path, size, mtime: '' });
const dir = (path) => ({ path, dir: true, size: 0, mtime: '' });

const shape = (nodes) => nodes.map((n) => (n.dir ? { [n.name]: shape(n.children) } : n.name));

describe('buildTree', () => {
  it('nests a flat path listing', () => {
    // Grouped only by the top-level folder, every run's files landed in one
    // flat list of basenames with nothing to say which run they came from.
    expect(shape(buildTree([
      file('workflow.py'),
      file('runs/aaa/output.json'),
      file('runs/aaa/nodes/01-first.json'),
    ]))).toEqual([
      { runs: [{ aaa: [{ nodes: ['01-first.json'] }, 'output.json'] }] },
      'workflow.py',
    ]);
  });

  it('puts folders before files, whatever they are called', () => {
    // `a.txt` sorts before `zzz` alphabetically; folders still come first.
    expect(shape(buildTree([
      file('a.txt'), file('zzz/inside'), file('b.txt'), file('aaa/inside'),
    ]))).toEqual([{ aaa: ['inside'] }, { zzz: ['inside'] }, 'a.txt', 'b.txt']);
  });

  it('sorts each level alphabetically', () => {
    expect(shape(buildTree([
      file('runs/zzz/o.json'), file('runs/aaa/o.json'), file('runs/mmm/o.json'),
    ]))).toEqual([{ runs: [{ aaa: ['o.json'] }, { mmm: ['o.json'] }, { zzz: ['o.json'] }] }]);
  });

  it('invents the folders that only appear inside paths', () => {
    // The listing has no entry for `runs`, only for what is under it.
    const [runs] = buildTree([file('runs/aaa/out.json')]);
    expect(runs.dir).toBe(true);
    expect(runs.path).toBe('runs');
  });

  it('keeps an empty folder that has nothing under it', () => {
    expect(shape(buildTree([dir('scratch')]))).toEqual([{ scratch: [] }]);
  });

  it('does not duplicate a folder that is also listed on its own', () => {
    expect(shape(buildTree([dir('runs'), file('runs/a.json')])))
      .toEqual([{ runs: ['a.json'] }]);
  });

  it("carries each file's details through", () => {
    const [f] = buildTree([file('run.py', 2048)]);
    expect(f).toMatchObject({ name: 'run.py', path: 'run.py', size: 2048, dir: false });
  });

  it('copes with nothing at all', () => {
    expect(buildTree([])).toEqual([]);
    expect(buildTree(undefined)).toEqual([]);
  });
});

describe('countFiles', () => {
  it('counts everything underneath, however deep', () => {
    const [runs] = buildTree([
      file('runs/aaa/out.json'),
      file('runs/aaa/nodes/01.json'),
      file('runs/bbb/out.json'),
    ]);
    // What a folded folder says it is hiding.
    expect(countFiles(runs)).toBe(3);
  });

  it('counts an empty folder as nothing', () => {
    const [scratch] = buildTree([dir('scratch')]);
    expect(countFiles(scratch)).toBe(0);
  });
});
