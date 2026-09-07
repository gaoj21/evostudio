import React, { useEffect, useRef, useState } from 'react';
import ThemeToggle from './ThemeToggle.jsx';

export default function TopBar({
  graphs,
  graphId,
  runMode,
  saving,
  dirty,
  onSelectGraph,
  onNew,
  onDelete,
  onSave,
  onRun,
  onEvolve,
  onReview,
  watching,
  onToggleWatch,
  onWorkspace,
  onExport,
  onImport,
  onRuns,
  onSchedule,
  onRename,
  onBackToEdit,
  compact,
}) {
  // On a narrow screen only identity, Run and Save stay on the bar; everything
  // else moves into a sheet, so the toolbar never overflows the window.
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);
  // Renaming happens here because this is where the name is shown. It used to
  // live only in the Inspector, which shows workflow settings when *nothing*
  // is selected — so naming a workflow you had just started building meant
  // deselecting first, and there was nothing to say so.
  const [renaming, setRenaming] = useState(null);
  const renameRef = useRef(null);
  const isRenaming = renaming !== null;

  // Only when the box opens. Keyed on `renaming` itself this re-selected the
  // whole field after every keystroke, so each new character replaced
  // everything typed so far and the name came out one letter long.
  useEffect(() => {
    if (isRenaming) renameRef.current?.select();
  }, [isRenaming]);

  const current = graphs.find((g) => g.id === graphId);

  const commitRename = () => {
    const name = (renaming || '').trim();
    setRenaming(null);
    if (name && name !== current?.name) onRename(name);
  };

  const picker = (
    renaming !== null ? (
      <input
        ref={renameRef}
        className="graph-rename"
        value={renaming}
        aria-label="Workflow name"
        onChange={(e) => setRenaming(e.target.value)}
        onBlur={commitRename}
        onKeyDown={(e) => {
          if (e.key === "Enter") commitRename();
          if (e.key === "Escape") setRenaming(null);
        }}
      />
    ) : (
      <>
        <select value={graphId || ''} onChange={(e) => onSelectGraph(e.target.value)} disabled={runMode}>
          {graphs.map((g) => (
            <option key={g.id} value={g.id}>
              {g.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          className={compact ? 'icon-btn' : 'rename-workflow-btn'}
          title="Rename this workflow"
          aria-label="Rename this workflow"
          disabled={runMode || !graphId}
          onClick={() => setRenaming(current?.name || '')}
        >
          {compact ? '✎' : 'Rename'}
        </button>
      </>
    )
  );

  useEffect(() => {
    if (!menuOpen) return undefined;
    const close = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [menuOpen]);

  const menuGroups = [
    {
      label: 'Inspect',
      items: [
        { label: 'Run history', onClick: onRuns, disabled: !graphId },
        { label: 'Workspace files', onClick: onWorkspace, disabled: !graphId },
      ],
    },
    {
      label: 'Improve & automate',
      items: [
        { label: 'Evolve prompts', onClick: onEvolve, disabled: !graphId },
        { label: 'Review queue', onClick: onReview },
        { label: 'Schedule runs', onClick: onSchedule, disabled: !graphId },
        {
          key: 'watch',
          label: watching ? 'Stop watching' : 'Watch sources',
          onClick: onToggleWatch,
          disabled: !graphId,
        },
      ],
    },
    {
      label: 'Project',
      items: [
        ...(compact ? [{ label: 'New workflow', onClick: onNew, disabled: runMode }] : []),
        { label: 'Export project', onClick: onExport, disabled: !graphId },
        { label: 'Import project', onClick: onImport },
        { label: 'Delete workflow', onClick: onDelete, disabled: runMode || !graphId, danger: true },
      ],
    },
  ];

  const moreMenu = (label) => (
    <div className="topbar-menu-wrap" ref={menuRef}>
      <button
        type="button"
        className="topbar-more"
        aria-label="More actions"
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((open) => !open)}
      >
        {label}
      </button>
      {menuOpen && (
        <div className="topbar-menu">
          {menuGroups.map((group) => (
            <div className="topbar-menu-group" key={group.label}>
              <div className="topbar-menu-label">{group.label}</div>
              {group.items.map((item) => (
                <button
                  key={item.label}
                  type="button"
                  className={item.danger ? 'danger-ghost' : ''}
                  disabled={item.disabled}
                  onClick={() => {
                    setMenuOpen(false);
                    item.onClick?.();
                  }}
                >
                  {item.label}
                  {item.key === 'watch' && watching && <span className="watch-dot" />}
                </button>
              ))}
            </div>
          ))}
          {compact && (
            <div className="topbar-menu-theme">
              <span className="muted small">Theme</span>
              <ThemeToggle />
            </div>
          )}
        </div>
      )}
    </div>
  );

  if (compact) {
    return (
      <header className="topbar topbar-compact">
        <div className="topbar-row topbar-actions">
          <span className="brand-mark" aria-hidden="true" />
          {picker}
          <span className="spacer" />
          {runMode ? (
            <button onClick={onBackToEdit}>← Edit</button>
          ) : (
            <>
              <button
                className={dirty ? 'dirty' : ''}
                onClick={onSave}
                disabled={saving || !graphId}
                title={dirty ? 'Unsaved changes' : 'Saved'}
              >
                {saving ? '…' : 'Save'}
                {dirty && !saving && <span className="dirty-dot" />}
              </button>
              <button className="primary" onClick={onRun} disabled={!graphId}>
                Run ▶
              </button>
            </>
          )}
          {moreMenu('⋯')}
        </div>
      </header>
    );
  }

  // One row: identity and actions. The name is edited in place next to the
  // picker, where it is shown; the goal and output directory stay in the
  // Inspector under "Workflow", where they are rarely touched by hand.
  return (
    <header className="topbar">
      <div className="topbar-row topbar-actions">
        <span className="brand">
          <span className="brand-mark" aria-hidden="true" />
          EvoAgentX Studio
        </span>
        <span className="topbar-divider" />
        {picker}
        <button onClick={onNew} disabled={runMode}>
          New
        </button>
        <span className="spacer" />
        {runMode ? (
          <button onClick={onBackToEdit}>← Back to edit</button>
        ) : (
          <>
            <button
              className={dirty ? 'dirty' : ''}
              onClick={onSave}
              disabled={saving || !graphId}
              title={dirty ? 'Unsaved changes' : 'Saved'}
            >
              {saving ? 'Saving…' : 'Save'}
              {dirty && !saving && <span className="dirty-dot" />}
            </button>
            <button className="primary" onClick={onRun} disabled={!graphId}>
              Run ▶
            </button>
            {moreMenu('More ▾')}
          </>
        )}
        <span className="topbar-divider" />
        <ThemeToggle />
      </div>
    </header>
  );
}
