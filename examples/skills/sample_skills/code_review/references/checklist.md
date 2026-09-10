# Code Review Checklist

- Input validation on all external data
- No hard-coded credentials or secrets
- Errors are caught and surfaced, not silently swallowed
- Public functions have docstrings
- No unused imports or dead code
- Complexity: functions longer than ~50 lines should be split
