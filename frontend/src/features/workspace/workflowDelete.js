// What deleting a workflow does, said the same way wherever it can be
// deleted (the editor's menu and the Platform task menu). Mirrors
// backend graphs.delete_graph / _archive_graph_data.
export const DELETE_WORKFLOW_TEXT =
  'The workflow, its evaluators and its schedule are deleted permanently. '
  + 'Its runs, batches, memory, workspace files and evaluator draft are not erased: they are moved '
  + 'to an archive in the Studio data directory (filed as "<id>.deleted-<time>") '
  + 'and no longer appear here. A new workflow with this name starts empty.';
