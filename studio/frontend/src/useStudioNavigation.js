import { useCallback, useState } from 'react';

/**
 * One navigation model shared by desktop, tablet and phone shells.
 *
 * Layout decides where a destination is displayed; callers only ask to open a
 * destination. This keeps actions such as Workspace and node selection from
 * updating a tab that is currently hidden on compact layouts.
 */
export function useStudioNavigation(layout) {
  const [leftTab, setLeftTab] = useState('library');
  const [rightTab, setRightTab] = useState('inspector');
  const [compactPane, setCompactPane] = useState('chat');
  const [libraryOpen, setLibraryOpen] = useState(false);

  const openLeft = useCallback((tab) => setLeftTab(tab), []);
  const openRight = useCallback((tab) => setRightTab(tab), []);

  const showCompact = useCallback((pane) => {
    setCompactPane(pane);
    setLibraryOpen(false);
  }, []);

  const openChat = useCallback(() => {
    setRightTab('chat');
    if (layout !== 'desktop') {
      setCompactPane('chat');
      setLibraryOpen(false);
    }
  }, [layout]);

  const openWorkspace = useCallback(() => {
    setLeftTab('workspace');
    if (layout === 'phone') {
      setCompactPane('library');
      setLibraryOpen(false);
    } else if (layout === 'tablet') {
      setLibraryOpen(true);
    }
  }, [layout]);

  const revealSelection = useCallback((id) => {
    if (id && layout === 'phone') {
      setCompactPane('setup');
      setLibraryOpen(false);
    }
  }, [layout]);

  const toggleLibrary = useCallback(() => {
    if (layout === 'phone') {
      setCompactPane('library');
      setLibraryOpen(false);
    } else if (layout === 'tablet') {
      setLibraryOpen((open) => !open);
    } else {
      setLeftTab('library');
    }
  }, [layout]);

  const closeLibrary = useCallback(() => setLibraryOpen(false), []);

  return {
    closeLibrary,
    compactPane,
    leftTab,
    libraryOpen,
    openChat,
    openLeft,
    openRight,
    openWorkspace,
    revealSelection,
    rightTab,
    showCompact,
    toggleLibrary,
  };
}
